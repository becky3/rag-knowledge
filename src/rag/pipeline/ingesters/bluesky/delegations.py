"""BlueSky 投稿内 URL の自動取り込み委譲.

仕様: docs/specs/ingesters/bluesky.md「投稿内 URL の自動取り込み」

YouTube / Web への委譲は ``YoutubeClassifier`` / ``YoutubeDelegator`` /
``WebDelegator`` Protocol 経由で実行する（越境直 import を回避）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from rag.pipeline.ingesters._common import (
    IngestErrorCategory,
    IngestErrorDetail,
    IngestResult,
)
from rag.pipeline.ingesters.bluesky.url_routing import (
    classify_url,
    extract_urls_from_item,
)

if TYPE_CHECKING:
    from rag.pipeline.ingesters.web import WebDelegator
    from rag.pipeline.ingesters.youtube_protocols import (
        YoutubeClassifier,
        YoutubeDelegator,
    )

logger = logging.getLogger(__name__)


async def follow_urls(
    placed_items: list[dict[str, Any]],
    *,
    classifier: YoutubeClassifier,
    youtube_delegator: YoutubeDelegator | None,
    web_delegator: WebDelegator,
    youtube_request_interval: float,
    force_youtube_reingest: bool = False,
    result: IngestResult | None = None,
) -> dict[str, int]:
    """配置済み投稿から URL を抽出し、Web/YouTube 委譲先に取り込ませる.

    Web URL は ``WebDelegator.run_for_urls`` でバッチ取得（subprocess 直起動を
    回避するため Python API 経由）。YouTube URL は ``YoutubeDelegator.ingest_videos``
    で個別取り込み（URL 間にレート制限スリープ）。

    各 placed_item の ``_suppress_youtube_reingest`` フラグにより YouTube 抑制対象
    判定を行う。抑制対象（True）の URL は ``force_youtube_reingest`` が True の場合
    のみ取り込む。抑制対象外（False）の URL は常に取り込む。

    Args:
        placed_items: 配置済みフィードアイテムのリスト
        classifier: YoutubeClassifier Protocol 実装
        youtube_delegator: YoutubeDelegator Protocol 実装（None の場合 YouTube は スキップ計上）
        web_delegator: WebDelegator Protocol 実装
        youtube_request_interval: YouTube URL 連続取り込み間のスリープ秒
        force_youtube_reingest: 抑制対象の YouTube URL を強制的に取り込むか
        result: 委譲失敗の計上先 IngestResult。指定時は errors + category="delegation"
            を追加する（従来の stats 返却は互換維持）

    Returns:
        ``{"web_placed": N, "youtube_placed": N, "skipped": N, "errors": N}``
    """
    stats: dict[str, int] = {
        "web_placed": 0,
        "youtube_placed": 0,
        "skipped": 0,
        "errors": 0,
    }

    web_urls, youtube_urls, youtube_url_suppressed, classify_stats = (
        _classify_placed_items(placed_items, classifier=classifier, result=result)
    )
    stats["skipped"] += classify_stats["skipped"]
    stats["errors"] += classify_stats["errors"]

    all_url_count = (
        len(web_urls) + len(youtube_urls) + stats["skipped"] + stats["errors"]
    )
    if all_url_count == 0:
        return stats

    logger.info("投稿内から %d 件の URL を抽出しました", all_url_count)

    if web_urls:
        web_placed, web_errors, web_error_details = await _fetch_web_urls(
            web_urls,
            web_delegator=web_delegator,
        )
        stats["web_placed"] = web_placed
        stats["errors"] += web_errors
        if result is not None and web_error_details:
            result.errors += len(web_error_details)
            result.error_details.extend(web_error_details)

    if not force_youtube_reingest:
        target_youtube_urls = [
            u for u in youtube_urls if not youtube_url_suppressed.get(u, False)
        ]
        skipped_count = len(youtube_urls) - len(target_youtube_urls)
        if skipped_count > 0:
            logger.info(
                "抑制対象の YouTube 再取り込みは無効です"
                "（%d 件スキップ、抑制対象外 %d 件は取り込み）",
                skipped_count,
                len(target_youtube_urls),
            )
            stats["skipped"] += skipped_count
        youtube_urls = target_youtube_urls

    if youtube_urls:
        # bluesky 投稿内 YouTube URL の取り込み。
        # 各 URL を ingest_videos([url]) で呼ぶことで、公開 API 内部の try/finally に
        # よって Whisper モデルの VRAM 解放が保証される。
        # （仕様: docs/specs/ingesters/youtube.md「Whisper モデルライフサイクル」）
        # per-URL レート制限を維持するため bulk 一括ではなく URL ごとに呼び出す。
        for i, url in enumerate(youtube_urls):
            if youtube_delegator is not None:
                try:
                    yt_results = await youtube_delegator.ingest_videos([url])
                    yt_result = yt_results[0]
                    stats["youtube_placed"] += yt_result.placed
                    if yt_result.errors > 0:
                        stats["errors"] += yt_result.errors
                except Exception as exc:
                    logger.exception("YouTube URL の取り込みに失敗: %s", url)
                    stats["errors"] += 1
                    if result is not None:
                        result.errors += 1
                        result.error_details.append(IngestErrorDetail(
                            category=IngestErrorCategory.DELEGATION.value,
                            target=url,
                            url=url,
                            message=f"youtube delegation failed: {exc}",
                        ))
                if i < len(youtube_urls) - 1:
                    await asyncio.sleep(youtube_request_interval)
            else:
                logger.warning("YouTube インジェスターが未指定: %s", url)
                stats["skipped"] += 1

    logger.info(
        "URL 取り込み完了: web=%d, youtube=%d, skipped=%d, errors=%d",
        stats["web_placed"],
        stats["youtube_placed"],
        stats["skipped"],
        stats["errors"],
    )
    return stats


def _classify_placed_items(
    placed_items: list[dict[str, Any]],
    *,
    classifier: YoutubeClassifier,
    result: IngestResult | None,
) -> tuple[list[str], list[str], dict[str, bool], dict[str, int]]:
    """全投稿から URL を一括抽出・重複排除・種別分類する.

    YouTube URL は投稿の suppress 情報を保持する（同一 URL が「抑制対象」と
    「抑制対象外」両方に存在する場合は安全側で取り込む）。

    Returns:
        (web_urls, youtube_urls, youtube_url_suppressed, stats)
        stats は ``{"skipped": N, "errors": N}`` の形式。
    """
    web_urls: list[str] = []
    youtube_urls: list[str] = []
    seen: set[str] = set()
    youtube_url_suppressed: dict[str, bool] = {}
    stats: dict[str, int] = {"skipped": 0, "errors": 0}

    for item in placed_items:
        item_suppressed = item.get("_suppress_youtube_reingest", False)
        for url in extract_urls_from_item(item):
            url_type = classify_url(url, classifier)
            if url_type == "youtube":
                youtube_url_suppressed[url] = (
                    youtube_url_suppressed.get(url, True) and item_suppressed
                )
            if url not in seen:
                seen.add(url)
                if url_type == "web":
                    web_urls.append(url)
                elif url_type == "youtube":
                    youtube_urls.append(url)
                elif url_type == "invalid_youtube":
                    logger.warning(
                        "不正な YouTube URL を検出したためスキップ: %s", url,
                    )
                    stats["errors"] += 1
                    if result is not None:
                        result.errors += 1
                        result.error_details.append(IngestErrorDetail(
                            category=IngestErrorCategory.DELEGATION.value,
                            target=url,
                            url=url,
                            message="invalid youtube url",
                        ))
                else:
                    stats["skipped"] += 1

    return web_urls, youtube_urls, youtube_url_suppressed, stats


async def _fetch_web_urls(
    urls: list[str],
    *,
    web_delegator: WebDelegator,
) -> tuple[int, int, list[IngestErrorDetail]]:
    """Web URL を WebDelegator（複数 URL モード）の Python API で取得する.

    親プロセスが既に write_lock を保持している前提で、subprocess を介さず同一
    プロセス内で WebIngester のコア処理を呼び出す（#686）。

    URL バリデーション (validate_url) と SSRF チェック (check_ssrf) を冒頭で
    実施する。subprocess 経由から Python API 直呼出しに変更したことで、従来
    CLI ``site-ingest`` の入口で行われていた多層防御の初回チェック層が欠落する
    ため、bluesky 側で補完する（Scrapy Downloader Middleware の per-request
    チェックは引き続き有効）。

    Returns:
        (配置されたファイル数の合計, エラー件数, errors の dict リスト)。
        ``error_details`` の件数とエラー件数は常に一致する。
    """
    from rag.utils.url import check_ssrf, validate_url

    logger.info(
        "site-ingest（複数 URL モード）で %d 件の Web URL を取り込みます",
        len(urls),
    )

    validated_urls: list[str] = []
    validation_errors: list[IngestErrorDetail] = []
    for url in urls:
        try:
            validated = validate_url(url)
            check_ssrf(validated)
        except ValueError as exc:
            logger.warning("Web URL バリデーション失敗: %s (%s)", url, exc)
            validation_errors.append(IngestErrorDetail(
                category=IngestErrorCategory.DELEGATION.value,
                target=url,
                url=url,
                message=f"url validation failed: {exc}",
            ))
            continue
        validated_urls.append(validated)

    if not validated_urls:
        return 0, len(validation_errors), validation_errors

    try:
        execution = await web_delegator.run_for_urls(validated_urls)
    except Exception as exc:
        logger.exception("web 取り込みの実行に失敗: %d 件", len(validated_urls))
        execute_errors: list[IngestErrorDetail] = [
            IngestErrorDetail(
                category=IngestErrorCategory.DELEGATION.value,
                target=url,
                url=url,
                message=f"web ingest failed: {exc}",
            )
            for url in validated_urls
        ]
        all_errors = validation_errors + execute_errors
        return 0, len(all_errors), all_errors

    placed = execution.ingest.placed + execution.ingest.overwritten
    error_details: list[IngestErrorDetail] = list(execution.ingest.error_details)
    if execution.parse_errors > 0:
        error_details.append(IngestErrorDetail(
            category=IngestErrorCategory.DELEGATION.value,
            target="web-ingest:jsonl",
            message=(
                f"JSONL のパースに失敗した行が {execution.parse_errors} 件"
                "あります（WebIngester 出力）"
            ),
        ))
    all_error_details = validation_errors + error_details
    total_errors = len(all_error_details)
    logger.info(
        "web 取り込み完了: 合計 %d 件配置, %d 件エラー",
        placed, total_errors,
    )
    if execution.scrapy_success and execution.crawl_result is not None:
        execution.crawl_result.cleanup()
    return placed, total_errors, all_error_details

"""BlueSky インジェスター.

仕様: docs/specs/ingesters/bluesky.md

AT Protocol API 経由で BlueSky の投稿を取得し、
source_store にファイルを配置する。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from rag.pipeline.ingesters._common import IngestResult, now_iso
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from py_common_lib.httpx import ConstrainedClient
    from rag.pipeline.ingesters.web import WebIngester
    from rag.pipeline.ingesters.youtube import YoutubeIngester
    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)

# ハードリミット: 投稿取得上限
MAX_POSTS_HARD_LIMIT = 1000

# ページネーション1ページあたりの取得件数
FEED_PAGE_SIZE = 100

# DID 内のコロンを全角に置換する文字（ローカル定数）
_COLON_FULLWIDTH = "\uff1a"


def _escape_did(did: str) -> str:
    """DID 内のコロンを全角に置換する."""
    return did.replace(":", _COLON_FULLWIDTH)


def _make_title(text: str) -> str:
    """投稿テキストからタイトルを生成する.

    先頭 50 文字 + "..." 形式。50 文字以下はそのまま返す。
    """
    if len(text) <= 50:
        return text
    return text[:50] + "..."


def _validate_max_posts(max_posts: object) -> int:
    """max_posts のバリデーションを行う.

    Returns:
        バリデーション済みの max_posts 値（クランプ適用後）

    Raises:
        TypeError: 非整数型（bool を含む）の場合
        ValueError: 0 以下の場合
    """
    if isinstance(max_posts, bool) or not isinstance(max_posts, int):
        raise TypeError(
            f"max_posts は整数で指定してください（受け取った値: {max_posts!r}）"
        )
    if max_posts <= 0:
        raise ValueError(
            f"max_posts は 1 以上で指定してください（受け取った値: {max_posts}）"
        )
    if max_posts > MAX_POSTS_HARD_LIMIT:
        logger.warning(
            "max_posts が上限 %d を超えています（%d）。%d にクランプします",
            MAX_POSTS_HARD_LIMIT,
            max_posts,
            MAX_POSTS_HARD_LIMIT,
        )
        return MAX_POSTS_HARD_LIMIT
    return max_posts


# YouTube URL 判定パターン
_YOUTUBE_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:youtube\.com/(?:watch\?.*v=|shorts/)|youtu\.be/)",
)

# BlueSky URL 判定パターン（スキップ対象）
_BSKY_URL_RE = re.compile(
    r"^https?://bsky\.app/profile/",
)


def extract_urls_from_item(item: dict[str, Any]) -> list[str]:
    """フィードアイテムから URL を抽出する.

    仕様: docs/specs/ingesters/bluesky.md「投稿内 URL の自動取り込み」

    抽出元:
    1. facets（リッチテキスト内リンク）
    2. embed.external（外部リンクカード）
    3. embed.media.external（recordWithMedia の外部リンクカード）

    引用元投稿の URL は対象外。

    Args:
        item: getAuthorFeed レスポンスのフィードアイテム

    Returns:
        重複排除済みの URL リスト（出現順を保持）
    """
    urls: list[str] = []
    seen: set[str] = set()

    post = item.get("post")
    if not isinstance(post, dict):
        return urls

    record = post.get("record")
    if not isinstance(record, dict):
        return urls

    def _add(url: str) -> None:
        if url and url not in seen and url.startswith(("http://", "https://")):
            seen.add(url)
            urls.append(url)

    # 1. facets
    facets = record.get("facets")
    if isinstance(facets, list):
        for facet in facets:
            if not isinstance(facet, dict):
                continue
            features = facet.get("features")
            if not isinstance(features, list):
                continue
            for feature in features:
                if (
                    isinstance(feature, dict)
                    and feature.get("$type") == "app.bsky.richtext.facet#link"
                ):
                    _add(feature.get("uri", ""))

    # 2. embed.external / embed.media.external
    embed = record.get("embed")
    if isinstance(embed, dict):
        embed_type = embed.get("$type", "")
        if embed_type == "app.bsky.embed.external":
            external = embed.get("external")
            if isinstance(external, dict):
                _add(external.get("uri", ""))
        elif embed_type == "app.bsky.embed.recordWithMedia":
            media = embed.get("media")
            if isinstance(media, dict):
                media_type = media.get("$type", "")
                if media_type == "app.bsky.embed.external":
                    external = media.get("external")
                    if isinstance(external, dict):
                        _add(external.get("uri", ""))

    return urls


def classify_url(url: str) -> Literal["youtube", "web", "skip"]:
    """URL を種別判定する.

    Returns:
        "youtube", "web", or "skip"
    """
    if _BSKY_URL_RE.match(url):
        return "skip"
    if _YOUTUBE_URL_RE.match(url):
        return "youtube"
    return "web"


class BlueskyIngester:
    """BlueSky インジェスター.

    AT Protocol の getAuthorFeed API を使用して投稿を取得し、
    source_store に JSON ファイルとして配置する。
    """

    def __init__(
        self,
        source_store: SourceStore,
        *,
        appview_url: str = "https://public.api.bsky.app",
        max_posts: int = 200,
        include_reposts: bool = True,
    ) -> None:
        self._store = source_store
        self._appview_url = appview_url.rstrip("/")
        self._max_posts = max_posts
        self._include_reposts = include_reposts

    async def crawl_bluesky(
        self,
        handle: str,
        *,
        max_posts: int | None = None,
        include_reposts: bool | None = None,
        client: ConstrainedClient | None = None,
    ) -> tuple[IngestResult, list[dict[str, Any]]]:
        """BlueSky 投稿を取得し source_store に配置する.

        Args:
            handle: BlueSky ハンドル
            max_posts: 取得する最大投稿数（None の場合はインスタンス設定を使用）
            include_reposts: リポストを含めるか（None の場合はインスタンス設定を使用）
            client: ConstrainedClient インスタンス

        Returns:
            (配置結果, 配置済みフィードアイテムのリスト)
        """
        result = IngestResult()

        # バリデーション
        if not handle or not handle.strip():
            raise ValueError("handle が空です")
        if handle.startswith("did:"):
            raise ValueError(
                f"handle に DID 形式は指定できません: {handle}"
            )

        effective_max = _validate_max_posts(
            max_posts if max_posts is not None else self._max_posts
        )
        effective_include_reposts = (
            include_reposts
            if include_reposts is not None
            else self._include_reposts
        )

        if client is None:
            raise ValueError("client (ConstrainedClient) が必要です")

        # ページネーション走査
        cursor: str | None = None
        total_processed = 0
        seen_paths: set[str] = set()
        placed_items: list[dict[str, Any]] = []

        while total_processed < effective_max:
            # API リクエスト
            params: dict[str, str | int] = {
                "actor": handle,
                "limit": FEED_PAGE_SIZE,
            }
            if cursor:
                params["cursor"] = cursor

            from urllib.parse import urlencode
            url = f"{self._appview_url}/xrpc/app.bsky.feed.getAuthorFeed?{urlencode(params)}"
            resp = await client.get(url)
            data = resp.json()

            feed = data.get("feed", [])
            if not feed:
                break

            for item in feed:
                if total_processed >= effective_max:
                    break

                # リポストフィルタ
                reason = item.get("reason")
                is_repost = (
                    reason is not None
                    and isinstance(reason, dict)
                    and reason.get("$type") == "app.bsky.feed.defs#reasonRepost"
                )
                if is_repost and not effective_include_reposts:
                    continue

                total_processed += 1

                post = item.get("post", {})
                post_uri = post.get("uri", "")
                author = post.get("author", {})
                record = post.get("record", {})

                # DID と rkey を抽出
                did = author.get("did", "")
                handle_author = author.get("handle", "")
                # AT URI: at://did:plc:xxx/app.bsky.feed.post/rkey
                rkey = post_uri.rsplit("/", 1)[-1] if "/" in post_uri else ""

                # 年月の導出
                created_at_str = record.get("createdAt", "")
                try:
                    dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                    year = str(dt.year)
                    month = f"{dt.month:02d}"
                except (ValueError, TypeError):
                    year = "unknown"
                    month = "00"

                # ファイルパス
                escaped_did = _escape_did(did)
                rel_path = f"bluesky/{escaped_did}/{year}/{month}/{rkey}.json"

                # 重複検出: seen_paths + ファイル存在チェック
                if rel_path in seen_paths:
                    result.skipped += 1
                    continue
                seen_paths.add(rel_path)

                dest = self._store.root_dir / rel_path
                if dest.exists():
                    result.skipped += 1
                    continue

                # JSON データ保存
                json_data = json.dumps(item, ensure_ascii=False, indent=2)
                data_bytes = json_data.encode("utf-8")

                # .meta 生成
                text = record.get("text", "")
                title = _make_title(text)

                # embed 情報の判定
                embed = record.get("embed") or {}
                has_images = False
                has_video = False
                has_external_link = False

                if isinstance(embed, dict):
                    embed_type = embed.get("$type", "")
                    if "image" in embed_type:
                        has_images = True
                    if "video" in embed_type:
                        has_video = True
                    if "external" in embed_type:
                        has_external_link = True
                    # recordWithMedia
                    media = embed.get("media", {})
                    if isinstance(media, dict):
                        media_type = media.get("$type", "")
                        if "image" in media_type:
                            has_images = True
                        if "video" in media_type:
                            has_video = True
                        if "external" in media_type:
                            has_external_link = True

                is_reply = "reply" in record if isinstance(record, dict) else False

                bsky_url = f"https://bsky.app/profile/{handle_author}/post/{rkey}"

                metadata = {
                    "source_id": post_uri,
                    "source_type": "bluesky",
                    "title": title,
                    "collected_at": now_iso(),
                    "handle": handle_author,
                    "did": did,
                    "rkey": rkey,
                    "url": bsky_url,
                    "created_at": created_at_str,
                    "has_images": has_images,
                    "has_video": has_video,
                    "has_external_link": has_external_link,
                    "is_reply": is_reply,
                    "is_repost": is_repost,
                }

                try:
                    self._store.place_file(
                        source_type="bluesky",
                        data=data_bytes,
                        rel_path=rel_path,
                        metadata=metadata,
                    )
                    result.placed += 1
                    placed_items.append(item)
                except Exception:
                    logger.exception("投稿の配置に失敗しました: %s", rel_path)
                    result.errors += 1
                    result.error_details.append(rel_path)

            # 次ページの確認
            cursor = data.get("cursor")
            if not cursor:
                break

        return result, placed_items

    async def follow_urls(
        self,
        placed_items: list[dict[str, Any]],
        *,
        client: ConstrainedClient | None = None,
        web_ingester: WebIngester | None = None,
        youtube_ingester: YoutubeIngester | None = None,
    ) -> dict[str, int]:
        """配置済み投稿から URL を抽出し、Web/YouTube インジェスターに委譲する.

        仕様: docs/specs/ingesters/bluesky.md「投稿内 URL の自動取り込み」

        Args:
            placed_items: 配置済みフィードアイテムのリスト
            client: ConstrainedClient（Web インジェスターに共有）
            web_ingester: WebIngester インスタンス
            youtube_ingester: YoutubeIngester インスタンス

        Returns:
            {"web_placed": N, "youtube_placed": N, "skipped": N, "errors": N}
        """
        stats: dict[str, int] = {
            "web_placed": 0,
            "youtube_placed": 0,
            "skipped": 0,
            "errors": 0,
        }

        # 全投稿から URL を一括抽出・重複排除
        all_urls: list[str] = []
        seen: set[str] = set()
        for item in placed_items:
            for url in extract_urls_from_item(item):
                if url not in seen:
                    seen.add(url)
                    all_urls.append(url)

        if not all_urls:
            return stats

        logger.info("投稿内から %d 件の URL を抽出しました", len(all_urls))

        from py_common_lib.core.budget_tracker import BudgetExhaustedError

        for url in all_urls:
            # バジェット枯渇チェック（Web URL は ConstrainedClient 経由）
            if client is not None and client.budget.remaining <= 0:
                remaining_count = len(all_urls) - (
                    stats["web_placed"] + stats["youtube_placed"]
                    + stats["skipped"] + stats["errors"]
                )
                if remaining_count > 0:
                    logger.warning(
                        "バジェット枯渇のため残り %d 件の URL をスキップします",
                        remaining_count,
                    )
                    stats["skipped"] += remaining_count
                break

            url_type = classify_url(url)

            if url_type == "skip":
                stats["skipped"] += 1
                continue

            if url_type == "youtube" and youtube_ingester is not None:
                try:
                    yt_result = await youtube_ingester.ingest_video(video_url=url)
                    stats["youtube_placed"] += yt_result.placed
                    if yt_result.errors > 0:
                        stats["errors"] += yt_result.errors
                except Exception:
                    logger.exception("YouTube URL の取り込みに失敗: %s", url)
                    stats["errors"] += 1
            elif url_type == "web" and web_ingester is not None:
                try:
                    web_result = await web_ingester.add(
                        url=url, client=client,
                    )
                    stats["web_placed"] += web_result.placed
                    if web_result.errors > 0:
                        stats["errors"] += web_result.errors
                except BudgetExhaustedError:
                    logger.warning("バジェット枯渇: %s をスキップ", url)
                    stats["skipped"] += 1
                    break
                except Exception:
                    logger.exception("Web URL の取り込みに失敗: %s", url)
                    stats["errors"] += 1
            else:
                logger.warning(
                    "URL タイプ '%s' の委譲先インジェスターが未指定: %s",
                    url_type, url,
                )
                stats["skipped"] += 1

        logger.info(
            "URL 取り込み完了: web=%d, youtube=%d, skipped=%d, errors=%d",
            stats["web_placed"],
            stats["youtube_placed"],
            stats["skipped"],
            stats["errors"],
        )
        return stats

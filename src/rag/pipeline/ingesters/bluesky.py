"""BlueSky インジェスター.

仕様: docs/specs/ingesters/bluesky.md

AT Protocol API 経由で BlueSky の投稿を取得し、
source_store にファイルを配置する。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from rag.pipeline.ingesters._common import IngestResult, ProgressCallback, now_iso
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from py_common_lib.httpx import ConstrainedClient
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


# Content-Type → 拡張子マッピング（メディア DL 用）
_CONTENT_TYPE_EXT: dict[str, str] = {
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "video/mp2t": ".ts",
}

# デフォルト拡張子（Content-Type が不明な場合）
_DEFAULT_IMAGE_EXT = ".webp"


def _ext_from_content_type(content_type: str) -> str:
    """Content-Type ヘッダーから拡張子を決定する."""
    # "image/webp; charset=utf-8" のようなパラメータを除去
    media_type = content_type.split(";")[0].strip().lower()
    return _CONTENT_TYPE_EXT.get(media_type, _DEFAULT_IMAGE_EXT)


def _extract_media_urls(item: dict[str, Any]) -> tuple[list[str], str | None]:
    """フィードアイテムから画像 URL リストと動画プレイリスト URL を抽出する.

    view 版 embed（post.embed）から取得する。
    recordWithMedia の場合は post.embed.media 配下にネストされる。

    Returns:
        (画像 fullsize URL リスト, 動画 playlist URL or None)
    """
    image_urls: list[str] = []
    playlist_url: str | None = None

    post = item.get("post")
    if not isinstance(post, dict):
        return image_urls, playlist_url

    embed = post.get("embed")
    if not isinstance(embed, dict):
        return image_urls, playlist_url

    embed_type = embed.get("$type", "")

    # recordWithMedia の場合は media 配下を参照
    if embed_type == "app.bsky.embed.recordWithMedia#view":
        media = embed.get("media")
        if isinstance(media, dict):
            _collect_media_from_embed(media, image_urls)
            pl = media.get("playlist")
            if isinstance(pl, str) and pl:
                playlist_url = pl
    else:
        _collect_media_from_embed(embed, image_urls)
        pl = embed.get("playlist")
        if isinstance(pl, str) and pl:
            playlist_url = pl

    return image_urls, playlist_url


def _collect_media_from_embed(embed: dict[str, Any], image_urls: list[str]) -> None:
    """embed オブジェクトから画像 URL を収集する."""
    images = embed.get("images")
    if isinstance(images, list):
        for img in images:
            if isinstance(img, dict):
                fullsize = img.get("fullsize")
                if isinstance(fullsize, str) and fullsize:
                    image_urls.append(fullsize)


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
        appview_url: str,
        max_posts: int,
        include_reposts: bool,
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
        force: bool = False,
        client: ConstrainedClient | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[IngestResult, list[dict[str, Any]]]:
        """BlueSky 投稿を取得し source_store に配置する.

        Args:
            handle: BlueSky ハンドル
            max_posts: 取得する最大投稿数（None の場合はインスタンス設定を使用）
            include_reposts: リポストを含めるか（None の場合はインスタンス設定を使用）
            force: 上書き再取得モード（既存ファイルを上書き + メディア再DL）
            client: ConstrainedClient インスタンス
            progress_callback: 進捗コールバック (processed, total, current)

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
                if dest.exists() and not force:
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
                    "at_uri": post_uri,
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
                    continue

                # メディア DL（画像・動画）
                if has_images or has_video:
                    media_dir = (
                        self._store.root_dir
                        / "bluesky"
                        / escaped_did
                        / year
                        / month
                        / "media"
                        / rkey
                    )
                    await self._download_media(
                        item, media_dir=media_dir, client=client,
                    )

                if progress_callback is not None:
                    progress_callback(total_processed, effective_max, bsky_url)

            # 次ページの確認
            cursor = data.get("cursor")
            if not cursor:
                break

        return result, placed_items

    async def _download_media(
        self,
        item: dict[str, Any],
        *,
        media_dir: Path,
        client: ConstrainedClient,
    ) -> None:
        """投稿に添付されたメディア（画像・動画）を DL して配置する.

        Args:
            item: フィードアイテム
            media_dir: メディア配置先ディレクトリ
            client: ConstrainedClient インスタンス
        """
        image_urls, playlist_url = _extract_media_urls(item)

        # 画像 DL
        for idx, img_url in enumerate(image_urls):
            try:
                resp = await client.get(img_url)
                content_type = resp.headers.get("content-type", "")
                ext = _ext_from_content_type(content_type)
                filename = f"image_{idx}{ext}"
                dest = media_dir / filename
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(resp.content)
                logger.debug("画像を保存しました: %s", dest)
            except Exception:
                logger.exception("画像の DL に失敗しました: %s", img_url)

        # 動画 DL（HLS ts セグメント結合）
        if playlist_url:
            try:
                await self._download_hls_video(
                    playlist_url,
                    dest=media_dir / "video_0.ts",
                    client=client,
                )
            except Exception:
                logger.exception("動画の DL に失敗しました: %s", playlist_url)

    async def _download_hls_video(
        self,
        playlist_url: str,
        *,
        dest: Path,
        client: ConstrainedClient,
    ) -> None:
        """HLS プレイリストから ts セグメントを DL し、バイナリ結合して保存する.

        Args:
            playlist_url: HLS プレイリスト URL (.m3u8)
            dest: 保存先パス
            client: ConstrainedClient インスタンス
        """
        # プレイリスト取得
        resp = await client.get(playlist_url)
        playlist_text = resp.text

        # ts セグメント URL を抽出
        base_url = playlist_url.rsplit("/", 1)[0] + "/"
        segment_urls: list[str] = []
        for line in playlist_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 相対 URL → 絶対 URL
            if line.startswith("http://") or line.startswith("https://"):
                segment_urls.append(line)
            else:
                segment_urls.append(base_url + line)

        if not segment_urls:
            logger.warning("HLS プレイリストに ts セグメントが見つかりません: %s", playlist_url)
            return

        # ts セグメントを DL して結合
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            for seg_url in segment_urls:
                seg_resp = await client.get(seg_url)
                f.write(seg_resp.content)

        logger.debug("動画を保存しました（%d セグメント）: %s", len(segment_urls), dest)

    async def follow_urls(
        self,
        placed_items: list[dict[str, Any]],
        *,
        youtube_ingester: YoutubeIngester | None = None,
        force: bool = False,
        force_youtube_reingest: bool = False,
    ) -> dict[str, int]:
        """配置済み投稿から URL を抽出し、site_ingest/YouTube インジェスターに委譲する.

        仕様: docs/specs/ingesters/bluesky.md「投稿内 URL の自動取り込み」

        Web URL は CLI の site-ingest コマンド（複数 URL モード）でバッチ取得する。
        YouTube URL は個別に YoutubeIngester で取り込む。

        Args:
            placed_items: 配置済みフィードアイテムのリスト
            youtube_ingester: YoutubeIngester インスタンス
            force: 上書き再取得モード（Web URL の再取得を有効にする）
            force_youtube_reingest: force 時に YouTube URL を再取得するか

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

        # URL を種別ごとに分類
        web_urls: list[str] = []
        youtube_urls: list[str] = []
        for url in all_urls:
            url_type = classify_url(url)
            if url_type == "web":
                web_urls.append(url)
            elif url_type == "youtube":
                youtube_urls.append(url)
            else:
                stats["skipped"] += 1

        # Web URL をバッチ取得（site-ingest 複数 URL モード、download_only）
        if web_urls:
            web_placed, web_errors = await self._fetch_web_urls(web_urls)
            stats["web_placed"] = web_placed
            stats["errors"] += web_errors

        # YouTube URL を個別取り込み（URL 間にレート制限スリープを挿入）
        # force 時は force_youtube_reingest 設定に従う
        if force and not force_youtube_reingest:
            logger.info(
                "force モードですが YouTube 再取り込みは無効です（%d 件スキップ）",
                len(youtube_urls),
            )
            stats["skipped"] += len(youtube_urls)
        else:
            for i, url in enumerate(youtube_urls):
                if youtube_ingester is not None:
                    try:
                        yt_result = await youtube_ingester.ingest_video(video_url=url)
                        stats["youtube_placed"] += yt_result.placed
                        if yt_result.errors > 0:
                            stats["errors"] += yt_result.errors
                    except Exception:
                        logger.exception("YouTube URL の取り込みに失敗: %s", url)
                        stats["errors"] += 1
                    # リクエスト間隔待機（次の URL がある場合のみ）
                    if i < len(youtube_urls) - 1:
                        await asyncio.sleep(youtube_ingester.request_interval)
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

    # Windows コマンドライン長制限（約32K文字）を考慮したバッチサイズ
    # URL あたり平均 ~80 文字 × 200 = ~16K文字で安全マージンを確保
    _URL_BATCH_SIZE = 200

    async def _fetch_web_urls(self, urls: list[str]) -> tuple[int, int]:
        """Web URL を site-ingest CLI subprocess（複数 URL モード）でバッチ取得する.

        Windows のコマンドライン長制限を考慮し、URL リストが大きい場合は
        バッチに分割して複数回の subprocess を実行する。

        Args:
            urls: 取得対象の Web URL リスト

        Returns:
            (配置されたファイル数の合計, エラー件数)
        """
        logger.info("site-ingest（複数 URL モード）で %d 件の Web URL を取り込みます", len(urls))

        total_placed = 0
        total_errors = 0
        for i in range(0, len(urls), self._URL_BATCH_SIZE):
            batch = urls[i:i + self._URL_BATCH_SIZE]
            try:
                placed = await self._run_site_ingest_batch(batch)
                total_placed += placed
            except Exception:
                logger.exception(
                    "site-ingest バッチ処理に失敗（スキップして続行）: batch_size=%d",
                    len(batch),
                )
                total_errors += len(batch)

        logger.info("site-ingest 完了: 合計 %d 件配置, %d 件エラー", total_placed, total_errors)
        return total_placed, total_errors

    async def _run_site_ingest_batch(
        self, urls: list[str], *, force: bool = False,
    ) -> int:
        """site-ingest CLI subprocess を 1 バッチ分実行する."""
        cmd = [
            sys.executable, "-m", "rag.cli",
            "site-ingest", *urls, "--download-only",
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")
            logger.error(
                "site-ingest subprocess が失敗: exit_code=%d, stderr=%s",
                process.returncode, stderr_text[:500],
            )
            raise RuntimeError(f"site-ingest failed with exit_code={process.returncode}")

        # stdout から配置数を抽出（"N件新規配置" パターン）
        stdout_text = stdout.decode("utf-8", errors="replace")
        match = re.search(r"(\d+)件新規配置", stdout_text)
        if match:
            return int(match.group(1))

        logger.warning(
            "site-ingest の出力から配置数を取得できませんでした: %s",
            stdout_text[:200],
        )
        return 0

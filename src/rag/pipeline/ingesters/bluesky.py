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
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import httpx

from rag.pipeline.ingesters._common import (
    IngestErrorCategory,
    IngestResult,
    ProgressCallback,
    extract_http_status,
    fetch_get,
    now_iso,
)
from rag.pipeline.ingesters.youtube import classify_youtube_url
from rag.pipeline.site_ingest_runner import execute_site_ingest
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from py_common_lib.httpx import ConstrainedClient
    from rag.config import RAGSettings
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


def _extract_link_card(external: object) -> dict[str, str] | None:
    """embed.external オブジェクトからリンクカード情報を抽出する."""
    if not isinstance(external, dict):
        return None
    card: dict[str, str] = {}
    for key in ("uri", "title", "description"):
        val = external.get(key, "")
        if isinstance(val, str) and val.strip():
            card[key] = val.strip()
    return card if card else None


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
    ext = _CONTENT_TYPE_EXT.get(media_type)
    if ext is None:
        logger.warning("不明な Content-Type、デフォルト拡張子を使用: %s", media_type)
        return _DEFAULT_IMAGE_EXT
    return ext


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


def classify_url(url: str) -> Literal["youtube", "web", "skip", "invalid_youtube"]:
    """URL を種別判定する.

    YouTube 動画 URL の判定は YouTube インジェスター側の SSoT を参照する
    （対応パターンは docs/specs/ingesters/youtube.md「対応 URL 形式」）。

    Returns:
        - "youtube": 認識可能な YouTube 動画 URL
        - "invalid_youtube": YouTube 動画 URL のパターンに見えるが video_id 形式が不正。
          site_ingest に流すと無駄な HTTP アクセスが発生するため、呼び出し側でエラーとして扱う
        - "skip": BlueSky 投稿 URL 等の取り込み対象外
        - "web": 上記以外（チャンネル URL・プレイリスト URL・一般 Web ページ等）
    """
    if _BSKY_URL_RE.match(url):
        return "skip"
    yt = classify_youtube_url(url)
    if yt == "video":
        return "youtube"
    if yt == "malformed":
        return "invalid_youtube"
    return "web"


_BSKY_POST_URL_RE = re.compile(
    r"^https?://bsky\.app/profile/([^/]+)/post/([a-zA-Z0-9]+)(?:[?#].*)?$",
)


def parse_bluesky_url(url: str) -> tuple[str, str] | None:
    """BlueSky 投稿 URL から (handle, rkey) を抽出する.

    Returns:
        (handle, rkey) タプル、またはパース失敗時は None
    """
    m = _BSKY_POST_URL_RE.match(url.strip())
    if m is None:
        return None
    return m.group(1), m.group(2)


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
        logger.info(
            "BlueSky crawl started: handle=%s, max_posts=%s, include_reposts=%s, force=%s",
            handle,
            max_posts if max_posts is not None else self._max_posts,
            include_reposts if include_reposts is not None else self._include_reposts,
            force,
        )
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

            url = f"{self._appview_url}/xrpc/app.bsky.feed.getAuthorFeed?{urlencode(params)}"
            resp = await fetch_get(client, url)
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

                place_outcome = await self._place_single_post(
                    item,
                    is_repost=is_repost,
                    force=force,
                    client=client,
                    result=result,
                    seen_paths=seen_paths,
                )

                if place_outcome is not None:
                    placed_ok, is_overwrite = place_outcome
                    if placed_ok:
                        # 上書き投稿の YouTube URL は force_youtube_reingest 設定で抑制可
                        placed_items.append(
                            {**item, "_suppress_youtube_reingest": is_overwrite},
                        )
                    if progress_callback is not None:
                        post = item.get("post", {})
                        author = post.get("author", {})
                        post_uri = post.get("uri", "")
                        rkey = post_uri.rsplit("/", 1)[-1] if "/" in post_uri else ""
                        bsky_url = f"https://bsky.app/profile/{author.get('handle', '')}/post/{rkey}"
                        progress_callback(total_processed, effective_max, bsky_url)

            # 次ページの確認
            cursor = data.get("cursor")
            if not cursor:
                break

        logger.info(
            "BlueSky crawl completed: placed=%d, overwritten=%d, skipped=%d, errors=%d",
            result.placed, result.overwritten, result.skipped, result.errors,
        )
        return result, placed_items

    async def _place_single_post(
        self,
        item: dict[str, Any],
        *,
        is_repost: bool,
        force: bool,
        client: ConstrainedClient,
        result: IngestResult,
        seen_paths: set[str] | None = None,
    ) -> tuple[bool, bool] | None:
        """単一投稿を source_store に配置する.

        placed_items の組み立ては呼び出し側の責務とする。本関数は配置結果（成功可否と
        既存ファイル上書きだったかの事実）のみを返す。呼び出し側は用途に応じて
        ``_suppress_youtube_reingest`` 等のポリシーフラグを決定し、placed_items を
        構築する。

        Returns:
            (placed_ok, is_overwrite) のタプル、またはスキップ時は None。
            placed_ok=True なら配置成功、False なら配置失敗。
            is_overwrite はファイル配置の事実（既存ファイルを上書きしたか）。
        """
        post = item.get("post", {})
        post_uri = post.get("uri", "")
        author = post.get("author", {})
        record = post.get("record", {})

        did = author.get("did", "")
        handle_author = author.get("handle", "")
        rkey = post_uri.rsplit("/", 1)[-1] if "/" in post_uri else ""

        created_at_str = record.get("createdAt", "")
        try:
            dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            year = str(dt.year)
            month = f"{dt.month:02d}"
        except (ValueError, TypeError):
            year = "unknown"
            month = "00"

        escaped_did = _escape_did(did)
        rel_path = f"bluesky/{escaped_did}/{year}/{month}/{rkey}.json"

        if seen_paths is not None:
            if rel_path in seen_paths:
                result.skipped += 1
                return None
            seen_paths.add(rel_path)

        dest = self._store.root_dir / rel_path
        is_overwrite = dest.exists()
        if is_overwrite and not force:
            result.skipped += 1
            return None

        json_data = json.dumps(item, ensure_ascii=False, indent=2)
        data_bytes = json_data.encode("utf-8")

        text = record.get("text", "")
        title = _make_title(text)

        embed = record.get("embed") or {}
        has_images = False
        has_video = False
        has_external_link = False
        link_card: dict[str, str] | None = None

        if isinstance(embed, dict):
            embed_type = embed.get("$type", "")
            if "image" in embed_type:
                has_images = True
            if "video" in embed_type:
                has_video = True
            if "external" in embed_type:
                has_external_link = True
                link_card = _extract_link_card(embed.get("external"))
            media = embed.get("media", {})
            if isinstance(media, dict):
                media_type = media.get("$type", "")
                if "image" in media_type:
                    has_images = True
                if "video" in media_type:
                    has_video = True
                if "external" in media_type:
                    has_external_link = True
                    if link_card is None:
                        link_card = _extract_link_card(media.get("external"))

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
            "link_card": link_card,
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
            if is_overwrite:
                result.overwritten += 1
            else:
                result.placed += 1
        except Exception as exc:
            logger.exception("投稿の配置に失敗しました: %s", rel_path)
            result.errors += 1
            result.error_details.append(
                {
                    "category": IngestErrorCategory.PLACEMENT.value,
                    "target": rel_path,
                    "message": str(exc),
                },
            )
            return False, is_overwrite

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
                item,
                media_dir=media_dir,
                client=client,
                result=result,
                rel_path=rel_path,
            )

        return True, is_overwrite

    async def ingest_posts(
        self,
        urls: list[str],
        *,
        client: ConstrainedClient,
    ) -> tuple[IngestResult, list[dict[str, Any]]]:
        """指定 URL の BlueSky 投稿を取得して source_store に配置する.

        仕様: docs/specs/ingesters/bluesky.md「投稿取得フロー（rag_add_bluesky）」

        投稿配置完了後、呼び出し側が `follow_urls` を実行することで投稿内 URL の
        自動取り込みが走る。`rag_add_bluesky` はピンポイント修復用途のため、
        placed_items の各 item には ``_suppress_youtube_reingest=False`` を付与する。
        これにより follow_urls 側で常に取り込み対象となり、`force_youtube_reingest`
        設定の値に関わらず YouTube URL を常に再取得する。

        Returns:
            (配置結果, 配置済みフィードアイテムのリスト)
        """
        result = IngestResult()
        placed_items: list[dict[str, Any]] = []

        if not urls:
            return result, placed_items

        parsed: list[tuple[str, str]] = []
        for url in urls:
            hr = parse_bluesky_url(url)
            if hr is None:
                logger.warning("BlueSky URL のパースに失敗しました: %s", url)
                result.errors += 1
                result.error_details.append(
                    {
                        "category": IngestErrorCategory.METADATA_FETCH.value,
                        "target": url,
                        "message": "Invalid BlueSky URL format",
                    },
                )
                continue
            parsed.append(hr)

        if not parsed:
            return result, placed_items

        # handle → DID 解決（同一 handle はまとめる）
        handle_to_did: dict[str, str] = {}
        unique_handles = {h for h, _ in parsed}
        for handle in unique_handles:
            try:
                params = urlencode({"handle": handle})
                resolve_url = (
                    f"{self._appview_url}/xrpc/"
                    f"com.atproto.identity.resolveHandle?{params}"
                )
                resp = await fetch_get(client, resolve_url)
                data = resp.json()
                did = data.get("did", "")
                if did:
                    handle_to_did[handle] = did
                else:
                    logger.warning("DID の解決結果が空です: %s", handle)
            except Exception as exc:
                logger.warning("DID 解決に失敗しました: handle=%s, error=%s", handle, exc)

        # 各投稿を取得・配置
        for handle, rkey in parsed:
            did = handle_to_did.get(handle)
            if not did:
                result.errors += 1
                result.error_details.append(
                    {
                        "category": IngestErrorCategory.METADATA_FETCH.value,
                        "target": f"https://bsky.app/profile/{handle}/post/{rkey}",
                        "message": f"Failed to resolve DID for handle: {handle}",
                    },
                )
                continue

            at_uri = f"at://{did}/app.bsky.feed.post/{rkey}"
            try:
                params = urlencode({"uris": at_uri})
                posts_url = (
                    f"{self._appview_url}/xrpc/"
                    f"app.bsky.feed.getPosts?{params}"
                )
                resp = await fetch_get(client, posts_url)
                data = resp.json()
                posts = data.get("posts", [])
            except Exception as exc:
                logger.warning("投稿の取得に失敗しました: %s, error=%s", at_uri, exc)
                result.errors += 1
                result.error_details.append(
                    {
                        "category": IngestErrorCategory.METADATA_FETCH.value,
                        "target": f"https://bsky.app/profile/{handle}/post/{rkey}",
                        "message": str(exc),
                    },
                )
                continue

            if not posts:
                logger.warning("投稿が見つかりません: %s", at_uri)
                result.errors += 1
                result.error_details.append(
                    {
                        "category": IngestErrorCategory.METADATA_FETCH.value,
                        "target": f"https://bsky.app/profile/{handle}/post/{rkey}",
                        "message": "Post not found (may be deleted)",
                    },
                )
                continue

            # getPosts はフィードアイテムではなく post オブジェクトを返すため変換
            post_obj = posts[0]
            item: dict[str, Any] = {"post": post_obj, "reason": None}

            place_outcome = await self._place_single_post(
                item,
                is_repost=False,
                force=True,
                client=client,
                result=result,
            )
            if place_outcome is not None and place_outcome[0]:
                # ピンポイント修復用途のため、新規/上書きいずれも抑制対象外として記録し、
                # follow_urls 側で常に取り込み対象とする
                placed_items.append(
                    {**item, "_suppress_youtube_reingest": False},
                )

        logger.info(
            "BlueSky ingest_posts completed: placed=%d, overwritten=%d, errors=%d",
            result.placed, result.overwritten, result.errors,
        )
        return result, placed_items

    async def _download_media(
        self,
        item: dict[str, Any],
        *,
        media_dir: Path,
        client: ConstrainedClient,
        result: IngestResult,
        rel_path: str,
    ) -> None:
        """投稿に添付されたメディア（画像・動画）を DL して配置する.

        Args:
            item: フィードアイテム
            media_dir: メディア配置先ディレクトリ
            client: ConstrainedClient インスタンス
            result: 失敗計上先の IngestResult
            rel_path: 投稿ファイルの相対パス（partial_failure_details.target 用）
        """
        image_urls, playlist_url = _extract_media_urls(item)

        # 画像 DL
        for idx, img_url in enumerate(image_urls):
            try:
                resp = await fetch_get(client, img_url)
                content_type = resp.headers.get("content-type", "")
                ext = _ext_from_content_type(content_type)
                filename = f"image_{idx}{ext}"
                dest = media_dir / filename
                dest.parent.mkdir(parents=True, exist_ok=True)
                # 拡張子が変わった場合に古いファイルが残らないようクリーンアップ
                for existing in media_dir.glob(f"image_{idx}.*"):
                    if existing != dest:
                        existing.unlink(missing_ok=True)
                dest.write_bytes(resp.content)
                logger.debug("画像を保存しました: %s", dest)
            except Exception as exc:
                logger.exception("画像の DL に失敗しました: %s", img_url)
                result.partial_failures += 1
                detail: dict[str, Any] = {
                    "category": IngestErrorCategory.MEDIA_DOWNLOAD.value,
                    "target": rel_path,
                    "url": img_url,
                    "message": str(exc),
                }
                status = extract_http_status(exc)
                if status is not None:
                    detail["status"] = status
                result.partial_failure_details.append(detail)

        # 動画 DL（HLS ts セグメント結合）
        if playlist_url:
            try:
                await self._download_hls_video(
                    playlist_url,
                    dest=media_dir / "video_0.ts",
                    client=client,
                    result=result,
                    rel_path=rel_path,
                )
            except Exception as exc:
                logger.exception("動画の DL に失敗しました: %s", playlist_url)
                result.partial_failures += 1
                detail_video: dict[str, Any] = {
                    "category": IngestErrorCategory.MEDIA_DOWNLOAD.value,
                    "target": rel_path,
                    "url": playlist_url,
                    "message": str(exc),
                }
                status = extract_http_status(exc)
                if status is not None:
                    detail_video["status"] = status
                result.partial_failure_details.append(detail_video)

    async def _download_hls_video(
        self,
        playlist_url: str,
        *,
        dest: Path,
        client: ConstrainedClient,
        result: IngestResult,
        rel_path: str,
    ) -> None:
        """HLS プレイリストから ts セグメントを DL し、バイナリ結合して保存する.

        Args:
            playlist_url: HLS プレイリスト URL (.m3u8)
            dest: 保存先パス
            client: ConstrainedClient インスタンス
            result: 失敗計上先の IngestResult
            rel_path: 投稿ファイルの相対パス（partial_failure_details.target 用）
        """
        # プレイリスト取得（2xx 以外はすべて HTTPStatusError として送出される）
        resp = await self._get_following_same_origin_redirect(
            client,
            playlist_url,
        )
        playlist_text = resp.text

        # マスタープレイリスト判定: #EXT-X-STREAM-INF が含まれる場合は
        # バリアントプレイリスト URL を解決してから ts セグメントを取得する
        if "#EXT-X-STREAM-INF" in playlist_text:
            variant_url = self._select_hls_variant(playlist_text, playlist_url)
            if variant_url is None:
                logger.warning(
                    "マスタープレイリストからバリアントを取得できません: %s",
                    playlist_url,
                )
                result.partial_failures += 1
                result.partial_failure_details.append(
                    {
                        "category": IngestErrorCategory.MEDIA_DOWNLOAD.value,
                        "target": rel_path,
                        "url": playlist_url,
                        "message": "HLS variant not selectable",
                    },
                )
                return
            logger.debug("HLS バリアント選択: %s", variant_url)
            resp = await self._get_following_same_origin_redirect(
                client,
                variant_url,
            )
            playlist_text = resp.text
            playlist_url = variant_url

        # ts セグメント URL を抽出（urljoin でルート相対・相対パスも正しく解決）
        segment_urls: list[str] = []
        for line in playlist_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            segment_urls.append(urljoin(playlist_url, line))

        if not segment_urls:
            logger.warning("HLS プレイリストに ts セグメントが見つかりません: %s", playlist_url)
            result.partial_failures += 1
            result.partial_failure_details.append(
                {
                    "category": IngestErrorCategory.MEDIA_DOWNLOAD.value,
                    "target": rel_path,
                    "url": playlist_url,
                    "message": "HLS playlist has no ts segments",
                },
            )
            return

        # ts セグメントを temp ファイルに逐次書き込み、全成功後に atomic rename
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path_str = tempfile.mkstemp(
            dir=str(dest.parent), suffix=".tmp",
        )
        tmp_path = Path(tmp_path_str)
        try:
            with open(tmp_fd, "wb") as f:
                for seg_url in segment_urls:
                    seg_resp = await self._get_following_same_origin_redirect(
                        client,
                        seg_url,
                    )
                    f.write(seg_resp.content)
            tmp_path.replace(dest)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        logger.debug("動画を保存しました（%d セグメント）: %s", len(segment_urls), dest)

    @staticmethod
    def _select_hls_variant(
        master_playlist: str,
        master_url: str,
    ) -> str | None:
        """マスタープレイリストから最低 BANDWIDTH のバリアント URL を返す.

        仕様: docs/specs/ingesters/bluesky.md

        Vision 解析用途のため低画質で十分。BANDWIDTH 属性をパースし、
        最小値のバリアントを選択する。BANDWIDTH が取得できない場合は
        最初のバリアントにフォールバックする。
        """
        lines = master_playlist.splitlines()
        candidates: list[tuple[int, str]] = []  # (bandwidth, url)
        fallback_url: str | None = None

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("#EXT-X-STREAM-INF"):
                # 次の非空行がバリアント URL
                variant_url: str | None = None
                for j in range(i + 1, len(lines)):
                    variant_line = lines[j].strip()
                    if variant_line and not variant_line.startswith("#"):
                        variant_url = variant_line
                        i = j
                        break
                if variant_url is not None:
                    if fallback_url is None:
                        fallback_url = variant_url
                    m = BlueskyIngester._BW_RE.search(line)
                    if m:
                        candidates.append((int(m.group(1)), variant_url))
            i += 1

        if candidates:
            # BANDWIDTH 最小のバリアントを選択
            _, best_url = min(candidates, key=lambda c: c[0])
            return urljoin(master_url, best_url)
        if fallback_url is not None:
            return urljoin(master_url, fallback_url)
        return None

    _BW_RE = re.compile(r"BANDWIDTH=(\d+)")
    _REDIRECT_STATUSES = frozenset({301, 302, 307, 308})

    @staticmethod
    def _base_domain(hostname: str | None) -> str:
        """ホスト名から末尾2セグメント（eTLD+1 相当）を返す.

        ccTLD（.co.uk 等）では正確な eTLD+1 を返さない簡易実装。
        BlueSky CDN のドメイン構成（.app TLD）では問題ない。
        """
        if not hostname:
            return ""
        parts = hostname.rsplit(".", 2)
        # "video.cdn.bsky.app" → ["video", "cdn", "bsky.app"] ではなく
        # rsplit(".", 2) → ["video.cdn", "bsky", "app"]
        # 末尾2つ = "bsky.app"
        if len(parts) >= 2:
            return f"{parts[-2]}.{parts[-1]}"
        return hostname

    @staticmethod
    async def _get_following_same_origin_redirect(
        client: ConstrainedClient,
        url: str,
        *,
        _max_redirects: int = 5,
    ) -> httpx.Response:
        """同一ベースドメインのリダイレクトのみ追従する GET リクエスト.

        ConstrainedClient の follow_redirects=False を維持しつつ、
        CDN の 3xx リダイレクトに対応する。

        **契約**: 2xx レスポンスのみを返す。以下のケースはすべて
        `httpx.HTTPStatusError` を送出する（3xx レスポンスを呼び出し側に
        漏らさない）:

        - 異ドメインへのリダイレクト（SSRF 防止）
        - リダイレクト回数上限到達
        - 3xx なのに `location` ヘッダが欠落
        - リダイレクト対象外の 3xx（300 / 303 / 304 等）・4xx・5xx

        **Why**: 3xx の本文（多くは HTML のエラーページ）が呼び出し側で
        プレイリスト・ts セグメントとして解釈され、破損ファイルが source_store
        に混入するリスクを構造的に排除するため。呼び出し側は `except Exception`
        で捕捉し、`partial_failures` を記録すること。

        `fetch_get` は使わない: `fetch_get` は非 2xx で例外化するため、
        リダイレクト追従中の 3xx で中断してしまう。代わりに、リダイレクト
        対象ステータス (`_REDIRECT_STATUSES`: 301/302/307/308) 以外の応答に
        到達した時点で `raise_for_status` を呼ぶ。

        Args:
            client: ConstrainedClient インスタンス
            url: リクエスト先 URL
            _max_redirects: リダイレクト追従の最大回数

        Raises:
            httpx.HTTPStatusError: 2xx 以外の応答に至ったすべての失敗経路
        """
        original_parsed = urlparse(url)
        original_base = BlueskyIngester._base_domain(original_parsed.hostname)
        resp = await client.get(url)

        for _ in range(_max_redirects):
            if resp.status_code not in BlueskyIngester._REDIRECT_STATUSES:
                # `fetch_get` と同じ契約: 非 2xx はすべて例外化する。
                # `raise_for_status` では 300 / 303 / 304 等のリダイレクト対象外
                # 3xx を例外化できないため、`is_success` で判定する。
                if not resp.is_success:
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code} error for url '{url}'",
                        request=resp.request,
                        response=resp,
                    )
                return resp
            location = resp.headers.get("location", "")
            if not location:
                # 3xx なのに location 欠落。以後追従できないため例外化
                raise httpx.HTTPStatusError(
                    f"redirect response without location header: {url}",
                    request=resp.request,
                    response=resp,
                )
            # 相対 URL を解決
            resolved = urljoin(url, location)
            parsed = urlparse(resolved)
            redirect_base = BlueskyIngester._base_domain(parsed.hostname)
            if parsed.scheme != original_parsed.scheme or redirect_base != original_base:
                logger.warning(
                    "リダイレクト先が異なるドメイン（拒否）: %s → %s",
                    url,
                    resolved,
                )
                raise httpx.HTTPStatusError(
                    f"cross-domain redirect rejected: {url} -> {resolved}",
                    request=resp.request,
                    response=resp,
                )
            url = resolved
            resp = await client.get(url)

        # ループを抜けた時点で 3xx のままならリダイレクト回数上限
        if resp.status_code in BlueskyIngester._REDIRECT_STATUSES:
            logger.warning("リダイレクト回数上限に到達: %s", url)
            raise httpx.HTTPStatusError(
                f"redirect limit exceeded: {url}",
                request=resp.request,
                response=resp,
            )
        # 非 2xx（raise_for_status でカバーされない 300/303/304 等を含む）は例外化
        if not resp.is_success:
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code} error for url '{url}'",
                request=resp.request,
                response=resp,
            )
        return resp

    async def follow_urls(
        self,
        placed_items: list[dict[str, Any]],
        *,
        source_store: SourceStore,
        settings: RAGSettings,
        youtube_ingester: YoutubeIngester | None = None,
        force_youtube_reingest: bool = False,
        result: IngestResult | None = None,
    ) -> dict[str, int]:
        """配置済み投稿から URL を抽出し、site_ingest/YouTube インジェスターに委譲する.

        仕様: docs/specs/ingesters/bluesky.md「投稿内 URL の自動取り込み」

        Web URL は site-ingest（複数 URL モード）の Python API を直接呼び出してバッチ
        取得する。親 CLI が write_lock を保持した状態で動作させるため、subprocess
        による二重ロック取得を避ける（#686）。
        YouTube URL は個別に YoutubeIngester で取り込む。

        各 placed_item の ``_suppress_youtube_reingest`` フラグにより YouTube 抑制対象
        判定を行う。抑制対象（True）の URL は ``force_youtube_reingest`` が True の場合
        のみ取り込む。抑制対象外（False）の URL は常に取り込む。

        Args:
            placed_items: 配置済みフィードアイテムのリスト
                （``_suppress_youtube_reingest`` フラグ付き）
            source_store: site-ingest の配置先 SourceStore
            settings: site-ingest のパラメータ参照用
            youtube_ingester: YoutubeIngester インスタンス
            force_youtube_reingest: 抑制対象の YouTube URL を強制的に取り込むか
            result: 委譲失敗の計上先 IngestResult。指定時は errors + category="delegation"
                を追加する（従来の stats 返却は互換維持）

        Returns:
            {"web_placed": N, "youtube_placed": N, "skipped": N, "errors": N}
        """
        stats: dict[str, int] = {
            "web_placed": 0,
            "youtube_placed": 0,
            "skipped": 0,
            "errors": 0,
        }

        # 全投稿から URL を一括抽出・重複排除・種別分類
        # YouTube URL は投稿の suppress 情報を保持する（抑制対象外なら常に取り込むため）
        # 同一 URL が「抑制対象」と「抑制対象外」両方に存在する場合は安全側（取り込む）に倒す
        web_urls: list[str] = []
        youtube_urls: list[str] = []
        seen: set[str] = set()
        youtube_url_suppressed: dict[str, bool] = {}
        for item in placed_items:
            item_suppressed = item.get("_suppress_youtube_reingest", False)
            for url in extract_urls_from_item(item):
                url_type = classify_url(url)
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
                        logger.warning("不正な YouTube URL を検出したためスキップ: %s", url)
                        stats["errors"] += 1
                        if result is not None:
                            result.errors += 1
                            result.error_details.append(
                                {
                                    "category": IngestErrorCategory.DELEGATION.value,
                                    "target": url,
                                    "url": url,
                                    "message": "invalid youtube url",
                                },
                            )
                    else:
                        stats["skipped"] += 1

        all_url_count = (
            len(web_urls) + len(youtube_urls) + stats["skipped"] + stats["errors"]
        )
        if all_url_count == 0:
            return stats

        logger.info("投稿内から %d 件の URL を抽出しました", all_url_count)

        # Web URL をバッチ取得（site-ingest 複数 URL モード、Python API 直呼出し）
        if web_urls:
            web_placed, web_errors, web_error_details = await self._fetch_web_urls(
                web_urls, source_store=source_store, settings=settings,
            )
            stats["web_placed"] = web_placed
            stats["errors"] += web_errors
            if result is not None and web_error_details:
                result.errors += len(web_error_details)
                result.error_details.extend(web_error_details)

        # YouTube URL を個別取り込み（URL 間にレート制限スリープを挿入）
        # 抑制対象の YouTube URL は force_youtube_reingest 設定に従う
        # 抑制対象外の YouTube URL は常に取り込む
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
            for i, url in enumerate(youtube_urls):
                if youtube_ingester is not None:
                    try:
                        yt_result = await youtube_ingester.ingest_video(video_url=url)
                        stats["youtube_placed"] += yt_result.placed
                        if yt_result.errors > 0:
                            stats["errors"] += yt_result.errors
                    except Exception as exc:
                        logger.exception("YouTube URL の取り込みに失敗: %s", url)
                        stats["errors"] += 1
                        if result is not None:
                            result.errors += 1
                            result.error_details.append(
                                {
                                    "category": IngestErrorCategory.DELEGATION.value,
                                    "target": url,
                                    "url": url,
                                    "message": f"youtube delegation failed: {exc}",
                                },
                            )
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

    async def _fetch_web_urls(
        self,
        urls: list[str],
        *,
        source_store: SourceStore,
        settings: RAGSettings,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        """Web URL を site-ingest（複数 URL モード）の Python API で取得する.

        親プロセスが既に write_lock を保持している前提で、subprocess を介さず同一
        プロセス内で site-ingest のコア処理を呼び出す（#686）。Scrapy 自体は
        site-ingest 内部で別 subprocess として起動される（reactor 制約のため）。

        URL バリデーション (validate_url) と SSRF チェック (check_ssrf) を冒頭で
        実施する。subprocess 経由から Python API 直呼出しに変更したことで、
        従来 CLI ``site-ingest`` の入口で行われていた多層防御の初回チェック層が
        欠落するため、bluesky 側で補完する（Scrapy Downloader Middleware の
        per-request チェックは引き続き有効）。

        Args:
            urls: 取得対象の Web URL リスト
            source_store: 配置先の SourceStore
            settings: site-ingest 設定の参照元

        Returns:
            (配置されたファイル数の合計, エラー件数, errors の dict リスト)。
            ``error_details`` の件数とエラー件数は常に一致する。
        """
        from rag.utils.url import check_ssrf, validate_url

        logger.info("site-ingest（複数 URL モード）で %d 件の Web URL を取り込みます", len(urls))

        validated_urls: list[str] = []
        validation_errors: list[dict[str, Any]] = []
        for url in urls:
            try:
                validated = validate_url(url)
                check_ssrf(validated)
            except ValueError as exc:
                logger.warning("Web URL バリデーション失敗: %s (%s)", url, exc)
                validation_errors.append(
                    {
                        "category": IngestErrorCategory.DELEGATION.value,
                        "target": url,
                        "url": url,
                        "message": f"url validation failed: {exc}",
                    },
                )
                continue
            validated_urls.append(validated)

        if not validated_urls:
            return 0, len(validation_errors), validation_errors

        try:
            execution = await execute_site_ingest(
                urls=validated_urls,
                source_store=source_store,
                settings=settings,
            )
        except Exception as exc:
            logger.exception("site-ingest 実行に失敗: %d 件", len(validated_urls))
            execute_errors = [
                {
                    "category": IngestErrorCategory.DELEGATION.value,
                    "target": url,
                    "url": url,
                    "message": f"site-ingest failed: {exc}",
                }
                for url in validated_urls
            ]
            all_errors = validation_errors + execute_errors
            return 0, len(all_errors), all_errors

        bridge = execution.bridge
        placed = bridge.ingest.placed + bridge.ingest.overwritten
        # bridge.ingest.errors は error_details に対応するエントリを持つ。
        # bridge.parse_errors は JSONL 行単位のパースエラー件数で個別 detail を
        # 持たないため、件数整合のため集約エントリを 1 件追加する。
        error_details: list[dict[str, Any]] = list(bridge.ingest.error_details)
        if bridge.parse_errors > 0:
            error_details.append(
                {
                    "category": IngestErrorCategory.DELEGATION.value,
                    "target": "site-ingest:jsonl",
                    "message": (
                        f"JSONL のパースに失敗した行が {bridge.parse_errors} 件"
                        "あります（site-ingest 出力）"
                    ),
                },
            )
        all_error_details = validation_errors + error_details
        total_errors = len(all_error_details)
        logger.info(
            "site-ingest 完了: 合計 %d 件配置, %d 件エラー",
            placed, total_errors,
        )
        # 正常完了後のクリーンアップ（仕様: docs/specs/site-ingest.md）。
        # BlueSky 経由では bridge 完了後ただちに cleanup してよい
        # （後続のインデックス化は親 controller が一括で実行する）
        if execution.scrapy_success and execution.crawl_result is not None:
            execution.crawl_result.cleanup()
        return placed, total_errors, all_error_details

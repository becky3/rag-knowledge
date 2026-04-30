"""BlueSky 投稿の source_store 配置 + .meta 生成 + メディア DL のキック.

仕様: docs/specs/ingesters/bluesky.md
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from rag.pipeline.ingesters._common import (
    IngestErrorCategory,
    IngestResult,
    extract_http_status,
    now_iso,
)

if TYPE_CHECKING:
    from rag.pipeline.ingesters.bluesky_media_downloader import (
        BlueskyMediaDownloader,
    )
    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)

# DID 内のコロンを全角に置換する文字（Windows パス互換性）
_COLON_FULLWIDTH = "："

# Content-Type → 拡張子マッピング（メディア DL 用、配置時の拡張子決定）
_CONTENT_TYPE_EXT: dict[str, str] = {
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "video/mp2t": ".ts",
}

# Content-Type が不明な場合のデフォルト拡張子
_DEFAULT_IMAGE_EXT = ".webp"


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


def _ext_from_content_type(content_type: str) -> str:
    """Content-Type ヘッダーから拡張子を決定する."""
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


def _collect_media_from_embed(
    embed: dict[str, Any], image_urls: list[str],
) -> None:
    """embed オブジェクトから画像 URL を収集する."""
    images = embed.get("images")
    if isinstance(images, list):
        for img in images:
            if isinstance(img, dict):
                fullsize = img.get("fullsize")
                if isinstance(fullsize, str) and fullsize:
                    image_urls.append(fullsize)


async def _download_media(
    item: dict[str, Any],
    *,
    media_downloader: BlueskyMediaDownloader,
    media_dir: Any,  # Path（TYPE_CHECKING で直接 import せず Any で受ける）
    result: IngestResult,
    rel_path: str,
) -> None:
    """投稿に添付されたメディア（画像・動画）を DL し配置する.

    BlueskyMediaDownloader Protocol 経由で DL を実行する。
    画像は Content-Type ベースで拡張子を決定、動画は ``.ts`` 固定。
    失敗は ``partial_failures`` に計上し、投稿 JSON 配置自体には影響しない。
    """
    image_urls, playlist_url = _extract_media_urls(item)

    for idx, img_url in enumerate(image_urls):
        try:
            data, content_type = await media_downloader.download_image(img_url)
            ext = _ext_from_content_type(content_type)
            filename = f"image_{idx}{ext}"
            dest = media_dir / filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            for existing in media_dir.glob(f"image_{idx}.*"):
                if existing != dest:
                    existing.unlink(missing_ok=True)
            dest.write_bytes(data)
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

    if playlist_url:
        try:
            video_data = await media_downloader.download_hls_video(playlist_url)
            dest = media_dir / "video_0.ts"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(video_data)
            logger.debug("動画を保存しました: %s", dest)
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


async def place_post(
    item: dict[str, Any],
    *,
    is_repost: bool,
    force: bool,
    store: SourceStore,
    media_downloader: BlueskyMediaDownloader,
    result: IngestResult,
    seen_paths: set[str] | None = None,
) -> tuple[bool, bool] | None:
    """単一投稿を source_store に配置する.

    placed_items の組み立ては呼び出し側の責務。本関数は配置結果（成功可否と
    既存ファイル上書きだったかの事実）のみを返す。

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

    dest = store.root_dir / rel_path
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
        store.place_file(
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
            store.root_dir
            / "bluesky"
            / escaped_did
            / year
            / month
            / "media"
            / rkey
        )
        await _download_media(
            item,
            media_downloader=media_downloader,
            media_dir=media_dir,
            result=result,
            rel_path=rel_path,
        )

    return True, is_overwrite

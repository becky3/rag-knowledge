"""BlueSky インジェスター.

仕様: docs/specs/ingesters/bluesky.md

AT Protocol API 経由で BlueSky の投稿を取得し、
source_store にファイルを配置する。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from rag.pipeline.ingesters._common import IngestResult, now_iso
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from py_common_lib.httpx import ConstrainedClient
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
        request_timeout: int = 30,
        request_interval: float = 1.0,
        include_reposts: bool = True,
    ) -> None:
        self._store = source_store
        self._appview_url = appview_url.rstrip("/")
        self._max_posts = max_posts
        self._request_timeout = request_timeout
        self._request_interval = request_interval
        self._include_reposts = include_reposts

    async def crawl_bluesky(
        self,
        handle: str,
        *,
        max_posts: int | None = None,
        include_reposts: bool | None = None,
        client: ConstrainedClient | None = None,
    ) -> IngestResult:
        """BlueSky 投稿を取得し source_store に配置する.

        Args:
            handle: BlueSky ハンドル
            max_posts: 取得する最大投稿数（None の場合はインスタンス設定を使用）
            include_reposts: リポストを含めるか（None の場合はインスタンス設定を使用）
            client: ConstrainedClient インスタンス

        Returns:
            配置結果
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
                    dt = datetime.fromisoformat(created_at_str)
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
                except Exception:
                    logger.exception("投稿の配置に失敗しました: %s", rel_path)
                    result.errors += 1
                    result.error_details.append(rel_path)

            # 次ページの確認
            cursor = data.get("cursor")
            if not cursor:
                break

        return result

"""BlueSky インジェスター: AT Protocol API 経由で BlueSky 投稿を取得しナレッジベースに取り込む

仕様: docs/specs/bluesky-ingester.md
Issue: #185, #194
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from .base import BaseIngester, IngestedContent

if TYPE_CHECKING:
    from py_common_lib.httpx import ConstrainedClient

logger = logging.getLogger(__name__)

# --- ハードリミット（コード内定数。設定・引数・環境変数で緩和不可） ---
MAX_POSTS_HARD_LIMIT = 1000
"""投稿取得上限（タイムライン全体。リポストを含む全アイテムが対象）"""

# --- AT Protocol ---
FEED_PAGE_SIZE = 100
"""getAuthorFeed API の 1 ページあたり取得件数（API 上限）"""

COLLECTION_POST = "app.bsky.feed.post"
"""オリジナル投稿のコレクション"""

REASON_REPOST = "app.bsky.feed.defs#reasonRepost"
"""リポスト理由の $type"""

# --- バリデーション ---
_AT_URI_PATTERN = re.compile(
    r"^at://(?P<did>did:[a-z]+:[a-zA-Z0-9._:%-]+)"
    r"/(?P<collection>[a-zA-Z0-9.]+)"
    r"/(?P<rkey>[a-zA-Z0-9._~-]+)$"
)
"""AT URI のパターン"""

# --- テキスト抽出 ---
TITLE_MAX_LENGTH = 50
"""タイトルの最大文字数"""


def _validate_max_posts(value: int) -> int:
    """max_posts をバリデーションし、必要に応じてクランプする.

    仕様: docs/specs/bluesky-ingester.md「バリデーションとクランプの使い分け」

    - バリデーションエラー（拒否）: 型不正、0、負数
    - クランプ（警告ログ付き）: 正の整数だが許容範囲外

    Args:
        value: 検証する値

    Returns:
        検証済みの値（クランプ適用後）

    Raises:
        ValueError: 0 または負数の場合
        TypeError: 整数でない場合
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(
            f"max_posts must be an integer, got {type(value).__name__}"
        )
    if value <= 0:
        raise ValueError(
            f"max_posts must be a positive integer, got {value}"
        )
    if value > MAX_POSTS_HARD_LIMIT:
        logger.warning(
            "max_posts=%d exceeds hard limit %d, clamping to %d",
            value,
            MAX_POSTS_HARD_LIMIT,
            MAX_POSTS_HARD_LIMIT,
        )
        return MAX_POSTS_HARD_LIMIT
    return value


def _parse_at_uri(uri: str) -> tuple[str, str, str] | None:
    """AT URI を解析して (did, collection, rkey) を返す.

    Args:
        uri: AT URI 文字列

    Returns:
        (did, collection, rkey) のタプル、またはパース失敗時は None
    """
    m = _AT_URI_PATTERN.match(uri)
    if m is None:
        return None
    return m.group("did"), m.group("collection"), m.group("rkey")


def _extract_text_from_post(value: dict[str, Any]) -> str:
    """投稿レコードからテキストを構造化して抽出する.

    仕様: docs/specs/bluesky-ingester.md「テキスト抽出」

    構造:
    1. 投稿テキスト（先頭）
    2. 画像/動画 ALT テキスト（[Image ALT] / [Video ALT] プレフィックス）
    3. リンクカード（[Link Card] セクション）

    引用元テキストは呼び出し元で [Quote] セクションとして追加する。

    Args:
        value: 投稿レコードの value/record オブジェクト

    Returns:
        構造化されたプレーンテキスト
    """
    sections: list[str] = []

    # 1. 投稿テキスト（先頭）
    text = value.get("text", "")
    if text:
        sections.append(text)

    # 2-3. embed からメディア情報を抽出
    embed = value.get("embed")
    if isinstance(embed, dict):
        media_sections = _extract_embed_sections(embed)
        sections.extend(media_sections)

    return "\n\n".join(sections)


def _extract_embed_sections(embed: dict[str, Any]) -> list[str]:
    """embed オブジェクトから構造化セクションを抽出する.

    Args:
        embed: embed オブジェクト

    Returns:
        構造化セクションのリスト
    """
    embed_type = embed.get("$type", "")

    if embed_type == "app.bsky.embed.recordWithMedia":
        media = embed.get("media")
        if isinstance(media, dict):
            return _extract_media_sections(media)
        return []

    return _extract_media_sections(embed)


def _extract_media_sections(media: dict[str, Any]) -> list[str]:
    """メディアオブジェクトから構造化セクションを抽出する.

    Args:
        media: メディアオブジェクト（embed または embed.media）

    Returns:
        構造化セクションのリスト
    """
    sections: list[str] = []

    # 画像 ALT テキスト
    images = media.get("images")
    if isinstance(images, list):
        alt_texts = [
            img.get("alt", "")
            for img in images
            if isinstance(img, dict) and img.get("alt", "")
        ]
        if alt_texts:
            sections.append("[Image ALT] " + "\n".join(alt_texts))

    # 動画 ALT テキスト
    video_alt = media.get("alt", "")
    if video_alt:
        sections.append(f"[Video ALT] {video_alt}")

    # リンクカード
    external = media.get("external")
    if isinstance(external, dict):
        card_parts: list[str] = ["[Link Card]"]
        ext_title = external.get("title", "")
        if ext_title:
            card_parts.append(f"Title: {ext_title}")
        ext_uri = external.get("uri", "")
        if ext_uri:
            card_parts.append(f"URL: {ext_uri}")
        ext_desc = external.get("description", "")
        if ext_desc:
            card_parts.append(f"Description: {ext_desc}")
        if len(card_parts) > 1:
            sections.append("\n".join(card_parts))

    return sections


def _make_title(text: str) -> str:
    """投稿テキストからタイトルを生成する.

    Args:
        text: 投稿テキスト

    Returns:
        先頭 50 文字（超過時は末尾に「...」を付加）
    """
    # 改行を空白に置換してフラット化
    flat = text.replace("\n", " ").strip()
    if len(flat) > TITLE_MAX_LENGTH:
        return flat[:TITLE_MAX_LENGTH] + "..."
    return flat


def _validate_appview_url(url: str) -> str:
    """AppView URL をバリデーションし、正規化する.

    Args:
        url: AppView URL 文字列

    Returns:
        正規化済み AppView URL（末尾スラッシュ除去済み）

    Raises:
        ValueError: URL が空または HTTPS スキームでない場合
    """
    url = url.strip()
    if not url:
        raise ValueError("appview_url must not be empty")
    if not url.startswith("https://"):
        raise ValueError(
            f"appview_url must use HTTPS scheme, got: {url!r}"
        )
    return url.rstrip("/")


def _extract_quote_text_from_view_embed(
    view_embed: dict[str, Any] | None,
) -> str | None:
    """view embed（post.embed）から引用元テキストを取得する.

    getAuthorFeed のレスポンスでは引用元テキストが post.embed に展開済み。

    パス:
    - app.bsky.embed.record#view → post.embed.record.value.text
    - app.bsky.embed.recordWithMedia#view → post.embed.record.record.value.text

    Args:
        view_embed: post.embed オブジェクト（view 版）

    Returns:
        引用元テキスト、または引用なし/非投稿引用時は None
    """
    if not isinstance(view_embed, dict):
        return None

    embed_type = view_embed.get("$type", "")

    if embed_type == "app.bsky.embed.record#view":
        record = view_embed.get("record")
        if isinstance(record, dict):
            value = record.get("value")
            if isinstance(value, dict):
                text = value.get("text", "")
                return text if text else None
    elif embed_type == "app.bsky.embed.recordWithMedia#view":
        record = view_embed.get("record")
        if isinstance(record, dict):
            inner_record = record.get("record")
            if isinstance(inner_record, dict):
                value = inner_record.get("value")
                if isinstance(value, dict):
                    text = value.get("text", "")
                    return text if text else None

    return None


class BlueskyIngester(BaseIngester):
    """BlueSky 投稿取り込み用インジェスター.

    仕様: docs/specs/bluesky-ingester.md

    getAuthorFeed API を使用してタイムラインを走査し、テキストを抽出する。
    全 HTTP リクエストは ConstrainedClient 経由で実行する。
    """

    def __init__(
        self,
        client: ConstrainedClient,
        *,
        appview_url: str = "https://public.api.bsky.app",
        max_posts: int = 200,
    ) -> None:
        """BlueskyIngester を初期化する.

        Args:
            client: ConstrainedClient インスタンス
            appview_url: AppView のベース URL（デフォルト: https://public.api.bsky.app）
            max_posts: 取得する最大投稿数（デフォルト: 200、許容範囲: 1〜1000）

        Raises:
            ValueError: max_posts が 0 または負数の場合、appview_url が空または非 HTTPS の場合
            TypeError: max_posts が整数でない場合
        """
        self._client = client
        self._appview_url = _validate_appview_url(appview_url)
        self._max_posts = _validate_max_posts(max_posts)

    def validate_identifier(self, identifier: str) -> str:
        """AT URI を検証し、正規化済みの AT URI を返す.

        Args:
            identifier: 検証する AT URI

        Returns:
            正規化済み AT URI

        Raises:
            ValueError: AT URI が不正な場合
        """
        if not identifier or not identifier.strip():
            raise ValueError("AT URI must not be empty")
        uri = identifier.strip()
        parsed = _parse_at_uri(uri)
        if parsed is None:
            raise ValueError(
                f"Invalid AT URI format: {uri!r}. "
                "Expected format: at://did:xxx/collection/rkey"
            )
        return uri

    async def fetch_single(self, identifier: str) -> IngestedContent | None:
        """AT URI による個別投稿取得.

        getRecord API で単一投稿を取得し、IngestedContent を構築する。

        Args:
            identifier: AT URI（例: at://did:plc:xxx/app.bsky.feed.post/rkey）

        Returns:
            IngestedContent、または取得失敗時は None
        """
        uri = self.validate_identifier(identifier)
        parsed = _parse_at_uri(uri)
        if parsed is None:
            return None

        did, collection, rkey = parsed
        record = await self._get_record(did, collection, rkey)
        if record is None:
            return None

        value = record.get("value")
        if not isinstance(value, dict):
            logger.warning("Unexpected record value type for %s", uri)
            return None

        text = _extract_text_from_post(value)
        if not text.strip():
            logger.warning("No text extracted from post: %s", uri)
            return None

        return self._build_content(
            did=did,
            rkey=rkey,
            value=value,
            text=text,
            handle=did,
            is_repost=False,
        )

    async def crawl(
        self,
        handle: str,
        *,
        max_posts: int | None = None,
        include_reposts: bool = False,
    ) -> list[IngestedContent]:
        """指定ユーザーの BlueSky 投稿を統一タイムラインから一括取得する.

        Args:
            handle: BlueSky ハンドル（例: user.bsky.social）
            max_posts: 取得する最大投稿数（None 時はコンストラクタの値を使用）
            include_reposts: タイムラインにリポストを含めるか

        Returns:
            IngestedContent のリスト

        Raises:
            ValueError: handle が不正な場合
        """
        handle = self._validate_handle(handle)

        effective_max = self._max_posts
        if max_posts is not None:
            effective_max = _validate_max_posts(max_posts)

        # source_id の重複チェック用セット
        seen_source_ids: set[str] = set()
        contents: list[IngestedContent] = []

        # 統一タイムラインの走査
        feed_items = await self._get_author_feed(
            handle, max_items=effective_max
        )

        for item in feed_items:
            content = self._process_feed_item(
                item, handle, seen_source_ids,
                include_reposts=include_reposts,
            )
            if content is not None:
                contents.append(content)

        return contents

    def _process_feed_item(
        self,
        item: dict[str, Any],
        handle: str,
        seen_source_ids: set[str],
        *,
        include_reposts: bool,
    ) -> IngestedContent | None:
        """フィードアイテムを処理して IngestedContent を構築する.

        Args:
            item: getAuthorFeed のフィードアイテム
            handle: ユーザーハンドル（ツール呼び出し元のハンドル）
            seen_source_ids: 処理済み source_id のセット
            include_reposts: リポストを含めるか

        Returns:
            IngestedContent、またはスキップ/失敗時は None
        """
        # リポスト判定
        reason = item.get("reason")
        is_repost = (
            isinstance(reason, dict)
            and reason.get("$type") == REASON_REPOST
        )

        # リポスト除外フィルタ
        if is_repost and not include_reposts:
            return None

        post = item.get("post")
        if not isinstance(post, dict):
            return None

        uri = post.get("uri", "")
        parsed = _parse_at_uri(uri)
        if parsed is None:
            logger.warning("Invalid AT URI in feed item: %s", uri)
            return None

        did, _collection, rkey = parsed
        source_id = f"at://{did}/{COLLECTION_POST}/{rkey}"

        # 重複チェック
        if source_id in seen_source_ids:
            return None
        seen_source_ids.add(source_id)

        record = post.get("record")
        if not isinstance(record, dict):
            logger.warning("Unexpected record type in feed item: %s", uri)
            return None

        # テキスト抽出（post.record = raw record）
        text = _extract_text_from_post(record)

        # 引用元テキストの取得（post.embed = view 版）
        view_embed = post.get("embed")
        quote_text = _extract_quote_text_from_view_embed(view_embed)
        if quote_text:
            quote_section = "[Quote]\n" + quote_text
            text = text + "\n\n" + quote_section if text else quote_section

        # リポストヘッダーの付与
        if is_repost:
            author = post.get("author")
            author_handle = author.get("handle", "") if isinstance(author, dict) else ""
            if author_handle:
                text = f"[Repost: @{author_handle}]\n{text}"

        if not text.strip():
            logger.debug("Skipping empty post: %s", uri)
            return None

        return self._build_content(
            did=did,
            rkey=rkey,
            value=record,
            text=text,
            handle=handle,
            is_repost=is_repost,
        )

    async def _get_author_feed(
        self,
        actor: str,
        *,
        max_items: int,
    ) -> list[dict[str, Any]]:
        """getAuthorFeed API をページネーション走査する.

        Args:
            actor: ハンドルまたは DID
            max_items: 取得上限

        Returns:
            フィードアイテムのリスト
        """
        items: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            query_params: dict[str, str | int] = {
                "actor": actor,
                "limit": FEED_PAGE_SIZE,
            }
            if cursor is not None:
                query_params["cursor"] = cursor

            url = (
                f"{self._appview_url}/xrpc/app.bsky.feed.getAuthorFeed"
                f"?{urlencode(query_params)}"
            )

            try:
                resp = await self._client.get(url)
            except Exception:
                logger.exception(
                    "Failed to fetch feed for %s", actor
                )
                break

            if resp.status_code != 200:
                logger.error(
                    "getAuthorFeed returned status %d for %s",
                    resp.status_code,
                    actor,
                )
                break

            try:
                data: dict[str, Any] = resp.json()
            except Exception:
                logger.exception(
                    "Failed to parse getAuthorFeed JSON for %s", actor
                )
                break

            feed = data.get("feed")
            if not isinstance(feed, list):
                logger.error(
                    "Unexpected feed format in getAuthorFeed response (%s)",
                    actor,
                )
                break

            if not feed:
                break

            for feed_item in feed:
                if len(items) >= max_items:
                    break
                if isinstance(feed_item, dict):
                    items.append(feed_item)

            if len(items) >= max_items:
                if len(items) > max_items:
                    items = items[:max_items]
                logger.info(
                    "Reached max_items limit (%d) for %s",
                    max_items,
                    actor,
                )
                break

            next_cursor = data.get("cursor")
            if next_cursor is None:
                break

            cursor = str(next_cursor)

        logger.info(
            "Listed %d feed items for %s",
            len(items),
            actor,
        )
        return items

    async def _get_record(
        self,
        repo: str,
        collection: str,
        rkey: str,
    ) -> dict[str, Any] | None:
        """getRecord API で個別レコードを取得する.

        fetch_single で使用。タイムライン一括取得では使用しない。

        Args:
            repo: DID またはハンドル
            collection: レコードコレクション
            rkey: レコードキー

        Returns:
            レコードオブジェクト、または取得失敗時は None
        """
        query_params = urlencode({
            "repo": repo,
            "collection": collection,
            "rkey": rkey,
        })
        url = (
            f"{self._appview_url}/xrpc/com.atproto.repo.getRecord"
            f"?{query_params}"
        )

        try:
            resp = await self._client.get(url)
        except Exception:
            logger.exception(
                "Failed to get record: %s/%s/%s", repo, collection, rkey
            )
            return None

        if resp.status_code != 200:
            log_fn = logger.warning if resp.status_code == 404 else logger.error
            log_fn(
                "getRecord returned status %d for %s/%s/%s",
                resp.status_code,
                repo,
                collection,
                rkey,
            )
            return None

        try:
            data: dict[str, Any] = resp.json()
            return data
        except Exception:
            logger.exception(
                "Failed to parse getRecord JSON for %s/%s/%s",
                repo,
                collection,
                rkey,
            )
            return None

    def _build_content(
        self,
        *,
        did: str,
        rkey: str,
        value: dict[str, Any],
        text: str,
        handle: str,
        is_repost: bool,
    ) -> IngestedContent:
        """IngestedContent を構築する.

        Args:
            did: DID
            rkey: レコードキー
            value: レコードの value/record オブジェクト
            text: 抽出済みテキスト
            handle: ユーザーハンドル
            is_repost: リポスト経由の投稿か

        Returns:
            IngestedContent
        """
        source_id = f"at://{did}/{COLLECTION_POST}/{rkey}"
        title = _make_title(text)
        created_at = value.get("createdAt", "")

        embed = value.get("embed")
        has_images = False
        has_video = False
        has_external_link = False

        if isinstance(embed, dict):
            embed_type = embed.get("$type", "")
            media_obj = embed

            if embed_type == "app.bsky.embed.recordWithMedia":
                media_obj = embed.get("media", {})

            has_images = isinstance(media_obj.get("images"), list) and len(
                media_obj.get("images", [])
            ) > 0
            has_video = media_obj.get("$type", "") == "app.bsky.embed.video"
            has_external_link = isinstance(media_obj.get("external"), dict)

        is_reply = isinstance(value.get("reply"), dict)

        metadata: dict[str, object] = {
            "handle": handle,
            "did": did,
            "rkey": rkey,
            "url": f"https://bsky.app/profile/{handle}/post/{rkey}",
            "createdAt": created_at,
            "has_images": has_images,
            "has_video": has_video,
            "has_external_link": has_external_link,
            "is_reply": is_reply,
            "is_repost": is_repost,
        }

        return IngestedContent(
            source_id=source_id,
            title=title,
            text=text,
            ingested_at=IngestedContent.now_iso(),
            source_type="bluesky",
            metadata=metadata,
            skip_chunking=True,
        )

    @staticmethod
    def _validate_handle(handle: str) -> str:
        """ハンドルを検証する.

        Args:
            handle: BlueSky ハンドル

        Returns:
            正規化済みハンドル

        Raises:
            ValueError: ハンドルが不正な場合
        """
        if not handle or not handle.strip():
            raise ValueError("handle must not be empty")
        handle = handle.strip()
        if handle.startswith("did:"):
            raise ValueError(
                f"DID format is not accepted as handle: {handle!r}. "
                "Please specify a handle (e.g., user.bsky.social)"
            )
        return handle

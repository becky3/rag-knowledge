"""BlueSky インジェスター: AT Protocol API 経由で BlueSky 投稿を取得しナレッジベースに取り込む

仕様: docs/specs/bluesky-ingester.md
Issue: #185
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
"""投稿取得上限（オリジナル投稿のみ。リポストは対象外）"""

# --- AT Protocol ---
LISTRECORDS_PAGE_SIZE = 100
"""listRecords API の 1 ページあたり取得件数（API 上限）"""

COLLECTION_POST = "app.bsky.feed.post"
"""オリジナル投稿のコレクション"""

COLLECTION_REPOST = "app.bsky.feed.repost"
"""リポストのコレクション"""

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
    """投稿レコードからテキストを抽出する.

    仕様: docs/specs/bluesky-ingester.md「テキスト抽出」

    抽出対象:
    - 投稿テキスト (value.text)
    - 画像 ALT テキスト (value.embed.images[].alt)
    - 動画 ALT テキスト (value.embed.alt)
    - リンクカードタイトル (value.embed.external.title)
    - リンクカード説明 (value.embed.external.description)
    - recordWithMedia 時の media 配下も同様

    Args:
        value: 投稿レコードの value オブジェクト

    Returns:
        改行で連結されたプレーンテキスト
    """
    parts: list[str] = []

    # 投稿テキスト
    text = value.get("text", "")
    if text:
        parts.append(text)

    embed = value.get("embed")
    if isinstance(embed, dict):
        _extract_embed_text(embed, parts)

    return "\n".join(parts)


def _extract_embed_text(embed: dict[str, Any], parts: list[str]) -> None:
    """embed オブジェクトからテキストを抽出する.

    Args:
        embed: embed オブジェクト
        parts: テキストパーツのリスト（破壊的に追加）
    """
    embed_type = embed.get("$type", "")

    if embed_type == "app.bsky.embed.recordWithMedia":
        # recordWithMedia: media 配下にメディア情報がネスト
        media = embed.get("media")
        if isinstance(media, dict):
            _extract_media_text(media, parts)
    else:
        # 通常の embed
        _extract_media_text(embed, parts)


def _extract_media_text(media: dict[str, Any], parts: list[str]) -> None:
    """メディアオブジェクトからテキストを抽出する.

    Args:
        media: メディアオブジェクト（embed または embed.media）
        parts: テキストパーツのリスト（破壊的に追加）
    """
    # 画像 ALT テキスト
    images = media.get("images")
    if isinstance(images, list):
        for img in images:
            if isinstance(img, dict):
                alt = img.get("alt", "")
                if alt:
                    parts.append(alt)

    # 動画 ALT テキスト
    video_alt = media.get("alt", "")
    if video_alt:
        parts.append(video_alt)

    # リンクカード
    external = media.get("external")
    if isinstance(external, dict):
        ext_title = external.get("title", "")
        if ext_title:
            parts.append(ext_title)
        ext_desc = external.get("description", "")
        if ext_desc:
            parts.append(ext_desc)


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


def _validate_pds_url(url: str) -> str:
    """PDS URL をバリデーションし、正規化する.

    Args:
        url: PDS URL 文字列

    Returns:
        正規化済み PDS URL（末尾スラッシュ除去済み）

    Raises:
        ValueError: URL が空または HTTPS スキームでない場合
    """
    url = url.strip()
    if not url:
        raise ValueError("pds_url must not be empty")
    if not url.startswith("https://"):
        raise ValueError(
            f"pds_url must use HTTPS scheme, got: {url!r}"
        )
    return url.rstrip("/")


class BlueskyIngester(BaseIngester):
    """BlueSky 投稿取り込み用インジェスター.

    仕様: docs/specs/bluesky-ingester.md

    AT Protocol API を使用して投稿一覧を走査し、テキストを抽出する。
    全 HTTP リクエストは ConstrainedClient 経由で実行する。
    """

    def __init__(
        self,
        client: ConstrainedClient,
        *,
        pds_url: str = "https://bsky.social",
        max_posts: int = 200,
    ) -> None:
        """BlueskyIngester を初期化する.

        Args:
            client: ConstrainedClient インスタンス
            pds_url: PDS のベース URL（デフォルト: https://bsky.social）
            max_posts: 取得する最大投稿数（デフォルト: 200、許容範囲: 1〜1000）

        Raises:
            ValueError: max_posts が 0 または負数の場合、pds_url が空または非 HTTPS の場合
            TypeError: max_posts が整数でない場合
        """
        self._client = client
        self._pds_url = _validate_pds_url(pds_url)
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
        """指定ユーザーの BlueSky 投稿を一括取得する.

        Args:
            handle: BlueSky ハンドル（例: user.bsky.social）
            max_posts: 取得する最大投稿数（None 時はコンストラクタの値を使用）
            include_reposts: リポストを取得対象に含めるか

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

        # 1. オリジナル投稿の走査
        post_records = await self._list_records(
            handle, COLLECTION_POST, max_records=effective_max
        )

        for record in post_records:
            content = await self._process_post_record(
                record, handle, seen_source_ids, is_repost=False
            )
            if content is not None:
                contents.append(content)

        # 2. リポストの走査（オプション）
        if include_reposts:
            repost_records = await self._list_records(
                handle, COLLECTION_REPOST, max_records=None
            )
            for repost_record in repost_records:
                content = await self._process_repost_record(
                    repost_record, handle, seen_source_ids
                )
                if content is not None:
                    contents.append(content)

        return contents

    async def _process_post_record(
        self,
        record: dict[str, Any],
        handle: str,
        seen_source_ids: set[str],
        *,
        is_repost: bool,
    ) -> IngestedContent | None:
        """投稿レコードを処理して IngestedContent を構築する.

        Args:
            record: listRecords/getRecord のレコードオブジェクト
            handle: ユーザーハンドル
            seen_source_ids: 処理済み source_id のセット
            is_repost: リポスト経由で取得した投稿かどうか

        Returns:
            IngestedContent、またはスキップ/失敗時は None
        """
        uri = record.get("uri", "")
        parsed = _parse_at_uri(uri)
        if parsed is None:
            logger.warning("Invalid AT URI in record: %s", uri)
            return None

        did, _collection, rkey = parsed
        source_id = f"at://{did}/{COLLECTION_POST}/{rkey}"

        # 重複チェック
        if source_id in seen_source_ids:
            return None
        seen_source_ids.add(source_id)

        value = record.get("value")
        if not isinstance(value, dict):
            logger.warning("Unexpected record value type: %s", uri)
            return None

        # テキスト抽出
        text = _extract_text_from_post(value)

        # 引用元テキストの取得
        quote_text = await self._fetch_quote_text(value)
        if quote_text:
            text = text + "\n[引用元]\n" + quote_text if text else "[引用元]\n" + quote_text

        if not text.strip():
            logger.debug("Skipping empty post: %s", uri)
            return None

        return self._build_content(
            did=did,
            rkey=rkey,
            value=value,
            text=text,
            handle=handle,
            is_repost=is_repost,
        )

    async def _process_repost_record(
        self,
        repost_record: dict[str, Any],
        handle: str,
        seen_source_ids: set[str],
    ) -> IngestedContent | None:
        """リポストレコードを処理し、元投稿を取得する.

        Args:
            repost_record: リポストレコード
            handle: リポスト元ユーザーハンドル
            seen_source_ids: 処理済み source_id のセット

        Returns:
            IngestedContent、またはスキップ/失敗時は None
        """
        value = repost_record.get("value")
        if not isinstance(value, dict):
            return None

        subject = value.get("subject")
        if not isinstance(subject, dict):
            return None

        subject_uri = subject.get("uri", "")
        parsed = _parse_at_uri(subject_uri)
        if parsed is None:
            logger.warning("Invalid subject URI in repost: %s", subject_uri)
            return None

        did, collection, rkey = parsed
        source_id = f"at://{did}/{COLLECTION_POST}/{rkey}"

        # 重複チェック
        if source_id in seen_source_ids:
            return None

        # 元投稿を取得
        original_record = await self._get_record(did, collection, rkey)
        if original_record is None:
            return None

        # 元投稿をポストレコードとして処理
        return await self._process_post_record(
            original_record, handle, seen_source_ids, is_repost=True
        )

    async def _fetch_quote_text(self, value: dict[str, Any]) -> str | None:
        """引用元投稿のテキストを取得する.

        embed.$type が app.bsky.embed.record または app.bsky.embed.recordWithMedia の
        場合に引用元の AT URI を取得し、getRecord で引用元投稿のテキストを取得する。

        Args:
            value: 投稿レコードの value オブジェクト

        Returns:
            引用元テキスト、または引用なし/取得失敗時は None
        """
        embed = value.get("embed")
        if not isinstance(embed, dict):
            return None

        embed_type = embed.get("$type", "")

        quote_uri: str | None = None
        if embed_type == "app.bsky.embed.record":
            record_ref = embed.get("record")
            if isinstance(record_ref, dict):
                quote_uri = record_ref.get("uri")
        elif embed_type == "app.bsky.embed.recordWithMedia":
            record_ref = embed.get("record")
            if isinstance(record_ref, dict):
                inner_record = record_ref.get("record")
                if isinstance(inner_record, dict):
                    quote_uri = inner_record.get("uri")

        if not quote_uri:
            return None

        parsed = _parse_at_uri(quote_uri)
        if parsed is None:
            logger.warning("Invalid quote URI: %s", quote_uri)
            return None

        did, collection, rkey = parsed
        quote_record = await self._get_record(did, collection, rkey)
        if quote_record is None:
            return None

        quote_value = quote_record.get("value")
        if not isinstance(quote_value, dict):
            return None

        return _extract_text_from_post(quote_value) or None

    async def _list_records(
        self,
        repo: str,
        collection: str,
        *,
        max_records: int | None,
    ) -> list[dict[str, Any]]:
        """listRecords API をページネーション走査する.

        Args:
            repo: DID またはハンドル
            collection: レコードコレクション
            max_records: 取得上限（None で無制限、バジェット上限まで）

        Returns:
            レコードオブジェクトのリスト
        """
        records: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            query_params: dict[str, str | int] = {
                "repo": repo,
                "collection": collection,
                "limit": LISTRECORDS_PAGE_SIZE,
            }
            if cursor is not None:
                query_params["cursor"] = cursor

            url = f"{self._pds_url}/xrpc/com.atproto.repo.listRecords?{urlencode(query_params)}"

            try:
                resp = await self._client.get(url)
            except Exception:
                logger.exception(
                    "Failed to fetch %s records for %s", collection, repo
                )
                break

            if resp.status_code != 200:
                logger.error(
                    "listRecords returned status %d for %s (repo: %s)",
                    resp.status_code,
                    collection,
                    repo,
                )
                break

            try:
                data: dict[str, Any] = resp.json()
            except Exception:
                logger.exception(
                    "Failed to parse listRecords JSON for %s (repo: %s)",
                    collection,
                    repo,
                )
                break

            page_records = data.get("records")
            if not isinstance(page_records, list):
                logger.error(
                    "Unexpected records format in listRecords response (repo: %s)",
                    repo,
                )
                break

            if not page_records:
                break

            for rec in page_records:
                if max_records is not None and len(records) >= max_records:
                    break
                if isinstance(rec, dict):
                    records.append(rec)

            if max_records is not None and len(records) >= max_records:
                if len(records) > max_records:
                    records = records[:max_records]
                logger.info(
                    "Reached max_records limit (%d) for %s (repo: %s)",
                    max_records,
                    collection,
                    repo,
                )
                break

            next_cursor = data.get("cursor")
            if next_cursor is None:
                break

            cursor = str(next_cursor)

        logger.info(
            "Listed %d %s records for %s",
            len(records),
            collection,
            repo,
        )
        return records

    async def _get_record(
        self,
        repo: str,
        collection: str,
        rkey: str,
    ) -> dict[str, Any] | None:
        """getRecord API で個別レコードを取得する.

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
        url = f"{self._pds_url}/xrpc/com.atproto.repo.getRecord?{query_params}"

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
            value: レコードの value オブジェクト
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

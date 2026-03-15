"""BlueSky インジェスターのテスト

仕様: docs/specs/bluesky-ingester.md
Issue: #185, #194

テスト方針:
- 単体テスト: BlueskyIngester のメソッド（テキスト抽出、バリデーション、source_id 生成）
- API レスポンスモック: getAuthorFeed・getRecord のレスポンスを fixture として定義
- 正常系: 統一タイムライン取得・テキスト抽出・引用リポスト・リポストフィルタ・ページネーション
- 異常系: 存在しないハンドル、空投稿、DID 形式の拒否、API エラー応答
- バリデーション・クランプ: max_posts の 0/負数/上限超過
- 既存 source_id スキップの動作確認
- テスト時は投稿取得上限 3 件で実行
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.ingesters.bluesky import (
    MAX_POSTS_HARD_LIMIT,
    BlueskyIngester,
    _extract_quote_text_from_view_embed,
    _extract_text_from_post,
    _make_title,
    _parse_at_uri,
    _validate_appview_url,
    _validate_max_posts,
)


# --- ヘルパー ---


def _make_mock_client() -> MagicMock:
    """モック ConstrainedClient を作成する."""
    mock = MagicMock()
    mock.get = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


def _make_feed_response(
    feed: list[dict],
    cursor: str | None = None,
) -> MagicMock:
    """getAuthorFeed API のモックレスポンスを作成する."""
    resp = MagicMock()
    resp.status_code = 200
    data: dict = {"feed": feed}
    if cursor is not None:
        data["cursor"] = cursor
    resp.json.return_value = data
    return resp


def _make_get_record_response(
    uri: str = "at://did:plc:test123/app.bsky.feed.post/abc123",
    value: dict | None = None,
) -> MagicMock:
    """getRecord API のモックレスポンスを作成する."""
    resp = MagicMock()
    resp.status_code = 200
    if value is None:
        value = {"text": "Test post", "createdAt": "2024-01-01T00:00:00Z"}
    resp.json.return_value = {
        "uri": uri,
        "cid": "bafytest",
        "value": value,
    }
    return resp


def _make_feed_item(
    did: str = "did:plc:test123",
    handle: str = "user.bsky.social",
    rkey: str = "abc123",
    text: str = "Hello BlueSky!",
    embed: dict | None = None,
    view_embed: dict | None = None,
    reply: dict | None = None,
    created_at: str = "2024-01-01T00:00:00Z",
    reason: dict | None = None,
) -> dict:
    """フィードアイテムオブジェクトを作成する."""
    record: dict = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": created_at,
    }
    if embed is not None:
        record["embed"] = embed
    if reply is not None:
        record["reply"] = reply

    post: dict = {
        "uri": f"at://{did}/app.bsky.feed.post/{rkey}",
        "cid": "bafytest",
        "author": {
            "did": did,
            "handle": handle,
        },
        "record": record,
    }
    if view_embed is not None:
        post["embed"] = view_embed

    item: dict = {"post": post}
    if reason is not None:
        item["reason"] = reason

    return item


def _make_repost_reason(
    by_did: str = "did:plc:reposter",
    by_handle: str = "reposter.bsky.social",
) -> dict:
    """リポスト理由オブジェクトを作成する."""
    return {
        "$type": "app.bsky.feed.defs#reasonRepost",
        "by": {
            "did": by_did,
            "handle": by_handle,
        },
        "indexedAt": "2024-01-02T00:00:00Z",
    }


def _make_error_response(status_code: int = 404) -> MagicMock:
    """エラーレスポンスを作成する."""
    resp = MagicMock()
    resp.status_code = status_code
    return resp


# --- _validate_max_posts テスト ---


class TestValidateMaxPosts:
    """max_posts のバリデーション・クランプテスト."""

    def test_valid_value(self) -> None:
        """許容範囲内の値がそのまま返ること."""
        assert _validate_max_posts(200) == 200
        assert _validate_max_posts(1) == 1
        assert _validate_max_posts(1000) == 1000

    def test_clamp_exceeds_hard_limit(self) -> None:
        """ハードリミット超過の正の整数がクランプされること."""
        assert _validate_max_posts(2000) == MAX_POSTS_HARD_LIMIT
        assert _validate_max_posts(1001) == MAX_POSTS_HARD_LIMIT
        assert _validate_max_posts(9999) == MAX_POSTS_HARD_LIMIT

    def test_reject_zero(self) -> None:
        """0 がバリデーションエラーになること."""
        with pytest.raises(ValueError, match="positive integer"):
            _validate_max_posts(0)

    def test_reject_negative(self) -> None:
        """負数がバリデーションエラーになること."""
        with pytest.raises(ValueError, match="positive integer"):
            _validate_max_posts(-1)
        with pytest.raises(ValueError, match="positive integer"):
            _validate_max_posts(-100)

    def test_reject_non_integer(self) -> None:
        """非整数がバリデーションエラーになること."""
        with pytest.raises(TypeError, match="must be an integer"):
            _validate_max_posts(50.5)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="must be an integer"):
            _validate_max_posts("50")  # type: ignore[arg-type]

    def test_reject_bool(self) -> None:
        """bool がバリデーションエラーになること（bool は int のサブクラス）."""
        with pytest.raises(TypeError, match="must be an integer"):
            _validate_max_posts(True)  # type: ignore[arg-type]


# --- _parse_at_uri テスト ---


class TestParseAtUri:
    """AT URI パーサーのテスト."""

    def test_valid_uri(self) -> None:
        """正しい AT URI がパースされること."""
        result = _parse_at_uri("at://did:plc:test123/app.bsky.feed.post/abc123")
        assert result is not None
        assert result == ("did:plc:test123", "app.bsky.feed.post", "abc123")

    def test_valid_uri_with_web_did(self) -> None:
        """did:web 形式の AT URI がパースされること."""
        result = _parse_at_uri("at://did:web:example.com/app.bsky.feed.post/xyz")
        assert result is not None
        assert result[0] == "did:web:example.com"

    def test_invalid_uri(self) -> None:
        """不正な AT URI で None が返ること."""
        assert _parse_at_uri("https://example.com") is None
        assert _parse_at_uri("at://invalid") is None
        assert _parse_at_uri("") is None


# --- _extract_text_from_post テスト ---


class TestExtractTextFromPost:
    """テキスト抽出のテスト."""

    def test_text_only(self) -> None:
        """投稿テキストのみ抽出されること."""
        value = {"text": "Hello World"}
        assert _extract_text_from_post(value) == "Hello World"

    def test_empty_text(self) -> None:
        """空テキストが空文字列として返ること."""
        value = {"text": ""}
        assert _extract_text_from_post(value) == ""

    def test_image_alt_text(self) -> None:
        """画像 ALT テキストが [Image ALT] プレフィックス付きで抽出されること."""
        value = {
            "text": "Photo post",
            "embed": {
                "$type": "app.bsky.embed.images",
                "images": [
                    {"alt": "A beautiful sunset"},
                    {"alt": "Mountain view"},
                ],
            },
        }
        result = _extract_text_from_post(value)
        assert result.startswith("Photo post")
        assert "[Image ALT] " in result
        assert "A beautiful sunset" in result
        assert "Mountain view" in result
        # セクション間は空行区切り
        assert "\n\n" in result

    def test_image_empty_alt(self) -> None:
        """空の ALT テキストは無視されること."""
        value = {
            "text": "Photo",
            "embed": {
                "$type": "app.bsky.embed.images",
                "images": [{"alt": ""}, {"alt": "Good alt"}],
            },
        }
        result = _extract_text_from_post(value)
        assert "Good alt" in result

    def test_video_alt_text(self) -> None:
        """動画 ALT テキストが [Video ALT] プレフィックス付きで抽出されること."""
        value = {
            "text": "Video post",
            "embed": {
                "$type": "app.bsky.embed.video",
                "alt": "Video description",
            },
        }
        result = _extract_text_from_post(value)
        assert result.startswith("Video post")
        assert "[Video ALT] Video description" in result

    def test_external_link(self) -> None:
        """リンクカードが [Link Card] セクション構造で抽出されること."""
        value = {
            "text": "Check this out",
            "embed": {
                "$type": "app.bsky.embed.external",
                "external": {
                    "title": "Example Article",
                    "description": "Article description",
                    "uri": "https://example.com",
                },
            },
        }
        result = _extract_text_from_post(value)
        assert result.startswith("Check this out")
        assert "[Link Card]" in result
        assert "Title: Example Article" in result
        assert "URL: https://example.com" in result
        assert "Description: Article description" in result

    def test_record_with_media_images(self) -> None:
        """recordWithMedia 型の画像 ALT が [Image ALT] プレフィックス付きで抽出されること."""
        value = {
            "text": "Quote with images",
            "embed": {
                "$type": "app.bsky.embed.recordWithMedia",
                "record": {
                    "record": {
                        "uri": "at://did:plc:other/app.bsky.feed.post/xyz",
                        "cid": "bafyother",
                    },
                },
                "media": {
                    "$type": "app.bsky.embed.images",
                    "images": [{"alt": "Media image alt"}],
                },
            },
        }
        result = _extract_text_from_post(value)
        assert result.startswith("Quote with images")
        assert "[Image ALT] Media image alt" in result

    def test_record_with_media_external(self) -> None:
        """recordWithMedia 型の media.external が [Link Card] セクションで抽出されること."""
        value = {
            "text": "Quote with link",
            "embed": {
                "$type": "app.bsky.embed.recordWithMedia",
                "record": {
                    "record": {
                        "uri": "at://did:plc:other/app.bsky.feed.post/xyz",
                        "cid": "bafyother",
                    },
                },
                "media": {
                    "$type": "app.bsky.embed.external",
                    "external": {
                        "title": "Link Title",
                        "description": "Link Desc",
                        "uri": "https://example.com",
                    },
                },
            },
        }
        result = _extract_text_from_post(value)
        assert result.startswith("Quote with link")
        assert "[Link Card]" in result
        assert "Title: Link Title" in result
        assert "URL: https://example.com" in result
        assert "Description: Link Desc" in result

    def test_no_embed(self) -> None:
        """embed なしでテキストのみ返ること."""
        value = {"text": "Just text"}
        assert _extract_text_from_post(value) == "Just text"


# --- _extract_quote_text_from_view_embed テスト ---


class TestExtractQuoteTextFromViewEmbed:
    """view embed からの引用元テキスト抽出テスト."""

    def test_record_view_with_text(self) -> None:
        """record#view から引用元テキストが取得できること."""
        view_embed = {
            "$type": "app.bsky.embed.record#view",
            "record": {
                "$type": "app.bsky.embed.record#viewRecord",
                "uri": "at://did:plc:other/app.bsky.feed.post/xyz",
                "value": {
                    "text": "Original quoted post",
                    "createdAt": "2024-01-01T00:00:00Z",
                },
            },
        }
        assert _extract_quote_text_from_view_embed(view_embed) == "Original quoted post"

    def test_record_with_media_view_with_text(self) -> None:
        """recordWithMedia#view から引用元テキストが取得できること."""
        view_embed = {
            "$type": "app.bsky.embed.recordWithMedia#view",
            "record": {
                "record": {
                    "$type": "app.bsky.embed.record#viewRecord",
                    "uri": "at://did:plc:other/app.bsky.feed.post/xyz",
                    "value": {
                        "text": "Quoted with media",
                        "createdAt": "2024-01-01T00:00:00Z",
                    },
                },
            },
            "media": {
                "$type": "app.bsky.embed.images#view",
                "images": [],
            },
        }
        assert _extract_quote_text_from_view_embed(view_embed) == "Quoted with media"

    def test_non_post_quote_returns_none(self) -> None:
        """投稿以外の引用（スターターパック等）で None が返ること."""
        view_embed = {
            "$type": "app.bsky.embed.record#view",
            "record": {
                "$type": "app.bsky.feed.defs#generatorView",
                "uri": "at://did:plc:other/app.bsky.feed.generator/xyz",
                "creator": {},
            },
        }
        assert _extract_quote_text_from_view_embed(view_embed) is None

    def test_no_embed_returns_none(self) -> None:
        """embed なしで None が返ること."""
        assert _extract_quote_text_from_view_embed(None) is None

    def test_non_quote_embed_returns_none(self) -> None:
        """引用以外の embed で None が返ること."""
        view_embed = {
            "$type": "app.bsky.embed.images#view",
            "images": [],
        }
        assert _extract_quote_text_from_view_embed(view_embed) is None

    def test_empty_quote_text_returns_none(self) -> None:
        """引用元テキストが空で None が返ること."""
        view_embed = {
            "$type": "app.bsky.embed.record#view",
            "record": {
                "value": {
                    "text": "",
                    "createdAt": "2024-01-01T00:00:00Z",
                },
            },
        }
        assert _extract_quote_text_from_view_embed(view_embed) is None


# --- _make_title テスト ---


class TestMakeTitle:
    """タイトル生成のテスト."""

    def test_short_text(self) -> None:
        """50 文字以下のテキストがそのまま返ること."""
        assert _make_title("Hello") == "Hello"

    def test_long_text(self) -> None:
        """50 文字超のテキストが切り詰められること."""
        text = "A" * 100
        result = _make_title(text)
        assert len(result) == 53  # 50 + "..."
        assert result.endswith("...")

    def test_multiline_text(self) -> None:
        """改行が空白に置換されること."""
        text = "Line 1\nLine 2\nLine 3"
        assert _make_title(text) == "Line 1 Line 2 Line 3"

    def test_exact_50_chars(self) -> None:
        """ちょうど 50 文字でも「...」が付かないこと."""
        text = "A" * 50
        assert _make_title(text) == text


# --- BlueskyIngester.validate_identifier テスト ---


class TestBlueskyIngesterValidate:
    """BlueskyIngester.validate_identifier のテスト."""

    @pytest.fixture()
    def ingester(self) -> BlueskyIngester:
        return BlueskyIngester(client=_make_mock_client(), max_posts=3)

    def test_valid_at_uri(self, ingester: BlueskyIngester) -> None:
        """正しい AT URI がそのまま返ること."""
        uri = "at://did:plc:test123/app.bsky.feed.post/abc123"
        assert ingester.validate_identifier(uri) == uri

    def test_at_uri_with_whitespace(self, ingester: BlueskyIngester) -> None:
        """前後の空白がトリムされること."""
        uri = "  at://did:plc:test123/app.bsky.feed.post/abc123  "
        assert ingester.validate_identifier(uri) == uri.strip()

    def test_empty_identifier(self, ingester: BlueskyIngester) -> None:
        """空文字列がバリデーションエラーになること."""
        with pytest.raises(ValueError, match="must not be empty"):
            ingester.validate_identifier("")
        with pytest.raises(ValueError, match="must not be empty"):
            ingester.validate_identifier("   ")

    def test_invalid_at_uri(self, ingester: BlueskyIngester) -> None:
        """不正な AT URI がバリデーションエラーになること."""
        with pytest.raises(ValueError, match="Invalid AT URI format"):
            ingester.validate_identifier("https://example.com")
        with pytest.raises(ValueError, match="Invalid AT URI format"):
            ingester.validate_identifier("at://invalid")


# --- BlueskyIngester._validate_handle テスト ---


class TestBlueskyIngesterValidateHandle:
    """BlueskyIngester._validate_handle のテスト."""

    def test_valid_handle(self) -> None:
        """正しいハンドルがそのまま返ること."""
        assert BlueskyIngester._validate_handle("user.bsky.social") == "user.bsky.social"

    def test_handle_with_whitespace(self) -> None:
        """前後の空白がトリムされること."""
        assert BlueskyIngester._validate_handle("  user.bsky.social  ") == "user.bsky.social"

    def test_empty_handle(self) -> None:
        """空文字列がバリデーションエラーになること."""
        with pytest.raises(ValueError, match="must not be empty"):
            BlueskyIngester._validate_handle("")
        with pytest.raises(ValueError, match="must not be empty"):
            BlueskyIngester._validate_handle("   ")

    def test_did_format_rejected(self) -> None:
        """DID 形式がバリデーションエラーになること."""
        with pytest.raises(ValueError, match="DID format is not accepted"):
            BlueskyIngester._validate_handle("did:plc:test123")


# --- BlueskyIngester.fetch_single テスト ---


class TestBlueskyIngesterFetchSingle:
    """BlueskyIngester.fetch_single のテスト."""

    @pytest.fixture()
    def mock_client(self) -> MagicMock:
        return _make_mock_client()

    @pytest.fixture()
    def ingester(self, mock_client: MagicMock) -> BlueskyIngester:
        return BlueskyIngester(client=mock_client, max_posts=3)

    async def test_fetch_single_success(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """正常に投稿を取得して IngestedContent を返すこと."""
        mock_client.get.return_value = _make_get_record_response(
            uri="at://did:plc:test123/app.bsky.feed.post/abc123",
            value={
                "text": "Hello BlueSky!",
                "createdAt": "2024-01-01T00:00:00Z",
            },
        )

        result = await ingester.fetch_single(
            "at://did:plc:test123/app.bsky.feed.post/abc123"
        )

        assert result is not None
        assert result.source_id == "at://did:plc:test123/app.bsky.feed.post/abc123"
        assert result.title == "Hello BlueSky!"
        assert result.text == "Hello BlueSky!"
        assert result.source_type == "bluesky"
        assert result.metadata["did"] == "did:plc:test123"
        assert result.metadata["rkey"] == "abc123"
        assert result.metadata["handle"] == "did:plc:test123"
        assert result.metadata["url"] == "https://bsky.app/profile/did:plc:test123/post/abc123"
        assert result.metadata["is_repost"] is False
        assert result.skip_chunking is True

    async def test_fetch_single_404(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """404 エラーで None を返すこと."""
        mock_client.get.return_value = _make_error_response(404)

        result = await ingester.fetch_single(
            "at://did:plc:test123/app.bsky.feed.post/abc123"
        )

        assert result is None

    async def test_fetch_single_empty_text(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """空テキスト投稿で None を返すこと."""
        mock_client.get.return_value = _make_get_record_response(
            value={"text": "", "createdAt": "2024-01-01T00:00:00Z"},
        )

        result = await ingester.fetch_single(
            "at://did:plc:test123/app.bsky.feed.post/abc123"
        )

        assert result is None

    async def test_fetch_single_request_exception(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """リクエスト例外で None を返すこと."""
        mock_client.get.side_effect = Exception("Connection error")

        result = await ingester.fetch_single(
            "at://did:plc:test123/app.bsky.feed.post/abc123"
        )

        assert result is None

    async def test_fetch_single_invalid_uri(
        self, ingester: BlueskyIngester
    ) -> None:
        """不正な AT URI でバリデーションエラーになること."""
        with pytest.raises(ValueError, match="must not be empty"):
            await ingester.fetch_single("")


# --- BlueskyIngester.crawl テスト ---


class TestBlueskyIngesterCrawl:
    """BlueskyIngester.crawl のテスト."""

    @pytest.fixture()
    def mock_client(self) -> MagicMock:
        return _make_mock_client()

    @pytest.fixture()
    def ingester(self, mock_client: MagicMock) -> BlueskyIngester:
        return BlueskyIngester(client=mock_client, max_posts=3)

    async def test_crawl_basic(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """基本的な投稿取得が正常に動作すること."""
        mock_client.get.return_value = _make_feed_response(
            feed=[
                _make_feed_item(rkey="post1", text="First post"),
                _make_feed_item(rkey="post2", text="Second post"),
            ],
            cursor=None,
        )

        contents = await ingester.crawl("user.bsky.social")

        assert len(contents) == 2
        assert contents[0].text == "First post"
        assert contents[1].text == "Second post"

    async def test_crawl_respects_max_posts(
        self, mock_client: MagicMock
    ) -> None:
        """max_posts を超える投稿が返らないこと."""
        ingester = BlueskyIngester(client=mock_client, max_posts=2)
        mock_client.get.return_value = _make_feed_response(
            feed=[
                _make_feed_item(rkey="post1", text="Post 1"),
                _make_feed_item(rkey="post2", text="Post 2"),
                _make_feed_item(rkey="post3", text="Post 3"),
            ],
            cursor=None,
        )

        contents = await ingester.crawl("user.bsky.social")

        assert len(contents) == 2

    async def test_crawl_pagination(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """ページネーションが正常に動作すること."""
        mock_client.get.side_effect = [
            _make_feed_response(
                feed=[
                    _make_feed_item(rkey="post1", text="Page 1 Post 1"),
                    _make_feed_item(rkey="post2", text="Page 1 Post 2"),
                ],
                cursor="cursor_2",
            ),
            _make_feed_response(
                feed=[
                    _make_feed_item(rkey="post3", text="Page 2 Post 1"),
                ],
                cursor=None,
            ),
        ]

        contents = await ingester.crawl("user.bsky.social")

        assert len(contents) == 3
        assert mock_client.get.call_count == 2

    async def test_crawl_empty_posts(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """投稿が 0 件の場合、空リストを返すこと."""
        mock_client.get.return_value = _make_feed_response(
            feed=[], cursor=None
        )

        contents = await ingester.crawl("user.bsky.social")

        assert contents == []

    async def test_crawl_api_error(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """API エラー時に空リストを返すこと."""
        mock_client.get.return_value = _make_error_response(500)

        contents = await ingester.crawl("user.bsky.social")

        assert contents == []

    async def test_crawl_empty_handle(
        self, ingester: BlueskyIngester
    ) -> None:
        """空のハンドルでバリデーションエラーになること."""
        with pytest.raises(ValueError, match="must not be empty"):
            await ingester.crawl("")

    async def test_crawl_did_handle_rejected(
        self, ingester: BlueskyIngester
    ) -> None:
        """DID 形式のハンドルでバリデーションエラーになること."""
        with pytest.raises(ValueError, match="DID format is not accepted"):
            await ingester.crawl("did:plc:test123")

    async def test_crawl_with_quote_from_view_embed(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """引用リポスト投稿の引用元テキストが view embed から取得されること."""
        mock_client.get.return_value = _make_feed_response(
            feed=[
                _make_feed_item(
                    rkey="quote1",
                    text="My comment on this",
                    embed={
                        "$type": "app.bsky.embed.record",
                        "record": {
                            "uri": "at://did:plc:other/app.bsky.feed.post/original1",
                            "cid": "bafyother",
                        },
                    },
                    view_embed={
                        "$type": "app.bsky.embed.record#view",
                        "record": {
                            "$type": "app.bsky.embed.record#viewRecord",
                            "uri": "at://did:plc:other/app.bsky.feed.post/original1",
                            "value": {
                                "text": "Original post text",
                                "createdAt": "2024-01-01T00:00:00Z",
                            },
                        },
                    },
                ),
            ],
            cursor=None,
        )

        contents = await ingester.crawl("user.bsky.social")

        assert len(contents) == 1
        assert "My comment on this" in contents[0].text
        assert "[Quote]" in contents[0].text
        assert "Original post text" in contents[0].text
        # getRecord は呼ばれない（view embed から取得）
        assert mock_client.get.call_count == 1

    async def test_crawl_quote_non_post(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """引用元が投稿以外の場合、引用元テキストなしで取り込まれること."""
        mock_client.get.return_value = _make_feed_response(
            feed=[
                _make_feed_item(
                    rkey="quote1",
                    text="Check this feed",
                    embed={
                        "$type": "app.bsky.embed.record",
                        "record": {
                            "uri": "at://did:plc:other/app.bsky.feed.generator/xyz",
                            "cid": "bafyother",
                        },
                    },
                    view_embed={
                        "$type": "app.bsky.embed.record#view",
                        "record": {
                            "$type": "app.bsky.feed.defs#generatorView",
                            "uri": "at://did:plc:other/app.bsky.feed.generator/xyz",
                            "creator": {},
                        },
                    },
                ),
            ],
            cursor=None,
        )

        contents = await ingester.crawl("user.bsky.social")

        assert len(contents) == 1
        assert contents[0].text == "Check this feed"
        assert "[Quote]" not in contents[0].text

    async def test_crawl_repost_included(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """include_reposts=True でリポストが含まれること."""
        mock_client.get.return_value = _make_feed_response(
            feed=[
                _make_feed_item(rkey="post1", text="My post"),
                _make_feed_item(
                    did="did:plc:other",
                    handle="other.bsky.social",
                    rkey="reposted1",
                    text="Reposted content",
                    reason=_make_repost_reason(),
                ),
            ],
            cursor=None,
        )

        contents = await ingester.crawl(
            "user.bsky.social", include_reposts=True
        )

        assert len(contents) == 2
        texts = [c.text for c in contents]
        assert "My post" in texts
        # リポストの [Repost: @handle] ヘッダー
        repost_content = next(c for c in contents if "Reposted content" in c.text)
        assert repost_content.text.startswith("[Repost: @other.bsky.social]")
        assert "Reposted content" in repost_content.text
        assert repost_content.metadata["is_repost"] is True
        # リポストの handle・url は元投稿者のもの
        assert repost_content.metadata["handle"] == "other.bsky.social"
        assert repost_content.metadata["url"] == "https://bsky.app/profile/other.bsky.social/post/reposted1"

    async def test_crawl_repost_filtered_when_excluded(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """include_reposts=False でリポストが除外されること."""
        mock_client.get.return_value = _make_feed_response(
            feed=[
                _make_feed_item(rkey="post1", text="My post"),
                _make_feed_item(
                    did="did:plc:other",
                    rkey="reposted1",
                    text="Reposted content",
                    reason=_make_repost_reason(),
                ),
            ],
            cursor=None,
        )

        contents = await ingester.crawl("user.bsky.social", include_reposts=False)

        assert len(contents) == 1
        assert contents[0].text == "My post"

    async def test_crawl_duplicate_source_id_skipped(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """同一 source_id の重複がスキップされること."""
        mock_client.get.return_value = _make_feed_response(
            feed=[
                _make_feed_item(
                    did="did:plc:test123", rkey="post1", text="First"
                ),
                _make_feed_item(
                    did="did:plc:test123", rkey="post1", text="Duplicate"
                ),
            ],
            cursor=None,
        )

        contents = await ingester.crawl("user.bsky.social")

        assert len(contents) == 1

    async def test_crawl_skips_empty_text_posts(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """テキストが空の投稿がスキップされること."""
        mock_client.get.return_value = _make_feed_response(
            feed=[
                _make_feed_item(rkey="post1", text=""),
                _make_feed_item(rkey="post2", text="Valid post"),
            ],
            cursor=None,
        )

        contents = await ingester.crawl("user.bsky.social")

        assert len(contents) == 1
        assert contents[0].text == "Valid post"

    async def test_crawl_metadata_fields(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """メタデータフィールドが正しく設定されること."""
        mock_client.get.return_value = _make_feed_response(
            feed=[
                _make_feed_item(
                    rkey="post1",
                    text="Test metadata",
                    created_at="2024-06-15T12:00:00Z",
                    reply={"parent": {"uri": "at://did:plc:other/app.bsky.feed.post/parent"}},
                    embed={
                        "$type": "app.bsky.embed.images",
                        "images": [{"alt": "test image"}],
                    },
                ),
            ],
            cursor=None,
        )

        contents = await ingester.crawl("user.bsky.social")

        assert len(contents) == 1
        meta = contents[0].metadata
        assert meta["handle"] == "user.bsky.social"
        assert meta["did"] == "did:plc:test123"
        assert meta["rkey"] == "post1"
        assert meta["url"] == "https://bsky.app/profile/user.bsky.social/post/post1"
        assert meta["createdAt"] == "2024-06-15T12:00:00Z"
        assert meta["has_images"] is True
        assert meta["is_reply"] is True
        assert meta["is_repost"] is False
        assert contents[0].skip_chunking is True

    async def test_crawl_source_id_format(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """source_id が AT URI 形式であること."""
        mock_client.get.return_value = _make_feed_response(
            feed=[
                _make_feed_item(
                    did="did:plc:abc123", rkey="post1", text="Test"
                ),
            ],
            cursor=None,
        )

        contents = await ingester.crawl("user.bsky.social")

        assert len(contents) == 1
        assert contents[0].source_id == "at://did:plc:abc123/app.bsky.feed.post/post1"

    async def test_crawl_request_exception(
        self, ingester: BlueskyIngester, mock_client: MagicMock
    ) -> None:
        """リクエスト例外時に空リストを返すこと."""
        mock_client.get.side_effect = Exception("Connection error")

        contents = await ingester.crawl("user.bsky.social")

        assert contents == []


# --- BlueskyIngester コンストラクタテスト ---


class TestBlueskyIngesterInit:
    """BlueskyIngester コンストラクタのテスト."""

    def test_default_max_posts(self) -> None:
        """デフォルト max_posts が 200 であること."""
        ingester = BlueskyIngester(client=_make_mock_client())
        assert ingester._max_posts == 200

    def test_custom_max_posts(self) -> None:
        """カスタム max_posts が設定できること."""
        ingester = BlueskyIngester(client=_make_mock_client(), max_posts=10)
        assert ingester._max_posts == 10

    def test_clamp_max_posts(self) -> None:
        """ハードリミット超過時にクランプされること."""
        ingester = BlueskyIngester(client=_make_mock_client(), max_posts=2000)
        assert ingester._max_posts == MAX_POSTS_HARD_LIMIT

    def test_reject_zero_max_posts(self) -> None:
        """max_posts=0 でバリデーションエラーになること."""
        with pytest.raises(ValueError):
            BlueskyIngester(client=_make_mock_client(), max_posts=0)

    def test_reject_negative_max_posts(self) -> None:
        """負の max_posts でバリデーションエラーになること."""
        with pytest.raises(ValueError):
            BlueskyIngester(client=_make_mock_client(), max_posts=-5)

    def test_custom_appview_url(self) -> None:
        """カスタム AppView URL が設定できること."""
        ingester = BlueskyIngester(
            client=_make_mock_client(),
            appview_url="https://custom.appview.example.com",
        )
        assert ingester._appview_url == "https://custom.appview.example.com"

    def test_appview_url_trailing_slash_stripped(self) -> None:
        """AppView URL の末尾スラッシュが除去されること."""
        ingester = BlueskyIngester(
            client=_make_mock_client(),
            appview_url="https://public.api.bsky.app/",
        )
        assert ingester._appview_url == "https://public.api.bsky.app"

    def test_appview_url_whitespace_stripped(self) -> None:
        """AppView URL の前後空白がトリムされること."""
        ingester = BlueskyIngester(
            client=_make_mock_client(),
            appview_url="  https://public.api.bsky.app  ",
        )
        assert ingester._appview_url == "https://public.api.bsky.app"

    def test_reject_empty_appview_url(self) -> None:
        """空の AppView URL でバリデーションエラーになること."""
        with pytest.raises(ValueError, match="must not be empty"):
            BlueskyIngester(client=_make_mock_client(), appview_url="")
        with pytest.raises(ValueError, match="must not be empty"):
            BlueskyIngester(client=_make_mock_client(), appview_url="   ")

    def test_reject_non_https_appview_url(self) -> None:
        """非 HTTPS の AppView URL でバリデーションエラーになること."""
        with pytest.raises(ValueError, match="HTTPS"):
            BlueskyIngester(client=_make_mock_client(), appview_url="http://public.api.bsky.app")


# --- _validate_appview_url テスト ---


class TestValidateAppviewUrl:
    """AppView URL のバリデーションテスト."""

    def test_valid_https_url(self) -> None:
        """正しい HTTPS URL がそのまま返ること."""
        assert _validate_appview_url("https://public.api.bsky.app") == "https://public.api.bsky.app"

    def test_trailing_slash_stripped(self) -> None:
        """末尾スラッシュが除去されること."""
        assert _validate_appview_url("https://public.api.bsky.app/") == "https://public.api.bsky.app"

    def test_whitespace_stripped(self) -> None:
        """前後の空白がトリムされること."""
        assert _validate_appview_url("  https://public.api.bsky.app  ") == "https://public.api.bsky.app"

    def test_reject_empty(self) -> None:
        """空文字列がバリデーションエラーになること."""
        with pytest.raises(ValueError, match="must not be empty"):
            _validate_appview_url("")
        with pytest.raises(ValueError, match="must not be empty"):
            _validate_appview_url("   ")

    def test_reject_http(self) -> None:
        """HTTP スキームがバリデーションエラーになること."""
        with pytest.raises(ValueError, match="HTTPS"):
            _validate_appview_url("http://public.api.bsky.app")

    def test_reject_no_scheme(self) -> None:
        """スキームなしがバリデーションエラーになること."""
        with pytest.raises(ValueError, match="HTTPS"):
            _validate_appview_url("public.api.bsky.app")


# --- ハードリミット定数テスト ---


class TestHardLimits:
    """ハードリミット定数の値テスト."""

    def test_max_posts_hard_limit(self) -> None:
        """投稿取得上限が仕様通りであること."""
        assert MAX_POSTS_HARD_LIMIT == 1000

"""Zenn インジェスターのテスト

仕様: docs/specs/zenn-ingester.md
Issue: #162

テスト方針:
- 単体テスト: ZennIngester の discover / fetch_single / validate_identifier
- Zenn API レスポンスはモックを使用（実 API を叩かない）
- ページネーション走査のエッジケース（0件、1ページ、上限到達）
- 記事詳細取得のエラーハンドリング（404、空 body_html）
- バリデーションテスト: max_articles の異常値（0、負数、上限超過、型不正）
- クランプテスト: 許容範囲外の正の整数がクランプされること
- テスト安全値: 走査上限 1 ページ、記事取得上限 3 件
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.ingesters.zenn import (
    MAX_ARTICLES_HARD_LIMIT,
    MAX_PAGINATION_PAGES,
    ZennIngester,
    _validate_max_articles,
)


# --- ヘルパー ---


def _make_mock_client() -> MagicMock:
    """モック ConstrainedClient を作成する."""
    mock = MagicMock()
    mock.get = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


def _make_article_list_response(
    articles: list[dict],
    next_page: int | None = None,
) -> MagicMock:
    """記事一覧 API のモックレスポンスを作成する."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "articles": articles,
        "next_page": next_page,
    }
    return resp


def _make_article_detail_response(
    slug: str = "test-article",
    title: str = "Test Article",
    body_html: str = "<p>Test content</p>",
    path: str = "/testuser/articles/test-article",
    article_type: str = "tech",
    published_at: str = "2024-01-01T00:00:00+09:00",
    liked_count: int = 10,
    topics: list | None = None,
) -> MagicMock:
    """記事詳細 API のモックレスポンスを作成する."""
    resp = MagicMock()
    resp.status_code = 200
    data = {
        "article": {
            "slug": slug,
            "title": title,
            "body_html": body_html,
            "path": path,
            "article_type": article_type,
            "published_at": published_at,
            "liked_count": liked_count,
            "topics": topics or [],
            "user": {"username": "testuser"},
        },
    }
    resp.json.return_value = data
    return resp


def _make_error_response(status_code: int = 404) -> MagicMock:
    """エラーレスポンスを作成する."""
    resp = MagicMock()
    resp.status_code = status_code
    return resp


# --- _validate_max_articles テスト ---


class TestValidateMaxArticles:
    """max_articles のバリデーション・クランプテスト."""

    def test_valid_value(self) -> None:
        """許容範囲内の値がそのまま返ること."""
        assert _validate_max_articles(50) == 50
        assert _validate_max_articles(1) == 1
        assert _validate_max_articles(100) == 100

    def test_clamp_exceeds_hard_limit(self) -> None:
        """ハードリミット超過の正の整数がクランプされること."""
        assert _validate_max_articles(200) == MAX_ARTICLES_HARD_LIMIT
        assert _validate_max_articles(101) == MAX_ARTICLES_HARD_LIMIT
        assert _validate_max_articles(999) == MAX_ARTICLES_HARD_LIMIT

    def test_reject_zero(self) -> None:
        """0 がバリデーションエラーになること."""
        with pytest.raises(ValueError, match="positive integer"):
            _validate_max_articles(0)

    def test_reject_negative(self) -> None:
        """負数がバリデーションエラーになること."""
        with pytest.raises(ValueError, match="positive integer"):
            _validate_max_articles(-1)
        with pytest.raises(ValueError, match="positive integer"):
            _validate_max_articles(-100)

    def test_reject_non_integer(self) -> None:
        """非整数がバリデーションエラーになること."""
        with pytest.raises(TypeError, match="must be an integer"):
            _validate_max_articles(50.5)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="must be an integer"):
            _validate_max_articles("50")  # type: ignore[arg-type]

    def test_reject_bool(self) -> None:
        """bool がバリデーションエラーになること（bool は int のサブクラス）."""
        with pytest.raises(TypeError, match="must be an integer"):
            _validate_max_articles(True)  # type: ignore[arg-type]


# --- ZennIngester.validate_identifier テスト ---


class TestZennIngesterValidate:
    """ZennIngester.validate_identifier のテスト."""

    @pytest.fixture()
    def ingester(self) -> ZennIngester:
        return ZennIngester(client=_make_mock_client(), max_articles=3)

    def test_valid_slug(self, ingester: ZennIngester) -> None:
        """正しい slug がそのまま返ること."""
        assert ingester.validate_identifier("test-article") == "test-article"
        assert ingester.validate_identifier("my_article_123") == "my_article_123"
        assert ingester.validate_identifier("a") == "a"

    def test_slug_with_whitespace(self, ingester: ZennIngester) -> None:
        """前後の空白がトリムされること."""
        assert ingester.validate_identifier("  test-article  ") == "test-article"

    def test_empty_slug(self, ingester: ZennIngester) -> None:
        """空文字列がバリデーションエラーになること."""
        with pytest.raises(ValueError, match="must not be empty"):
            ingester.validate_identifier("")
        with pytest.raises(ValueError, match="must not be empty"):
            ingester.validate_identifier("   ")

    def test_invalid_slug_format(self, ingester: ZennIngester) -> None:
        """不正な slug フォーマットがバリデーションエラーになること."""
        with pytest.raises(ValueError, match="Invalid slug format"):
            ingester.validate_identifier("Test-Article")  # 大文字
        with pytest.raises(ValueError, match="Invalid slug format"):
            ingester.validate_identifier("-invalid")  # ハイフン始まり
        with pytest.raises(ValueError, match="Invalid slug format"):
            ingester.validate_identifier("invalid slug")  # スペース含む


# --- ZennIngester.discover テスト ---


class TestZennIngesterDiscover:
    """ZennIngester.discover のテスト."""

    @pytest.fixture()
    def mock_client(self) -> MagicMock:
        return _make_mock_client()

    @pytest.fixture()
    def ingester(self, mock_client: MagicMock) -> ZennIngester:
        return ZennIngester(client=mock_client, max_articles=3)

    async def test_discover_single_page(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """1ページの記事一覧を正常に取得できること."""
        mock_client.get.return_value = _make_article_list_response(
            articles=[
                {"slug": "article-1"},
                {"slug": "article-2"},
            ],
            next_page=None,
        )

        slugs = await ingester.discover("testuser")

        assert slugs == ["article-1", "article-2"]
        mock_client.get.assert_called_once()

    async def test_discover_pagination(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """複数ページのページネーションが正常に動作すること."""
        # max_articles=3 なので3件目で停止する
        mock_client.get.side_effect = [
            _make_article_list_response(
                articles=[{"slug": "article-1"}, {"slug": "article-2"}],
                next_page=2,
            ),
            _make_article_list_response(
                articles=[{"slug": "article-3"}, {"slug": "article-4"}],
                next_page=None,
            ),
        ]

        slugs = await ingester.discover("testuser")

        assert slugs == ["article-1", "article-2", "article-3"]
        assert mock_client.get.call_count == 2

    async def test_discover_empty_articles(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """記事が0件の場合、空リストを返すこと."""
        mock_client.get.return_value = _make_article_list_response(
            articles=[],
            next_page=None,
        )

        slugs = await ingester.discover("testuser")

        assert slugs == []

    async def test_discover_api_error(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """API エラー時は取得済みデータを返すこと."""
        mock_client.get.side_effect = [
            _make_article_list_response(
                articles=[{"slug": "article-1"}],
                next_page=2,
            ),
            _make_error_response(500),
        ]

        slugs = await ingester.discover("testuser")

        assert slugs == ["article-1"]

    async def test_discover_request_exception(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """リクエスト例外時は取得済みデータを返すこと."""
        mock_client.get.side_effect = Exception("Connection error")

        slugs = await ingester.discover("testuser")

        assert slugs == []

    async def test_discover_empty_username(
        self, ingester: ZennIngester
    ) -> None:
        """空のユーザー名でバリデーションエラーになること."""
        with pytest.raises(ValueError, match="username must not be empty"):
            await ingester.discover("")
        with pytest.raises(ValueError, match="username must not be empty"):
            await ingester.discover("   ")

    async def test_discover_respects_max_articles(
        self, mock_client: MagicMock
    ) -> None:
        """max_articles を超える記事が返らないこと."""
        ingester = ZennIngester(client=mock_client, max_articles=2)
        mock_client.get.return_value = _make_article_list_response(
            articles=[
                {"slug": "article-1"},
                {"slug": "article-2"},
                {"slug": "article-3"},
            ],
            next_page=None,
        )

        slugs = await ingester.discover("testuser")

        assert len(slugs) == 2

    async def test_discover_pagination_limit(
        self, mock_client: MagicMock
    ) -> None:
        """ページネーション走査上限で走査が停止すること."""
        ingester = ZennIngester(client=mock_client, max_articles=100)

        # MAX_PAGINATION_PAGES + 1 ページ分のレスポンスを用意
        responses = []
        for i in range(MAX_PAGINATION_PAGES + 1):
            responses.append(
                _make_article_list_response(
                    articles=[{"slug": f"article-{i}"}],
                    next_page=i + 2,
                )
            )
        mock_client.get.side_effect = responses

        slugs = await ingester.discover("testuser")

        # MAX_PAGINATION_PAGES ページ分のみ取得される
        assert len(slugs) == MAX_PAGINATION_PAGES
        assert mock_client.get.call_count == MAX_PAGINATION_PAGES

    async def test_discover_invalid_json(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """JSON パースエラー時に走査を中断すること."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("Invalid JSON")
        mock_client.get.return_value = resp

        slugs = await ingester.discover("testuser")

        assert slugs == []


# --- ZennIngester.fetch_single テスト ---


class TestZennIngesterFetchSingle:
    """ZennIngester.fetch_single のテスト."""

    @pytest.fixture()
    def mock_client(self) -> MagicMock:
        return _make_mock_client()

    @pytest.fixture()
    def ingester(self, mock_client: MagicMock) -> ZennIngester:
        return ZennIngester(client=mock_client, max_articles=3)

    async def test_fetch_single_success(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """正常に記事を取得して IngestedContent を返すこと."""
        mock_client.get.return_value = _make_article_detail_response(
            slug="test-article",
            title="Test Article",
            body_html="<p>Hello World</p>",
            path="/testuser/articles/test-article",
            article_type="tech",
            published_at="2024-01-01T00:00:00+09:00",
            liked_count=10,
            topics=[{"display_name": "Python", "name": "python"}],
        )

        result = await ingester.fetch_single("test-article")

        assert result is not None
        assert result.source_id == "https://zenn.dev/testuser/articles/test-article"
        assert result.title == "Test Article"
        assert result.text == "Hello World"
        assert result.source_type == "zenn"
        assert result.metadata["slug"] == "test-article"
        assert result.metadata["article_type"] == "tech"
        assert result.metadata["published_at"] == "2024-01-01T00:00:00+09:00"
        assert result.metadata["liked_count"] == 10
        assert result.metadata["topics"] == ["Python"]
        assert result.metadata["username"] == "testuser"

    async def test_fetch_single_404(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """404 エラーで None を返すこと."""
        mock_client.get.return_value = _make_error_response(404)

        result = await ingester.fetch_single("nonexistent-article")

        assert result is None

    async def test_fetch_single_empty_body_html(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """空の body_html で None を返すこと."""
        mock_client.get.return_value = _make_article_detail_response(
            body_html="",
        )

        result = await ingester.fetch_single("test-article")

        assert result is None

    async def test_fetch_single_whitespace_only_body_html(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """空白のみの body_html で None を返すこと."""
        mock_client.get.return_value = _make_article_detail_response(
            body_html="<div>   </div>",
        )

        result = await ingester.fetch_single("test-article")

        assert result is None

    async def test_fetch_single_request_exception(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """リクエスト例外で None を返すこと."""
        mock_client.get.side_effect = Exception("Connection error")

        result = await ingester.fetch_single("test-article")

        assert result is None

    async def test_fetch_single_invalid_json(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """JSON パースエラーで None を返すこと."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("Invalid JSON")
        mock_client.get.return_value = resp

        result = await ingester.fetch_single("test-article")

        assert result is None

    async def test_fetch_single_invalid_slug(
        self, ingester: ZennIngester
    ) -> None:
        """不正な slug でバリデーションエラーになること."""
        with pytest.raises(ValueError, match="must not be empty"):
            await ingester.fetch_single("")

    async def test_fetch_single_html_extraction(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """HTML が Markdown 形式に変換され、不要タグが除去されること."""
        mock_client.get.return_value = _make_article_detail_response(
            body_html=(
                "<h1>Title</h1>"
                "<p>Paragraph 1</p>"
                "<script>alert('xss')</script>"
                "<style>.hidden{display:none}</style>"
                "<p>Paragraph 2</p>"
            ),
        )

        result = await ingester.fetch_single("test-article")

        assert result is not None
        assert "alert" not in result.text
        assert ".hidden" not in result.text
        # 見出しが Markdown 形式で保持されること
        assert "# Title" in result.text
        assert "Paragraph 1" in result.text
        assert "Paragraph 2" in result.text

    async def test_fetch_single_markdown_heading_structure(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """複数レベルの Markdown 見出し構造が保持されること."""
        mock_client.get.return_value = _make_article_detail_response(
            body_html=(
                "<h2>Section 1</h2>"
                "<p>Content of section 1</p>"
                "<h2>Section 2</h2>"
                "<p>Content of section 2</p>"
                "<h3>Subsection 2.1</h3>"
                "<p>Content of subsection 2.1</p>"
            ),
        )

        result = await ingester.fetch_single("test-article")

        assert result is not None
        assert "## Section 1" in result.text
        assert "## Section 2" in result.text
        assert "### Subsection 2.1" in result.text

    async def test_fetch_single_markdown_table_structure(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """テーブル構造が Markdown 形式で保持されること."""
        mock_client.get.return_value = _make_article_detail_response(
            body_html=(
                "<table>"
                "<thead><tr><th>Name</th><th>Value</th></tr></thead>"
                "<tbody><tr><td>A</td><td>1</td></tr></tbody>"
                "</table>"
            ),
        )

        result = await ingester.fetch_single("test-article")

        assert result is not None
        assert "| Name | Value |" in result.text
        assert "| --- | --- |" in result.text
        assert "| A | 1 |" in result.text

    async def test_fetch_single_markdown_link_url_removal(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """リンク URL が除去され、テキストのみ保持されること."""
        mock_client.get.return_value = _make_article_detail_response(
            body_html=(
                "<p>See <a href=\"https://example.com/page\">this link</a> for details.</p>"
                "<p>Visit <a href=\"https://example.com/other\">example site</a>.</p>"
            ),
        )

        result = await ingester.fetch_single("test-article")

        assert result is not None
        assert "this link" in result.text
        assert "example site" in result.text
        assert "https://example.com/page" not in result.text
        assert "https://example.com/other" not in result.text

    async def test_fetch_single_markdown_image_url_removal(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """画像 URL が除去され、alt テキストのみ保持されること."""
        mock_client.get.return_value = _make_article_detail_response(
            body_html=(
                "<p>Here is an image:</p>"
                '<img src="https://example.com/image.png" alt="example image">'
                "<p>And another:</p>"
                '<img src="https://example.com/photo.jpg" alt="">'
            ),
        )

        result = await ingester.fetch_single("test-article")

        assert result is not None
        assert "example image" in result.text
        assert "https://example.com/image.png" not in result.text
        assert "https://example.com/photo.jpg" not in result.text


    async def test_fetch_single_topics_parsing(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """トピックが正しくパースされること."""
        mock_client.get.return_value = _make_article_detail_response(
            topics=[
                {"display_name": "Python", "name": "python"},
                {"display_name": "FastAPI", "name": "fastapi"},
            ],
        )

        result = await ingester.fetch_single("test-article")

        assert result is not None
        assert result.metadata["topics"] == ["Python", "FastAPI"]

    async def test_fetch_single_string_topics(
        self, ingester: ZennIngester, mock_client: MagicMock
    ) -> None:
        """文字列形式のトピックも正しくパースされること."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "article": {
                "slug": "test-article",
                "title": "Test",
                "body_html": "<p>Content</p>",
                "path": "/testuser/articles/test-article",
                "article_type": "tech",
                "published_at": "2024-01-01T00:00:00+09:00",
                "liked_count": 0,
                "topics": ["python", "fastapi"],
                "user": {"username": "testuser"},
            }
        }
        mock_client.get.return_value = resp

        result = await ingester.fetch_single("test-article")

        assert result is not None
        assert result.metadata["topics"] == ["python", "fastapi"]


# --- ZennIngester コンストラクタテスト ---


class TestZennIngesterInit:
    """ZennIngester コンストラクタのテスト."""

    def test_default_max_articles(self) -> None:
        """デフォルト max_articles が 50 であること."""
        ingester = ZennIngester(client=_make_mock_client())
        assert ingester._max_articles == 50

    def test_custom_max_articles(self) -> None:
        """カスタム max_articles が設定できること."""
        ingester = ZennIngester(client=_make_mock_client(), max_articles=10)
        assert ingester._max_articles == 10

    def test_clamp_max_articles(self) -> None:
        """ハードリミット超過時にクランプされること."""
        ingester = ZennIngester(client=_make_mock_client(), max_articles=200)
        assert ingester._max_articles == MAX_ARTICLES_HARD_LIMIT

    def test_reject_zero_max_articles(self) -> None:
        """max_articles=0 でバリデーションエラーになること."""
        with pytest.raises(ValueError):
            ZennIngester(client=_make_mock_client(), max_articles=0)

    def test_reject_negative_max_articles(self) -> None:
        """負の max_articles でバリデーションエラーになること."""
        with pytest.raises(ValueError):
            ZennIngester(client=_make_mock_client(), max_articles=-5)


# --- ハードリミット定数テスト ---


class TestHardLimits:
    """ハードリミット定数の値テスト."""

    def test_max_pagination_pages(self) -> None:
        """ページネーション走査上限が仕様通りであること."""
        assert MAX_PAGINATION_PAGES == 10

    def test_max_articles_hard_limit(self) -> None:
        """記事取得上限が仕様通りであること."""
        assert MAX_ARTICLES_HARD_LIMIT == 100

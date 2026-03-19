"""Web インジェスターのテスト.

仕様: docs/specs/ingesters/web.md

テスト方針:
- URL バリデーション（空、無効スキーム、ホスト名なし、フラグメント除去）
- SSRF 対策（localhost、プライベート IP）
- タイトル抽出（charset_normalizer + BeautifulSoup）
- リンク抽出（同一ドメイン、フラグメント除去、重複除去）
- add（単一ページ追加）
- crawl（一括クロール）
- crawl_preview（プレビュー）
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rag.pipeline.ingesters.web import (
    MAX_CRAWL_PAGES_HARD_LIMIT,
    WebIngester,
    _check_ssrf,
    _extract_links,
    _extract_title,
    _validate_url,
)
from rag.store.source_store import SourceStore


@pytest.fixture()
def source_store(tmp_path: Path) -> SourceStore:
    """テスト用 SourceStore を生成する."""
    store = SourceStore(tmp_path / "source_store")
    store.initialize()
    return store


class TestValidateUrl:
    """_validate_url のテスト."""

    def test_valid_http(self) -> None:
        """http スキームが通ること."""
        assert _validate_url("http://example.com/page") == "http://example.com/page"

    def test_valid_https(self) -> None:
        """https スキームが通ること."""
        assert _validate_url("https://example.com/page") == "https://example.com/page"

    def test_empty_raises(self) -> None:
        """空の URL でエラーになること."""
        with pytest.raises(ValueError, match="空"):
            _validate_url("")

    def test_invalid_scheme_raises(self) -> None:
        """無効なスキームでエラーになること."""
        with pytest.raises(ValueError, match="スキーム"):
            _validate_url("ftp://example.com/file")

    def test_no_hostname_raises(self) -> None:
        """ホスト名なしでエラーになること."""
        with pytest.raises(ValueError, match="ホスト名"):
            _validate_url("https:///path")

    def test_fragment_removed(self) -> None:
        """フラグメントが除去されること."""
        result = _validate_url("https://example.com/page#section")
        assert "#" not in result
        assert result == "https://example.com/page"


class TestCheckSsrf:
    """_check_ssrf のテスト."""

    def test_localhost_blocked(self) -> None:
        """localhost がブロックされること."""
        with pytest.raises(ValueError, match="プライベートホスト"):
            _check_ssrf("http://localhost/api")

    def test_localhost_localdomain_blocked(self) -> None:
        """localhost.localdomain がブロックされること."""
        with pytest.raises(ValueError, match="プライベートホスト"):
            _check_ssrf("http://localhost.localdomain/api")

    def test_private_ip_blocked(self) -> None:
        """プライベート IP がブロックされること."""
        with pytest.raises(ValueError, match="プライベート"):
            _check_ssrf("http://127.0.0.1/api")

    @patch("socket.getaddrinfo")
    def test_public_ip_allowed(self, mock_getaddr: MagicMock) -> None:
        """パブリック IP が許可されること."""
        mock_getaddr.return_value = [(2, 1, 6, "", ("93.184.216.34", 80))]
        # パブリック IP は SSRF ブロックされない（例外が発生しないことを確認）
        _check_ssrf("http://example.com/")


class TestExtractTitle:
    """_extract_title のテスト."""

    def test_basic_title(self) -> None:
        """基本的な title タグの抽出."""
        html = b"<html><head><title>Test Page</title></head></html>"
        assert _extract_title(html) == "Test Page"

    def test_no_title(self) -> None:
        """title タグがない場合に空文字列."""
        html = b"<html><head></head><body>content</body></html>"
        assert _extract_title(html) == ""

    def test_utf8_title(self) -> None:
        """UTF-8 の日本語タイトル."""
        html = "<html><head><title>テストページ</title></head></html>".encode("utf-8")
        assert _extract_title(html) == "テストページ"


class TestExtractLinks:
    """_extract_links のテスト."""

    def test_same_domain_links(self) -> None:
        """同一ドメインのリンクのみ抽出されること."""
        html = """
        <html><body>
        <a href="https://example.com/page1">Page 1</a>
        <a href="https://other.com/page2">Page 2</a>
        <a href="/page3">Page 3</a>
        </body></html>
        """
        links = _extract_links(html, "https://example.com/index")
        assert "https://example.com/page1" in links
        assert "https://example.com/page3" in links
        assert "https://other.com/page2" not in links

    def test_fragment_removed(self) -> None:
        """フラグメントが除去されること."""
        html = '<a href="https://example.com/page#section">Link</a>'
        links = _extract_links(html, "https://example.com/index")
        assert links == ["https://example.com/page"]

    def test_dedup(self) -> None:
        """重複 URL が除去されること."""
        html = """
        <a href="https://example.com/page">Link 1</a>
        <a href="https://example.com/page">Link 2</a>
        """
        links = _extract_links(html, "https://example.com/index")
        assert len(links) == 1

    def test_empty_html(self) -> None:
        """リンクがない HTML で空リスト."""
        links = _extract_links("<html></html>", "https://example.com")
        assert links == []


def _make_mock_response(
    content: bytes = b"<html><head><title>Test</title></head></html>",
    status_code: int = 200,
    text: str = "",
) -> MagicMock:
    """モック HTTP レスポンスを生成する."""
    resp = MagicMock()
    resp.content = content
    resp.status_code = status_code
    resp.text = text or content.decode("utf-8", errors="replace")
    resp.json.return_value = {}
    return resp


@pytest.mark.asyncio()
class TestAdd:
    """add のテスト."""

    async def test_basic_add(self, source_store: SourceStore) -> None:
        """基本的な単一ページ追加が動作すること."""
        html_content = b"<html><head><title>Test Page</title></head><body>content</body></html>"
        resp = _make_mock_response(content=html_content)
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        ingester = WebIngester(
            source_store,
            url_safety_check=False,
            respect_robots_txt=False,
        )
        result = await ingester.add(
            "https://example.com/docs/guide",
            client=client,
        )

        assert result.placed == 1
        assert result.errors == 0

    async def test_empty_url_raises(self, source_store: SourceStore) -> None:
        """空の URL でエラーになること."""
        ingester = WebIngester(source_store)
        with pytest.raises(ValueError, match="空"):
            await ingester.add("", client=AsyncMock())

    async def test_no_client_raises(self, source_store: SourceStore) -> None:
        """client 未指定でエラーになること."""
        ingester = WebIngester(source_store)
        with pytest.raises(ValueError, match="client"):
            await ingester.add("https://example.com")

    async def test_redirect_blocked(self, source_store: SourceStore) -> None:
        """リダイレクトがブロックされること."""
        resp = _make_mock_response(status_code=301)
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        ingester = WebIngester(
            source_store,
            url_safety_check=False,
            respect_robots_txt=False,
        )
        with pytest.raises(ValueError, match="リダイレクト"):
            await ingester.add("https://example.com/redirect", client=client)


@pytest.mark.asyncio()
class TestCrawl:
    """crawl のテスト."""

    async def test_basic_crawl(self, source_store: SourceStore) -> None:
        """基本的な一括クロールが動作すること."""
        index_html = b"""
        <html><body>
        <a href="https://example.com/page1">Page 1</a>
        <a href="https://example.com/page2">Page 2</a>
        </body></html>
        """
        page_html = b"<html><head><title>Page</title></head><body>content</body></html>"

        index_resp = _make_mock_response(content=index_html)
        page_resp = _make_mock_response(content=page_html)
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[index_resp, page_resp, page_resp])

        ingester = WebIngester(
            source_store,
            url_safety_check=False,
            respect_robots_txt=False,
        )
        result = await ingester.crawl(
            "https://example.com/index",
            client=client,
        )

        assert result.placed == 2

    async def test_pattern_filter(self, source_store: SourceStore) -> None:
        """正規表現パターンでフィルタされること."""
        index_html = b"""
        <html><body>
        <a href="https://example.com/docs/page1">Docs</a>
        <a href="https://example.com/blog/post1">Blog</a>
        </body></html>
        """
        page_html = b"<html><head><title>Doc</title></head><body>doc</body></html>"

        index_resp = _make_mock_response(content=index_html)
        page_resp = _make_mock_response(content=page_html)
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[index_resp, page_resp])

        ingester = WebIngester(
            source_store,
            url_safety_check=False,
            respect_robots_txt=False,
        )
        result = await ingester.crawl(
            "https://example.com/index",
            pattern=r"/docs/",
            client=client,
        )

        assert result.placed == 1


@pytest.mark.asyncio()
class TestCrawlPreview:
    """crawl_preview のテスト."""

    async def test_basic_preview(self, source_store: SourceStore) -> None:
        """基本的なプレビューが動作すること."""
        index_html = b"""
        <html><body>
        <a href="https://example.com/page1">Page 1</a>
        </body></html>
        """
        page_html = b"<html><head><title>Page Title</title></head></html>"

        index_resp = _make_mock_response(content=index_html)
        page_resp = _make_mock_response(content=page_html)
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[index_resp, page_resp])

        ingester = WebIngester(
            source_store,
            url_safety_check=False,
            respect_robots_txt=False,
        )
        previews = await ingester.crawl_preview(
            "https://example.com/index",
            client=client,
        )

        assert len(previews) == 1
        assert previews[0]["url"] == "https://example.com/page1"
        assert previews[0]["title"] == "Page Title"

    async def test_invalid_url_returns_empty(
        self, source_store: SourceStore
    ) -> None:
        """無効な URL で空リストが返ること."""
        ingester = WebIngester(source_store)
        result = await ingester.crawl_preview("", client=AsyncMock())
        assert result == []

    async def test_invalid_pattern_returns_empty(
        self, source_store: SourceStore
    ) -> None:
        """無効な正規表現パターンで空リストが返ること."""
        index_html = b"""
        <html><body>
        <a href="https://example.com/page1">Page 1</a>
        </body></html>
        """
        index_resp = _make_mock_response(content=index_html)
        client = AsyncMock()
        client.get = AsyncMock(return_value=index_resp)

        ingester = WebIngester(
            source_store,
            url_safety_check=False,
            respect_robots_txt=False,
        )
        result = await ingester.crawl_preview(
            "https://example.com/index",
            pattern="[invalid",
            client=client,
        )
        assert result == []

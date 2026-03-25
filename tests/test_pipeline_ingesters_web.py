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
    MAX_CRAWL_DEPTH_HARD_LIMIT,
    WebIngester,
    _decode_html_bytes,
    _extract_links,
    _extract_title,
    _is_allowed_content_type,
    _is_crawlable_url,
    _needs_html_extension,
)
from rag.store.source_store import SourceStore
from rag.utils.url import check_ssrf, validate_url


@pytest.fixture()
def source_store(tmp_path: Path) -> SourceStore:
    """テスト用 SourceStore を生成する."""
    store = SourceStore(tmp_path / "source_store")
    store.initialize()
    return store


class TestValidateUrl:
    """validate_url() のテスト."""

    def test_valid_http(self) -> None:
        """http スキームが通ること."""
        assert validate_url("http://example.com/page") == "http://example.com/page"

    def test_valid_https(self) -> None:
        """https スキームが通ること."""
        assert validate_url("https://example.com/page") == "https://example.com/page"

    def test_empty_raises(self) -> None:
        """空の URL でエラーになること."""
        with pytest.raises(ValueError, match="空"):
            validate_url("")

    def test_invalid_scheme_raises(self) -> None:
        """無効なスキームでエラーになること."""
        with pytest.raises(ValueError, match="スキーム"):
            validate_url("ftp://example.com/file")

    def test_no_hostname_raises(self) -> None:
        """ホスト名なしでエラーになること."""
        with pytest.raises(ValueError, match="ホスト名"):
            validate_url("https:///path")

    def test_fragment_removed(self) -> None:
        """フラグメントが除去されること."""
        result = validate_url("https://example.com/page#section")
        assert "#" not in result
        assert result == "https://example.com/page"


class TestCheckSsrf:
    """check_ssrf() のテスト."""

    def test_localhost_blocked(self) -> None:
        """localhost がブロックされること."""
        with pytest.raises(ValueError, match="プライベートホスト"):
            check_ssrf("http://localhost/api")

    def test_localhost_localdomain_blocked(self) -> None:
        """localhost.localdomain がブロックされること."""
        with pytest.raises(ValueError, match="プライベートホスト"):
            check_ssrf("http://localhost.localdomain/api")

    def test_private_ip_blocked(self) -> None:
        """プライベート IP がブロックされること."""
        with pytest.raises(ValueError, match="プライベート"):
            check_ssrf("http://127.0.0.1/api")

    @patch("socket.getaddrinfo")
    def test_public_ip_allowed(self, mock_getaddr: MagicMock) -> None:
        """パブリック IP が許可されること."""
        mock_getaddr.return_value = [(2, 1, 6, "", ("93.184.216.34", 80))]
        # パブリック IP は SSRF ブロックされない（例外が発生しないことを確認）
        check_ssrf("http://example.com/")

    @patch("socket.getaddrinfo")
    def test_ipv4_mapped_ipv6_blocked(self, mock_getaddr: MagicMock) -> None:
        """IPv4-mapped IPv6 アドレスがブロックされること."""
        mock_getaddr.return_value = [
            (10, 1, 6, "", ("::ffff:127.0.0.1", 80, 0, 0))
        ]
        with pytest.raises(ValueError, match="プライベート"):
            check_ssrf("http://mapped-v6.example.com/")

    @patch("socket.getaddrinfo")
    def test_ipv6_zone_index_blocked(self, mock_getaddr: MagicMock) -> None:
        """zone index 付き IPv6 リンクローカルアドレスがブロックされること."""
        mock_getaddr.return_value = [
            (10, 1, 6, "", ("fe80::1%lo0", 80, 0, 0))
        ]
        with pytest.raises(ValueError, match="プライベート"):
            check_ssrf("http://link-local.example.com/")

    @patch("socket.getaddrinfo")
    def test_unparseable_ip_rejected(self, mock_getaddr: MagicMock) -> None:
        """パース不能な IP アドレスは fail-closed で拒否されること."""
        mock_getaddr.return_value = [
            (2, 1, 6, "", ("not-an-ip", 80))
        ]
        with pytest.raises(ValueError, match="無効な IP アドレス"):
            check_ssrf("http://bad-dns.example.com/")


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
    content_type: str = "text/html; charset=utf-8",
) -> MagicMock:
    """モック HTTP レスポンスを生成する."""
    resp = MagicMock()
    resp.content = content
    resp.status_code = status_code
    resp.text = text or content.decode("utf-8", errors="replace")
    resp.headers = {"content-type": content_type}
    resp.json.return_value = {}
    return resp


class TestNeedsHtmlExtension:
    """_needs_html_extension のテスト."""

    def test_extensionless_url_needs_html(self) -> None:
        """拡張子なし URL は .html が必要."""
        assert _needs_html_extension("https://example.com/docs/guide") is True

    def test_html_url_does_not_need_html(self) -> None:
        """.html URL は .html 不要."""
        assert _needs_html_extension("https://example.com/page.html") is False

    def test_pdf_url_does_not_need_html(self) -> None:
        """.pdf URL は .html 不要."""
        assert _needs_html_extension("https://example.com/doc.pdf") is False

    def test_json_url_does_not_need_html(self) -> None:
        """.json URL は .html 不要."""
        assert _needs_html_extension("https://example.com/data.json") is False

    def test_unknown_extension_needs_html(self) -> None:
        """未知の拡張子は .html が必要."""
        assert _needs_html_extension("https://example.com/file.xyz") is True

    def test_trailing_slash_needs_html(self) -> None:
        """末尾スラッシュは .html が必要."""
        assert _needs_html_extension("https://example.com/docs/") is True


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
            respect_robots_txt=False,
        )
        with pytest.raises(ValueError, match="リダイレクト"):
            await ingester.add("https://example.com/redirect", client=client)


@pytest.mark.asyncio()
class TestAddExtension:
    """add の拡張子付与テスト."""

    async def test_extensionless_url_gets_html_extension(
        self, source_store: SourceStore,
    ) -> None:
        """拡張子なし URL のファイルが .html 拡張子付きで配置されること."""
        html_content = b"<html><head><title>Guide</title></head><body>guide</body></html>"
        resp = _make_mock_response(content=html_content)
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        ingester = WebIngester(
            source_store,
            respect_robots_txt=False,
        )
        result = await ingester.add(
            "https://example.com/docs/guide",
            client=client,
        )

        assert result.placed == 1

        # source_store 内に .html 拡張子付きでファイルが存在するか確認
        files = source_store.list_files(source_type="web")
        file_paths = [f.as_posix() for f in files]
        assert any(p.endswith(".html") for p in file_paths), (
            f"Expected .html file, got: {file_paths}"
        )

    async def test_url_with_html_extension_no_double(
        self, source_store: SourceStore,
    ) -> None:
        """既に .html 拡張子がある URL では二重付加されないこと."""
        html_content = b"<html><head><title>Page</title></head><body>page</body></html>"
        resp = _make_mock_response(content=html_content)
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        ingester = WebIngester(
            source_store,
            respect_robots_txt=False,
        )
        result = await ingester.add(
            "https://example.com/page.html",
            client=client,
        )

        assert result.placed == 1

        files = source_store.list_files(source_type="web")
        file_paths = [f.as_posix() for f in files]
        # .html.html にならないことを確認
        assert not any(p.endswith(".html.html") for p in file_paths), (
            f"Double .html detected: {file_paths}"
        )


    async def test_pdf_url_no_html_extension(
        self, source_store: SourceStore,
    ) -> None:
        """PDF URL には .html が付かず .pdf のまま配置されること."""
        pdf_content = b"%PDF-1.4 fake pdf"
        resp = _make_mock_response(content=pdf_content)
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        ingester = WebIngester(
            source_store,
            respect_robots_txt=False,
        )
        result = await ingester.add(
            "https://example.com/report.pdf",
            client=client,
        )

        assert result.placed == 1

        files = source_store.list_files(source_type="web")
        file_paths = [f.as_posix() for f in files]
        # .pdf.html にならないことを確認
        assert not any(p.endswith(".pdf.html") for p in file_paths), (
            f"Unexpected .pdf.html: {file_paths}"
        )
        assert any(p.endswith(".pdf") for p in file_paths), (
            f"Expected .pdf file, got: {file_paths}"
        )


@pytest.mark.asyncio()
class TestCrawlExtension:
    """crawl の拡張子付与テスト."""

    async def test_crawl_extensionless_urls(
        self, source_store: SourceStore,
    ) -> None:
        """crawl で拡張子なし URL のファイルが .html 拡張子付きで配置されること."""
        index_html = b"""
        <html><body>
        <a href="https://example.com/docs/page1">Page 1</a>
        </body></html>
        """
        page_html = b"<html><head><title>Page</title></head><body>content</body></html>"

        index_resp = _make_mock_response(content=index_html)
        page_resp = _make_mock_response(content=page_html)
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[index_resp, page_resp])

        ingester = WebIngester(
            source_store,
            respect_robots_txt=False,
        )
        result = await ingester.crawl(
            "https://example.com/index",
            client=client,
        )

        assert result.placed == 1

        files = source_store.list_files(source_type="web")
        file_paths = [f.as_posix() for f in files]
        assert any(p.endswith(".html") for p in file_paths), (
            f"Expected .html file, got: {file_paths}"
        )


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
            respect_robots_txt=False,
        )
        result = await ingester.crawl_preview(
            "https://example.com/index",
            pattern="[invalid",
            client=client,
        )
        assert result == []


class TestDecodeHtmlBytes:
    """_decode_html_bytes のテスト."""

    def test_utf8(self) -> None:
        """UTF-8 の HTML が正しくデコードされること."""
        html = "<html><body>テスト</body></html>".encode("utf-8")
        result = _decode_html_bytes(html)
        assert "テスト" in result

    def test_empty_bytes(self) -> None:
        """空バイト列でエラーにならないこと."""
        result = _decode_html_bytes(b"")
        assert result == ""


@pytest.mark.asyncio()
class TestCrawlDepth:
    """crawl の再帰クロール（depth）テスト."""

    async def test_depth_1_same_as_default(
        self, source_store: SourceStore
    ) -> None:
        """depth=1 は従来動作と同じ結果になること."""
        index_html = b"""
        <html><body>
        <a href="https://example.com/page1">Page 1</a>
        </body></html>
        """
        page_html = b"<html><head><title>Page</title></head><body>content</body></html>"

        index_resp = _make_mock_response(content=index_html)
        page_resp = _make_mock_response(content=page_html)
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[index_resp, page_resp])

        ingester = WebIngester(
            source_store,
            respect_robots_txt=False,
        )
        result = await ingester.crawl(
            "https://example.com/index",
            depth=1,
            client=client,
        )

        assert result.placed == 1
        assert result.errors == 0

    async def test_depth_2_follows_links(
        self, source_store: SourceStore
    ) -> None:
        """depth=2 で取得したページからさらにリンクを辿ること."""
        index_html = b"""
        <html><body>
        <a href="https://example.com/docs/page1">Page 1</a>
        </body></html>
        """
        # page1 has a link to page2
        page1_html = b"""
        <html><head><title>Page 1</title></head><body>
        <a href="https://example.com/docs/page2">Page 2</a>
        </body></html>
        """
        page2_html = b"<html><head><title>Page 2</title></head><body>content 2</body></html>"

        index_resp = _make_mock_response(content=index_html)
        page1_resp = _make_mock_response(content=page1_html)
        page2_resp = _make_mock_response(content=page2_html)
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[index_resp, page1_resp, page2_resp])

        ingester = WebIngester(
            source_store,
            respect_robots_txt=False,
        )
        result = await ingester.crawl(
            "https://example.com/index",
            pattern=r"/docs/",
            depth=2,
            client=client,
        )

        assert result.placed == 2
        assert result.errors == 0

    async def test_depth_loop_detection(
        self, source_store: SourceStore
    ) -> None:
        """再帰クロール中に訪問済み URL がスキップされること."""
        index_html = b"""
        <html><body>
        <a href="https://example.com/docs/page1">Page 1</a>
        </body></html>
        """
        # page1 links back to index and itself
        page1_html = b"""
        <html><head><title>Page 1</title></head><body>
        <a href="https://example.com/index">Back</a>
        <a href="https://example.com/docs/page1">Self</a>
        </body></html>
        """

        index_resp = _make_mock_response(content=index_html)
        page1_resp = _make_mock_response(content=page1_html)
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[index_resp, page1_resp])

        ingester = WebIngester(
            source_store,
            respect_robots_txt=False,
        )
        result = await ingester.crawl(
            "https://example.com/index",
            pattern=r"example\.com",
            depth=3,
            client=client,
        )

        # page1 only — index and page1 are both visited, no new links for depth 2
        assert result.placed == 1

    async def test_depth_clamped_to_hard_limit(
        self, source_store: SourceStore
    ) -> None:
        """depth がハードリミットを超えた場合にクランプされること."""
        # 各 depth で 1 ページずつリンクがある構造を作る
        # depth=99 を指定してもハードリミット（10）でクランプされるため
        # 最大でも 10 depth 分しか処理されない
        def make_page(n: int) -> bytes:
            return (
                f'<html><head><title>P{n}</title></head><body>'
                f'<a href="https://example.com/d/p{n + 1}">next</a>'
                f'</body></html>'
            ).encode()

        responses = [_make_mock_response(content=make_page(i)) for i in range(MAX_CRAWL_DEPTH_HARD_LIMIT + 5)]
        client = AsyncMock()
        client.get = AsyncMock(side_effect=responses)

        ingester = WebIngester(
            source_store,
            respect_robots_txt=False,
        )
        result = await ingester.crawl(
            "https://example.com/d/p0",
            pattern=r"/d/",
            depth=99,
            client=client,
        )

        # ハードリミット(10)でクランプされるため、最大 10 ページ
        assert result.placed <= MAX_CRAWL_DEPTH_HARD_LIMIT
        assert result.errors == 0

    async def test_max_pages_shared_across_depths(
        self, source_store: SourceStore
    ) -> None:
        """max_crawl_pages が全 depth で共有されること."""
        index_html = b"""
        <html><body>
        <a href="https://example.com/docs/p1">P1</a>
        <a href="https://example.com/docs/p2">P2</a>
        </body></html>
        """
        page_html = b"""
        <html><head><title>P</title></head><body>
        <a href="https://example.com/docs/p3">P3</a>
        </body></html>
        """

        index_resp = _make_mock_response(content=index_html)
        page_resp = _make_mock_response(content=page_html)
        client = AsyncMock()
        # index, p1, p2 (max_pages=2 reached before depth 2)
        client.get = AsyncMock(side_effect=[index_resp, page_resp, page_resp])

        ingester = WebIngester(
            source_store,
            max_crawl_pages=2,
            respect_robots_txt=False,
        )
        result = await ingester.crawl(
            "https://example.com/index",
            pattern=r"/docs/",
            depth=2,
            client=client,
        )

        # max_crawl_pages=2 なので最大 2 ページ
        assert result.placed == 2


@pytest.mark.asyncio()
class TestCrawlMaxErrors:
    """crawl の累計エラー停止テスト."""

    async def test_stops_on_error_threshold(
        self, source_store: SourceStore
    ) -> None:
        """累計エラー数が閾値に達した場合に操作が中断されること."""
        index_html = b"""
        <html><body>
        <a href="https://example.com/p1">P1</a>
        <a href="https://example.com/p2">P2</a>
        <a href="https://example.com/p3">P3</a>
        <a href="https://example.com/p4">P4</a>
        <a href="https://example.com/p5">P5</a>
        <a href="https://example.com/p6">P6</a>
        </body></html>
        """

        index_resp = _make_mock_response(content=index_html)
        error_resp = _make_mock_response(status_code=403)
        client = AsyncMock()
        # index succeeds, then all pages return 403
        client.get = AsyncMock(
            side_effect=[index_resp] + [error_resp] * 6
        )

        ingester = WebIngester(
            source_store,
            crawl_max_errors=5,
            respect_robots_txt=False,
        )
        result = await ingester.crawl(
            "https://example.com/index",
            client=client,
        )

        assert result.placed == 0
        # エラー数は閾値（5）で止まる（6件全ては処理されない）
        assert result.errors == 5

    async def test_max_errors_clamped_to_minimum(
        self, source_store: SourceStore
    ) -> None:
        """crawl_max_errors が下限（5）にクランプされること."""
        ingester = WebIngester(
            source_store,
            crawl_max_errors=1,  # 1 は下限 5 にクランプ
            respect_robots_txt=False,
        )
        assert ingester._crawl_max_errors == 5


@pytest.mark.asyncio()
class TestCrawlPreviewDepth:
    """crawl_preview の再帰クロール（depth）テスト."""

    async def test_preview_depth_2(
        self, source_store: SourceStore
    ) -> None:
        """depth=2 のプレビューでリンクを辿った結果が返ること."""
        index_html = b"""
        <html><body>
        <a href="https://example.com/docs/page1">Page 1</a>
        </body></html>
        """
        page1_html = b"""
        <html><head><title>Page 1</title></head><body>
        <a href="https://example.com/docs/page2">Page 2</a>
        </body></html>
        """
        page2_html = b"<html><head><title>Page 2</title></head><body>end</body></html>"

        index_resp = _make_mock_response(content=index_html)
        page1_resp = _make_mock_response(content=page1_html)
        page2_resp = _make_mock_response(content=page2_html)
        client = AsyncMock()
        client.get = AsyncMock(
            side_effect=[index_resp, page1_resp, page2_resp]
        )

        ingester = WebIngester(
            source_store,
            respect_robots_txt=False,
        )
        previews = await ingester.crawl_preview(
            "https://example.com/index",
            pattern=r"/docs/",
            depth=2,
            client=client,
        )

        assert len(previews) == 2
        urls = [p["url"] for p in previews]
        assert "https://example.com/docs/page1" in urls
        assert "https://example.com/docs/page2" in urls


class TestIsCrawlableUrl:
    """_is_crawlable_url のテスト."""

    def test_no_extension_allowed(self) -> None:
        """拡張子なし URL が許可されること."""
        assert _is_crawlable_url("https://example.com/docs/guide") is True

    def test_html_allowed(self) -> None:
        """.html が許可されること."""
        assert _is_crawlable_url("https://example.com/page.html") is True

    def test_htm_allowed(self) -> None:
        """.htm が許可されること."""
        assert _is_crawlable_url("https://example.com/page.htm") is True

    def test_pdf_allowed(self) -> None:
        """.pdf が許可されること."""
        assert _is_crawlable_url("https://example.com/report.pdf") is True

    def test_exe_blocked(self) -> None:
        """.exe がブロックされること."""
        assert _is_crawlable_url("https://example.com/setup.exe") is False

    def test_zip_blocked(self) -> None:
        """.zip がブロックされること."""
        assert _is_crawlable_url("https://example.com/archive.zip") is False

    def test_png_blocked(self) -> None:
        """.png がブロックされること."""
        assert _is_crawlable_url("https://example.com/image.png") is False

    def test_mp4_blocked(self) -> None:
        """.mp4 がブロックされること."""
        assert _is_crawlable_url("https://example.com/video.mp4") is False

    def test_js_blocked(self) -> None:
        """.js がブロックされること."""
        assert _is_crawlable_url("https://example.com/script.js") is False

    def test_css_blocked(self) -> None:
        """.css がブロックされること."""
        assert _is_crawlable_url("https://example.com/style.css") is False

    def test_trailing_slash_allowed(self) -> None:
        """末尾スラッシュ URL が許可されること."""
        assert _is_crawlable_url("https://example.com/docs/") is True


class TestIsAllowedContentType:
    """_is_allowed_content_type のテスト."""

    def test_text_html(self) -> None:
        """text/html が許可されること."""
        assert _is_allowed_content_type("text/html") is True

    def test_text_html_with_charset(self) -> None:
        """charset 付き text/html が許可されること."""
        assert _is_allowed_content_type("text/html; charset=utf-8") is True

    def test_application_pdf(self) -> None:
        """application/pdf が許可されること."""
        assert _is_allowed_content_type("application/pdf") is True

    def test_image_png_blocked(self) -> None:
        """image/png がブロックされること."""
        assert _is_allowed_content_type("image/png") is False

    def test_application_octet_stream_blocked(self) -> None:
        """application/octet-stream がブロックされること."""
        assert _is_allowed_content_type("application/octet-stream") is False

    def test_empty_blocked(self) -> None:
        """空文字列がブロックされること."""
        assert _is_allowed_content_type("") is False


class TestExtractLinksFiltering:
    """_extract_links の URL 拡張子フィルタテスト."""

    def test_binary_links_filtered(self) -> None:
        """バイナリ拡張子のリンクがフィルタされること."""
        html = """
        <html><body>
        <a href="https://example.com/page.html">HTML</a>
        <a href="https://example.com/setup.exe">EXE</a>
        <a href="https://example.com/image.png">PNG</a>
        <a href="https://example.com/docs/guide">No ext</a>
        <a href="https://example.com/report.pdf">PDF</a>
        </body></html>
        """
        links = _extract_links(html, "https://example.com/index")
        assert "https://example.com/page.html" in links
        assert "https://example.com/docs/guide" in links
        assert "https://example.com/report.pdf" in links
        assert "https://example.com/setup.exe" not in links
        assert "https://example.com/image.png" not in links


@pytest.mark.asyncio()
class TestCrawlContentTypeFilter:
    """crawl の Content-Type フィルタテスト."""

    async def test_non_html_content_type_skipped(
        self, source_store: SourceStore
    ) -> None:
        """Content-Type が許可対象外のページがスキップされること."""
        index_html = b"""
        <html><body>
        <a href="https://example.com/page1">Page 1</a>
        <a href="https://example.com/page2">Page 2</a>
        </body></html>
        """
        html_page = b"<html><head><title>Page</title></head><body>content</body></html>"

        index_resp = _make_mock_response(content=index_html)
        # page1: text/html（許可）
        html_resp = MagicMock()
        html_resp.content = html_page
        html_resp.status_code = 200
        html_resp.headers = {"content-type": "text/html; charset=utf-8"}
        # page2: image/png（ブロック）
        img_resp = MagicMock()
        img_resp.content = b"\x89PNG\r\n"
        img_resp.status_code = 200
        img_resp.headers = {"content-type": "image/png"}

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[index_resp, html_resp, img_resp])

        ingester = WebIngester(
            source_store,
            respect_robots_txt=False,
        )
        result = await ingester.crawl(
            "https://example.com/index",
            client=client,
        )

        assert result.placed == 1  # page1 のみ
        assert result.skipped == 1  # page2 はスキップ

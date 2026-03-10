"""WebCrawler テスト

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from rag.web_crawler import CrawledPage, RobotsChecker, WebCrawler


# テスト用HTMLサンプル
SAMPLE_HTML_WITH_ARTICLE = """
<!DOCTYPE html>
<html>
<head>
    <title>テスト記事</title>
    <script>console.log('test');</script>
    <style>body { color: red; }</style>
</head>
<body>
    <nav>ナビゲーション</nav>
    <header>ヘッダー</header>
    <article>
        <h1>記事タイトル</h1>
        <p>これは記事の本文です。</p>
        <p>2つ目の段落です。</p>
    </article>
    <footer>フッター</footer>
</body>
</html>
"""

SAMPLE_HTML_WITH_MAIN = """
<!DOCTYPE html>
<html>
<head><title>メインコンテンツ</title></head>
<body>
    <nav>ナビ</nav>
    <main>
        <p>メインエリアのテキスト</p>
    </main>
</body>
</html>
"""

SAMPLE_HTML_BODY_ONLY = """
<!DOCTYPE html>
<html>
<head><title>ボディのみ</title></head>
<body>
    <p>ボディ内のテキスト</p>
</body>
</html>
"""

SAMPLE_INDEX_HTML = """
<!DOCTYPE html>
<html>
<head><title>リンク集</title></head>
<body>
    <h1>記事一覧</h1>
    <ul>
        <li><a href="/article/1">記事1</a></li>
        <li><a href="/article/2">記事2</a></li>
        <li><a href="https://example.com/article/3">記事3</a></li>
        <li><a href="https://other-domain.com/page">外部リンク</a></li>
        <li><a href="/doc/guide.html">ガイド</a></li>
        <li><a href="/doc/faq.html">FAQ</a></li>
    </ul>
</body>
</html>
"""


class MockResponse:
    """モックHTTPレスポンス."""

    def __init__(
        self,
        status: int = 200,
        text: str = "",
        headers: dict[str, str] | None = None,
        raw_bytes: bytes | None = None,
    ) -> None:
        self.status = status
        self._text = text
        self.headers: dict[str, str] = headers or {}
        self._raw_bytes = raw_bytes

    async def text(self, errors: str = "strict") -> str:  # noqa: ARG002
        return self._text

    async def read(self) -> bytes:
        """レスポンスボディをバイト列として返す."""
        if self._raw_bytes is not None:
            return self._raw_bytes
        return self._text.encode("utf-8")


class MockClientSession:
    """モックaiohttpクライアントセッション."""

    def __init__(
        self, status: int = 200, text: str = "", raw_bytes: bytes | None = None
    ) -> None:
        self._status = status
        self._text = text
        self._raw_bytes = raw_bytes

    async def __aenter__(self) -> "MockClientSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def get(self, url: str, **kwargs: object) -> "MockContextManager":  # noqa: ARG002
        return MockContextManager(self._status, self._text, raw_bytes=self._raw_bytes)


class MockContextManager:
    """モックコンテキストマネージャ（session.get()の戻り値）."""

    def __init__(
        self,
        status: int,
        text: str,
        headers: dict[str, str] | None = None,
        raw_bytes: bytes | None = None,
    ) -> None:
        self._status = status
        self._text = text
        self._headers = headers
        self._raw_bytes = raw_bytes

    async def __aenter__(self) -> MockResponse:
        return MockResponse(self._status, self._text, self._headers, self._raw_bytes)

    async def __aexit__(self, *args: object) -> None:
        pass


class TestCrawledPage:
    """CrawledPage データクラスのテスト."""

    def test_crawled_page_creation(self) -> None:
        """CrawledPage が正しく作成されること."""
        page = CrawledPage(
            url="https://example.com/test",
            title="テストタイトル",
            text="テスト本文",
            crawled_at="2024-01-01T00:00:00+00:00",
        )
        assert page.url == "https://example.com/test"
        assert page.title == "テストタイトル"
        assert page.text == "テスト本文"
        assert page.crawled_at == "2024-01-01T00:00:00+00:00"


class TestWebCrawlerValidation:
    """WebCrawler URL検証のテスト."""

    def test_non_http_scheme_rejected(self) -> None:
        """AC31: http/https 以外のスキームが拒否されること."""
        crawler = WebCrawler(respect_robots_txt=False)

        with pytest.raises(ValueError, match="許可されていないスキーム"):
            crawler.validate_url("file:///etc/passwd")

        with pytest.raises(ValueError, match="許可されていないスキーム"):
            crawler.validate_url("ftp://example.com/file")

    def test_valid_url_passes(self) -> None:
        """有効なURLが検証を通過すること."""
        crawler = WebCrawler(respect_robots_txt=False)
        result = crawler.validate_url("https://example.com/page")
        assert result == "https://example.com/page"

    def test_http_scheme_allowed(self) -> None:
        """http スキームも許可されること."""
        crawler = WebCrawler(respect_robots_txt=False)
        result = crawler.validate_url("http://example.com/page")
        assert result == "http://example.com/page"

    def test_any_domain_allowed(self) -> None:
        """任意のドメインが許可されること."""
        crawler = WebCrawler(respect_robots_txt=False)
        result = crawler.validate_url("https://any-domain.com/page")
        assert result == "https://any-domain.com/page"

    def test_localhost_rejected(self) -> None:
        """SSRF対策: localhostへのアクセスが拒否されること."""
        crawler = WebCrawler(respect_robots_txt=False)

        with pytest.raises(ValueError, match="localhost"):
            crawler.validate_url("http://localhost/admin")

        with pytest.raises(ValueError, match="localhost"):
            crawler.validate_url("https://localhost:8080/api")

    def test_loopback_ip_rejected(self) -> None:
        """SSRF対策: ループバックIP (127.0.0.1) へのアクセスが拒否されること."""
        crawler = WebCrawler(respect_robots_txt=False)

        with pytest.raises(ValueError, match="ループバックアドレス"):
            crawler.validate_url("http://127.0.0.1/admin")

        with pytest.raises(ValueError, match="ループバックアドレス"):
            crawler.validate_url("http://127.0.0.2/admin")

    def test_private_ip_rejected(self) -> None:
        """SSRF対策: プライベートIP (RFC1918) へのアクセスが拒否されること."""
        crawler = WebCrawler(respect_robots_txt=False)

        # 10.0.0.0/8
        with pytest.raises(ValueError, match="プライベートIPアドレス"):
            crawler.validate_url("http://10.0.0.1/internal")

        # 172.16.0.0/12
        with pytest.raises(ValueError, match="プライベートIPアドレス"):
            crawler.validate_url("http://172.16.0.1/internal")

        # 192.168.0.0/16
        with pytest.raises(ValueError, match="プライベートIPアドレス"):
            crawler.validate_url("http://192.168.1.1/internal")

    def test_link_local_ip_rejected(self) -> None:
        """SSRF対策: リンクローカルIP (169.254.0.0/16) へのアクセスが拒否されること."""
        crawler = WebCrawler(respect_robots_txt=False)

        # AWS metadata endpoint
        with pytest.raises(ValueError, match="リンクローカルアドレス"):
            crawler.validate_url("http://169.254.169.254/latest/meta-data/")

    def test_validate_url_strips_fragment(self) -> None:
        """AC36: validate_url() がURLフラグメントを除去すること."""
        crawler = WebCrawler(respect_robots_txt=False)
        result = crawler.validate_url("https://example.com/page#section1")
        assert result == "https://example.com/page"

    def test_validate_url_strips_fragment_with_path(self) -> None:
        """AC36: パス付きURLのフラグメントも除去されること."""
        crawler = WebCrawler(respect_robots_txt=False)
        result = crawler.validate_url("https://example.com/path/to/page#anchor")
        assert result == "https://example.com/path/to/page"

    def test_validate_url_without_fragment_unchanged(self) -> None:
        """AC36: フラグメントのないURLはそのまま返されること."""
        crawler = WebCrawler(respect_robots_txt=False)
        result = crawler.validate_url("https://example.com/page")
        assert result == "https://example.com/page"


class TestWebCrawlerTextExtraction:
    """WebCrawler テキスト抽出のテスト."""

    def test_extract_text_from_article(self) -> None:
        """<article> タグから本文を抽出すること."""
        crawler = WebCrawler(respect_robots_txt=False)
        title, text = crawler._extract_text(SAMPLE_HTML_WITH_ARTICLE)

        assert title == "テスト記事"
        assert "記事タイトル" in text
        assert "これは記事の本文です" in text
        assert "2つ目の段落です" in text
        # 除去されるべき要素
        assert "ナビゲーション" not in text
        assert "ヘッダー" not in text
        assert "フッター" not in text
        assert "console.log" not in text

    def test_extract_text_from_main(self) -> None:
        """<main> タグから本文を抽出すること（<article> がない場合）."""
        crawler = WebCrawler(respect_robots_txt=False)
        title, text = crawler._extract_text(SAMPLE_HTML_WITH_MAIN)

        assert title == "メインコンテンツ"
        assert "メインエリアのテキスト" in text
        assert "ナビ" not in text

    def test_extract_text_from_body(self) -> None:
        """<body> から本文を抽出すること（<article>, <main> がない場合）."""
        crawler = WebCrawler(respect_robots_txt=False)
        title, text = crawler._extract_text(SAMPLE_HTML_BODY_ONLY)

        assert title == "ボディのみ"
        assert "ボディ内のテキスト" in text


class TestWebCrawlerMarkdownConversion:
    """WebCrawler HTML→Markdown変換のテスト."""

    def test_headings_converted_to_markdown(self) -> None:
        """見出し（h1-h6）がMarkdown見出しに変換されること."""
        html = """
        <html><head><title>見出しテスト</title></head>
        <body>
            <article>
                <h1>大見出し</h1>
                <p>本文1</p>
                <h2>中見出し</h2>
                <p>本文2</p>
                <h3>小見出し</h3>
                <p>本文3</p>
            </article>
        </body></html>
        """
        crawler = WebCrawler(respect_robots_txt=False)
        title, text = crawler._extract_text(html)

        assert "# 大見出し" in text
        assert "## 中見出し" in text
        assert "### 小見出し" in text

    def test_table_converted_to_markdown(self) -> None:
        """テーブルがMarkdownテーブル形式に変換されること."""
        html = """
        <html><head><title>テーブルテスト</title></head>
        <body>
            <article>
                <table>
                    <thead>
                        <tr><th>名前</th><th>年齢</th></tr>
                    </thead>
                    <tbody>
                        <tr><td>太郎</td><td>30</td></tr>
                        <tr><td>花子</td><td>25</td></tr>
                    </tbody>
                </table>
            </article>
        </body></html>
        """
        crawler = WebCrawler(respect_robots_txt=False)
        _, text = crawler._extract_text(html)

        # Markdown テーブル形式の検証
        assert "| 名前 | 年齢 |" in text
        assert "| 太郎 | 30 |" in text
        assert "| 花子 | 25 |" in text
        # セパレータ行の存在
        assert "| --- | --- |" in text

    def test_list_converted_to_markdown(self) -> None:
        """リスト（ul/ol）がMarkdownリストに変換されること."""
        html = """
        <html><head><title>リストテスト</title></head>
        <body>
            <article>
                <ul>
                    <li>項目A</li>
                    <li>項目B</li>
                    <li>項目C</li>
                </ul>
            </article>
        </body></html>
        """
        crawler = WebCrawler(respect_robots_txt=False)
        _, text = crawler._extract_text(html)

        assert "項目A" in text
        assert "項目B" in text
        assert "項目C" in text
        # リスト記号が付与されていること（markdownify のデフォルトは * + - ）
        assert "* 項目A" in text or "- 項目A" in text

    def test_link_text_only_no_url(self) -> None:
        """リンクはテキストのみ保持され、URLは除去されること."""
        html = """
        <html><head><title>リンクテスト</title></head>
        <body>
            <article>
                <p>詳しくは<a href="https://example.com/details">こちら</a>をご覧ください。</p>
            </article>
        </body></html>
        """
        crawler = WebCrawler(respect_robots_txt=False)
        _, text = crawler._extract_text(html)

        assert "こちら" in text
        assert "https://example.com/details" not in text
        assert "[こちら]" not in text

    def test_image_alt_text_only(self) -> None:
        """画像はalt属性のテキストのみ保持されること."""
        html = """
        <html><head><title>画像テスト</title></head>
        <body>
            <article>
                <p>以下は画像です。</p>
                <img src="photo.jpg" alt="風景写真">
            </article>
        </body></html>
        """
        crawler = WebCrawler(respect_robots_txt=False)
        _, text = crawler._extract_text(html)

        assert "風景写真" in text
        assert "photo.jpg" not in text
        assert "![" not in text

    def test_image_without_alt_excluded(self) -> None:
        """alt属性のない画像は出力に含まれないこと."""
        html = """
        <html><head><title>画像テスト</title></head>
        <body>
            <article>
                <p>テキスト</p>
                <img src="photo.jpg">
            </article>
        </body></html>
        """
        crawler = WebCrawler(respect_robots_txt=False)
        _, text = crawler._extract_text(html)

        assert "photo.jpg" not in text

    def test_code_block_preserved(self) -> None:
        """コードブロックがMarkdownコードブロック形式で保持されること."""
        html = """
        <html><head><title>コードテスト</title></head>
        <body>
            <article>
                <pre><code>def hello():
    print("hello")</code></pre>
            </article>
        </body></html>
        """
        crawler = WebCrawler(respect_robots_txt=False)
        _, text = crawler._extract_text(html)

        assert "```" in text
        assert 'print("hello")' in text

    def test_strong_em_preserved(self) -> None:
        """強調（bold/italic）がMarkdown形式で保持されること."""
        html = """
        <html><head><title>強調テスト</title></head>
        <body>
            <article>
                <p>これは<strong>重要な</strong>テキストで、<em>強調された</em>部分があります。</p>
            </article>
        </body></html>
        """
        crawler = WebCrawler(respect_robots_txt=False)
        _, text = crawler._extract_text(html)

        assert "重要な" in text
        assert "強調された" in text

    def test_unwanted_tags_still_removed(self) -> None:
        """Markdown変換後も不要タグ（script, style, nav等）が除去されていること."""
        html = """
        <html><head><title>除去テスト</title>
        <script>alert('xss');</script>
        <style>.hidden{display:none}</style>
        </head>
        <body>
            <nav>ナビゲーション</nav>
            <header>ヘッダー部分</header>
            <article>
                <h1>本文</h1>
                <p>記事のテキスト</p>
            </article>
            <aside>サイドバー</aside>
            <footer>フッター</footer>
        </body></html>
        """
        crawler = WebCrawler(respect_robots_txt=False)
        _, text = crawler._extract_text(html)

        assert "記事のテキスト" in text
        assert "alert" not in text
        assert "display:none" not in text
        assert "ナビゲーション" not in text
        assert "ヘッダー部分" not in text
        assert "サイドバー" not in text
        assert "フッター" not in text

    def test_consecutive_blank_lines_normalized(self) -> None:
        """連続する空白行が正規化されること."""
        html = """
        <html><head><title>空白テスト</title></head>
        <body>
            <article>
                <p>段落1</p>
                <br><br><br>
                <p>段落2</p>
            </article>
        </body></html>
        """
        crawler = WebCrawler(respect_robots_txt=False)
        _, text = crawler._extract_text(html)

        # 3行以上の連続空行がないこと
        assert "\n\n\n" not in text
        assert "段落1" in text
        assert "段落2" in text


class TestWebCrawlerCrawlIndexPage:
    """WebCrawler.crawl_index_page のテスト."""

    @pytest.mark.asyncio
    async def test_crawl_index_page_extracts_urls(self) -> None:
        """AC12: リンク集ページからURLリストを抽出できること."""
        crawler = WebCrawler(respect_robots_txt=False)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(200, SAMPLE_INDEX_HTML),
        ):
            urls = await crawler.crawl_index_page("https://example.com/articles")

        # SAMPLE_INDEX_HTML には6つのリンクがあるが、
        # 外部ドメイン (other-domain.com) はスキップされる
        expected_urls = {
            "https://example.com/article/1",
            "https://example.com/article/2",
            "https://example.com/article/3",
            # "https://other-domain.com/page" は外部ドメインのためスキップ
            "https://example.com/doc/guide.html",
            "https://example.com/doc/faq.html",
        }
        assert set(urls) == expected_urls
        assert len(urls) == 5  # 外部ドメイン1件を除く

    @pytest.mark.asyncio
    async def test_url_pattern_filtering(self) -> None:
        """AC13: URLパターン（正規表現）によるフィルタリングが機能すること."""
        crawler = WebCrawler(respect_robots_txt=False)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(200, SAMPLE_INDEX_HTML),
        ):
            # .html で終わるURLのみ抽出
            urls = await crawler.crawl_index_page(
                "https://example.com/articles",
                url_pattern=r"\.html$",
            )

        assert len(urls) == 2
        assert all(url.endswith(".html") for url in urls)

    @pytest.mark.asyncio
    async def test_crawl_index_page_skips_external_domain_links(self) -> None:
        """AC41: 外部ドメインのリンクがスキップされること（クロール範囲の制御）."""
        crawler = WebCrawler(respect_robots_txt=False)

        # 外部ドメインへのリンクを含むHTML
        html_with_external_links = """
        <!DOCTYPE html>
        <html>
        <head><title>リンク集</title></head>
        <body>
            <a href="/internal/page1">内部リンク1</a>
            <a href="https://example.com/internal/page2">内部リンク2</a>
            <a href="https://external-site.com/page">外部サイト1</a>
            <a href="https://another-external.org/doc">外部サイト2</a>
            <a href="https://malicious.web.fc2.com/exploit">悪意のあるサイト</a>
        </body>
        </html>
        """

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(200, html_with_external_links),
        ):
            urls = await crawler.crawl_index_page("https://example.com/links")

        # 内部リンクのみ抽出され、外部ドメインはスキップされる
        assert len(urls) == 2
        assert all("example.com" in url for url in urls)
        assert not any("external-site.com" in url for url in urls)
        assert not any("another-external.org" in url for url in urls)
        assert not any("fc2.com" in url for url in urls)

    @pytest.mark.asyncio
    async def test_crawl_index_page_deduplicates_fragment_urls(self) -> None:
        """AC37: アンカー違いの同一ページURLが重複除去されること."""
        crawler = WebCrawler(respect_robots_txt=False)

        # アンカー違いのリンクを含むHTML
        html_with_fragments = """
        <!DOCTYPE html>
        <html>
        <head><title>リンク集</title></head>
        <body>
            <a href="https://example.com/page#section1">セクション1</a>
            <a href="https://example.com/page#section2">セクション2</a>
            <a href="https://example.com/page#section3">セクション3</a>
            <a href="https://example.com/other">他のページ</a>
        </body>
        </html>
        """

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(200, html_with_fragments),
        ):
            urls = await crawler.crawl_index_page("https://example.com/index")

        # アンカー違いは1つに統合される
        assert len(urls) == 2
        assert "https://example.com/page" in urls
        assert "https://example.com/other" in urls

    @pytest.mark.asyncio
    async def test_max_crawl_pages_limit(self) -> None:
        """AC34: 1回のクロールで取得するページ数が max_pages で制限されること."""
        crawler = WebCrawler(max_pages=2, respect_robots_txt=False)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(200, SAMPLE_INDEX_HTML),
        ):
            urls = await crawler.crawl_index_page("https://example.com/articles")

        assert len(urls) <= 2


class TestWebCrawlerCrawlPage:
    """WebCrawler.crawl_page のテスト."""

    @pytest.mark.asyncio
    async def test_crawl_page_extracts_text(self) -> None:
        """AC14: 単一ページの本文テキストを取得できること."""
        crawler = WebCrawler(respect_robots_txt=False)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(200, SAMPLE_HTML_WITH_ARTICLE),
        ):
            page = await crawler.crawl_page("https://example.com/article/1")

        assert page is not None
        assert page.url == "https://example.com/article/1"
        assert page.title == "テスト記事"
        assert "これは記事の本文です" in page.text
        assert page.crawled_at  # ISO 8601 形式のタイムスタンプ

    @pytest.mark.asyncio
    async def test_crawl_page_returns_none_on_http_error(self) -> None:
        """HTTPエラー時に None を返すこと."""
        crawler = WebCrawler(respect_robots_txt=False)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(404, "Not Found"),
        ):
            page = await crawler.crawl_page("https://example.com/not-found")

        assert page is None

    @pytest.mark.asyncio
    async def test_crawl_page_returns_none_on_scheme_validation_failure(self) -> None:
        """スキーム検証失敗時に None を返すこと."""
        crawler = WebCrawler(respect_robots_txt=False)

        page = await crawler.crawl_page("file:///etc/passwd")

        assert page is None

    @pytest.mark.asyncio
    async def test_crawl_page_strips_fragment_from_url(self) -> None:
        """crawl_page() がフラグメント除去済みURLをCrawledPage.urlに格納すること."""
        crawler = WebCrawler(respect_robots_txt=False)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(200, SAMPLE_HTML_WITH_ARTICLE),
        ):
            page = await crawler.crawl_page("https://example.com/article/1#section")

        assert page is not None
        assert page.url == "https://example.com/article/1"

    @pytest.mark.asyncio
    async def test_crawl_page_rejects_redirect(self) -> None:
        """SSRF対策: リダイレクト応答を拒否すること."""
        crawler = WebCrawler(respect_robots_txt=False)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(302, ""),
        ):
            page = await crawler.crawl_page("https://example.com/redirect")

        assert page is None


class TestWebCrawlerCrawlIndexPageRedirect:
    """WebCrawler.crawl_index_page のリダイレクト対策テスト."""

    @pytest.mark.asyncio
    async def test_crawl_index_page_rejects_redirect(self) -> None:
        """SSRF対策: インデックスページのリダイレクト応答を拒否すること."""
        crawler = WebCrawler(respect_robots_txt=False)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(301, ""),
        ):
            urls = await crawler.crawl_index_page("https://example.com/articles")

        assert urls == []


class TestWebCrawlerCrawlPages:
    """WebCrawler.crawl_pages のテスト."""

    @pytest.mark.asyncio
    async def test_crawl_pages_isolates_errors(self) -> None:
        """AC15: 複数ページを並行クロールし、ページ単位のエラーを隔離すること."""
        crawler = WebCrawler(crawl_delay=0, respect_robots_txt=False)

        # 特定のURLを失敗させる（並行実行でも順序非依存）
        fail_url = "https://example.com/article/2"

        class MockClientSessionWithErrors:
            """エラーをシミュレートするモックセッション."""

            async def __aenter__(self) -> "MockClientSessionWithErrors":
                return self

            async def __aexit__(self, *args: object) -> None:
                pass

            def get(self, url: str, **kwargs: object) -> "MockContextManagerWithErrors":  # noqa: ARG002
                # URLに応じて成功/失敗を決定（順序非依存）
                if url == fail_url:
                    return MockContextManagerWithErrors(500, "Server Error")
                return MockContextManagerWithErrors(200, SAMPLE_HTML_WITH_ARTICLE)

        class MockContextManagerWithErrors:
            """エラーをシミュレートするモックコンテキストマネージャ."""

            def __init__(self, status: int, text: str) -> None:
                self._status = status
                self._text = text

            async def __aenter__(self) -> MockResponse:
                return MockResponse(self._status, self._text)

            async def __aexit__(self, *args: object) -> None:
                pass

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSessionWithErrors(),
        ):
            urls = [
                "https://example.com/article/1",
                fail_url,  # これは失敗
                "https://example.com/article/3",
            ]
            pages = await crawler.crawl_pages(urls)

        # fail_url が失敗しても、他のページは成功している
        assert len(pages) == 2
        page_urls = [p.url for p in pages]
        assert "https://example.com/article/1" in page_urls
        assert "https://example.com/article/3" in page_urls
        assert fail_url not in page_urls

    @pytest.mark.asyncio
    async def test_crawl_delay_between_requests(self) -> None:
        """AC35: 同一ドメインへの連続リクエスト間に crawl_delay の待機が挿入されること."""
        crawler = WebCrawler(crawl_delay=0.1, respect_robots_txt=False)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(200, SAMPLE_HTML_WITH_ARTICLE),
        ):
            with patch(
                "rag.web_crawler.asyncio.sleep", new_callable=AsyncMock
            ) as mock_sleep:
                # 同一ドメインへの複数リクエスト + 異なるドメインへのリクエスト
                urls = [
                    "https://example.com/article/1",
                    "https://example.com/article/2",  # 同一ドメイン（遅延あり）
                    "https://other.com/page",  # 異なるドメイン（遅延なし）
                ]
                await crawler.crawl_pages(urls)

                # sleep が呼ばれたことを確認（同一ドメインへの遅延）
                # 並行実行のため、厳密な回数は実行順序に依存するが、少なくとも1回は呼ばれる
                assert mock_sleep.call_count >= 1

    @pytest.mark.asyncio
    async def test_crawl_pages_empty_list(self) -> None:
        """空のURLリストに対して空のリストを返すこと."""
        crawler = WebCrawler(respect_robots_txt=False)
        pages = await crawler.crawl_pages([])
        assert pages == []


class TestWebCrawlerConcurrency:
    """WebCrawler 並行制御のテスト."""

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrent_requests(self) -> None:
        """Semaphore により同時接続数が制限されること."""
        max_concurrent = 2
        crawler = WebCrawler(
            max_concurrent=max_concurrent,
            crawl_delay=0,
            respect_robots_txt=False,
        )

        # 同時実行数を追跡
        current_concurrent = 0
        max_observed_concurrent = 0
        lock = asyncio.Lock()

        class MockClientSessionWithConcurrencyTracking:
            """同時実行数を追跡するモックセッション."""

            async def __aenter__(self) -> "MockClientSessionWithConcurrencyTracking":
                return self

            async def __aexit__(self, *args: object) -> None:
                pass

            def get(self, url: str, **kwargs: object) -> "MockContextManagerWithDelay":  # noqa: ARG002
                return MockContextManagerWithDelay()

        class MockContextManagerWithDelay:
            """遅延を入れて同時実行をシミュレートするコンテキストマネージャ."""

            async def __aenter__(self) -> MockResponse:
                nonlocal current_concurrent, max_observed_concurrent
                async with lock:
                    current_concurrent += 1
                    max_observed_concurrent = max(max_observed_concurrent, current_concurrent)
                # 少し待機して同時実行をシミュレート
                await asyncio.sleep(0.05)
                return MockResponse(200, SAMPLE_HTML_WITH_ARTICLE)

            async def __aexit__(self, *args: object) -> None:
                nonlocal current_concurrent
                async with lock:
                    current_concurrent -= 1

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSessionWithConcurrencyTracking(),
        ):
            urls = [f"https://example.com/article/{i}" for i in range(5)]
            await crawler.crawl_pages(urls)

        # 同時実行数が max_concurrent を超えていないことを確認
        assert max_observed_concurrent <= max_concurrent


# エンコーディング検出用のHTMLサンプル
SAMPLE_HTML_SHIFT_JIS = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="Shift_JIS">
    <title>日本語ページ</title>
</head>
<body>
    <article>
        <h1>Shift_JISエンコード</h1>
        <p>これはShift_JISでエンコードされたページです。</p>
    </article>
</body>
</html>
"""

SAMPLE_HTML_EUC_JP = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="EUC-JP">
    <title>EUC-JPページ</title>
</head>
<body>
    <article>
        <h1>EUC-JPエンコード</h1>
        <p>これはEUC-JPでエンコードされたページです。</p>
    </article>
</body>
</html>
"""


class TestWebCrawlerEncodingDetection:
    """WebCrawler エンコーディング自動検出のテスト."""

    @pytest.mark.asyncio
    async def test_shift_jis_encoding_detected(self) -> None:
        """AC14: Shift_JISエンコードされたHTMLが正しくデコードされること."""
        crawler = WebCrawler(respect_robots_txt=False)

        # Shift_JISでエンコードされたバイト列を作成
        shift_jis_bytes = SAMPLE_HTML_SHIFT_JIS.encode("shift_jis")

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(200, raw_bytes=shift_jis_bytes),
        ):
            page = await crawler.crawl_page("https://example.com/shift_jis_page")

        assert page is not None
        assert page.title == "日本語ページ"
        assert "Shift_JISエンコード" in page.text
        assert "これはShift_JISでエンコードされたページです" in page.text

    @pytest.mark.asyncio
    async def test_utf8_encoding_detected(self) -> None:
        """AC14: UTF-8エンコードされたHTMLが正しくデコードされること."""
        crawler = WebCrawler(respect_robots_txt=False)

        # UTF-8でエンコードされたバイト列を作成
        utf8_bytes = SAMPLE_HTML_WITH_ARTICLE.encode("utf-8")

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(200, raw_bytes=utf8_bytes),
        ):
            page = await crawler.crawl_page("https://example.com/utf8_page")

        assert page is not None
        assert page.title == "テスト記事"
        assert "これは記事の本文です" in page.text

    @pytest.mark.asyncio
    async def test_euc_jp_encoding_detected(self) -> None:
        """AC14: EUC-JPエンコードされたHTMLが正しくデコードされること."""
        crawler = WebCrawler(respect_robots_txt=False)

        # EUC-JPでエンコードされたバイト列を作成
        euc_jp_bytes = SAMPLE_HTML_EUC_JP.encode("euc_jp")

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(200, raw_bytes=euc_jp_bytes),
        ):
            page = await crawler.crawl_page("https://example.com/euc_jp_page")

        assert page is not None
        assert page.title == "EUC-JPページ"
        assert "EUC-JPエンコード" in page.text
        assert "これはEUC-JPでエンコードされたページです" in page.text

    @pytest.mark.asyncio
    async def test_encoding_detection_fallback(self) -> None:
        """AC14: エンコーディング検出に失敗した場合、UTF-8でフォールバックすること."""
        crawler = WebCrawler(respect_robots_txt=False)

        # 無効なバイト列（UTF-8として解釈できない部分を含む）
        # 0x80-0xFFの単独バイトはUTF-8として不正
        invalid_bytes = b"<html><body>Test content with invalid byte: \x80\xff</body></html>"

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(200, raw_bytes=invalid_bytes),
        ):
            page = await crawler.crawl_page("https://example.com/invalid_encoding")

        # フォールバックでデコードされ、置換文字が使用されることを確認
        assert page is not None
        assert "Test content" in page.text

    @pytest.mark.asyncio
    async def test_crawl_index_page_with_shift_jis(self) -> None:
        """AC14: crawl_index_pageでもShift_JISが正しくデコードされること."""
        crawler = WebCrawler(respect_robots_txt=False)

        # Shift_JISでエンコードされたリンク集ページ
        shift_jis_index = """
<!DOCTYPE html>
<html>
<head><title>リンク集</title></head>
<body>
    <a href="/article/1">記事1</a>
    <a href="/article/2">記事2</a>
</body>
</html>
"""
        shift_jis_bytes = shift_jis_index.encode("shift_jis")

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(200, raw_bytes=shift_jis_bytes),
        ):
            urls = await crawler.crawl_index_page("https://example.com/index")

        # URLが正しく抽出されていることを確認
        assert len(urls) == 2
        assert "https://example.com/article/1" in urls
        assert "https://example.com/article/2" in urls


# robots.txt テスト用のサンプル
SAMPLE_ROBOTS_TXT = """\
User-agent: RAGKnowledgeBot
Disallow: /private/
Disallow: /admin/
Crawl-delay: 5

User-agent: *
Disallow: /secret/
"""

SAMPLE_ROBOTS_TXT_WILDCARD_ONLY = """\
User-agent: *
Disallow: /blocked/
"""


class MockRobotsSession:
    """robots.txt リクエストとページリクエストの両方を処理するモックセッション."""

    def __init__(
        self,
        robots_txt: str = "",
        robots_status: int = 200,
        page_html: str = SAMPLE_HTML_WITH_ARTICLE,
        page_status: int = 200,
    ) -> None:
        self._robots_txt = robots_txt
        self._robots_status = robots_status
        self._page_html = page_html
        self._page_status = page_status

    async def __aenter__(self) -> "MockRobotsSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def get(self, url: str, **kwargs: object) -> "MockRobotsContextManager":  # noqa: ARG002
        if url.endswith("/robots.txt"):
            return MockRobotsContextManager(self._robots_status, self._robots_txt)
        return MockRobotsContextManager(self._page_status, self._page_html)


class MockRobotsContextManager:
    """robots.txt 対応モックコンテキストマネージャ."""

    def __init__(self, status: int, text: str) -> None:
        self._status = status
        self._text = text

    async def __aenter__(self) -> MockResponse:
        return MockResponse(self._status, self._text)

    async def __aexit__(self, *args: object) -> None:
        pass


class TestRobotsChecker:
    """RobotsChecker のテスト."""

    @pytest.mark.asyncio
    async def test_can_fetch_allowed_url(self) -> None:
        """許可されたURLに対して True を返すこと."""
        checker = RobotsChecker()
        timeout = aiohttp.ClientTimeout(total=10)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockRobotsSession(robots_txt=SAMPLE_ROBOTS_TXT),
        ):
            result = await checker.can_fetch("https://example.com/public/page", timeout)

        assert result is True

    @pytest.mark.asyncio
    async def test_can_fetch_disallowed_url(self) -> None:
        """Disallow 指定されたURLに対して False を返すこと."""
        checker = RobotsChecker()
        timeout = aiohttp.ClientTimeout(total=10)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockRobotsSession(robots_txt=SAMPLE_ROBOTS_TXT),
        ):
            result = await checker.can_fetch("https://example.com/private/data", timeout)

        assert result is False

    @pytest.mark.asyncio
    async def test_can_fetch_wildcard_disallow(self) -> None:
        """ワイルドカード User-agent の Disallow が適用されること."""
        checker = RobotsChecker()
        timeout = aiohttp.ClientTimeout(total=10)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockRobotsSession(robots_txt=SAMPLE_ROBOTS_TXT_WILDCARD_ONLY),
        ):
            result = await checker.can_fetch("https://example.com/blocked/page", timeout)

        assert result is False

    @pytest.mark.asyncio
    async def test_crawl_delay_parsed(self) -> None:
        """Crawl-delay が正しくパースされること."""
        checker = RobotsChecker()
        timeout = aiohttp.ClientTimeout(total=10)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockRobotsSession(robots_txt=SAMPLE_ROBOTS_TXT),
        ):
            delay = await checker.get_crawl_delay("https://example.com/page", timeout)

        assert delay == 5

    @pytest.mark.asyncio
    async def test_crawl_delay_none_when_not_specified(self) -> None:
        """Crawl-delay 未指定時は None を返すこと."""
        checker = RobotsChecker()
        timeout = aiohttp.ClientTimeout(total=10)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockRobotsSession(robots_txt=SAMPLE_ROBOTS_TXT_WILDCARD_ONLY),
        ):
            delay = await checker.get_crawl_delay("https://example.com/page", timeout)

        assert delay is None

    @pytest.mark.asyncio
    async def test_fail_open_on_fetch_error(self) -> None:
        """AC74: robots.txt の取得に失敗した場合、フェイルオープンでクロールを許可すること."""
        checker = RobotsChecker()
        timeout = aiohttp.ClientTimeout(total=10)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            side_effect=aiohttp.ClientError("Connection refused"),
        ):
            result = await checker.can_fetch("https://example.com/private/data", timeout)

        # 取得失敗 → 全て許可（フェイルオープン）
        assert result is True

    @pytest.mark.asyncio
    async def test_fail_open_on_404(self) -> None:
        """AC74: robots.txt が 404 の場合、全てのクロールを許可すること."""
        checker = RobotsChecker()
        timeout = aiohttp.ClientTimeout(total=10)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockRobotsSession(robots_status=404),
        ):
            result = await checker.can_fetch("https://example.com/any/path", timeout)

        assert result is True

    @pytest.mark.asyncio
    async def test_cache_hit(self) -> None:
        """AC75: robots.txt がキャッシュされ、TTL 内は再取得されないこと."""
        checker = RobotsChecker(cache_ttl=3600)
        timeout = aiohttp.ClientTimeout(total=10)

        mock_session = MockRobotsSession(robots_txt=SAMPLE_ROBOTS_TXT)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=mock_session,
        ) as mock_cls:
            # 1回目: 取得される
            await checker.can_fetch("https://example.com/page1", timeout)
            first_call_count = mock_cls.call_count

            # 2回目: キャッシュヒット（再取得されない）
            await checker.can_fetch("https://example.com/page2", timeout)
            second_call_count = mock_cls.call_count

        # 同じドメインなのでキャッシュヒット → セッション作成回数が増えない
        assert second_call_count == first_call_count

    @pytest.mark.asyncio
    async def test_cache_expiry(self) -> None:
        """AC75: キャッシュ TTL 超過後は再取得されること."""
        checker = RobotsChecker(cache_ttl=0)  # TTL=0 で即時期限切れ
        timeout = aiohttp.ClientTimeout(total=10)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockRobotsSession(robots_txt=SAMPLE_ROBOTS_TXT),
        ) as mock_cls:
            # 1回目
            await checker.can_fetch("https://example.com/page1", timeout)
            first_call_count = mock_cls.call_count

            # 2回目: TTL=0 なのでキャッシュ期限切れ → 再取得
            await checker.can_fetch("https://example.com/page2", timeout)
            second_call_count = mock_cls.call_count

        assert second_call_count > first_call_count

    def test_robots_url_generation(self) -> None:
        """robots.txt URL が正しく生成されること."""
        assert (
            RobotsChecker._robots_url("https://example.com/page/1")
            == "https://example.com/robots.txt"
        )
        assert (
            RobotsChecker._robots_url("http://example.com:8080/path")
            == "http://example.com:8080/robots.txt"
        )

    def test_cache_key_generation(self) -> None:
        """キャッシュキーがスキーム+ホスト+ポートで生成されること."""
        key1 = RobotsChecker._cache_key("https://example.com/page1")
        key2 = RobotsChecker._cache_key("https://example.com/page2")
        key3 = RobotsChecker._cache_key("http://example.com/page1")

        # 同じドメイン・スキームのURLは同じキャッシュキー
        assert key1 == key2
        # 異なるスキームは異なるキャッシュキー
        assert key1 != key3


class TestWebCrawlerRobotsTxt:
    """WebCrawler robots.txt 統合テスト."""

    @pytest.mark.asyncio
    async def test_crawl_page_skips_disallowed_url(self) -> None:
        """AC71: robots.txt で Disallow されたパスのクロールがスキップされること."""
        crawler = WebCrawler(respect_robots_txt=True)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockRobotsSession(
                robots_txt=SAMPLE_ROBOTS_TXT,
                page_html=SAMPLE_HTML_WITH_ARTICLE,
            ),
        ):
            # Disallow: /private/ → スキップされる
            page = await crawler.crawl_page("https://example.com/private/data")

        assert page is None

    @pytest.mark.asyncio
    async def test_crawl_page_allows_permitted_url(self) -> None:
        """AC71: robots.txt で許可されたパスはクロールされること."""
        crawler = WebCrawler(respect_robots_txt=True)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockRobotsSession(
                robots_txt=SAMPLE_ROBOTS_TXT,
                page_html=SAMPLE_HTML_WITH_ARTICLE,
            ),
        ):
            page = await crawler.crawl_page("https://example.com/public/page")

        assert page is not None
        assert "これは記事の本文です" in page.text

    @pytest.mark.asyncio
    async def test_crawl_page_ignores_robots_when_disabled(self) -> None:
        """AC72: respect_robots_txt=False の場合、robots.txt を無視してクロールすること."""
        crawler = WebCrawler(respect_robots_txt=False)

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockClientSession(200, SAMPLE_HTML_WITH_ARTICLE),
        ):
            # Disallow されたパスでもクロールされる
            page = await crawler.crawl_page("https://example.com/private/data")

        assert page is not None

    @pytest.mark.asyncio
    async def test_crawl_index_page_filters_disallowed_urls(self) -> None:
        """AC71: crawl_index_page で Disallow されたURLがフィルタリングされること."""
        crawler = WebCrawler(respect_robots_txt=True)

        # /private/ と /admin/ はDisallow
        html_with_mixed_links = """
        <!DOCTYPE html>
        <html>
        <head><title>リンク集</title></head>
        <body>
            <a href="/public/page1">公開ページ1</a>
            <a href="/private/secret">非公開ページ</a>
            <a href="/admin/dashboard">管理画面</a>
            <a href="/public/page2">公開ページ2</a>
        </body>
        </html>
        """

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockRobotsSession(
                robots_txt=SAMPLE_ROBOTS_TXT,
                page_html=html_with_mixed_links,
            ),
        ):
            urls = await crawler.crawl_index_page("https://example.com/index")

        # /private/ と /admin/ はフィルタリングされる
        assert len(urls) == 2
        assert "https://example.com/public/page1" in urls
        assert "https://example.com/public/page2" in urls
        assert not any("/private/" in url for url in urls)
        assert not any("/admin/" in url for url in urls)

    @pytest.mark.asyncio
    async def test_crawl_delay_from_robots_txt(self) -> None:
        """AC73: robots.txt の Crawl-delay が設定値より大きい場合、そちらが採用されること."""
        # crawl_delay=1.0 だが、robots.txt の Crawl-delay=5
        crawler = WebCrawler(
            crawl_delay=1.0,
            respect_robots_txt=True,
        )

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockRobotsSession(robots_txt=SAMPLE_ROBOTS_TXT),
        ):
            delay = await crawler._get_effective_crawl_delay("https://example.com/page")

        # robots.txt の 5 秒が採用される
        assert delay == 5

    @pytest.mark.asyncio
    async def test_configured_delay_when_larger(self) -> None:
        """AC73: 設定値が robots.txt の Crawl-delay より大きい場合、設定値が採用されること."""
        crawler = WebCrawler(
            crawl_delay=10.0,
            respect_robots_txt=True,
        )

        with patch(
            "rag.web_crawler.aiohttp.ClientSession",
            return_value=MockRobotsSession(robots_txt=SAMPLE_ROBOTS_TXT),
        ):
            delay = await crawler._get_effective_crawl_delay("https://example.com/page")

        # 設定値の 10 秒が採用される
        assert delay == 10.0

"""Scrapy Spider のユニットテスト.

仕様: docs/specs/site-ingest.md

テスト方針:
- Spider パラメータ化のバリデーション（start_url/output_dir 必須、allowed_domains 自動導出）
- URL → ファイル名変換のエッジケース
- Content-Type 判定（テキスト系/非テキスト系）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.scrapy.spider import SiteSpider


# --- _url_to_filename テスト ---


class TestUrlToFilename:
    """URL → ファイル名変換のテスト."""

    def test_simple_path(self) -> None:
        """シンプルなパスがディレクトリ構造を維持して変換されること."""
        result = SiteSpider._url_to_filename("https://example.com/docs/guide")
        assert result == "docs/guide.html"

    def test_root_path(self) -> None:
        """ルートパスは index.html になること."""
        result = SiteSpider._url_to_filename("https://example.com/")
        assert result == "index.html"

    def test_empty_path(self) -> None:
        """パスなしは index.html になること."""
        result = SiteSpider._url_to_filename("https://example.com")
        assert result == "index.html"

    def test_single_segment(self) -> None:
        """1セグメントのパス."""
        result = SiteSpider._url_to_filename("https://example.com/page")
        assert result == "page.html"

    def test_deep_path(self) -> None:
        """深い階層のパスがディレクトリ構造を維持すること."""
        result = SiteSpider._url_to_filename(
            "https://example.com/a/b/c/d/page"
        )
        assert result == "a/b/c/d/page.html"

    def test_path_with_html_extension(self) -> None:
        """既に .html 拡張子がある場合は二重付加しないこと."""
        result = SiteSpider._url_to_filename(
            "https://example.com/page.html"
        )
        assert result == "page.html"

    def test_trailing_slash_removed(self) -> None:
        """末尾スラッシュが除去されること."""
        result = SiteSpider._url_to_filename("https://example.com/docs/")
        assert result == "docs.html"

    def test_query_string_not_in_filename(self) -> None:
        """クエリパラメータはファイル名に含まれない（urlparse.path にはクエリは含まれない）."""
        result = SiteSpider._url_to_filename(
            "https://example.com/search?q=test"
        )
        assert result == "search.html"


# --- _is_text_response テスト ---


class TestIsTextResponse:
    """Content-Type 判定のテスト."""

    @pytest.fixture()
    def spider(self, tmp_path: Path) -> SiteSpider:
        """テスト用 Spider インスタンス（Scrapy 初期化をバイパス）."""
        spider = SiteSpider.__new__(SiteSpider)
        spider._url_pattern = None
        spider._no_follow = False
        spider._output_dir = tmp_path
        spider._max_pages = 0
        spider._page_count = 0
        spider._error_count = 0
        return spider

    def test_text_html(self, spider: SiteSpider) -> None:
        assert spider._is_text_response("text/html") is True

    def test_text_html_with_charset(self, spider: SiteSpider) -> None:
        assert spider._is_text_response("text/html; charset=utf-8") is True

    def test_text_plain(self, spider: SiteSpider) -> None:
        assert spider._is_text_response("text/plain") is True

    def test_xhtml(self, spider: SiteSpider) -> None:
        assert spider._is_text_response("application/xhtml+xml") is True

    def test_xml(self, spider: SiteSpider) -> None:
        assert spider._is_text_response("application/xml") is True

    def test_image_rejected(self, spider: SiteSpider) -> None:
        assert spider._is_text_response("image/png") is False

    def test_video_rejected(self, spider: SiteSpider) -> None:
        assert spider._is_text_response("video/mp4") is False

    def test_binary_rejected(self, spider: SiteSpider) -> None:
        assert spider._is_text_response("application/octet-stream") is False

    def test_pdf_rejected(self, spider: SiteSpider) -> None:
        assert spider._is_text_response("application/pdf") is False

    def test_empty_rejected(self, spider: SiteSpider) -> None:
        assert spider._is_text_response("") is False


# --- Spider 初期化テスト ---


class TestSpiderInit:
    """Spider 初期化のテスト."""

    def test_start_url_required(self, tmp_path: Path) -> None:
        """start_url が空の場合はエラーになること."""
        with pytest.raises(ValueError, match="start_url"):
            SiteSpider(start_url="", output_dir=str(tmp_path))

    def test_output_dir_required(self) -> None:
        """output_dir が空の場合はエラーになること."""
        with pytest.raises(ValueError, match="output_dir"):
            SiteSpider(start_url="https://example.com", output_dir="")

    def test_allowed_domains_auto_derived(self, tmp_path: Path) -> None:
        """allowed_domains が未指定時に start_url から自動導出されること."""
        spider = SiteSpider(
            start_url="https://docs.example.com/guide",
            output_dir=str(tmp_path),
        )
        assert spider.allowed_domains == ["docs.example.com"]

    def test_allowed_domains_explicit(self, tmp_path: Path) -> None:
        """allowed_domains がカンマ区切りで指定できること."""
        spider = SiteSpider(
            start_url="https://example.com",
            allowed_domains="example.com, sub.example.com",
            output_dir=str(tmp_path),
        )
        assert spider.allowed_domains == ["example.com", "sub.example.com"]

    def test_url_pattern_compiled(self, tmp_path: Path) -> None:
        """url_pattern が正規表現としてコンパイルされること."""
        spider = SiteSpider(
            start_url="https://example.com",
            url_pattern=r"/docs/.*",
            output_dir=str(tmp_path),
        )
        assert spider._url_pattern is not None
        assert spider._url_pattern.search("https://example.com/docs/guide")

    def test_no_url_pattern(self, tmp_path: Path) -> None:
        """url_pattern が空の場合は None になること."""
        spider = SiteSpider(
            start_url="https://example.com",
            output_dir=str(tmp_path),
        )
        assert spider._url_pattern is None

    def test_output_dir_created(self, tmp_path: Path) -> None:
        """output_dir が自動作成されること."""
        out = tmp_path / "new_dir" / "output"
        SiteSpider(
            start_url="https://example.com",
            output_dir=str(out),
        )
        assert out.exists()

    def test_max_pages_default_zero(self, tmp_path: Path) -> None:
        """max_pages 未指定時は 0（無制限）になること."""
        spider = SiteSpider(
            start_url="https://example.com",
            output_dir=str(tmp_path),
        )
        assert spider._max_pages == 0

    def test_max_pages_set(self, tmp_path: Path) -> None:
        """max_pages が設定されること."""
        spider = SiteSpider(
            start_url="https://example.com",
            output_dir=str(tmp_path),
            max_pages=20,
        )
        assert spider._max_pages == 20


# --- parse() の max_pages クローズテスト ---


class TestParseMaxPages:
    """parse() の max_pages 到達時の close_spider テスト."""

    @staticmethod
    def _make_spider(tmp_path: Path, max_pages: int) -> SiteSpider:
        """テスト用 Spider インスタンスを作成する."""
        from unittest.mock import MagicMock

        spider = SiteSpider.__new__(SiteSpider)
        spider.name = "site_spider"
        spider._url_pattern = None
        spider._no_follow = False
        spider._output_dir = tmp_path
        spider._max_pages = max_pages
        spider._page_count = 0
        spider._error_count = 0
        # crawler.engine.close_spider のモック
        spider.crawler = MagicMock()
        # logger は Scrapy の property のためモック不要（crawler モックにより動作する）
        return spider

    @staticmethod
    def _make_response(url: str, status: int = 200) -> object:
        """テスト用モック Response を作成する."""
        from unittest.mock import MagicMock

        mock_headers = MagicMock()
        mock_headers.get.return_value = b"text/html; charset=utf-8"
        mock_response = MagicMock()
        mock_response.url = url
        mock_response.status = status
        mock_response.body = b"<html><title>Test</title></html>"
        mock_response.headers = mock_headers
        mock_response.meta = {"depth": 0}
        mock_response.css.return_value.get.return_value = "Test"
        mock_response.css.return_value.getall.return_value = []
        return mock_response

    def test_close_spider_called_on_max_pages(self, tmp_path: Path) -> None:
        """200 OK が max_pages に達したら close_spider が呼ばれること."""
        from unittest.mock import patch

        spider = self._make_spider(tmp_path, max_pages=2)
        resolved_file = (tmp_path / "page.html").resolve()
        resolved_file.parent.mkdir(parents=True, exist_ok=True)
        resolved_file.write_text("<html></html>", encoding="utf-8")

        resp1 = self._make_response("https://example.com/page1")
        resp2 = self._make_response("https://example.com/page2")

        with patch.object(spider, "_save_html", return_value=resolved_file):
            list(spider.parse(resp1))
            list(spider.parse(resp2))

        spider.crawler.engine.close_spider.assert_called_once_with(
            spider, "max_pages_reached",
        )

    def test_close_spider_not_called_below_max(self, tmp_path: Path) -> None:
        """200 OK が max_pages 未満なら close_spider は呼ばれないこと."""
        from unittest.mock import patch

        spider = self._make_spider(tmp_path, max_pages=3)
        resolved_file = (tmp_path / "page.html").resolve()
        resolved_file.parent.mkdir(parents=True, exist_ok=True)
        resolved_file.write_text("<html></html>", encoding="utf-8")

        resp = self._make_response("https://example.com/page1")
        with patch.object(spider, "_save_html", return_value=resolved_file):
            list(spider.parse(resp))

        spider.crawler.engine.close_spider.assert_not_called()

    def test_404_not_counted_for_max_pages(self, tmp_path: Path) -> None:
        """404 レスポンスは max_pages のカウントに含まれないこと."""
        from unittest.mock import patch

        spider = self._make_spider(tmp_path, max_pages=1)
        resolved_file = (tmp_path / "page.html").resolve()
        resolved_file.parent.mkdir(parents=True, exist_ok=True)
        resolved_file.write_text("<html></html>", encoding="utf-8")

        # 404 → close_spider は呼ばれない
        resp_404 = self._make_response("https://example.com/missing", status=404)
        with patch.object(spider, "_save_html", return_value=resolved_file):
            list(spider.parse(resp_404))

        spider.crawler.engine.close_spider.assert_not_called()
        assert spider._page_count == 0

        # 200 → close_spider が呼ばれる
        resp_200 = self._make_response("https://example.com/page1")
        with patch.object(spider, "_save_html", return_value=resolved_file):
            list(spider.parse(resp_200))

        spider.crawler.engine.close_spider.assert_called_once()

    def test_no_follow_links_after_max_pages(self, tmp_path: Path) -> None:
        """max_pages 到達後はリンク追跡しないこと."""
        from unittest.mock import MagicMock, patch

        spider = self._make_spider(tmp_path, max_pages=1)
        resolved_file = (tmp_path / "page.html").resolve()
        resolved_file.parent.mkdir(parents=True, exist_ok=True)
        resolved_file.write_text("<html></html>", encoding="utf-8")

        resp = self._make_response("https://example.com/page1")
        mock_follow = MagicMock(return_value=iter([]))

        with (
            patch.object(spider, "_save_html", return_value=resolved_file),
            patch.object(spider, "_follow_links", mock_follow),
        ):
            list(spider.parse(resp))

        mock_follow.assert_not_called()

    def test_close_spider_on_save_html_failure(self, tmp_path: Path) -> None:
        """_save_html 失敗時でも max_pages 到達なら close_spider が呼ばれること."""
        from unittest.mock import patch

        spider = self._make_spider(tmp_path, max_pages=1)

        resp = self._make_response("https://example.com/page1")
        with patch.object(spider, "_save_html", return_value=None):
            list(spider.parse(resp))

        spider.crawler.engine.close_spider.assert_called_once_with(
            spider, "max_pages_reached",
        )


# --- parse() の filepath 回帰テスト ---


class TestParseFilepathRegression:
    """parse() が yield する filepath が相対パスになることの回帰テスト.

    _save_html は resolve 済み絶対パスを返すが、parse() 内で
    self._output_dir.resolve() に対して relative_to を呼ぶことで
    正しい相対パスに変換される。この修正の回帰を防止する。
    """

    def test_parse_yields_relative_filepath(self, tmp_path: Path) -> None:
        """parse() が yield するアイテムの filepath が相対パスであること."""
        from unittest.mock import MagicMock, patch

        # 相対パスで output_dir を設定（回帰の再現条件）
        output_dir = tmp_path / "html"
        output_dir.mkdir(parents=True, exist_ok=True)

        spider = SiteSpider.__new__(SiteSpider)
        spider.name = "site_spider"
        spider._url_pattern = None
        spider._no_follow = False
        spider._output_dir = output_dir
        spider._max_pages = 0
        spider._page_count = 0
        spider._error_count = 0

        # _save_html が resolve 済み絶対パスを返す（実装の実際の挙動）
        resolved_file = (output_dir / "page.html").resolve()
        resolved_file.write_text("<html><title>Test</title></html>", encoding="utf-8")

        # parse() に渡すモック Response
        mock_headers = MagicMock()
        mock_headers.get.return_value = b"text/html; charset=utf-8"

        mock_response = MagicMock()
        mock_response.url = "https://example.com/page.html"
        mock_response.status = 200
        mock_response.body = b"<html><title>Test</title></html>"
        mock_response.headers = mock_headers
        mock_response.meta = {"depth": 0}
        mock_response.css.return_value.get.return_value = "Test"
        mock_response.css.return_value.getall.return_value = []

        # _save_html を resolve 済み絶対パスを返すようにパッチ
        with patch.object(spider, "_save_html", return_value=resolved_file):
            items = list(spider.parse(mock_response))

        # parse() が yield したアイテムの filepath が相対パスであること
        data_items = [i for i in items if isinstance(i, dict)]
        assert len(data_items) == 1
        assert data_items[0]["filepath"] == "page.html"
        assert not Path(data_items[0]["filepath"]).is_absolute()

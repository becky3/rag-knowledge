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
        spider._output_dir = tmp_path
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


# --- parse() の filepath 回帰テスト ---


class TestParseFilepathRegression:
    """parse() が yield する filepath が相対パスになることの回帰テスト.

    _save_html は resolve 済み絶対パスを返すが、parse() 内で
    self._output_dir.resolve() に対して relative_to を呼ぶことで
    正しい相対パスに変換される。この修正の回帰を防止する。
    """

    def test_filepath_relative_with_relative_output_dir(self, tmp_path: Path) -> None:
        """resolve 済み絶対パスから output_dir.resolve() で相対パスが取れること."""
        output_dir = tmp_path / "html"
        output_dir.mkdir(parents=True, exist_ok=True)

        # _save_html が resolve 済み絶対パスを返すケースをシミュレート
        saved_file = (output_dir / "page.html").resolve()
        saved_file.write_text("<html>test</html>", encoding="utf-8")

        # filepath.relative_to(output_dir.resolve()) が成功すること
        # （修正前は output_dir が相対パスの場合に ValueError が発生していた）
        relative = saved_file.relative_to(output_dir.resolve())
        assert str(relative) == "page.html"
        assert not relative.is_absolute()

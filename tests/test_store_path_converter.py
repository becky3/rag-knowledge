"""path_converter モジュールのテスト.

仕様: docs/specs/source-store.md — URL パス変換規則
"""

from __future__ import annotations

import pytest

from rag.store.path_converter import path_to_url, url_to_path


class TestUrlToPath:
    """URL → source_store 相対パス変換."""

    def test_basic_https(self) -> None:
        result = url_to_path("https://example.com/docs/guide")
        assert result == "web/https/example.com/docs/guide"

    def test_basic_http(self) -> None:
        result = url_to_path("http://example.com/page")
        assert result == "web/http/example.com/page"

    def test_query_parameter(self) -> None:
        """クエリパラメータの ? を全角に置換する."""
        result = url_to_path("https://example.com/docs/guide?lang=ja")
        assert result == "web/https/example.com/docs/guide\uff1flang=ja"

    def test_port_number(self) -> None:
        """ポート番号の : を全角に置換する."""
        result = url_to_path("http://localhost:8080/api/docs")
        assert result == "web/http/localhost\uff1a8080/api/docs"

    def test_fragment_removed(self) -> None:
        """フラグメントは除去される."""
        result = url_to_path("https://example.com/page#section")
        assert result == "web/https/example.com/page"

    def test_fragment_same_as_no_fragment(self) -> None:
        """フラグメント違いの URL は同一パスになる."""
        assert url_to_path("https://example.com/page") == url_to_path(
            "https://example.com/page#section"
        )

    def test_unsupported_scheme(self) -> None:
        with pytest.raises(ValueError, match="サポートされていない URL スキーム"):
            url_to_path("ftp://example.com/file")

    def test_root_url(self) -> None:
        result = url_to_path("https://example.com/")
        assert result == "web/https/example.com"

    def test_host_only(self) -> None:
        result = url_to_path("https://example.com")
        assert result == "web/https/example.com"

    def test_trailing_slash(self) -> None:
        """末尾 / が除去されること（ディレクトリ扱い回避）."""
        result = url_to_path("https://example.com/path/")
        assert result == "web/https/example.com/path"

    def test_trailing_slash_nested(self) -> None:
        """ネストしたパスの末尾 / が除去されること."""
        result = url_to_path("https://example.com/docs/guide/")
        assert result == "web/https/example.com/docs/guide"

    def test_trailing_slash_same_as_no_slash(self) -> None:
        """末尾 / ありとなしで同一パスになること."""
        assert url_to_path("https://example.com/path/") == url_to_path(
            "https://example.com/path"
        )

    def test_multiple_query_params(self) -> None:
        result = url_to_path("https://example.com/search?q=test&page=2")
        assert result == "web/https/example.com/search\uff1fq=test&page=2"


class TestPathToUrl:
    """source_store 相対パス → URL 逆変換."""

    def test_basic_https(self) -> None:
        result = path_to_url("web/https/example.com/docs/guide")
        assert result == "https://example.com/docs/guide"

    def test_basic_http(self) -> None:
        result = path_to_url("web/http/example.com/page")
        assert result == "http://example.com/page"

    def test_fullwidth_question_mark(self) -> None:
        """全角？を半角に戻す."""
        result = path_to_url("web/https/example.com/docs/guide\uff1flang=ja")
        assert result == "https://example.com/docs/guide?lang=ja"

    def test_fullwidth_colon(self) -> None:
        """全角：を半角に戻す."""
        result = path_to_url("web/http/localhost\uff1a8080/api/docs")
        assert result == "http://localhost:8080/api/docs"

    def test_not_web_prefix(self) -> None:
        with pytest.raises(ValueError, match="web/ プレフィックスではありません"):
            path_to_url("bluesky/did/post.json")

    def test_invalid_scheme(self) -> None:
        with pytest.raises(ValueError, match="不正なスキーム"):
            path_to_url("web/ftp/example.com/file")


class TestRoundTrip:
    """URL → パス → URL の往復変換."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/docs/guide",
            "http://localhost:8080/api/docs",
            "https://example.com/search?q=test&page=2",
            "https://example.com/path/to/page",
        ],
    )
    def test_roundtrip(self, url: str) -> None:
        """URL → パス → URL で元に戻る."""
        path = url_to_path(url)
        restored = path_to_url(path)
        assert restored == url

    def test_roundtrip_fragment_stripped(self) -> None:
        """フラグメント付き URL は往復でフラグメントが消える."""
        url = "https://example.com/page#section"
        path = url_to_path(url)
        restored = path_to_url(path)
        assert restored == "https://example.com/page"

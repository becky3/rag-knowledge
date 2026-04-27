"""path_filter ユーティリティのテスト.

仕様: docs/specs/rebuild-stats.md / docs/specs/pipeline-controller.md
"""

from __future__ import annotations

import pytest

from rag.store.path_filter import (
    PathFilterError,
    derive_source_type,
    escape_like,
    normalize_path_prefix,
)


class TestNormalizePathPrefix:
    """normalize_path_prefix の正規化・バリデーション."""

    def test_returns_stripped_posix_path(self) -> None:
        assert normalize_path_prefix("local/foo") == "local/foo"
        assert normalize_path_prefix("local/foo/") == "local/foo"
        assert normalize_path_prefix("local\\foo") == "local/foo"

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(PathFilterError):
            normalize_path_prefix("")

    def test_rejects_only_slashes(self) -> None:
        """`/` や `//` は strip 後に空文字列となるため拒否."""
        with pytest.raises(PathFilterError):
            normalize_path_prefix("/")
        with pytest.raises(PathFilterError):
            normalize_path_prefix("//")

    @pytest.mark.parametrize(
        "bad_path",
        ["..", "../foo", "local/../etc", "local/.."],
    )
    def test_rejects_parent_traversal(self, bad_path: str) -> None:
        with pytest.raises(PathFilterError):
            normalize_path_prefix(bad_path)

    @pytest.mark.parametrize(
        "bad_path",
        [".", "./foo", "local/./bar"],
    )
    def test_rejects_current_dir_segment(self, bad_path: str) -> None:
        with pytest.raises(PathFilterError):
            normalize_path_prefix(bad_path)

    @pytest.mark.parametrize(
        "bad_path",
        ["local//foo", "local///foo", "local/foo//bar"],
    )
    def test_rejects_consecutive_slashes(self, bad_path: str) -> None:
        """連続スラッシュは下流で別文字列扱いになるため拒否 (#694 review)."""
        with pytest.raises(PathFilterError):
            normalize_path_prefix(bad_path)

    @pytest.mark.parametrize(
        "bad_path",
        ["unknown_root/foo", "Local/foo", "tmp/foo"],
    )
    def test_rejects_unknown_root_source_type(self, bad_path: str) -> None:
        """先頭セグメントが既知の source_type でない path は拒否."""
        with pytest.raises(PathFilterError, match="source_store"):
            normalize_path_prefix(bad_path)

    @pytest.mark.parametrize(
        "ok_path",
        ["web", "bluesky/foo", "zenn/user/articles", "local/sub/x.md"],
    )
    def test_accepts_known_source_type_roots(self, ok_path: str) -> None:
        normalize_path_prefix(ok_path)


class TestDeriveSourceType:
    """derive_source_type は path の先頭セグメントを返す."""

    def test_returns_first_segment(self) -> None:
        assert derive_source_type("local/foo/bar.md") == "local"
        assert derive_source_type("bluesky") == "bluesky"

    @pytest.mark.parametrize(
        "bad_path",
        ["", "/web/foo", "unknown_root/foo"],
    )
    def test_rejects_unnormalized_input(self, bad_path: str) -> None:
        """正規化済みでない入力は契約違反として PathFilterError を送出."""
        with pytest.raises(PathFilterError, match="正規化済み"):
            derive_source_type(bad_path)


class TestEscapeLike:
    """escape_like の SQLite LIKE メタ文字エスケープ."""

    def test_escapes_percent(self) -> None:
        assert escape_like("a%b") == r"a\%b"

    def test_escapes_underscore(self) -> None:
        assert escape_like("a_b") == r"a\_b"

    def test_escapes_backslash(self) -> None:
        assert escape_like("a\\b") == r"a\\b"

    def test_passthrough_normal_chars(self) -> None:
        assert escape_like("local/unity-docs") == "local/unity-docs"

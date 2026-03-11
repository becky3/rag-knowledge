"""LocalFileIngester のテスト

仕様: docs/specs/rag-knowledge.md (LocalFileIngester セクション)
Issue: #98

テスト方針:
- バリデーションテスト（正常パス、許可外パス、パストラバーサル試行、未対応拡張子）
- fetch_single テスト（正常読み込み、UTF-8 デコードエラー、空ファイル、権限なし）
- discover テスト（glob パターン、サブディレクトリ、対象 0 件）
- セキュリティテスト（.. エスケープ、シンボリックリンク経由のエスケープ、許可ディレクトリ未設定時の拒否）
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from rag.ingesters.local_file import ALLOWED_EXTENSIONS, LocalFileIngester


@pytest.fixture
def tmp_allowed_dir(tmp_path: Path) -> Path:
    """許可ディレクトリとしての一時ディレクトリを作成する."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    return allowed


@pytest.fixture
def ingester(tmp_allowed_dir: Path) -> LocalFileIngester:
    """許可ディレクトリ付き LocalFileIngester を作成する."""
    return LocalFileIngester(allowed_dirs=[str(tmp_allowed_dir)])


@pytest.fixture
def ingester_no_dirs() -> LocalFileIngester:
    """許可ディレクトリ未設定の LocalFileIngester を作成する."""
    return LocalFileIngester()


# --- バリデーションテスト ---


class TestValidateIdentifier:
    """validate_identifier() のテスト."""

    def test_valid_md_file(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """正常な .md ファイルが検証を通過すること."""
        md_file = tmp_allowed_dir / "test.md"
        md_file.write_text("# Hello", encoding="utf-8")

        result = ingester.validate_identifier(str(md_file))
        assert result == str(md_file.resolve())

    def test_valid_txt_file(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """正常な .txt ファイルが検証を通過すること."""
        txt_file = tmp_allowed_dir / "test.txt"
        txt_file.write_text("Hello", encoding="utf-8")

        result = ingester.validate_identifier(str(txt_file))
        assert result == str(txt_file.resolve())

    def test_disallowed_directory(
        self, ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """許可外ディレクトリのファイルで ValueError が発生すること."""
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_file = outside / "test.md"
        outside_file.write_text("Content", encoding="utf-8")

        with pytest.raises(ValueError, match="許可されていないディレクトリ"):
            ingester.validate_identifier(str(outside_file))

    def test_path_traversal_dotdot(
        self, ingester: LocalFileIngester, tmp_path: Path, tmp_allowed_dir: Path
    ) -> None:
        """.. によるパストラバーサルが拒否されること."""
        # tmp_allowed_dir の親に別のファイルを作成
        outside_file = tmp_path / "secret.md"
        outside_file.write_text("Secret", encoding="utf-8")

        traversal_path = str(tmp_allowed_dir / ".." / "secret.md")
        with pytest.raises(ValueError, match="許可されていないディレクトリ"):
            ingester.validate_identifier(traversal_path)

    def test_file_not_found(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """存在しないファイルで ValueError が発生すること."""
        nonexistent = tmp_allowed_dir / "nonexistent.md"

        with pytest.raises(ValueError, match="ファイルが存在しません"):
            ingester.validate_identifier(str(nonexistent))

    def test_directory_rejected(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """ディレクトリが通常ファイルではないとして拒否されること."""
        subdir = tmp_allowed_dir / "subdir"
        subdir.mkdir()

        with pytest.raises(ValueError, match="通常ファイルではありません"):
            ingester.validate_identifier(str(subdir))

    def test_unsupported_extension(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """未対応拡張子で ValueError が発生すること."""
        py_file = tmp_allowed_dir / "test.py"
        py_file.write_text("print('hello')", encoding="utf-8")

        with pytest.raises(ValueError, match="未対応の拡張子"):
            ingester.validate_identifier(str(py_file))

    def test_no_allowed_dirs_fails(
        self, ingester_no_dirs: LocalFileIngester, tmp_path: Path
    ) -> None:
        """許可ディレクトリ未設定時に ValueError が発生すること（フェイルクローズ）."""
        md_file = tmp_path / "test.md"
        md_file.write_text("Content", encoding="utf-8")

        with pytest.raises(ValueError, match="許可ディレクトリ"):
            ingester_no_dirs.validate_identifier(str(md_file))


# --- セキュリティテスト ---


class TestSecurityConstraints:
    """セキュリティ制約のテスト."""

    def test_symlink_escape_rejected(
        self,
        ingester: LocalFileIngester,
        tmp_path: Path,
        tmp_allowed_dir: Path,
    ) -> None:
        """シンボリックリンク経由の許可ディレクトリ外エスケープが拒否されること."""
        # 許可外にファイルを作成
        outside = tmp_path / "outside"
        outside.mkdir()
        secret_file = outside / "secret.md"
        secret_file.write_text("Secret", encoding="utf-8")

        # 許可ディレクトリ内にシンボリックリンクを作成
        symlink = tmp_allowed_dir / "link.md"
        symlink.symlink_to(secret_file)

        with pytest.raises(ValueError, match="許可されていないディレクトリ"):
            ingester.validate_identifier(str(symlink))

    def test_symlink_within_allowed_dir(
        self,
        ingester: LocalFileIngester,
        tmp_allowed_dir: Path,
    ) -> None:
        """許可ディレクトリ内のシンボリックリンクは許可されること."""
        real_file = tmp_allowed_dir / "real.md"
        real_file.write_text("Content", encoding="utf-8")

        symlink = tmp_allowed_dir / "link.md"
        symlink.symlink_to(real_file)

        result = ingester.validate_identifier(str(symlink))
        assert result == str(real_file.resolve())

    def test_broken_symlink_rejected(
        self,
        ingester: LocalFileIngester,
        tmp_allowed_dir: Path,
    ) -> None:
        """リンク切れシンボリックリンクが拒否されること."""
        target = tmp_allowed_dir / "nonexistent.md"
        symlink = tmp_allowed_dir / "broken_link.md"
        symlink.symlink_to(target)

        with pytest.raises(ValueError, match="ファイルが存在しません"):
            ingester.validate_identifier(str(symlink))

    def test_fail_close_no_dirs(self, ingester_no_dirs: LocalFileIngester) -> None:
        """許可ディレクトリ未設定時にフェイルクローズすること."""
        with pytest.raises(ValueError, match="許可ディレクトリ"):
            ingester_no_dirs.validate_identifier("/any/path/file.md")

    def test_fail_close_empty_dirs(self) -> None:
        """空の許可ディレクトリリストでフェイルクローズすること."""
        ingester = LocalFileIngester(allowed_dirs=[])
        with pytest.raises(ValueError, match="許可ディレクトリ"):
            ingester.validate_identifier("/any/path/file.md")


# --- fetch_single テスト ---


class TestFetchSingle:
    """fetch_single() のテスト."""

    async def test_fetch_md_file(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """正常な .md ファイルを読み込めること."""
        md_file = tmp_allowed_dir / "document.md"
        md_file.write_text("# Title\n\nContent here.", encoding="utf-8")

        result = await ingester.fetch_single(str(md_file))

        assert result is not None
        assert result.source_id == str(md_file.resolve())
        assert result.title == "document"
        assert result.text == "# Title\n\nContent here."
        assert result.source_type == "local_file"
        assert result.ingested_at  # ISO 8601 文字列であること

    async def test_fetch_txt_file(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """正常な .txt ファイルを読み込めること."""
        txt_file = tmp_allowed_dir / "notes.txt"
        txt_file.write_text("Plain text content.", encoding="utf-8")

        result = await ingester.fetch_single(str(txt_file))

        assert result is not None
        assert result.title == "notes"
        assert result.text == "Plain text content."

    async def test_fetch_empty_file(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """空ファイルが空テキストとして取り込まれること."""
        empty_file = tmp_allowed_dir / "empty.md"
        empty_file.write_text("", encoding="utf-8")

        result = await ingester.fetch_single(str(empty_file))

        assert result is not None
        assert result.text == ""

    async def test_fetch_utf8_decode_error(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """UTF-8 デコードエラー時に None を返すこと."""
        binary_file = tmp_allowed_dir / "binary.txt"
        binary_file.write_bytes(b"\xff\xfe\x00\x01\x80\x81\x82")

        result = await ingester.fetch_single(str(binary_file))
        assert result is None

    async def test_fetch_permission_denied(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """読み取り権限がない場合に None を返すこと."""
        no_read_file = tmp_allowed_dir / "no_read.md"
        no_read_file.write_text("Content", encoding="utf-8")
        no_read_file.chmod(0o000)

        try:
            result = await ingester.fetch_single(str(no_read_file))
            assert result is None
        finally:
            # テスト後にクリーンアップのため権限を戻す
            no_read_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

    async def test_fetch_validation_error_raises(
        self, ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """バリデーションエラー時に ValueError が raise されること."""
        outside_file = tmp_path / "outside.md"
        outside_file.write_text("Content", encoding="utf-8")

        with pytest.raises(ValueError):
            await ingester.fetch_single(str(outside_file))


# --- discover テスト ---


class TestDiscover:
    """discover() のテスト."""

    async def test_discover_default_pattern(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """デフォルトパターンでディレクトリ直下のファイルを発見すること."""
        (tmp_allowed_dir / "a.md").write_text("A", encoding="utf-8")
        (tmp_allowed_dir / "b.txt").write_text("B", encoding="utf-8")
        (tmp_allowed_dir / "c.py").write_text("C", encoding="utf-8")  # 未対応

        result = await ingester.discover(str(tmp_allowed_dir))

        # .md と .txt のみ
        assert len(result) == 2
        filenames = [Path(p).name for p in result]
        assert "a.md" in filenames
        assert "b.txt" in filenames
        assert "c.py" not in filenames

    async def test_discover_with_glob_pattern(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """glob パターン指定でファイルを発見すること."""
        subdir = tmp_allowed_dir / "sub"
        subdir.mkdir()
        (subdir / "doc.md").write_text("Doc", encoding="utf-8")
        (tmp_allowed_dir / "top.md").write_text("Top", encoding="utf-8")

        result = await ingester.discover(
            str(tmp_allowed_dir), pattern="**/*.md"
        )

        assert len(result) == 2
        filenames = [Path(p).name for p in result]
        assert "doc.md" in filenames
        assert "top.md" in filenames

    async def test_discover_subdirectory_not_included_by_default(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """デフォルトではサブディレクトリ内のファイルが含まれないこと."""
        subdir = tmp_allowed_dir / "sub"
        subdir.mkdir()
        (subdir / "nested.md").write_text("Nested", encoding="utf-8")
        (tmp_allowed_dir / "top.md").write_text("Top", encoding="utf-8")

        result = await ingester.discover(str(tmp_allowed_dir))

        assert len(result) == 1
        assert Path(result[0]).name == "top.md"

    async def test_discover_zero_matches(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """対象が 0 件の場合に空リストを返すこと."""
        result = await ingester.discover(str(tmp_allowed_dir))
        assert result == []

    async def test_discover_outside_allowed_dir(
        self, ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """許可外ディレクトリで discover すると ValueError が発生すること."""
        outside = tmp_path / "outside"
        outside.mkdir()

        with pytest.raises(ValueError, match="許可されていないディレクトリ"):
            await ingester.discover(str(outside))

    async def test_discover_nonexistent_directory(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """存在しないディレクトリで discover すると ValueError が発生すること."""
        nonexistent = tmp_allowed_dir / "nonexistent"

        with pytest.raises(ValueError, match="ディレクトリが存在しません"):
            await ingester.discover(str(nonexistent))

    async def test_discover_no_allowed_dirs(
        self, ingester_no_dirs: LocalFileIngester, tmp_path: Path
    ) -> None:
        """許可ディレクトリ未設定時にフェイルクローズすること."""
        with pytest.raises(ValueError, match="許可ディレクトリ"):
            await ingester_no_dirs.discover(str(tmp_path))

    async def test_discover_filters_unsupported_extensions(
        self, ingester: LocalFileIngester, tmp_allowed_dir: Path
    ) -> None:
        """glob パターンで発見されても未対応拡張子はフィルタされること."""
        (tmp_allowed_dir / "doc.md").write_text("Doc", encoding="utf-8")
        (tmp_allowed_dir / "script.py").write_text("Script", encoding="utf-8")

        result = await ingester.discover(
            str(tmp_allowed_dir), pattern="*.*"
        )

        assert len(result) == 1
        assert Path(result[0]).name == "doc.md"


# --- 複数許可ディレクトリテスト ---


class TestMultipleAllowedDirs:
    """複数の許可ディレクトリのテスト."""

    async def test_multiple_allowed_dirs(self, tmp_path: Path) -> None:
        """複数の許可ディレクトリが正しく動作すること."""
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()

        (dir1 / "file1.md").write_text("Content 1", encoding="utf-8")
        (dir2 / "file2.md").write_text("Content 2", encoding="utf-8")

        ingester = LocalFileIngester(allowed_dirs=[str(dir1), str(dir2)])

        result1 = await ingester.fetch_single(str(dir1 / "file1.md"))
        result2 = await ingester.fetch_single(str(dir2 / "file2.md"))

        assert result1 is not None
        assert result1.text == "Content 1"
        assert result2 is not None
        assert result2.text == "Content 2"


# --- ALLOWED_EXTENSIONS 定数テスト ---


class TestAllowedExtensions:
    """ALLOWED_EXTENSIONS 定数のテスト."""

    def test_md_is_allowed(self) -> None:
        """.md が許可されていること."""
        assert ".md" in ALLOWED_EXTENSIONS

    def test_txt_is_allowed(self) -> None:
        """.txt が許可されていること."""
        assert ".txt" in ALLOWED_EXTENSIONS

    def test_py_is_not_allowed(self) -> None:
        """.py が許可されていないこと."""
        assert ".py" not in ALLOWED_EXTENSIONS

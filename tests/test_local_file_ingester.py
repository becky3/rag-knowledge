"""LocalFileIngester のテスト.

仕様: docs/specs/rag-knowledge.md（LocalFileIngester セクション）
Issue: #98

テスト方針:
- バリデーションテスト（正常パス、許可外パス、パストラバーサル試行、未対応拡張子）
- fetch_single テスト（正常読み込み、UTF-8 デコードエラー、空ファイル、権限なし）
- discover テスト（glob パターン、サブディレクトリ、対象 0 件）
- セキュリティテスト（.. エスケープ、シンボリックリンク経由のエスケープ、許可ディレクトリ未設定時の拒否）
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from rag.ingesters.local_file import LocalFileIngester


@pytest.fixture
def allowed_dir(tmp_path: Path) -> Path:
    """テスト用の許可ディレクトリを作成する."""
    d = tmp_path / "allowed"
    d.mkdir()
    return d


@pytest.fixture
def ingester(allowed_dir: Path) -> LocalFileIngester:
    """許可ディレクトリ付きの LocalFileIngester を返す."""
    return LocalFileIngester(allowed_dirs=[str(allowed_dir)])


# --- バリデーションテスト ---


class TestValidateIdentifier:
    """validate_identifier のテスト."""

    def test_valid_md_file(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """正常な .md ファイルが検証を通過すること."""
        f = allowed_dir / "test.md"
        f.write_text("# Hello", encoding="utf-8")

        result = ingester.validate_identifier(str(f))
        assert result == str(f.resolve())

    def test_valid_txt_file(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """正常な .txt ファイルが検証を通過すること."""
        f = allowed_dir / "notes.txt"
        f.write_text("Some notes", encoding="utf-8")

        result = ingester.validate_identifier(str(f))
        assert result == str(f.resolve())

    def test_empty_path_raises(self, ingester: LocalFileIngester) -> None:
        """空パスで ValueError が発生すること."""
        with pytest.raises(ValueError, match="ファイルパスが空です"):
            ingester.validate_identifier("")

    def test_whitespace_only_path_raises(
        self, ingester: LocalFileIngester
    ) -> None:
        """空白のみのパスで ValueError が発生すること."""
        with pytest.raises(ValueError, match="ファイルパスが空です"):
            ingester.validate_identifier("   ")

    def test_nonexistent_file_raises(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """存在しないファイルで ValueError が発生すること."""
        f = allowed_dir / "missing.md"
        with pytest.raises(ValueError, match="ファイルが存在しません"):
            ingester.validate_identifier(str(f))

    def test_unsupported_extension_raises(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """未対応拡張子で ValueError が発生すること."""
        f = allowed_dir / "file.py"
        f.write_text("print('hello')", encoding="utf-8")

        with pytest.raises(ValueError, match="未対応の拡張子です"):
            ingester.validate_identifier(str(f))

    def test_directory_path_raises(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """ディレクトリパスで ValueError が発生すること."""
        d = allowed_dir / "subdir"
        d.mkdir()

        with pytest.raises(ValueError, match="通常ファイルではありません"):
            ingester.validate_identifier(str(d))

    def test_outside_allowed_dir_raises(
        self, ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """許可ディレクトリ外のファイルで ValueError が発生すること."""
        outside = tmp_path / "outside"
        outside.mkdir()
        f = outside / "secret.md"
        f.write_text("secret", encoding="utf-8")

        with pytest.raises(ValueError, match="許可ディレクトリ外"):
            ingester.validate_identifier(str(f))


# --- セキュリティテスト ---


class TestSecurityConstraints:
    """セキュリティ制約のテスト."""

    def test_path_traversal_with_dotdot(
        self, ingester: LocalFileIngester, tmp_path: Path, allowed_dir: Path
    ) -> None:
        """.. を使ったパストラバーサルが拒否されること."""
        outside = tmp_path / "secret.md"
        outside.write_text("secret data", encoding="utf-8")

        # allowed_dir から .. で親ディレクトリにエスケープ
        traversal_path = str(allowed_dir / ".." / "secret.md")
        with pytest.raises(ValueError, match="許可ディレクトリ外"):
            ingester.validate_identifier(traversal_path)

    def test_symlink_escape_outside_allowed_dir(
        self, ingester: LocalFileIngester, tmp_path: Path, allowed_dir: Path
    ) -> None:
        """シンボリックリンク経由での許可ディレクトリ外エスケープが拒否されること."""
        # 許可ディレクトリ外にファイルを作成
        outside = tmp_path / "outside_secret.md"
        outside.write_text("secret via symlink", encoding="utf-8")

        # 許可ディレクトリ内にシンボリックリンクを作成
        link = allowed_dir / "link.md"
        link.symlink_to(outside)

        with pytest.raises(ValueError, match="許可ディレクトリ外"):
            ingester.validate_identifier(str(link))

    def test_symlink_within_allowed_dir_succeeds(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """許可ディレクトリ内のシンボリックリンクは通過すること."""
        target = allowed_dir / "real.md"
        target.write_text("real content", encoding="utf-8")

        link = allowed_dir / "link.md"
        link.symlink_to(target)

        result = ingester.validate_identifier(str(link))
        assert result == str(target.resolve())

    def test_no_allowed_dirs_rejects_all(self, tmp_path: Path) -> None:
        """許可ディレクトリ未設定時に全ファイルを拒否すること（フェイルクローズ）."""
        ingester = LocalFileIngester(allowed_dirs=None)

        f = tmp_path / "file.md"
        f.write_text("content", encoding="utf-8")

        with pytest.raises(ValueError, match="許可ディレクトリが設定されていません"):
            ingester.validate_identifier(str(f))

    def test_empty_allowed_dirs_rejects_all(self, tmp_path: Path) -> None:
        """空の許可ディレクトリリストで全ファイルを拒否すること."""
        ingester = LocalFileIngester(allowed_dirs=[])

        f = tmp_path / "file.md"
        f.write_text("content", encoding="utf-8")

        with pytest.raises(ValueError, match="許可ディレクトリが設定されていません"):
            ingester.validate_identifier(str(f))

    def test_broken_symlink_raises(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """リンク切れシンボリックリンクでエラーが発生すること."""
        link = allowed_dir / "broken.md"
        link.symlink_to(allowed_dir / "nonexistent.md")

        with pytest.raises(ValueError, match="ファイルが存在しません"):
            ingester.validate_identifier(str(link))

    def test_multiple_allowed_dirs(self, tmp_path: Path) -> None:
        """複数の許可ディレクトリが正しく機能すること."""
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()

        f1 = dir1 / "file1.md"
        f1.write_text("content1", encoding="utf-8")
        f2 = dir2 / "file2.txt"
        f2.write_text("content2", encoding="utf-8")

        ingester = LocalFileIngester(allowed_dirs=[str(dir1), str(dir2)])

        assert ingester.validate_identifier(str(f1)) == str(f1.resolve())
        assert ingester.validate_identifier(str(f2)) == str(f2.resolve())


# --- fetch_single テスト ---


class TestFetchSingle:
    """fetch_single のテスト."""

    @pytest.mark.asyncio
    async def test_fetch_md_file(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """Markdown ファイルを正常に読み込めること."""
        f = allowed_dir / "document.md"
        f.write_text("# Title\n\nContent here", encoding="utf-8")

        result = await ingester.fetch_single(str(f))

        assert result is not None
        assert result.text == "# Title\n\nContent here"
        assert result.title == "document"
        assert result.source_type == "local_file"
        assert result.source_id == str(f.resolve())

    @pytest.mark.asyncio
    async def test_fetch_txt_file(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """テキストファイルを正常に読み込めること."""
        f = allowed_dir / "notes.txt"
        f.write_text("Plain text notes", encoding="utf-8")

        result = await ingester.fetch_single(str(f))

        assert result is not None
        assert result.text == "Plain text notes"
        assert result.title == "notes"

    @pytest.mark.asyncio
    async def test_fetch_empty_file(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """空ファイルは空テキストとして取り込むこと."""
        f = allowed_dir / "empty.md"
        f.write_text("", encoding="utf-8")

        result = await ingester.fetch_single(str(f))

        assert result is not None
        assert result.text == ""
        assert result.title == "empty"

    @pytest.mark.asyncio
    async def test_fetch_unicode_decode_error(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """UTF-8 デコードエラー時に None を返すこと."""
        f = allowed_dir / "binary.md"
        f.write_bytes(b"\x80\x81\x82\xff\xfe")

        result = await ingester.fetch_single(str(f))
        assert result is None

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission test")
    async def test_fetch_permission_error(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """読み取り権限がないファイルで None を返すこと."""
        f = allowed_dir / "noperm.md"
        f.write_text("content", encoding="utf-8")
        f.chmod(0o000)

        try:
            result = await ingester.fetch_single(str(f))
            assert result is None
        finally:
            # テスト後にクリーンアップできるよう権限を戻す
            f.chmod(stat.S_IRUSR | stat.S_IWUSR)

    @pytest.mark.asyncio
    async def test_fetch_validation_error_raises(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """存在しないファイルで ValueError が発生すること."""
        with pytest.raises(ValueError):
            await ingester.fetch_single(str(allowed_dir / "missing.md"))

    @pytest.mark.asyncio
    async def test_fetch_unsupported_extension_raises(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """未対応拡張子で ValueError が発生すること."""
        f = allowed_dir / "script.py"
        f.write_text("print()", encoding="utf-8")

        with pytest.raises(ValueError, match="未対応の拡張子です"):
            await ingester.fetch_single(str(f))


# --- discover テスト ---


class TestDiscover:
    """discover のテスト."""

    @pytest.mark.asyncio
    async def test_discover_default_pattern(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """デフォルトパターンで直下の対応拡張子ファイルを発見すること."""
        (allowed_dir / "file1.md").write_text("md", encoding="utf-8")
        (allowed_dir / "file2.txt").write_text("txt", encoding="utf-8")
        (allowed_dir / "file3.py").write_text("py", encoding="utf-8")

        result = await ingester.discover(str(allowed_dir))

        assert len(result) == 2
        names = [Path(p).name for p in result]
        assert "file1.md" in names
        assert "file2.txt" in names
        assert "file3.py" not in names

    @pytest.mark.asyncio
    async def test_discover_with_glob_pattern(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """glob パターンで特定ファイルを発見すること."""
        (allowed_dir / "doc1.md").write_text("doc1", encoding="utf-8")
        (allowed_dir / "doc2.md").write_text("doc2", encoding="utf-8")
        (allowed_dir / "notes.txt").write_text("notes", encoding="utf-8")

        result = await ingester.discover(str(allowed_dir), pattern="*.md")

        assert len(result) == 2
        names = [Path(p).name for p in result]
        assert "doc1.md" in names
        assert "doc2.md" in names

    @pytest.mark.asyncio
    async def test_discover_recursive_pattern(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """** パターンでサブディレクトリのファイルも発見すること."""
        sub = allowed_dir / "sub"
        sub.mkdir()
        (allowed_dir / "root.md").write_text("root", encoding="utf-8")
        (sub / "nested.md").write_text("nested", encoding="utf-8")

        result = await ingester.discover(
            str(allowed_dir), pattern="**/*.md"
        )

        assert len(result) == 2
        names = [Path(p).name for p in result]
        assert "root.md" in names
        assert "nested.md" in names

    @pytest.mark.asyncio
    async def test_discover_no_matches(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """対象が 0 件のときに空リストを返すこと."""
        (allowed_dir / "script.py").write_text("py", encoding="utf-8")

        result = await ingester.discover(str(allowed_dir))

        assert result == []

    @pytest.mark.asyncio
    async def test_discover_empty_directory(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """空ディレクトリで空リストを返すこと."""
        result = await ingester.discover(str(allowed_dir))
        assert result == []

    @pytest.mark.asyncio
    async def test_discover_nonexistent_directory(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """存在しないディレクトリで空リストを返すこと."""
        result = await ingester.discover(
            str(allowed_dir / "nonexistent")
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_discover_outside_allowed_dir_raises(
        self, ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """許可ディレクトリ外のディレクトリで ValueError が発生すること."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "file.md").write_text("content", encoding="utf-8")

        with pytest.raises(ValueError, match="許可ディレクトリ外"):
            await ingester.discover(str(outside))

    @pytest.mark.asyncio
    async def test_discover_empty_source(
        self, ingester: LocalFileIngester
    ) -> None:
        """空のソースパスで空リストを返すこと."""
        result = await ingester.discover("")
        assert result == []

    @pytest.mark.asyncio
    async def test_discover_no_allowed_dirs_raises(
        self, tmp_path: Path
    ) -> None:
        """許可ディレクトリ未設定時に ValueError が発生すること."""
        ingester = LocalFileIngester(allowed_dirs=None)
        d = tmp_path / "some_dir"
        d.mkdir()

        with pytest.raises(ValueError, match="許可ディレクトリが設定されていません"):
            await ingester.discover(str(d))

    @pytest.mark.asyncio
    async def test_discover_filters_unsupported_extensions(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """glob パターンで未対応拡張子がフィルタされること."""
        (allowed_dir / "valid.md").write_text("md", encoding="utf-8")
        (allowed_dir / "invalid.py").write_text("py", encoding="utf-8")

        result = await ingester.discover(str(allowed_dir), pattern="*")

        # .py はバリデーションで除外される
        assert len(result) == 1
        assert Path(result[0]).name == "valid.md"


# --- サーバー統合テスト ---


class TestServerRegistration:
    """server.py でのインジェスター登録テスト."""

    def test_local_file_ingester_import(self) -> None:
        """LocalFileIngester がインポート可能であること."""
        from rag.ingesters import LocalFileIngester

        assert LocalFileIngester is not None

    def test_local_file_ingester_initialization(self) -> None:
        """LocalFileIngester が許可ディレクトリ付きで初期化できること."""
        ingester = LocalFileIngester(allowed_dirs=["/tmp"])
        assert len(ingester._allowed_dirs) == 1  # noqa: SLF001


# --- エッジケーステスト ---


class TestEdgeCases:
    """エッジケースのテスト."""

    def test_allowed_dirs_with_whitespace_entries(self) -> None:
        """空白を含む allowed_dirs エントリがトリミングされること."""
        ingester = LocalFileIngester(
            allowed_dirs=["  /tmp  ", "", "  "]
        )
        assert len(ingester._allowed_dirs) == 1  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_fetch_japanese_content(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """日本語テキストを正常に読み込めること."""
        f = allowed_dir / "japanese.md"
        f.write_text("# 日本語ドキュメント\n\nこれはテストです。", encoding="utf-8")

        result = await ingester.fetch_single(str(f))

        assert result is not None
        assert "日本語ドキュメント" in result.text
        assert result.title == "japanese"

    @pytest.mark.asyncio
    async def test_fetch_large_file(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """大きなファイルを読み込めること（サイズ上限なし）."""
        f = allowed_dir / "large.md"
        content = "# Large Document\n\n" + ("x" * 100000)
        f.write_text(content, encoding="utf-8")

        result = await ingester.fetch_single(str(f))

        assert result is not None
        assert len(result.text) > 100000

    def test_case_insensitive_extension(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """拡張子の大文字小文字を区別しないこと."""
        f = allowed_dir / "file.MD"
        f.write_text("content", encoding="utf-8")

        result = ingester.validate_identifier(str(f))
        assert result == str(f.resolve())

    @pytest.mark.asyncio
    async def test_discover_symlink_in_dir(
        self, ingester: LocalFileIngester, allowed_dir: Path, tmp_path: Path
    ) -> None:
        """discover でシンボリックリンクが許可ディレクトリ外を指す場合はスキップされること."""
        # 許可ディレクトリ外のファイル
        outside = tmp_path / "outside_file.md"
        outside.write_text("outside", encoding="utf-8")

        # 許可ディレクトリ内に外部を指すシンボリックリンク
        link = allowed_dir / "link_outside.md"
        link.symlink_to(outside)

        # 正規のファイルも配置
        (allowed_dir / "valid.md").write_text("valid", encoding="utf-8")

        result = await ingester.discover(str(allowed_dir))

        # シンボリックリンクはフィルタされ、正規のファイルのみ
        assert len(result) == 1
        assert Path(result[0]).name == "valid.md"

    @pytest.mark.asyncio
    async def test_discover_dotdot_pattern_rejected(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """discover で .. を含む glob パターンが拒否されること."""
        (allowed_dir / "file.md").write_text("content", encoding="utf-8")

        with pytest.raises(ValueError, match="glob パターンに '..' は使用できません"):
            await ingester.discover(str(allowed_dir), pattern="../../**/*.md")

    @pytest.mark.asyncio
    async def test_discover_source_with_dotdot_resolves_correctly(
        self, ingester: LocalFileIngester, allowed_dir: Path
    ) -> None:
        """discover のソースパスに .. が含まれても正規化後に許可チェックが行われること."""
        sub = allowed_dir / "sub"
        sub.mkdir()
        (allowed_dir / "file.md").write_text("content", encoding="utf-8")

        # allowed_dir/sub/.. は allowed_dir に解決される（許可ディレクトリ内）
        result = await ingester.discover(str(sub / ".."))
        assert len(result) == 1

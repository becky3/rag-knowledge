"""git_ops モジュールのテスト.

テスト方針:
- git init / commit / diff / list_all_files の基本動作
- .gitignore の自動生成
- リネーム検出
- コミットなし時の has_commits / is_commit_valid
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.pipeline.git_ops import GitOperations


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """空の git リポジトリを持つ一時ディレクトリ."""
    repo = tmp_path / "source_store"
    repo.mkdir()
    return repo


class TestInitRepo:
    """init_repo のテスト."""

    def test_creates_git_dir(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        assert (git_repo / ".git").exists()

    def test_creates_gitignore(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        gitignore = git_repo / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text(encoding="utf-8")
        assert "metadata.db" in content
        assert "metadata.db-wal" in content
        assert "metadata.db-shm" in content

    def test_idempotent(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        ops.init_repo()
        assert (git_repo / ".git").exists()

    def test_preserves_existing_gitignore_and_appends(
        self, git_repo: Path,
    ) -> None:
        ops = GitOperations(git_repo)
        gitignore = git_repo / ".gitignore"
        gitignore.write_text("custom\n", encoding="utf-8")
        ops.init_repo()
        content = gitignore.read_text(encoding="utf-8")
        # 既存内容を保持しつつ metadata.db を追記
        assert content.startswith("custom\n")
        assert "metadata.db" in content

    def test_existing_repo_gets_gitignore_fixed(
        self, git_repo: Path,
    ) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        # .gitignore から metadata.db を除去
        gitignore = git_repo / ".gitignore"
        gitignore.write_text("*.tmp\n", encoding="utf-8")
        # 新インスタンスで init_repo → 補正される
        ops2 = GitOperations(git_repo)
        ops2.init_repo()
        content = gitignore.read_text(encoding="utf-8")
        assert "*.tmp" in content
        assert "metadata.db" in content

    def test_existing_repo_with_valid_gitignore_unchanged(
        self, git_repo: Path,
    ) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        gitignore = git_repo / ".gitignore"
        original = gitignore.read_text(encoding="utf-8")
        # 新インスタンスで init → 変更なし
        ops2 = GitOperations(git_repo)
        ops2.init_repo()
        assert gitignore.read_text(encoding="utf-8") == original


class TestCommit:
    """commit のテスト."""

    def test_commit_new_file(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        (git_repo / "test.txt").write_text("hello", encoding="utf-8")
        commit_id = ops.commit("test commit")
        assert commit_id is not None
        assert len(commit_id) == 40

    def test_commit_no_changes(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        (git_repo / "test.txt").write_text("hello", encoding="utf-8")
        ops.commit("first commit")
        result = ops.commit("empty commit")
        assert result is None

    def test_commit_modification(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        f = git_repo / "test.txt"
        f.write_text("v1", encoding="utf-8")
        first = ops.commit("first")
        f.write_text("v2", encoding="utf-8")
        second = ops.commit("second")
        assert first != second


class TestHasCommits:
    """has_commits のテスト."""

    def test_no_commits(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        assert ops.has_commits() is False

    def test_with_commit(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        (git_repo / "test.txt").write_text("hello", encoding="utf-8")
        ops.commit("initial")
        assert ops.has_commits() is True


class TestIsCommitValid:
    """is_commit_valid のテスト."""

    def test_valid_commit(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        (git_repo / "test.txt").write_text("hello", encoding="utf-8")
        commit_id = ops.commit("initial")
        assert commit_id is not None
        assert ops.is_commit_valid(commit_id) is True

    def test_invalid_commit(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        (git_repo / "test.txt").write_text("hello", encoding="utf-8")
        ops.commit("initial")
        assert ops.is_commit_valid("0" * 40) is False

    def test_null_commit_hash(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        (git_repo / "test.txt").write_text("hello", encoding="utf-8")
        ops.commit("initial")
        assert ops.is_commit_valid("0" * 40) is False


class TestGetDiff:
    """get_diff のテスト."""

    def test_added_file(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        (git_repo / "a.txt").write_text("first", encoding="utf-8")
        first = ops.commit("first")
        assert first is not None
        (git_repo / "b.txt").write_text("second", encoding="utf-8")
        ops.commit("second")
        diff = ops.get_diff(first)
        assert len(diff) == 1
        assert diff[0] == ("A", "b.txt", "")

    def test_modified_file(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        f = git_repo / "a.txt"
        f.write_text("v1", encoding="utf-8")
        first = ops.commit("first")
        assert first is not None
        f.write_text("v2", encoding="utf-8")
        ops.commit("second")
        diff = ops.get_diff(first)
        assert len(diff) == 1
        assert diff[0] == ("M", "a.txt", "")

    def test_deleted_file(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        f = git_repo / "a.txt"
        f.write_text("content", encoding="utf-8")
        first = ops.commit("first")
        assert first is not None
        f.unlink()
        ops.commit("delete")
        diff = ops.get_diff(first)
        assert len(diff) == 1
        assert diff[0] == ("D", "a.txt", "")

    def test_renamed_file(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        f = git_repo / "old.txt"
        f.write_text("content", encoding="utf-8")
        first = ops.commit("first")
        assert first is not None
        f.rename(git_repo / "new.txt")
        ops.commit("rename")
        diff = ops.get_diff(first)
        assert len(diff) == 1
        status, new_path, old_path = diff[0]
        assert status == "R"
        assert new_path == "new.txt"
        assert old_path == "old.txt"

    def test_no_changes(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        (git_repo / "a.txt").write_text("content", encoding="utf-8")
        commit_id = ops.commit("first")
        assert commit_id is not None
        diff = ops.get_diff(commit_id)
        assert diff == []

    def test_subdirectory_file(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        (git_repo / "a.txt").write_text("init", encoding="utf-8")
        first = ops.commit("first")
        assert first is not None
        sub = git_repo / "web" / "example.com"
        sub.mkdir(parents=True)
        (sub / "page.html").write_text("<html></html>", encoding="utf-8")
        ops.commit("add page")
        diff = ops.get_diff(first)
        assert len(diff) == 1
        assert diff[0] == ("A", "web/example.com/page.html", "")


class TestGetFilesTouchedInRange:
    """get_files_touched_in_range のテスト."""

    def test_returns_all_touched_files(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        (git_repo / "a.txt").write_text("v1", encoding="utf-8")
        first = ops.commit("first")
        assert first is not None
        (git_repo / "b.txt").write_text("new", encoding="utf-8")
        ops.commit("add b")
        (git_repo / "a.txt").write_text("v2", encoding="utf-8")
        ops.commit("modify a")
        touched = ops.get_files_touched_in_range(first)
        assert "a.txt" in touched
        assert "b.txt" in touched

    def test_includes_deleted_then_readded_file(self, git_repo: Path) -> None:
        """削除→同一内容再追加されたファイルが含まれること."""
        ops = GitOperations(git_repo)
        ops.init_repo()
        f = git_repo / "data.txt"
        f.write_text("content", encoding="utf-8")
        first = ops.commit("first")
        assert first is not None
        f.unlink()
        ops.commit("delete")
        f.write_text("content", encoding="utf-8")
        ops.commit("re-add same content")
        # ネット差分はゼロだが touched には含まれる
        diff = ops.get_diff(first)
        assert len(diff) == 0
        touched = ops.get_files_touched_in_range(first)
        assert "data.txt" in touched

    def test_no_changes(self, git_repo: Path) -> None:
        """基準コミット以降に変更がない場合、空集合を返す."""
        ops = GitOperations(git_repo)
        ops.init_repo()
        (git_repo / "a.txt").write_text("content", encoding="utf-8")
        commit_id = ops.commit("first")
        assert commit_id is not None
        touched = ops.get_files_touched_in_range(commit_id)
        assert touched == set()

    def test_deduplicates_same_path_touched_multiple_times(
        self, git_repo: Path,
    ) -> None:
        """同一パスが複数回変更されても1回だけ含まれる."""
        ops = GitOperations(git_repo)
        ops.init_repo()
        target = git_repo / "a.txt"
        target.write_text("v1", encoding="utf-8")
        first = ops.commit("first")
        assert first is not None
        target.write_text("v2", encoding="utf-8")
        ops.commit("modify a once")
        target.write_text("v3", encoding="utf-8")
        ops.commit("modify a twice")
        touched = ops.get_files_touched_in_range(first)
        assert touched == {"a.txt"}


class TestListAllFiles:
    """list_all_files のテスト."""

    def test_lists_tracked_files(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        (git_repo / "a.txt").write_text("a", encoding="utf-8")
        sub = git_repo / "web"
        sub.mkdir()
        (sub / "b.html").write_text("b", encoding="utf-8")
        ops.commit("initial")
        files = ops.list_all_files()
        assert ".gitignore" in files
        assert "a.txt" in files
        assert "web/b.html" in files

    def test_excludes_gitignored(self, git_repo: Path) -> None:
        ops = GitOperations(git_repo)
        ops.init_repo()
        (git_repo / "metadata.db").write_bytes(b"db")
        (git_repo / "a.txt").write_text("a", encoding="utf-8")
        ops.commit("initial")
        files = ops.list_all_files()
        assert "metadata.db" not in files
        assert "a.txt" in files


class TestParseDiffOutput:
    """_parse_diff_output のテスト."""

    def test_parse_add_modify_delete(self) -> None:
        output = "A\tnew.txt\nM\texisting.txt\nD\tremoved.txt\n"
        result = GitOperations._parse_diff_output(output)
        assert result == [
            ("A", "new.txt", ""),
            ("M", "existing.txt", ""),
            ("D", "removed.txt", ""),
        ]

    def test_parse_rename(self) -> None:
        output = "R100\told.txt\tnew.txt\n"
        result = GitOperations._parse_diff_output(output)
        assert result == [("R", "new.txt", "old.txt")]

    def test_parse_empty(self) -> None:
        assert GitOperations._parse_diff_output("") == []
        assert GitOperations._parse_diff_output("\n") == []

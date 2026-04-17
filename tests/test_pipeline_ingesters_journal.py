"""Journal インジェスターのテスト.

仕様: docs/specs/ingesters/journal.md
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from rag.pipeline.ingesters.journal import (
    MAX_FILES_HARD_LIMIT,
    JournalIngester,
    _sanitize_topic,
)
from rag.store.meta import read_meta
from rag.store.source_store import SourceStore


_FIXED_NOW = datetime(2026, 3, 23, 14, 30, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _freeze_datetime(monkeypatch: pytest.MonkeyPatch) -> None:
    """datetime.now() を固定してテストの安定性を確保する."""
    original_datetime = datetime

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:  # type: ignore[override]
            if tz is not None:
                return _FIXED_NOW.astimezone(tz)
            return _FIXED_NOW

        @classmethod
        def fromtimestamp(cls, t: float, tz: timezone | None = None) -> datetime:
            return original_datetime.fromtimestamp(t, tz=tz)

    monkeypatch.setattr(
        "rag.pipeline.ingesters.journal.datetime", FakeDatetime,
    )


@pytest.fixture()
def source_store(tmp_path: Path) -> SourceStore:
    store = SourceStore(tmp_path / "source_store")
    store.initialize()
    return store


@pytest.fixture()
def ingester(source_store: SourceStore) -> JournalIngester:
    return JournalIngester(source_store)


class TestAddEntry:
    """add_entry() の正常系・異常系テスト."""

    def test_add_entry_with_auto_id(
        self, ingester: JournalIngester, source_store: SourceStore,
    ) -> None:
        result = ingester.add_entry(
            title="Session Summary",
            body="# Summary\nToday's work",
            repository="rag-knowledge",
        )
        assert result.placed == 1
        assert result.errors == 0

        # ファイルが配置されていること
        journal_dir = source_store.root_dir / "journal" / "rag-knowledge"
        assert journal_dir.exists()
        md_files = list(journal_dir.glob("*.md"))
        assert len(md_files) == 1
        assert md_files[0].read_text(encoding="utf-8") == "# Summary\nToday's work"

        # entry_id が自動生成されていること（YYYYMMDD-HHMMSS-topic 形式）
        assert md_files[0].stem.startswith("20260323-143000-")

    def test_add_entry_with_explicit_id(
        self, ingester: JournalIngester, source_store: SourceStore,
    ) -> None:
        result = ingester.add_entry(
            title="Test Entry",
            body="Content here",
            repository="my-repo",
            entry_id="20260323-120000-test-entry",
        )
        assert result.placed == 1
        placed = source_store.root_dir / "journal" / "my-repo" / "20260323-120000-test-entry.md"
        assert placed.exists()
        assert placed.read_text(encoding="utf-8") == "Content here"

    def test_meta_file_generated(
        self, ingester: JournalIngester, source_store: SourceStore,
    ) -> None:
        ingester.add_entry(
            title="Meta Test",
            body="Body text",
            repository="test-repo",
            entry_id="20260323-143000-meta-test",
        )
        data_file = (
            source_store.root_dir / "journal" / "test-repo"
            / "20260323-143000-meta-test.md"
        )
        meta = read_meta(data_file)
        assert meta["source_type"] == "journal"
        assert meta["title"] == "Meta Test"
        assert meta["repository"] == "test-repo"
        assert "collected_at" in meta

    def test_overwrite_existing_entry(
        self, ingester: JournalIngester, source_store: SourceStore,
    ) -> None:
        """同一 entry_id で再登録すると上書きされること."""
        ingester.add_entry(
            title="V1", body="Version 1", repository="repo",
            entry_id="entry-001",
        )
        result = ingester.add_entry(
            title="V2", body="Version 2", repository="repo",
            entry_id="entry-001",
        )
        assert result.placed == 1
        placed = source_store.root_dir / "journal" / "repo" / "entry-001.md"
        assert placed.read_text(encoding="utf-8") == "Version 2"

    def test_metadata_db_registered(
        self, ingester: JournalIngester, source_store: SourceStore,
    ) -> None:
        ingester.add_entry(
            title="DB Test", body="Content", repository="repo",
            entry_id="db-test-001",
        )
        record = source_store.db.get_source("journal/repo/db-test-001.md")
        assert record is not None
        assert record.source_type == "journal"
        assert record.title == "DB Test"

    def test_empty_title_rejected(self, ingester: JournalIngester) -> None:
        result = ingester.add_entry(title="", body="content", repository="repo")
        assert result.errors == 1
        assert result.placed == 0

    def test_empty_body_rejected(self, ingester: JournalIngester) -> None:
        result = ingester.add_entry(title="Title", body="", repository="repo")
        assert result.errors == 1

    def test_empty_repository_rejected(self, ingester: JournalIngester) -> None:
        result = ingester.add_entry(title="Title", body="Body", repository="")
        assert result.errors == 1

    def test_whitespace_only_title_rejected(self, ingester: JournalIngester) -> None:
        result = ingester.add_entry(title="   ", body="Body", repository="repo")
        assert result.errors == 1

    def test_repository_path_traversal_dotdot(self, ingester: JournalIngester) -> None:
        result = ingester.add_entry(
            title="T", body="B", repository="../evil",
        )
        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "placement"
        assert ".." in detail["message"] or "不正" in detail["message"]

    def test_repository_path_traversal_slash(self, ingester: JournalIngester) -> None:
        result = ingester.add_entry(
            title="T", body="B", repository="evil/path",
        )
        assert result.errors == 1

    def test_repository_path_traversal_backslash(self, ingester: JournalIngester) -> None:
        result = ingester.add_entry(
            title="T", body="B", repository="evil\\path",
        )
        assert result.errors == 1

    def test_entry_id_path_traversal(self, ingester: JournalIngester) -> None:
        result = ingester.add_entry(
            title="T", body="B", repository="repo",
            entry_id="../../../etc/passwd",
        )
        assert result.errors == 1

    def test_entry_id_empty_string_rejected(self, ingester: JournalIngester) -> None:
        result = ingester.add_entry(
            title="T", body="B", repository="repo", entry_id="",
        )
        assert result.errors == 1


class TestEntryIdGeneration:
    """entry_id 自動生成のテスト."""

    def test_english_title(self, ingester: JournalIngester, source_store: SourceStore) -> None:
        ingester.add_entry(
            title="Pipeline Migration Review",
            body="Content", repository="repo",
        )
        journal_dir = source_store.root_dir / "journal" / "repo"
        md_files = list(journal_dir.glob("*.md"))
        assert len(md_files) == 1
        assert md_files[0].stem == "20260323-143000-pipeline-migration-review"

    def test_japanese_title_becomes_timestamp_only(
        self, ingester: JournalIngester, source_store: SourceStore,
    ) -> None:
        """日本語のみのタイトルでは topic が空になり、タイムスタンプのみになる."""
        ingester.add_entry(
            title="セッション記録", body="Content", repository="repo",
        )
        journal_dir = source_store.root_dir / "journal" / "repo"
        md_files = list(journal_dir.glob("*.md"))
        assert len(md_files) == 1
        assert md_files[0].stem == "20260323-143000"

    def test_mixed_title(
        self, ingester: JournalIngester, source_store: SourceStore,
    ) -> None:
        ingester.add_entry(
            title="PR #123 対応", body="Content", repository="repo",
        )
        journal_dir = source_store.root_dir / "journal" / "repo"
        md_files = list(journal_dir.glob("*.md"))
        assert len(md_files) == 1
        # 英数字部分のみが残る
        stem = md_files[0].stem
        assert stem.startswith("20260323-143000-")
        assert "123" in stem


class TestSanitizeTopic:
    """_sanitize_topic() の単体テスト."""

    def test_simple_english(self) -> None:
        assert _sanitize_topic("Hello World") == "hello-world"

    def test_special_characters(self) -> None:
        assert _sanitize_topic("Fix: Bug #42!") == "fix-bug-42"

    def test_japanese_only(self) -> None:
        assert _sanitize_topic("日本語テスト") == ""

    def test_mixed(self) -> None:
        result = _sanitize_topic("PR 123 の対応")
        assert "pr" in result
        assert "123" in result

    def test_truncation(self) -> None:
        long_title = "a" * 100
        result = _sanitize_topic(long_title)
        assert len(result) <= 50

    def test_empty_string(self) -> None:
        assert _sanitize_topic("") == ""

    def test_hyphens_normalized(self) -> None:
        assert _sanitize_topic("a---b") == "a-b"


class TestImportDirectory:
    """import_directory() のテスト."""

    @pytest.fixture()
    def journal_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "journal"
        d.mkdir()
        (d / "20260320-100000-first-session.md").write_text(
            "# First Session\nContent", encoding="utf-8",
        )
        (d / "20260321-140000-second-session.md").write_text(
            "# Second Session\nMore content", encoding="utf-8",
        )
        (d / "custom-name.md").write_text(
            "# Custom\nCustom content", encoding="utf-8",
        )
        return d

    def test_import_all_files(
        self, ingester: JournalIngester, journal_dir: Path, source_store: SourceStore,
    ) -> None:
        result = ingester.import_directory(str(journal_dir), "rag-knowledge")
        assert result.placed == 3
        assert result.errors == 0

        repo_dir = source_store.root_dir / "journal" / "rag-knowledge"
        assert (repo_dir / "20260320-100000-first-session.md").exists()
        assert (repo_dir / "20260321-140000-second-session.md").exists()
        assert (repo_dir / "custom-name.md").exists()

    def test_import_generates_meta(
        self, ingester: JournalIngester, journal_dir: Path, source_store: SourceStore,
    ) -> None:
        ingester.import_directory(str(journal_dir), "repo")
        data_file = (
            source_store.root_dir / "journal" / "repo"
            / "20260320-100000-first-session.md"
        )
        meta = read_meta(data_file)
        assert meta["source_type"] == "journal"
        assert meta["title"] == "first-session"
        assert meta["repository"] == "repo"

    def test_import_custom_name_title(
        self, ingester: JournalIngester, journal_dir: Path, source_store: SourceStore,
    ) -> None:
        """YYYYMMDD-HHMMSS- パターンに一致しないファイルはファイル名全体がタイトル."""
        ingester.import_directory(str(journal_dir), "repo")
        data_file = source_store.root_dir / "journal" / "repo" / "custom-name.md"
        meta = read_meta(data_file)
        assert meta["title"] == "custom-name"

    def test_import_empty_directory(
        self, ingester: JournalIngester, tmp_path: Path,
    ) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        result = ingester.import_directory(str(d), "repo")
        assert result.placed == 0
        assert result.errors == 0

    def test_import_nonexistent_directory(self, ingester: JournalIngester) -> None:
        result = ingester.import_directory("/nonexistent/path", "repo")
        assert result.errors == 1

    def test_import_file_as_directory(
        self, ingester: JournalIngester, tmp_path: Path,
    ) -> None:
        f = tmp_path / "not_a_dir.md"
        f.write_text("content")
        result = ingester.import_directory(str(f), "repo")
        assert result.errors == 1

    def test_import_empty_dir_path(self, ingester: JournalIngester) -> None:
        result = ingester.import_directory("", "repo")
        assert result.errors == 1

    def test_import_empty_repository(
        self, ingester: JournalIngester, tmp_path: Path,
    ) -> None:
        d = tmp_path / "dir"
        d.mkdir()
        result = ingester.import_directory(str(d), "")
        assert result.errors == 1

    def test_import_skips_empty_files(
        self, ingester: JournalIngester, tmp_path: Path,
    ) -> None:
        d = tmp_path / "with_empty"
        d.mkdir()
        (d / "empty.md").write_text("")
        (d / "content.md").write_text("has content")
        result = ingester.import_directory(str(d), "repo")
        assert result.placed == 1
        assert result.skipped == 1

    def test_import_hard_limit(
        self, ingester: JournalIngester, tmp_path: Path,
    ) -> None:
        d = tmp_path / "many"
        d.mkdir()
        count = MAX_FILES_HARD_LIMIT + 10
        for i in range(count):
            (d / f"entry_{i:04d}.md").write_text(f"Content {i}")
        result = ingester.import_directory(str(d), "repo")
        assert result.placed == MAX_FILES_HARD_LIMIT

    def test_import_repository_traversal(self, ingester: JournalIngester, tmp_path: Path) -> None:
        d = tmp_path / "dir"
        d.mkdir()
        (d / "test.md").write_text("content")
        result = ingester.import_directory(str(d), "../evil")
        assert result.errors == 1
        assert result.placed == 0


class TestErrorDetailsStructured:
    """error_details dict 化の検証."""

    def test_empty_title_error_dict(self, ingester: JournalIngester) -> None:
        """空 title で error_details に dict が積まれる."""
        result = ingester.add_entry(title="", body="content", repository="repo")
        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "placement"
        assert "title" in detail["message"]

    def test_repository_traversal_error_dict(self, ingester: JournalIngester) -> None:
        """不正な repository で error_details に dict が積まれる."""
        result = ingester.add_entry(
            title="T", body="B", repository="../evil",
        )
        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "placement"
        assert "message" in detail

    def test_import_nonexistent_dir_error_dict(
        self, ingester: JournalIngester,
    ) -> None:
        """存在しないディレクトリ import で error_details に dict が積まれる."""
        result = ingester.import_directory("/nonexistent/path", "repo")
        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "placement"
        assert "Directory not found" in detail["message"]


class TestSourceTypeIntegration:
    """source_type "journal" の統合テスト."""

    def test_detect_source_type_from_path(self, source_store: SourceStore) -> None:
        """source_store の detect_source_type が journal/ プレフィックスを判定すること."""
        from rag.store.source_store import detect_source_type
        assert detect_source_type("journal/repo/entry.md") == "journal"

    def test_journal_not_in_no_meta_types(self) -> None:
        """journal は NO_META_TYPES に含まれないこと（.meta を持つ）."""
        from rag.store.source_store import NO_META_TYPES
        assert "journal" not in NO_META_TYPES

    def test_list_files_by_source_type(
        self, ingester: JournalIngester, source_store: SourceStore,
    ) -> None:
        ingester.add_entry(
            title="List Test", body="Body", repository="repo",
            entry_id="list-test-001",
        )
        files = source_store.list_files(source_type="journal")
        assert len(files) == 1
        assert files[0].as_posix().startswith("journal/")

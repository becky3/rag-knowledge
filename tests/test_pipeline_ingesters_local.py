"""Local インジェスター（新アーキテクチャ）のテスト.

仕様: docs/specs/ingesters/local.md
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rag.pipeline.ingesters.local import MAX_FILES_HARD_LIMIT, _UPLOAD_DIR

if TYPE_CHECKING:
    from rag.pipeline.ingesters.local import LocalIngester
from rag.store.source_store import SourceStore

from factories import make_local_ingester


_FIXED_DATE = datetime.date(2026, 1, 15)


def _today_prefix() -> str:
    """テスト用: 固定日付のプレフィックスを返す."""
    return f"{_FIXED_DATE.year}/{_FIXED_DATE.month:02d}/{_FIXED_DATE.day:02d}"


@pytest.fixture(autouse=True)
def _freeze_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """date.today() を固定して日付境界でのテストフレークを防止する."""
    monkeypatch.setattr(
        "rag.pipeline.ingesters.local._facade.datetime",
        type("FakeDatetime", (), {"date": type("FakeDate", (), {"today": staticmethod(lambda: _FIXED_DATE)})})(),
    )


@pytest.fixture()
def source_store(tmp_path: Path) -> SourceStore:
    store = SourceStore(tmp_path / "source_store")
    store.initialize()
    return store


@pytest.fixture()
def ingester(source_store: SourceStore) -> Any:
    return make_local_ingester(source_store)


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    (d / "readme.md").write_text("# Readme", encoding="utf-8")
    (d / "notes.txt").write_text("Notes content", encoding="utf-8")
    sub = d / "sub"
    sub.mkdir()
    (sub / "deep.md").write_text("# Deep", encoding="utf-8")
    (d / "image.png").write_bytes(b"\x89PNG")
    return d


class TestAddDocument:
    def test_add_single_file(self, ingester: LocalIngester, source_store: SourceStore) -> None:
        data = b"# Test\nHello world"
        result = ingester.add_document(data, "sample.md")
        assert result.placed == 1
        assert result.errors == 0
        placed = source_store.root_dir / "local" / _UPLOAD_DIR / _today_prefix() / "sample.md"
        assert placed.exists()
        assert placed.read_bytes() == data

    def test_add_empty_data_raises_error(self, ingester: LocalIngester) -> None:
        result = ingester.add_document(b"", "empty.md")
        assert result.errors == 1

    def test_add_binary_content(self, ingester: LocalIngester, source_store: SourceStore) -> None:
        data = b"%PDF-1.4 binary content \x00\x01\x02"
        result = ingester.add_document(data, "doc.pdf")
        assert result.placed == 1
        placed = source_store.root_dir / "local" / _UPLOAD_DIR / _today_prefix() / "doc.pdf"
        assert placed.read_bytes() == data

    def test_add_replace_mode_new_file(self, ingester: LocalIngester) -> None:
        """replace モード: 新規配置時は placed に計上される."""
        result = ingester.add_document(b"v1", "new.md", upload_mode="replace")
        assert result.placed == 1
        assert result.overwritten == 0

    def test_add_replace_mode_overwrites(self, ingester: LocalIngester, source_store: SourceStore) -> None:
        """replace モード: 既存ファイル上書き時は overwritten に計上される（placed は 0）."""
        first = ingester.add_document(b"v1", "overwrite.md", upload_mode="replace")
        assert first.placed == 1
        assert first.overwritten == 0

        result = ingester.add_document(b"v2", "overwrite.md", upload_mode="replace")
        assert result.placed == 0
        assert result.overwritten == 1
        placed = source_store.root_dir / "local" / _UPLOAD_DIR / _today_prefix() / "overwrite.md"
        assert placed.read_bytes() == b"v2"

    def test_add_fail_mode_duplicate(self, ingester: LocalIngester) -> None:
        """fail モード（デフォルト）: 同名ファイルが既にある場合に FileExistsError が発生すること."""
        result1 = ingester.add_document(b"first", "dup.md")
        assert result1.placed == 1

        with pytest.raises(FileExistsError, match="同名ファイル"):
            ingester.add_document(b"second", "dup.md")

    def test_no_meta_for_local(self, ingester: LocalIngester, source_store: SourceStore) -> None:
        ingester.add_document(b"# Test", "sample.md")
        meta = source_store.root_dir / "local" / _UPLOAD_DIR / _today_prefix() / "sample.md.meta"
        assert not meta.exists()


class TestCrawlDocuments:
    def test_crawl_directory(self, ingester: LocalIngester, sample_dir: Path, source_store: SourceStore) -> None:
        result = ingester.crawl_documents(str(sample_dir))
        assert result.placed == 3
        assert result.errors == 0
        base = source_store.root_dir / "local" / _UPLOAD_DIR / _today_prefix() / "docs"
        assert (base / "readme.md").exists()
        assert (base / "notes.txt").exists()
        assert (base / "sub" / "deep.md").exists()
        assert not (base / "image.png").exists()

    def test_crawl_with_pattern(self, ingester: LocalIngester, sample_dir: Path) -> None:
        result = ingester.crawl_documents(str(sample_dir), "**/*.md")
        assert result.placed == 2

    def test_crawl_nonexistent_dir(self, ingester: LocalIngester) -> None:
        result = ingester.crawl_documents("/nonexistent/dir")
        assert result.errors == 1

    def test_crawl_empty_path(self, ingester: LocalIngester) -> None:
        result = ingester.crawl_documents("")
        assert result.errors == 1

    def test_crawl_file_as_dir(self, ingester: LocalIngester, sample_dir: Path) -> None:
        result = ingester.crawl_documents(str(sample_dir / "readme.md"))
        assert result.errors == 1

    def test_crawl_pattern_traversal(self, ingester: LocalIngester, sample_dir: Path) -> None:
        result = ingester.crawl_documents(str(sample_dir), "../**/*")
        assert result.errors == 1

    def test_crawl_absolute_pattern(self, ingester: LocalIngester, sample_dir: Path) -> None:
        result = ingester.crawl_documents(str(sample_dir), "/etc/**/*")
        assert result.errors == 1

    def test_crawl_no_matching_files(self, ingester: LocalIngester, sample_dir: Path) -> None:
        result = ingester.crawl_documents(str(sample_dir), "*.xyz")
        assert result.placed == 0

    def test_crawl_empty_files_skipped(self, ingester: LocalIngester, tmp_path: Path) -> None:
        d = tmp_path / "empty_test"
        d.mkdir()
        (d / "empty.md").write_text("")
        (d / "notempty.md").write_text("content")
        result = ingester.crawl_documents(str(d))
        assert result.placed == 1
        assert result.skipped == 1

    def test_crawl_hard_limit(self, ingester: LocalIngester, tmp_path: Path) -> None:
        d = tmp_path / "many_files"
        d.mkdir()
        for i in range(MAX_FILES_HARD_LIMIT + 10):
            (d / f"file_{i:04d}.md").write_text(f"Content {i}")
        result = ingester.crawl_documents(str(d))
        assert result.placed == MAX_FILES_HARD_LIMIT

    def test_crawl_fail_mode_skips_duplicates(self, ingester: LocalIngester, sample_dir: Path) -> None:
        """fail モード: 重複ファイルがスキップされること."""
        result1 = ingester.crawl_documents(str(sample_dir))
        assert result1.placed == 3

        result2 = ingester.crawl_documents(str(sample_dir))
        assert result2.placed == 0
        assert result2.skipped == 3

    def test_crawl_replace_mode(self, ingester: LocalIngester, sample_dir: Path) -> None:
        """replace モード: 重複ファイルが上書きされ overwritten に計上される（placed は 0）."""
        result1 = ingester.crawl_documents(str(sample_dir), upload_mode="replace")
        assert result1.placed == 3
        assert result1.overwritten == 0

        result2 = ingester.crawl_documents(str(sample_dir), upload_mode="replace")
        assert result2.placed == 0
        assert result2.overwritten == 3
        assert result2.skipped == 0


class TestUploadPath:
    """アップロードパスの構造テスト."""

    def test_upload_path_structure(self, ingester: LocalIngester, source_store: SourceStore) -> None:
        """ファイルが .upload/yyyy/MM/dd/ に配置されること."""
        ingester.add_document(b"# Test content", "sample.md")
        upload_base = source_store.root_dir / "local" / _UPLOAD_DIR
        assert upload_base.exists()
        # 日付ディレクトリの存在確認（_FIXED_DATE で固定）
        date_dir = upload_base / str(_FIXED_DATE.year) / f"{_FIXED_DATE.month:02d}" / f"{_FIXED_DATE.day:02d}"
        assert date_dir.exists()
        assert (date_dir / "sample.md").exists()


class TestErrorDetailsStructured:
    """error_details dict 化の検証."""

    def test_add_document_empty_data_dict(self, ingester: LocalIngester) -> None:
        """空データで error_details に dict が積まれる."""
        result = ingester.add_document(b"", "empty.md")
        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "placement"
        assert detail["target"] == "empty.md"
        assert "空" in detail["message"]

    def test_crawl_nonexistent_dir_dict(self, ingester: LocalIngester) -> None:
        """存在しないディレクトリで error_details に dict が積まれる."""
        result = ingester.crawl_documents("/nonexistent/dir")
        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "placement"
        assert "/nonexistent/dir" in detail["target"]
        assert "message" in detail


class TestCrawlHttpModeRestriction:
    """crawl_documents の HTTP モード制限テスト."""

    def test_crawl_http_mode_no_allowed_dirs(self, source_store: SourceStore, sample_dir: Path) -> None:
        ingester = make_local_ingester(source_store, http_mode_enabled=True, allowed_dirs=[])
        result = ingester.crawl_documents(str(sample_dir))
        assert result.errors == 1

    def test_crawl_http_mode_allowed_dir(self, source_store: SourceStore, sample_dir: Path) -> None:
        ingester = make_local_ingester(source_store, http_mode_enabled=True, allowed_dirs=[str(sample_dir)])
        result = ingester.crawl_documents(str(sample_dir))
        assert result.placed > 0

    def test_crawl_http_mode_denied_dir(self, source_store: SourceStore, tmp_path: Path, sample_dir: Path) -> None:
        allowed = tmp_path / "allowed_other"
        allowed.mkdir()
        ingester = make_local_ingester(source_store, http_mode_enabled=True, allowed_dirs=[str(allowed)])
        result = ingester.crawl_documents(str(sample_dir))
        assert result.errors == 1


class TestCreateLocalFetcher:
    """create_local_fetcher ファクトリのテスト.

    Local Fake は廃止済み（Issue #722）のため、ファクトリは常に RealLocalFetcher を返す。
    将来再び設定分岐を持つようになっても regression を検出するための固定テスト。
    """

    def test_returns_real_local_fetcher(self) -> None:
        from rag.pipeline.ingesters.local import (
            RealLocalFetcher,
            create_local_fetcher,
        )

        fetcher = create_local_fetcher()
        assert isinstance(fetcher, RealLocalFetcher)

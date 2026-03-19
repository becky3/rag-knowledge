"""Local インジェスター（新アーキテクチャ）のテスト.

仕様: docs/specs/ingesters/local.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.pipeline.ingesters.local import LocalIngester, MAX_FILES_HARD_LIMIT
from rag.store.source_store import SourceStore


@pytest.fixture()
def source_store(tmp_path: Path) -> SourceStore:
    store = SourceStore(tmp_path / "source_store")
    store.initialize()
    return store


@pytest.fixture()
def ingester(source_store: SourceStore) -> LocalIngester:
    return LocalIngester(source_store)


@pytest.fixture()
def sample_file(tmp_path: Path) -> Path:
    f = tmp_path / "sample.md"
    f.write_text("# Test\nHello world", encoding="utf-8")
    return f


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
    def test_add_single_file(self, ingester: LocalIngester, sample_file: Path, source_store: SourceStore) -> None:
        result = ingester.add_document(str(sample_file))
        assert result.placed == 1
        assert result.errors == 0
        placed = source_store.root_dir / "local" / "sample.md"
        assert placed.exists()

    def test_add_nonexistent_file(self, ingester: LocalIngester) -> None:
        result = ingester.add_document("/nonexistent/path.md")
        assert result.errors == 1

    def test_add_empty_path(self, ingester: LocalIngester) -> None:
        result = ingester.add_document("")
        assert result.errors == 1

    def test_add_unsupported_extension(self, ingester: LocalIngester, tmp_path: Path) -> None:
        f = tmp_path / "test.xyz"
        f.write_text("test")
        result = ingester.add_document(str(f))
        assert result.errors == 1

    def test_add_directory_path(self, ingester: LocalIngester, tmp_path: Path) -> None:
        d = tmp_path / "adir"
        d.mkdir()
        result = ingester.add_document(str(d))
        assert result.errors == 1

    def test_add_empty_file(self, ingester: LocalIngester, tmp_path: Path) -> None:
        f = tmp_path / "empty.md"
        f.write_text("")
        result = ingester.add_document(str(f))
        assert result.errors == 1

    def test_add_overwrite(self, ingester: LocalIngester, tmp_path: Path, source_store: SourceStore) -> None:
        f = tmp_path / "overwrite.md"
        f.write_text("v1", encoding="utf-8")
        ingester.add_document(str(f))
        f.write_text("v2", encoding="utf-8")
        result = ingester.add_document(str(f))
        assert result.placed == 1
        placed = source_store.root_dir / "local" / "overwrite.md"
        assert placed.read_text(encoding="utf-8") == "v2"

    def test_no_meta_for_local(self, ingester: LocalIngester, sample_file: Path, source_store: SourceStore) -> None:
        ingester.add_document(str(sample_file))
        meta = source_store.root_dir / "local" / "sample.md.meta"
        assert not meta.exists()


class TestCrawlDocuments:
    def test_crawl_directory(self, ingester: LocalIngester, sample_dir: Path, source_store: SourceStore) -> None:
        result = ingester.crawl_documents(str(sample_dir))
        assert result.placed == 3
        assert result.errors == 0
        base = source_store.root_dir / "local" / "docs"
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


class TestHttpModeRestriction:
    def test_http_mode_no_allowed_dirs(self, source_store: SourceStore, tmp_path: Path) -> None:
        ingester = LocalIngester(source_store, http_mode_enabled=True, allowed_dirs=[])
        f = tmp_path / "test.md"
        f.write_text("test content")
        result = ingester.add_document(str(f))
        assert result.errors == 1

    def test_http_mode_allowed_dir(self, source_store: SourceStore, tmp_path: Path) -> None:
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        f = allowed / "test.md"
        f.write_text("test content")
        ingester = LocalIngester(source_store, http_mode_enabled=True, allowed_dirs=[str(allowed)])
        result = ingester.add_document(str(f))
        assert result.placed == 1

    def test_http_mode_denied_dir(self, source_store: SourceStore, tmp_path: Path) -> None:
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        denied = tmp_path / "denied"
        denied.mkdir()
        f = denied / "test.md"
        f.write_text("test content")
        ingester = LocalIngester(source_store, http_mode_enabled=True, allowed_dirs=[str(allowed)])
        result = ingester.add_document(str(f))
        assert result.errors == 1

    def test_stdio_mode_no_restriction(self, ingester: LocalIngester, sample_file: Path) -> None:
        result = ingester.add_document(str(sample_file))
        assert result.placed == 1

"""コンテンツ一覧取得のテスト.

仕様: docs/specs/infrastructure/content-listing.md

テスト方針:
- MetadataDB の list_sources / count_sources_by_type メソッドの単体テスト
- list_recent_sources 共通関数のフォーマット・統合テスト
- エッジケース: 0件、limit > 該当件数、論理削除除外、ソート順、昇順/降順
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.store.metadata_db import MetadataDB


@pytest.fixture()
def db(tmp_path: Path) -> MetadataDB:
    """テスト用 MetadataDB インスタンス."""
    mdb = MetadataDB(tmp_path / "metadata.db")
    mdb.initialize()
    return mdb


def _register(
    db: MetadataDB,
    source_id: str,
    source_type: str = "web",
    title: str = "Test",
    collected_at: str = "2026-01-01T00:00:00Z",
    published_at: str = "",
    file_size: int = 1024,
    status: str = "active",
) -> None:
    """テスト用ソース登録ヘルパー."""
    db.register_source(
        source_id=source_id,
        source_type=source_type,
        file_path=f"{source_type}/{source_id.replace('/', '_')}",
        title=title,
        content_hash="hash",
        file_size=file_size,
        collected_at=collected_at,
        updated_at=collected_at,
        published_at=published_at,
    )
    if status == "deleted":
        db.set_status(source_id, "deleted")


class TestListSources:
    """MetadataDB.list_sources のテスト."""

    def test_empty(self, db: MetadataDB) -> None:
        """0件の場合は空リストを返す."""
        result = db.list_sources(source_type="web", limit=10)
        assert result == []

    def test_filter_by_source_type(self, db: MetadataDB) -> None:
        """source_type でフィルタされる."""
        _register(db, "web1", source_type="web")
        _register(db, "local1", source_type="local")

        result = db.list_sources(source_type="web", limit=10)
        assert len(result) == 1
        assert result[0].source_id == "web1"

    def test_order_by_published_at_desc(self, db: MetadataDB) -> None:
        """published_at 降順でソートされる（デフォルト）."""
        _register(db, "old", published_at="2026-01-01T00:00:00Z")
        _register(db, "mid", published_at="2026-01-15T00:00:00Z")
        _register(db, "new", published_at="2026-02-01T00:00:00Z")

        result = db.list_sources(source_type="web", limit=10)
        assert [r.source_id for r in result] == ["new", "mid", "old"]

    def test_order_by_published_at_asc(self, db: MetadataDB) -> None:
        """ascending=True で昇順ソート."""
        _register(db, "old", published_at="2026-01-01T00:00:00Z")
        _register(db, "mid", published_at="2026-01-15T00:00:00Z")
        _register(db, "new", published_at="2026-02-01T00:00:00Z")

        result = db.list_sources(source_type="web", limit=10, ascending=True)
        assert [r.source_id for r in result] == ["old", "mid", "new"]

    def test_published_at_differs_from_collected_at(self, db: MetadataDB) -> None:
        """published_at が collected_at と異なる場合、published_at でソートされる."""
        _register(
            db, "early_pub_late_collect",
            collected_at="2026-03-01T00:00:00Z",
            published_at="2026-01-01T00:00:00Z",
        )
        _register(
            db, "late_pub_early_collect",
            collected_at="2026-01-01T00:00:00Z",
            published_at="2026-03-01T00:00:00Z",
        )

        result = db.list_sources(source_type="web", limit=10)
        assert result[0].source_id == "late_pub_early_collect"
        assert result[1].source_id == "early_pub_late_collect"

    def test_published_at_fallback_to_collected_at(self, db: MetadataDB) -> None:
        """published_at 未指定時は collected_at が使われる."""
        _register(db, "no_pub", collected_at="2026-02-01T00:00:00Z")

        result = db.list_sources(source_type="web", limit=10)
        assert result[0].published_at == "2026-02-01T00:00:00Z"

    def test_limit(self, db: MetadataDB) -> None:
        """limit で取得件数が制限される."""
        for i in range(5):
            _register(
                db, f"web{i}",
                published_at=f"2026-01-{i+1:02d}T00:00:00Z",
            )

        result = db.list_sources(source_type="web", limit=2)
        assert len(result) == 2
        assert result[0].source_id == "web4"
        assert result[1].source_id == "web3"

    def test_limit_larger_than_count(self, db: MetadataDB) -> None:
        """limit が該当件数より大きい場合は全件返す."""
        _register(db, "web1")
        result = db.list_sources(source_type="web", limit=100)
        assert len(result) == 1

    def test_excludes_deleted(self, db: MetadataDB) -> None:
        """論理削除済みソースは除外される."""
        _register(db, "active1")
        _register(db, "deleted1", status="deleted")

        result = db.list_sources(source_type="web", limit=10)
        assert len(result) == 1
        assert result[0].source_id == "active1"


class TestCountSourcesByType:
    """MetadataDB.count_sources_by_type のテスト."""

    def test_empty(self, db: MetadataDB) -> None:
        assert db.count_sources_by_type(source_type="web") == 0

    def test_counts_only_active(self, db: MetadataDB) -> None:
        _register(db, "a1")
        _register(db, "a2")
        _register(db, "d1", status="deleted")

        assert db.count_sources_by_type(source_type="web") == 2

    def test_counts_by_type(self, db: MetadataDB) -> None:
        _register(db, "web1", source_type="web")
        _register(db, "local1", source_type="local")

        assert db.count_sources_by_type(source_type="web") == 1
        assert db.count_sources_by_type(source_type="local") == 1


class TestListRecentSources:
    """list_recent_sources 共通関数のテスト."""

    def _setup_db(self, tmp_path: Path) -> Path:
        """source_store_dir に metadata.db を準備する."""
        source_store_dir = tmp_path / "source_store"
        source_store_dir.mkdir()
        db = MetadataDB(source_store_dir / "metadata.db")
        db.initialize()
        return source_store_dir

    def test_format_output(self, tmp_path: Path) -> None:
        """出力フォーマットが仕様に準拠する."""
        from rag.rag_knowledge import list_recent_sources

        source_store_dir = self._setup_db(tmp_path)
        db = MetadataDB(source_store_dir / "metadata.db")
        db.initialize()
        _register(
            db, "https://example.com/page", title="Sample Page",
            collected_at="2026-06-15T10:30:00+09:00",
            published_at="2026-06-15T10:30:00+09:00",
            file_size=46285,
        )
        db.close()

        result = list_recent_sources(str(source_store_dir), "web", 10)

        assert "source_type: web（1件 / 全1件" in result
        assert "1. Sample Page" in result
        assert "Source: https://example.com/page" in result
        assert "Published: 2026-06-15T10:30:00+09:00" in result
        assert "Size: 45.2 KB" in result

    def test_format_output_ascending(self, tmp_path: Path) -> None:
        """昇順指定時のヘッダー表示."""
        from rag.rag_knowledge import list_recent_sources

        source_store_dir = self._setup_db(tmp_path)
        db = MetadataDB(source_store_dir / "metadata.db")
        db.initialize()
        _register(db, "web1", collected_at="2026-01-01T00:00:00Z")
        db.close()

        result = list_recent_sources(str(source_store_dir), "web", 10, ascending=True)
        assert "古い順" in result

    def test_zero_results(self, tmp_path: Path) -> None:
        """0件の場合のフォーマット."""
        from rag.rag_knowledge import list_recent_sources

        source_store_dir = self._setup_db(tmp_path)
        result = list_recent_sources(str(source_store_dir), "web", 10)
        assert result == "source_type: web（0件 / 全0件）"

    def test_db_not_exists(self, tmp_path: Path) -> None:
        """metadata.db が存在しない場合."""
        from rag.rag_knowledge import list_recent_sources

        result = list_recent_sources(str(tmp_path / "nonexistent"), "web", 10)
        assert result == "source_type: web（0件 / 全0件）"

    def test_partial_display(self, tmp_path: Path) -> None:
        """limit で表示件数を制限し、総件数は全件数を表示."""
        from rag.rag_knowledge import list_recent_sources

        source_store_dir = self._setup_db(tmp_path)
        db = MetadataDB(source_store_dir / "metadata.db")
        db.initialize()
        for i in range(5):
            _register(
                db, f"https://example.com/p{i}", title=f"Page {i}",
                published_at=f"2026-01-{i+1:02d}T00:00:00Z",
            )
        db.close()

        result = list_recent_sources(str(source_store_dir), "web", 2)
        assert "source_type: web（2件 / 全5件" in result
        assert "1. Page 4" in result
        assert "2. Page 3" in result
        assert "3." not in result


class TestFormatFileSize:
    """format_file_size のテスト."""

    def test_bytes(self) -> None:
        from rag.rag_knowledge import format_file_size

        assert format_file_size(500) == "500 B"

    def test_kilobytes(self) -> None:
        from rag.rag_knowledge import format_file_size

        assert format_file_size(46285) == "45.2 KB"

    def test_megabytes(self) -> None:
        from rag.rag_knowledge import format_file_size

        assert format_file_size(5 * 1024 * 1024) == "5.0 MB"

    def test_gigabytes(self) -> None:
        from rag.rag_knowledge import format_file_size

        assert format_file_size(2 * 1024 * 1024 * 1024) == "2.0 GB"

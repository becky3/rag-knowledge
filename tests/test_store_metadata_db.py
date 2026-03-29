"""metadata_db モジュールのテスト.

仕様: docs/specs/source-store.md — metadata.db スキーマ
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.store.metadata_db import MetadataDB
from rag.store.models import NULL_COMMIT_HASH


@pytest.fixture()
def db(tmp_path: Path) -> MetadataDB:
    """テスト用 MetadataDB インスタンス."""
    mdb = MetadataDB(tmp_path / "metadata.db")
    mdb.initialize()
    return mdb


class TestSourcesCRUD:
    """sources テーブルの CRUD テスト."""

    def test_register_and_get(self, db: MetadataDB) -> None:
        db.register_source(
            source_id="https://example.com/page",
            source_type="web",
            file_path="web/https/example.com/page.html",
            title="Test Page",
            content_hash="abc123",
            file_size=1024,
            collected_at="2026-01-15T10:00:00Z",
            updated_at="2026-01-15T10:00:00Z",
        )

        record = db.get_source("https://example.com/page")
        assert record is not None
        assert record.source_id == "https://example.com/page"
        assert record.source_type == "web"
        assert record.title == "Test Page"
        assert record.status == "active"
        assert record.file_size == 1024

    def test_register_upsert(self, db: MetadataDB) -> None:
        """同一 source_id の再登録で上書きされる."""
        db.register_source(
            source_id="https://example.com/page",
            source_type="web",
            file_path="web/https/example.com/page.html",
            title="Old Title",
            content_hash="old",
            file_size=100,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.register_source(
            source_id="https://example.com/page",
            source_type="web",
            file_path="web/https/example.com/page.html",
            title="New Title",
            content_hash="new",
            file_size=200,
            collected_at="2026-01-02T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )

        record = db.get_source("https://example.com/page")
        assert record is not None
        assert record.title == "New Title"
        assert record.content_hash == "new"
        assert record.status == "active"

    def test_register_restores_deleted(self, db: MetadataDB) -> None:
        """deleted 状態のソースへの再登録で active に復帰する."""
        db.register_source(
            source_id="test",
            source_type="local",
            file_path="local/test.md",
            title="Test",
            content_hash="hash1",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.set_status("test", "deleted")

        db.register_source(
            source_id="test",
            source_type="local",
            file_path="local/test.md",
            title="Test Updated",
            content_hash="hash2",
            file_size=20,
            collected_at="2026-01-02T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )

        record = db.get_source("test")
        assert record is not None
        assert record.status == "active"
        assert record.title == "Test Updated"

    def test_get_nonexistent(self, db: MetadataDB) -> None:
        assert db.get_source("nonexistent") is None

    def test_get_by_path(self, db: MetadataDB) -> None:
        db.register_source(
            source_id="test-id",
            source_type="local",
            file_path="local/test.md",
            title="Test",
            content_hash="hash",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        record = db.get_source_by_path("local/test.md")
        assert record is not None
        assert record.source_id == "test-id"

    def test_update_source(self, db: MetadataDB) -> None:
        db.register_source(
            source_id="test",
            source_type="local",
            file_path="local/test.md",
            title="Old",
            content_hash="hash",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        db.update_source("test", title="New Title", file_size=20)
        record = db.get_source("test")
        assert record is not None
        assert record.title == "New Title"
        assert record.file_size == 20

    def test_update_nonexistent(self, db: MetadataDB) -> None:
        with pytest.raises(KeyError):
            db.update_source("nonexistent", title="X")

    def test_update_empty_fields(self, db: MetadataDB) -> None:
        with pytest.raises(ValueError, match="更新フィールドが指定されていません"):
            db.update_source("test")

    def test_update_invalid_field(self, db: MetadataDB) -> None:
        db.register_source(
            source_id="test",
            source_type="local",
            file_path="local/test.md",
            title="Test",
            content_hash="hash",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        with pytest.raises(ValueError, match="不正なフィールド"):
            db.update_source("test", id=999)

    def test_search_by_type(self, db: MetadataDB) -> None:
        db.register_source(
            source_id="web1",
            source_type="web",
            file_path="web/https/a.com/p.html",
            title="Web1",
            content_hash="h1",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.register_source(
            source_id="local1",
            source_type="local",
            file_path="local/test.md",
            title="Local1",
            content_hash="h2",
            file_size=20,
            collected_at="2026-01-02T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )

        web_results = db.search_sources(source_type="web")
        assert len(web_results) == 1
        assert web_results[0].source_id == "web1"

    def test_search_by_status(self, db: MetadataDB) -> None:
        db.register_source(
            source_id="active1",
            source_type="local",
            file_path="local/active.md",
            title="Active",
            content_hash="h1",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.register_source(
            source_id="deleted1",
            source_type="local",
            file_path="local/deleted.md",
            title="Deleted",
            content_hash="h2",
            file_size=20,
            collected_at="2026-01-02T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )
        db.set_status("deleted1", "deleted")

        active = db.search_sources(status="active")
        assert len(active) == 1
        assert active[0].source_id == "active1"

    def test_set_status(self, db: MetadataDB) -> None:
        db.register_source(
            source_id="test",
            source_type="local",
            file_path="local/test.md",
            title="Test",
            content_hash="hash",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        db.set_status("test", "deleted")
        record = db.get_source("test")
        assert record is not None
        assert record.status == "deleted"

        db.set_status("test", "active")
        record = db.get_source("test")
        assert record is not None
        assert record.status == "active"

    def test_set_status_nonexistent(self, db: MetadataDB) -> None:
        with pytest.raises(KeyError):
            db.set_status("nonexistent", "deleted")

    def test_source_count(self, db: MetadataDB) -> None:
        assert db.source_count() == 0

        db.register_source(
            source_id="s1",
            source_type="local",
            file_path="local/s1.md",
            title="S1",
            content_hash="h",
            file_size=1,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.register_source(
            source_id="s2",
            source_type="local",
            file_path="local/s2.md",
            title="S2",
            content_hash="h",
            file_size=1,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.set_status("s2", "deleted")

        assert db.source_count() == 2
        assert db.source_count(status="active") == 1
        assert db.source_count(status="deleted") == 1


class TestPipelineHistory:
    """pipeline_history テーブルのテスト."""

    def test_get_last_commit_id_empty(self, db: MetadataDB) -> None:
        """履歴がない場合 null commit hash を返す."""
        assert db.get_last_commit_id() == NULL_COMMIT_HASH

    def test_add_and_get_last(self, db: MetadataDB) -> None:
        db.add_pipeline_history(
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id="abc123",
            processed_at="2026-01-15T10:00:00Z",
        )
        assert db.get_last_commit_id() == "abc123"

    def test_multiple_entries(self, db: MetadataDB) -> None:
        """複数履歴追加で最新の to_commit_id を返す."""
        db.add_pipeline_history(
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id="commit1",
            processed_at="2026-01-01T00:00:00Z",
        )
        db.add_pipeline_history(
            from_commit_id="commit1",
            to_commit_id="commit2",
            processed_at="2026-01-02T00:00:00Z",
        )
        db.add_pipeline_history(
            from_commit_id="commit2",
            to_commit_id="commit3",
            processed_at="2026-01-03T00:00:00Z",
        )

        assert db.get_last_commit_id() == "commit3"

    def test_get_history(self, db: MetadataDB) -> None:
        db.add_pipeline_history(
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id="c1",
            processed_at="2026-01-01T00:00:00Z",
        )
        db.add_pipeline_history(
            from_commit_id="c1",
            to_commit_id="c2",
            processed_at="2026-01-02T00:00:00Z",
        )

        history = db.get_pipeline_history()
        assert len(history) == 2
        assert history[0].from_commit_id == NULL_COMMIT_HASH
        assert history[0].to_commit_id == "c1"
        assert history[1].from_commit_id == "c1"
        assert history[1].to_commit_id == "c2"

    def test_checkpoint(self, db: MetadataDB) -> None:
        """checkpoint が例外なく実行できる."""
        db.checkpoint()

    def test_add_pipeline_history_with_mode(self, db: MetadataDB) -> None:
        """mode パラメータ付きで履歴を追加できる."""
        db.add_pipeline_history(
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id="abc",
            processed_at="2026-01-01T00:00:00Z",
            mode="index",
        )
        history = db.get_pipeline_history()
        assert len(history) == 1
        assert history[0].mode == "index"

    def test_add_pipeline_history_default_mode(self, db: MetadataDB) -> None:
        """mode 未指定時は incremental がデフォルト."""
        db.add_pipeline_history(
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id="abc",
            processed_at="2026-01-01T00:00:00Z",
        )
        history = db.get_pipeline_history()
        assert history[0].mode == "incremental"

    def test_needs_index_rebuild_empty_history(self, db: MetadataDB) -> None:
        """履歴なしの場合 True を返す."""
        assert db.needs_index_rebuild() is True

    def test_needs_index_rebuild_after_index(self, db: MetadataDB) -> None:
        """最後が index で以降レコードなしの場合 False を返す."""
        db.add_pipeline_history(
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id="c1",
            processed_at="2026-01-01T00:00:00Z",
            mode="index",
        )
        assert db.needs_index_rebuild() is False

    def test_needs_index_rebuild_after_full(self, db: MetadataDB) -> None:
        """最後が full で以降レコードなしの場合 False を返す."""
        db.add_pipeline_history(
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id="c1",
            processed_at="2026-01-01T00:00:00Z",
            mode="full",
        )
        assert db.needs_index_rebuild() is False

    def test_needs_index_rebuild_incremental_after_index(self, db: MetadataDB) -> None:
        """index 後に incremental がある場合 True を返す."""
        db.add_pipeline_history(
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id="c1",
            processed_at="2026-01-01T00:00:00Z",
            mode="index",
        )
        db.add_pipeline_history(
            from_commit_id="c1",
            to_commit_id="c2",
            processed_at="2026-01-02T00:00:00Z",
            mode="incremental",
        )
        assert db.needs_index_rebuild() is True

    def test_needs_index_rebuild_only_incremental(self, db: MetadataDB) -> None:
        """index/full がなく incremental のみの場合 True を返す."""
        db.add_pipeline_history(
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id="c1",
            processed_at="2026-01-01T00:00:00Z",
            mode="incremental",
        )
        assert db.needs_index_rebuild() is True


class TestDeleteAllSources:
    """delete_all_sources のテスト."""

    def test_delete_all(self, db: MetadataDB) -> None:
        db.register_source(
            source_id="s1",
            source_type="local",
            file_path="local/s1.md",
            title="S1",
            content_hash="h",
            file_size=1,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.register_source(
            source_id="s2",
            source_type="web",
            file_path="web/https/s2.html",
            title="S2",
            content_hash="h",
            file_size=1,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        db.delete_all_sources()
        assert db.source_count() == 0

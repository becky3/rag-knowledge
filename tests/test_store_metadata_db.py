"""metadata_db モジュールのテスト.

仕様: docs/specs/source-store.md — metadata.db スキーマ
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag.store.metadata_db import MetadataDB
from rag.store.models import NULL_COMMIT_HASH, SourceStatus


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
            source_id="web/https/example.com/page.html",
            source_type="web",
            title="Test Page",
            content_hash="abc123",
            file_size=1024,
            collected_at="2026-01-15T10:00:00Z",
            updated_at="2026-01-15T10:00:00Z",
        )

        record = db.get_source("web/https/example.com/page.html")
        assert record is not None
        assert record.source_id == "web/https/example.com/page.html"
        assert record.source_type == "web"
        assert record.title == "Test Page"
        assert record.status is SourceStatus.ACTIVE
        assert record.file_size == 1024

    def test_register_upsert(self, db: MetadataDB) -> None:
        """同一 source_id の再登録で上書きされる."""
        db.register_source(
            source_id="web/https/example.com/page.html",
            source_type="web",
            title="Old Title",
            content_hash="old",
            file_size=100,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.register_source(
            source_id="web/https/example.com/page.html",
            source_type="web",
            title="New Title",
            content_hash="new",
            file_size=200,
            collected_at="2026-01-02T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )

        record = db.get_source("web/https/example.com/page.html")
        assert record is not None
        assert record.title == "New Title"
        assert record.content_hash == "new"
        assert record.status is SourceStatus.ACTIVE

    def test_register_restores_deleted(self, db: MetadataDB) -> None:
        """deleted 状態のソースへの再登録で active に復帰する."""
        db.register_source(
            source_id="local/test.md",
            source_type="local",
            title="Test",
            content_hash="hash1",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.set_status("local/test.md", SourceStatus.DELETED)

        db.register_source(
            source_id="local/test.md",
            source_type="local",
            title="Test Updated",
            content_hash="hash2",
            file_size=20,
            collected_at="2026-01-02T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )

        record = db.get_source("local/test.md")
        assert record is not None
        assert record.status is SourceStatus.ACTIVE
        assert record.title == "Test Updated"

    def test_get_nonexistent(self, db: MetadataDB) -> None:
        assert db.get_source("nonexistent") is None

    def test_update_source(self, db: MetadataDB) -> None:
        db.register_source(
            source_id="local/test.md",
            source_type="local",
            title="Old",
            content_hash="hash",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        db.update_source("local/test.md", title="New Title", file_size=20)
        record = db.get_source("local/test.md")
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
            source_id="local/test.md",
            source_type="local",
            title="Test",
            content_hash="hash",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        with pytest.raises(ValueError, match="不正なフィールド"):
            db.update_source("local/test.md", id=999)

    def test_search_by_type(self, db: MetadataDB) -> None:
        db.register_source(
            source_id="web/https/a.com/p.html",
            source_type="web",
            title="Web1",
            content_hash="h1",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.register_source(
            source_id="local/test.md",
            source_type="local",
            title="Local1",
            content_hash="h2",
            file_size=20,
            collected_at="2026-01-02T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )

        web_results = db.search_sources(source_type="web")
        assert len(web_results) == 1
        assert web_results[0].source_id == "web/https/a.com/p.html"

    def test_search_by_path_prefix(self, db: MetadataDB) -> None:
        for source_id in (
            "local/unity-docs/intro.md",
            "local/unity-docs/sub/api.md",
            "local/other/x.md",
        ):
            db.register_source(
                source_id=source_id,
                source_type="local",
                title=source_id,
                content_hash="h",
                file_size=10,
                collected_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )

        results = db.search_sources(path_prefix="local/unity-docs")
        ids = sorted(r.source_id for r in results)
        assert ids == [
            "local/unity-docs/intro.md",
            "local/unity-docs/sub/api.md",
        ]

    def test_search_by_path_prefix_escapes_like_metacharacters(
        self, db: MetadataDB,
    ) -> None:
        """LIKE のメタ文字（%, _）が含まれるパスでも誤マッチしない."""
        db.register_source(
            source_id="local/a_dir/x.md",  # LIKE _ は任意1文字なので注意
            source_type="local",
            title="t",
            content_hash="h",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.register_source(
            source_id="local/aXdir/y.md",  # _ が任意1文字としてマッチしてはならない
            source_type="local",
            title="t",
            content_hash="h",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        results = db.search_sources(path_prefix="local/a_dir")
        ids = [r.source_id for r in results]
        assert ids == ["local/a_dir/x.md"]

    def test_delete_sources_by_path(self, db: MetadataDB) -> None:
        for source_id in (
            "local/foo/x.md",
            "local/foo/sub/y.md",
            "local/bar/z.md",
        ):
            db.register_source(
                source_id=source_id,
                source_type="local",
                title=source_id,
                content_hash="h",
                file_size=10,
                collected_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )

        db.delete_sources_by_path("local/foo")

        remaining = sorted(r.source_id for r in db.search_sources())
        assert remaining == ["local/bar/z.md"]

    def test_search_by_status(self, db: MetadataDB) -> None:
        db.register_source(
            source_id="local/active.md",
            source_type="local",
            title="Active",
            content_hash="h1",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.register_source(
            source_id="local/deleted.md",
            source_type="local",
            title="Deleted",
            content_hash="h2",
            file_size=20,
            collected_at="2026-01-02T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )
        db.set_status("local/deleted.md", SourceStatus.DELETED)

        active = db.search_sources(status=SourceStatus.ACTIVE)
        assert len(active) == 1
        assert active[0].source_id == "local/active.md"

    def test_set_status(self, db: MetadataDB) -> None:
        db.register_source(
            source_id="local/test.md",
            source_type="local",
            title="Test",
            content_hash="hash",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        db.set_status("local/test.md", SourceStatus.DELETED)
        record = db.get_source("local/test.md")
        assert record is not None
        assert record.status is SourceStatus.DELETED

        db.set_status("local/test.md", SourceStatus.ACTIVE)
        record = db.get_source("local/test.md")
        assert record is not None
        assert record.status is SourceStatus.ACTIVE

    def test_set_status_nonexistent(self, db: MetadataDB) -> None:
        with pytest.raises(KeyError):
            db.set_status("nonexistent", SourceStatus.DELETED)

    def test_source_count(self, db: MetadataDB) -> None:
        assert db.source_count() == 0

        db.register_source(
            source_id="local/s1.md",
            source_type="local",
            title="S1",
            content_hash="h",
            file_size=1,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.register_source(
            source_id="local/s2.md",
            source_type="local",
            title="S2",
            content_hash="h",
            file_size=1,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.set_status("local/s2.md", SourceStatus.DELETED)

        assert db.source_count() == 2
        assert db.source_count(status=SourceStatus.ACTIVE) == 1
        assert db.source_count(status=SourceStatus.DELETED) == 1


class TestMetaColumn:
    """meta JSON カラムのテスト."""

    def test_register_with_meta(self, db: MetadataDB) -> None:
        """meta パラメータ付きで登録できる."""
        meta = json.dumps({"repository": "test-repo", "author": "alice"})
        db.register_source(
            source_id="journal/test-repo/entry.md",
            source_type="journal",
            title="Test Entry",
            content_hash="h",
            file_size=100,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            meta=meta,
        )

        record = db.get_source("journal/test-repo/entry.md")
        assert record is not None
        assert json.loads(record.meta) == {"repository": "test-repo", "author": "alice"}

    def test_register_default_meta(self, db: MetadataDB) -> None:
        """meta 未指定時は空 JSON オブジェクトがデフォルト."""
        db.register_source(
            source_id="local/test.md",
            source_type="local",
            title="Test",
            content_hash="h",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        record = db.get_source("local/test.md")
        assert record is not None
        assert record.meta == "{}"

    def test_upsert_updates_meta(self, db: MetadataDB) -> None:
        """再登録で meta が更新される."""
        db.register_source(
            source_id="journal/repo/e.md",
            source_type="journal",
            title="Entry",
            content_hash="h1",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            meta=json.dumps({"repository": "old"}),
        )
        db.register_source(
            source_id="journal/repo/e.md",
            source_type="journal",
            title="Entry Updated",
            content_hash="h2",
            file_size=20,
            collected_at="2026-01-02T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
            meta=json.dumps({"repository": "new"}),
        )

        record = db.get_source("journal/repo/e.md")
        assert record is not None
        assert json.loads(record.meta)["repository"] == "new"

    def test_list_sources_with_filter(self, db: MetadataDB) -> None:
        """filters で meta の値を絞り込める."""
        for repo in ("repo-a", "repo-b"):
            db.register_source(
                source_id=f"journal/{repo}/entry.md",
                source_type="journal",
                title=f"Entry {repo}",
                content_hash="h",
                file_size=10,
                collected_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
                published_at="2026-01-01T00:00:00Z",
                meta=json.dumps({"repository": repo}),
            )

        results = db.list_sources(
            source_type="journal",
            limit=10,
            filters={"repository": "repo-a"},
        )
        assert len(results) == 1
        assert results[0].source_id == "journal/repo-a/entry.md"

    def test_list_sources_without_filter(self, db: MetadataDB) -> None:
        """filters 未指定で全件返る."""
        for i in range(3):
            db.register_source(
                source_id=f"journal/repo/e{i}.md",
                source_type="journal",
                title=f"Entry {i}",
                content_hash="h",
                file_size=10,
                collected_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
                published_at=f"2026-01-0{i+1}T00:00:00Z",
            )

        results = db.list_sources(source_type="journal", limit=10)
        assert len(results) == 3

    def test_count_sources_by_type_with_filter(self, db: MetadataDB) -> None:
        """count_sources_by_type が filters で絞り込める."""
        entries = [
            ("journal/repo-a/e1.md", "repo-a"),
            ("journal/repo-a/e2.md", "repo-a"),
            ("journal/repo-b/e1.md", "repo-b"),
        ]
        for source_id, repo in entries:
            db.register_source(
                source_id=source_id,
                source_type="journal",
                title="Entry",
                content_hash="h",
                file_size=10,
                collected_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
                meta=json.dumps({"repository": repo}),
            )

        assert db.count_sources_by_type(
            source_type="journal",
            filters={"repository": "repo-a"},
        ) == 2
        assert db.count_sources_by_type(
            source_type="journal",
            filters={"repository": "repo-b"},
        ) == 1

    def test_filter_nonexistent_key(self, db: MetadataDB) -> None:
        """存在しないキーでフィルタすると 0 件."""
        db.register_source(
            source_id="journal/repo/e.md",
            source_type="journal",
            title="Entry",
            content_hash="h",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            meta=json.dumps({"repository": "test"}),
        )

        results = db.list_sources(
            source_type="journal",
            limit=10,
            filters={"nonexistent": "value"},
        )
        assert len(results) == 0

    def test_filter_key_validation_rejects_injection(self, db: MetadataDB) -> None:
        """フィルタキー名に SQL インジェクション文字列が含まれる場合 ValueError."""
        with pytest.raises(ValueError, match="不正な文字"):
            db.list_sources(
                source_type="journal",
                limit=10,
                filters={"') OR 1=1 --": "value"},
            )

    def test_filter_key_validation_rejects_dot(self, db: MetadataDB) -> None:
        """フィルタキー名にドットが含まれる場合 ValueError."""
        with pytest.raises(ValueError, match="不正な文字"):
            db.count_sources_by_type(
                source_type="journal",
                filters={"nested.key": "value"},
            )

    def test_update_source_meta(self, db: MetadataDB) -> None:
        """update_source で meta を更新できる."""
        db.register_source(
            source_id="journal/repo/e.md",
            source_type="journal",
            title="Entry",
            content_hash="h",
            file_size=10,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        new_meta = json.dumps({"repository": "updated"})
        db.update_source("journal/repo/e.md", meta=new_meta)

        record = db.get_source("journal/repo/e.md")
        assert record is not None
        assert json.loads(record.meta)["repository"] == "updated"


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

    def test_needs_index_rebuild_ignores_filtered_full(
        self, db: MetadataDB,
    ) -> None:
        """filter 付き full rebuild は判定基準から除外される (#678).

        filter 付きは subset しか触っていないため、未フィルタの full/index
        履歴がない限り True（rebuild 必要）を返す。
        """
        db.add_pipeline_history(
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id="c1",
            processed_at="2026-01-01T00:00:00Z",
            mode="full",
            filter_path="local/foo",
        )
        assert db.needs_index_rebuild() is True

    def test_needs_index_rebuild_ignores_filtered_source_type(
        self, db: MetadataDB,
    ) -> None:
        """filter (source_type) 付き index rebuild も判定基準から除外される."""
        db.add_pipeline_history(
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id="c1",
            processed_at="2026-01-01T00:00:00Z",
            mode="index",
            filter_source_type="local",
        )
        assert db.needs_index_rebuild() is True

    def test_needs_index_rebuild_unfiltered_after_filtered(
        self, db: MetadataDB,
    ) -> None:
        """filter 付き → 未フィルタの順で記録されると未フィルタが基準になる."""
        db.add_pipeline_history(
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id="c1",
            processed_at="2026-01-01T00:00:00Z",
            mode="full",
            filter_path="local/foo",
        )
        db.add_pipeline_history(
            from_commit_id="c1",
            to_commit_id="c2",
            processed_at="2026-01-02T00:00:00Z",
            mode="full",
        )
        assert db.needs_index_rebuild() is False

    def test_add_pipeline_history_records_filter(
        self, db: MetadataDB,
    ) -> None:
        """filter スコープが pipeline_history に保存される."""
        db.add_pipeline_history(
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id="c1",
            processed_at="2026-01-01T00:00:00Z",
            mode="full",
            filter_source_type="local",
            filter_path="",
        )
        db.add_pipeline_history(
            from_commit_id="c1",
            to_commit_id="c2",
            processed_at="2026-01-02T00:00:00Z",
            mode="index",
            filter_source_type="",
            filter_path="local/foo",
        )
        history = db.get_pipeline_history()
        assert history[0].filter_source_type == "local"
        assert history[0].filter_path == ""
        assert history[1].filter_source_type == ""
        assert history[1].filter_path == "local/foo"


class TestDeleteAllSources:
    """delete_all_sources のテスト."""

    def test_delete_all(self, db: MetadataDB) -> None:
        db.register_source(
            source_id="local/s1.md",
            source_type="local",
            title="S1",
            content_hash="h",
            file_size=1,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.register_source(
            source_id="web/https/s2.html",
            source_type="web",
            title="S2",
            content_hash="h",
            file_size=1,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        db.delete_all_sources()
        assert db.source_count() == 0


class TestDeleteSourcesByType:
    """delete_sources_by_type のテスト."""

    def test_delete_by_type(self, db: MetadataDB) -> None:
        """指定 type のみ削除し、他 type は保持する."""
        db.register_source(
            source_id="local/s1.md",
            source_type="local",
            title="S1",
            content_hash="h",
            file_size=1,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.register_source(
            source_id="web/https/s2.html",
            source_type="web",
            title="S2",
            content_hash="h",
            file_size=1,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        db.register_source(
            source_id="web/https/s3.html",
            source_type="web",
            title="S3",
            content_hash="h",
            file_size=1,
            collected_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

        db.delete_sources_by_type("web")

        # web の 2 件が削除され、local の 1 件が残る
        assert db.source_count() == 1
        assert db.get_source("local/s1.md") is not None
        assert db.get_source("web/https/s2.html") is None
        assert db.get_source("web/https/s3.html") is None

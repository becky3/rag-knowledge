"""パイプライン制御コントローラのテスト.

テスト方針:
- パイプライン全体フローの結合テスト
- git diff による変更検知のテスト
- 差分更新・全再構築モードのテスト
- pipeline_history の記録・参照テスト
- コンバーター/インデクサーのスタブを使ったテスト
- .meta のみ変更の検出テスト
- リネーム時の metadata.db 操作テスト
- エラー時のスキップ・pipeline_history 非記録テスト
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from rag.pipeline.controller import PipelineController
from rag.pipeline.models import PipelineMode
from rag.store.models import NULL_COMMIT_HASH, SourceMetadata, SourceType
from rag.store.source_store import SourceStore


# --- スタブ ---


class StubConverter:
    """コンバーターのスタブ実装."""

    def __init__(self) -> None:
        self.converted: list[str] = []
        self.deleted: list[str] = []
        self.cleared: list[SourceType | None] = []
        self.fail_on: set[str] = set()

    def convert(
        self,
        file_path: str,
        source_store_dir: Path,
        converted_store_dir: Path,
    ) -> Path:
        if file_path in self.fail_on:
            msg = f"converter error: {file_path}"
            raise RuntimeError(msg)
        self.converted.append(file_path)
        dest = converted_store_dir / file_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        source = source_store_dir / file_path
        if source.exists():
            dest.write_bytes(source.read_bytes())
        else:
            dest.write_text("converted", encoding="utf-8")
        return dest

    def delete(self, file_path: str, converted_store_dir: Path) -> None:
        self.deleted.append(file_path)
        dest = converted_store_dir / file_path
        if dest.exists():
            dest.unlink()

    def clear(
        self,
        converted_store_dir: Path,
        source_type: SourceType | None = None,
    ) -> None:
        self.cleared.append(source_type)
        if source_type:
            target = converted_store_dir / source_type
        else:
            target = converted_store_dir
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)


class StubIndexer:
    """インデクサーのスタブ実装."""

    def __init__(self) -> None:
        self.added: list[str] = []
        self.updated: list[str] = []
        self.deleted_ids: list[str] = []
        self.upserted: list[str] = []
        self.cleared: list[SourceType | None] = []
        self.fail_on: set[str] = set()

    async def add(
        self,
        source_id: str,
        converted_path: Path,
        metadata: SourceMetadata,
    ) -> None:
        if source_id in self.fail_on:
            msg = f"indexer add error: {source_id}"
            raise RuntimeError(msg)
        self.added.append(source_id)

    async def update(
        self,
        source_id: str,
        converted_path: Path,
        metadata: SourceMetadata,
    ) -> None:
        self.updated.append(source_id)

    async def delete(self, source_id: str) -> None:
        self.deleted_ids.append(source_id)

    async def upsert_metadata(
        self,
        source_id: str,
        metadata: SourceMetadata,
    ) -> None:
        self.upserted.append(source_id)

    async def clear(self, source_type: SourceType | None = None) -> None:
        self.cleared.append(source_type)

    def set_bm25_deferred_save(self, enabled: bool) -> None:
        pass

    def flush_bm25(self) -> None:
        pass

    @contextlib.contextmanager
    def bm25_deferred(self) -> Iterator[None]:
        yield


# --- フィクスチャ ---


@pytest.fixture()
def workspace(tmp_path: Path) -> dict[str, Path]:
    """source_store + converted_store のワークスペース."""
    source_dir = tmp_path / "source_store"
    source_dir.mkdir()
    converted_dir = tmp_path / "converted_store"
    converted_dir.mkdir()
    return {"source": source_dir, "converted": converted_dir}


@pytest.fixture()
def controller(
    workspace: dict[str, Path],
) -> tuple[PipelineController, StubConverter, StubIndexer]:
    """PipelineController とスタブを返す."""
    source_store = SourceStore(workspace["source"])
    source_store.initialize()
    converter = StubConverter()
    indexer = StubIndexer()
    ctrl = PipelineController(
        source_store=source_store,
        converted_store_dir=workspace["converted"],
        converter=converter,
        indexer=indexer,
    )
    return ctrl, converter, indexer


def _place_local_file(
    source_dir: Path,
    rel_path: str,
    content: str = "test content",
) -> None:
    """source_store にローカルファイルを配置する."""
    full = source_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


def _place_web_file(
    source_dir: Path,
    rel_path: str,
    content: str = "<html></html>",
    *,
    title: str = "Test Page",
    source_id: str = "",
) -> None:
    """source_store に Web ファイル + .meta を配置する."""
    full = source_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    meta = {
        "source_type": "web",
        "title": title,
        "collected_at": "2026-01-01T00:00:00+00:00",
    }
    meta_path = full.with_name(full.name + ".meta")
    with open(meta_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(meta, f, allow_unicode=True)


# --- テスト ---


class TestCommit:
    """commit のテスト."""

    def test_commit_creates_git_repo(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, _, _ = controller
        _place_local_file(workspace["source"], "local/test.txt")
        commit_id = ctrl.commit("ingest(local): manual update")
        assert commit_id is not None
        assert (workspace["source"] / ".git").exists()

    def test_commit_no_changes(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, _, _ = controller
        _place_local_file(workspace["source"], "local/test.txt")
        ctrl.commit("first")
        result = ctrl.commit("empty")
        assert result is None


class TestRunIncremental:
    """差分更新のテスト."""

    async def test_no_commits_returns_empty(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
    ) -> None:
        ctrl, _, _ = controller
        summary = await ctrl.run_incremental()
        assert summary.mode == PipelineMode.INCREMENTAL
        assert summary.total_files == 0
        assert summary.processed == 0

    async def test_first_run_processes_all(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, converter, indexer = controller
        _place_local_file(workspace["source"], "local/a.txt", "content a")
        _place_local_file(workspace["source"], "local/b.txt", "content b")
        ctrl.commit("ingest(local): manual update")

        summary = await ctrl.run_incremental()

        assert summary.mode == PipelineMode.INCREMENTAL
        assert summary.processed == 2
        assert summary.errors == []
        assert len(converter.converted) == 2
        assert len(indexer.added) == 2

        # pipeline_history が記録される
        history = ctrl.db.get_pipeline_history()
        assert len(history) == 1
        assert history[0].from_commit_id == NULL_COMMIT_HASH

    async def test_incremental_detects_added(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, converter, indexer = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("first")
        await ctrl.run_incremental()

        converter.converted.clear()
        indexer.added.clear()

        _place_local_file(workspace["source"], "local/b.txt")
        ctrl.commit("add b")

        summary = await ctrl.run_incremental()
        assert summary.processed == 1
        assert converter.converted == ["local/b.txt"]
        assert "local/b.txt" in indexer.added

    async def test_incremental_detects_modified(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, converter, indexer = controller
        _place_local_file(workspace["source"], "local/a.txt", "v1")
        ctrl.commit("first")
        await ctrl.run_incremental()

        converter.converted.clear()

        _place_local_file(workspace["source"], "local/a.txt", "v2")
        ctrl.commit("modify a")

        summary = await ctrl.run_incremental()
        assert summary.processed == 1
        assert converter.converted == ["local/a.txt"]
        assert "local/a.txt" in indexer.updated

    async def test_incremental_detects_deleted(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, converter, indexer = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("first")
        await ctrl.run_incremental()

        (workspace["source"] / "local" / "a.txt").unlink()
        ctrl.commit("delete a")

        summary = await ctrl.run_incremental()
        assert summary.processed == 1
        assert "local/a.txt" in converter.deleted
        assert "local/a.txt" in indexer.deleted_ids

        # metadata.db で論理削除されている
        record = ctrl.db.get_source("local/a.txt")
        assert record is not None
        assert record.status == "deleted"

    async def test_incremental_detects_renamed(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, converter, indexer = controller
        _place_local_file(workspace["source"], "local/old.txt")
        ctrl.commit("first")
        await ctrl.run_incremental()

        src = workspace["source"] / "local" / "old.txt"
        src.rename(workspace["source"] / "local" / "new.txt")
        ctrl.commit("rename")

        summary = await ctrl.run_incremental()
        assert summary.processed == 1
        assert "local/new.txt" in converter.converted
        assert "local/old.txt" in converter.deleted
        assert "local/old.txt" in indexer.deleted_ids
        assert "local/new.txt" in indexer.added

    async def test_no_changes_returns_empty(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, _, _ = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("first")
        await ctrl.run_incremental()

        summary = await ctrl.run_incremental()
        assert summary.total_files == 0
        assert summary.processed == 0

    async def test_meta_only_change(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, converter, indexer = controller
        _place_web_file(
            workspace["source"],
            "web/example.com/page.html",
            title="Old Title",
            source_id="web/example.com/page.html",
        )
        ctrl.commit("first")
        await ctrl.run_incremental()

        converter.converted.clear()
        indexer.added.clear()

        # .meta のみ更新
        meta_path = (
            workspace["source"]
            / "web"
            / "example.com"
            / "page.html.meta"
        )
        meta = {
            "source_type": "web",
            "title": "New Title",
            "collected_at": "2026-01-01T00:00:00+00:00",
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(meta, f, allow_unicode=True)
        ctrl.commit("update meta")

        summary = await ctrl.run_incremental()
        assert summary.processed == 1
        # コンバーターは呼ばれない
        assert converter.converted == []
        # インデクサーはメタデータ更新のみ
        assert "web/example.com/page.html" in indexer.upserted

    async def test_error_skips_file_no_history(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, converter, indexer = controller
        _place_local_file(workspace["source"], "local/good.txt")
        _place_local_file(workspace["source"], "local/bad.txt")
        converter.fail_on.add("local/bad.txt")
        ctrl.commit("first")

        summary = await ctrl.run_incremental()
        assert summary.processed == 1
        assert summary.skipped == 1
        assert len(summary.errors) == 1

        # pipeline_history は記録されない
        history = ctrl.db.get_pipeline_history()
        assert len(history) == 0

    async def test_invalid_last_commit_processes_all(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, converter, _ = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("first")

        # 偽の pipeline_history を挿入
        ctrl.db.add_pipeline_history(
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id="deadbeef" * 5,
            processed_at="2026-01-01T00:00:00+00:00",
        )

        _place_local_file(workspace["source"], "local/b.txt")
        ctrl.commit("second")

        summary = await ctrl.run_incremental()
        # 全ファイル処理（a.txt + b.txt）
        assert summary.processed == 2
        assert "local/a.txt" in converter.converted
        assert "local/b.txt" in converter.converted


class TestDeleteAndReAdd:
    """物理削除 → 再追加フローのテスト."""

    async def test_delete_and_readd_same_content(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """同一内容ファイルの削除→再追加でインデックスに復帰する."""
        ctrl, converter, indexer = controller
        content = "test content for readd"

        # 1. 初回追加
        _place_local_file(workspace["source"], "local/a.txt", content)
        ctrl.commit("add a")
        await ctrl.run_incremental()
        assert "local/a.txt" in indexer.added

        # 2. 物理削除（remove_file 相当: ファイル削除 + commit + pipeline）
        (workspace["source"] / "local" / "a.txt").unlink()
        ctrl.commit("delete a")
        await ctrl.run_incremental()
        assert "local/a.txt" in indexer.deleted_ids
        record = ctrl.db.get_source("local/a.txt")
        assert record is not None
        assert record.status == "deleted"

        # スタブをリセット
        indexer.added.clear()
        indexer.deleted_ids.clear()
        converter.converted.clear()

        # 3. 同一内容で再追加
        _place_local_file(workspace["source"], "local/a.txt", content)
        # place_file 相当: metadata.db を active に戻す
        ctrl.source_store._db.register_source(
            source_id="local/a.txt",
            source_type="local",
            title="a",
            content_hash="dummy",
            file_size=len(content),
            collected_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        ctrl.commit("readd a")
        summary = await ctrl.run_incremental()

        # git diff は A（追加）として検知し、パイプラインが処理する
        assert summary.processed == 1
        assert "local/a.txt" in indexer.added
        record = ctrl.db.get_source("local/a.txt")
        assert record is not None
        assert record.status == "active"

    async def test_delete_and_readd_different_content(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """異なる内容でのファイル削除→再追加でインデックスに復帰する."""
        ctrl, converter, indexer = controller

        # 1. 初回追加
        _place_local_file(workspace["source"], "local/b.txt", "original")
        ctrl.commit("add b")
        await ctrl.run_incremental()

        # 2. 物理削除
        (workspace["source"] / "local" / "b.txt").unlink()
        ctrl.commit("delete b")
        await ctrl.run_incremental()

        indexer.added.clear()
        indexer.deleted_ids.clear()
        converter.converted.clear()

        # 3. 異なる内容で再追加
        _place_local_file(workspace["source"], "local/b.txt", "new content")
        ctrl.source_store._db.register_source(
            source_id="local/b.txt",
            source_type="local",
            title="b",
            content_hash="dummy2",
            file_size=11,
            collected_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        ctrl.commit("readd b with new content")
        summary = await ctrl.run_incremental()

        assert summary.processed == 1
        assert "local/b.txt" in indexer.added


class TestRemoveFile:
    """source_store.remove_file のテスト."""

    def test_remove_file_deletes_file_and_meta(
        self,
        workspace: dict[str, Path],
    ) -> None:
        """ファイルと .meta を削除する."""
        source_dir = workspace["source"]
        source_store = SourceStore(source_dir)
        source_store.initialize()

        # web ファイル + .meta を配置
        _place_web_file(
            source_dir,
            "web/https/example.com/page.html",
            "<html>test</html>",
            title="Test",
            source_id="web/https/example.com/page.html",
        )
        # metadata.db に登録
        source_store._db.register_source(
            source_id="web/https/example.com/page.html",
            source_type="web",
            title="Test",
            content_hash="dummy",
            file_size=17,
            collected_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

        file_path = source_dir / "web/https/example.com/page.html"
        meta_path = source_dir / "web/https/example.com/page.html.meta"
        assert file_path.exists()
        assert meta_path.exists()

        source_store.remove_file("web/https/example.com/page.html")

        assert not file_path.exists()
        assert not meta_path.exists()

    def test_remove_file_raises_for_unknown_source(
        self,
        workspace: dict[str, Path],
    ) -> None:
        """存在しない source_id で KeyError."""
        source_dir = workspace["source"]
        source_store = SourceStore(source_dir)
        source_store.initialize()

        with pytest.raises(KeyError):
            source_store.remove_file("nonexistent")

    def test_remove_file_tolerates_missing_file(
        self,
        workspace: dict[str, Path],
    ) -> None:
        """ファイルが既にディスク上にない場合もエラーにならない."""
        source_dir = workspace["source"]
        source_store = SourceStore(source_dir)
        source_store.initialize()

        # DB にだけ登録（ファイルは作らない）
        source_store._db.register_source(
            source_id="local/ghost.txt",
            source_type="local",
            title="ghost",
            content_hash="dummy",
            file_size=0,
            collected_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

        # エラーにならない
        source_store.remove_file("local/ghost.txt")


class TestRunFullRebuild:
    """全再構築のテスト."""

    async def test_rebuild_rejects_uncommitted_changes(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """未コミットの変更がある場合、rebuild はエラーになる."""
        ctrl, _, _ = controller
        # 最初のコミットを作る（has_commits が True になるように）
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("first")

        # 未コミットのファイルを追加
        _place_local_file(workspace["source"], "local/uncommitted.txt")

        with pytest.raises(RuntimeError, match="未コミットの変更があります"):
            await ctrl.run_full_rebuild()

    async def test_rebuilds_all(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, converter, indexer = controller
        _place_local_file(workspace["source"], "local/a.txt", "content a")
        _place_local_file(workspace["source"], "local/b.txt", "content b")
        ctrl.commit("initial")
        await ctrl.run_incremental()

        converter.converted.clear()
        indexer.added.clear()

        summary = await ctrl.run_full_rebuild()

        assert summary.mode == PipelineMode.FULL_REBUILD
        assert summary.processed == 2
        assert len(converter.converted) == 2
        assert len(indexer.added) == 2
        assert converter.cleared == [None]
        assert indexer.cleared == [None]

    async def test_source_type_filter(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, converter, indexer = controller
        _place_local_file(workspace["source"], "local/a.txt")
        _place_web_file(workspace["source"], "web/example.com/page.html")
        ctrl.commit("initial")
        await ctrl.run_incremental()

        converter.converted.clear()
        indexer.added.clear()

        summary = await ctrl.run_full_rebuild(source_type="local")

        assert summary.processed == 1
        assert converter.converted == ["local/a.txt"]
        assert converter.cleared == ["local"]
        assert indexer.cleared == ["local"]

        # web のレコードは metadata.db に保持されている
        web_record = ctrl.db.get_source("web/example.com/page.html")
        assert web_record is not None
        assert web_record.source_type == "web"

    async def test_no_files_returns_empty(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
    ) -> None:
        ctrl, _, _ = controller
        ctrl.init_repo()
        ctrl.commit("initial")
        summary = await ctrl.run_full_rebuild()
        assert summary.total_files == 0
        assert summary.processed == 0

    async def test_concurrent_full_rebuild(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """concurrency > 1 で全再構築が正常に完了する."""
        ctrl, converter, indexer = controller
        for name in ["a.txt", "b.txt", "c.txt"]:
            _place_local_file(workspace["source"], f"local/{name}")
        ctrl.commit("initial")
        await ctrl.run_incremental()

        converter.converted.clear()
        indexer.added.clear()

        summary = await ctrl.run_full_rebuild(concurrency=3)

        assert summary.mode == PipelineMode.FULL_REBUILD
        assert summary.processed == 3
        assert len(converter.converted) == 3
        assert len(indexer.added) == 3


class TestRunConvertOnly:
    """コンバートのみ再実行のテスト."""

    async def test_reconverts_all(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, converter, indexer = controller
        _place_local_file(workspace["source"], "local/a.txt")
        _place_local_file(workspace["source"], "local/b.txt")
        ctrl.commit("initial")
        await ctrl.run_incremental()

        converter.converted.clear()
        indexer.added.clear()

        summary = await ctrl.run_convert_only()

        assert summary.mode == PipelineMode.CONVERT_ONLY
        assert summary.processed == 2
        assert len(converter.converted) == 2
        # インデクサーは呼ばれない
        assert len(indexer.added) == 0


class TestRunIndexOnly:
    """インデックスのみ再構築のテスト."""

    async def test_reindexes_all(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, converter, indexer = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("initial")
        await ctrl.run_incremental()

        indexer.added.clear()

        # converted_store にファイルがあることを確認
        assert (workspace["converted"] / "local" / "a.txt").exists()

        summary = await ctrl.run_index_only()

        assert summary.mode == PipelineMode.INDEX_ONLY
        assert summary.processed == 1
        assert indexer.cleared == [None]
        assert "local/a.txt" in indexer.added

    async def test_html_source_maps_to_md_converted(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """HTML ソースの run_index_only は .md に変換されたパスを探す."""
        ctrl, _, indexer = controller
        _place_web_file(
            workspace["source"],
            "web/example.com/page.html",
            source_id="web/example.com/page.html",
        )
        ctrl.commit("initial")
        await ctrl.run_incremental()

        indexer.added.clear()

        # converted_store に .md ファイルを配置（実際の converter が作るパス）
        converted_md = workspace["converted"] / "web" / "example.com" / "page.md"
        converted_md.parent.mkdir(parents=True, exist_ok=True)
        converted_md.write_text("converted content", encoding="utf-8")

        summary = await ctrl.run_index_only()

        assert summary.processed == 1
        assert "web/example.com/page.html" in indexer.added

    async def test_json_source_maps_to_md_converted(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """JSON ソースの run_index_only は .md に変換されたパスを探す."""
        ctrl, _, indexer = controller
        # Zenn JSON ソースを配置
        full = workspace["source"] / "zenn" / "articles" / "article1.json"
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text('{"title": "test"}', encoding="utf-8")
        meta = {
            "source_type": "zenn",
            "title": "Test Article",
            "collected_at": "2026-01-01T00:00:00+00:00",
        }
        meta_path = full.with_name(full.name + ".meta")
        with open(meta_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(meta, f, allow_unicode=True)
        ctrl.commit("initial")
        await ctrl.run_incremental()

        indexer.added.clear()

        # converted_store に .md ファイルを配置
        converted_md = workspace["converted"] / "zenn" / "articles" / "article1.md"
        converted_md.parent.mkdir(parents=True, exist_ok=True)
        converted_md.write_text("converted content", encoding="utf-8")

        summary = await ctrl.run_index_only()

        assert summary.processed == 1
        assert "zenn/articles/article1.json" in indexer.added

    async def test_skips_missing_converted(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, _, indexer = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("initial")
        await ctrl.run_incremental()

        # converted_store をクリアしてからインデックス再構築
        shutil.rmtree(workspace["converted"])
        workspace["converted"].mkdir()

        summary = await ctrl.run_index_only()
        assert summary.skipped == 1
        assert summary.processed == 0

    async def test_concurrent_index_only(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """concurrency > 1 で複数ソースが並列にインデックスされる."""
        ctrl, _, indexer = controller
        for name in ["a.txt", "b.txt", "c.txt"]:
            _place_local_file(workspace["source"], f"local/{name}")
        ctrl.commit("initial")
        await ctrl.run_incremental()

        indexer.added.clear()

        summary = await ctrl.run_index_only(concurrency=2)

        assert summary.mode == PipelineMode.INDEX_ONLY
        assert summary.processed == 3
        assert summary.skipped == 0
        assert len(indexer.added) == 3

    async def test_concurrent_index_handles_errors(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """concurrency > 1 でエラーが発生しても他ソースの処理は継続する."""
        ctrl, _, indexer = controller
        for name in ["a.txt", "b.txt", "c.txt"]:
            _place_local_file(workspace["source"], f"local/{name}")
        ctrl.commit("initial")
        await ctrl.run_incremental()

        indexer.added.clear()
        indexer.fail_on = {"local/b.txt"}

        summary = await ctrl.run_index_only(concurrency=4)

        assert summary.processed == 2
        assert summary.skipped == 1
        assert len(summary.errors) == 1


class TestPipelineHistory:
    """pipeline_history のテスト."""

    async def test_history_recorded_on_success(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, _, _ = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("first")
        await ctrl.run_incremental()

        history = ctrl.db.get_pipeline_history()
        assert len(history) == 1
        assert history[0].from_commit_id == NULL_COMMIT_HASH

    async def test_history_not_recorded_on_error(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, converter, _ = controller
        _place_local_file(workspace["source"], "local/a.txt")
        converter.fail_on.add("local/a.txt")
        ctrl.commit("first")
        await ctrl.run_incremental()

        history = ctrl.db.get_pipeline_history()
        assert len(history) == 0

    async def test_incremental_uses_last_commit_id(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, converter, _ = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("first")
        await ctrl.run_incremental()

        converter.converted.clear()

        _place_local_file(workspace["source"], "local/b.txt")
        ctrl.commit("second")
        summary = await ctrl.run_incremental()

        # 2回目は b.txt のみ処理
        assert summary.processed == 1
        assert converter.converted == ["local/b.txt"]

        history = ctrl.db.get_pipeline_history()
        assert len(history) == 2


class TestClassifyChanges:
    """_classify_changes のテスト."""

    def test_data_and_meta_together(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
    ) -> None:
        ctrl, _, _ = controller
        raw = [
            ("M", "web/example.com/page.html", ""),
            ("M", "web/example.com/page.html.meta", ""),
        ]
        changes = ctrl._classify_changes(raw)
        assert len(changes) == 1
        assert changes[0].file_path == "web/example.com/page.html"
        assert changes[0].status.value == "modified"

    def test_meta_only(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
    ) -> None:
        ctrl, _, _ = controller
        raw = [
            ("M", "web/example.com/page.html.meta", ""),
        ]
        changes = ctrl._classify_changes(raw)
        assert len(changes) == 1
        assert changes[0].status.value == "meta_only"
        assert changes[0].file_path == "web/example.com/page.html"

    def test_gitignore_filtered(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
    ) -> None:
        ctrl, _, _ = controller
        raw = [
            ("M", ".gitignore", ""),
            ("A", "local/test.txt", ""),
        ]
        changes = ctrl._classify_changes(raw)
        assert len(changes) == 1
        assert changes[0].file_path == "local/test.txt"


class TestRenamedLocalSourceId:
    """local リネーム時の source_id 更新テスト."""

    async def test_local_rename_deletes_old_inserts_new(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, _, indexer = controller
        _place_local_file(workspace["source"], "local/old.txt")
        ctrl.commit("first")
        await ctrl.run_incremental()

        old_record = ctrl.db.get_source("local/old.txt")
        assert old_record is not None
        assert old_record.status == "active"

        src = workspace["source"] / "local" / "old.txt"
        src.rename(workspace["source"] / "local" / "new.txt")
        ctrl.commit("rename")
        await ctrl.run_incremental()

        # 旧レコードは論理削除
        old_record = ctrl.db.get_source("local/old.txt")
        assert old_record is not None
        assert old_record.status == "deleted"

        # 新レコードが作成されている
        new_record = ctrl.db.get_source("local/new.txt")
        assert new_record is not None
        assert new_record.status == "active"

        # インデクサー: 旧削除 + 新追加
        assert "local/old.txt" in indexer.deleted_ids
        assert "local/new.txt" in indexer.added


class TestUncommittedChanges:
    """未コミット変更の扱いのテスト."""

    async def test_incremental_auto_commits_uncommitted_changes(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """incremental は未コミット変更を自動コミットして処理する."""
        ctrl, converter, indexer = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("first")
        await ctrl.run_incremental()

        # 未コミットのファイルを追加
        _place_local_file(workspace["source"], "local/new.txt")

        converter.converted.clear()
        indexer.added.clear()

        # incremental は自動コミットして差分処理する
        summary = await ctrl.run_incremental()
        assert summary.mode == PipelineMode.INCREMENTAL
        assert summary.processed == 1  # new.txt のみ（差分）

    async def test_incremental_auto_commit_message_format(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """incremental の自動コミットメッセージが仕様通りの形式になる."""
        ctrl, _, _ = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("first")
        await ctrl.run_incremental()

        # 未コミットのファイルを追加
        _place_local_file(workspace["source"], "local/new.txt")

        await ctrl.run_incremental()

        # git log で最新コミットメッセージを確認
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=str(workspace["source"]),
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
        )
        assert result.stdout.strip() == "auto-commit: incremental"

    async def test_incremental_no_uncommitted_changes(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """incremental で未コミット変更がない場合は正常に動作する."""
        ctrl, _, _ = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("first")
        await ctrl.run_incremental()

        # 変更なしで再実行 → 差分なしで正常終了
        summary = await ctrl.run_incremental()
        assert summary.mode == PipelineMode.INCREMENTAL
        assert summary.processed == 0

    async def test_full_rebuild_with_uncommitted_raises(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """full rebuild は未コミット変更があると RuntimeError."""
        ctrl, _, _ = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("first")

        _place_local_file(workspace["source"], "local/uncommitted.txt")

        with pytest.raises(RuntimeError, match="未コミットの変更があります"):
            await ctrl.run_full_rebuild()

    async def test_convert_only_with_uncommitted_raises(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """convert_only は未コミット変更があると RuntimeError."""
        ctrl, _, _ = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("first")
        await ctrl.run_incremental()

        _place_local_file(workspace["source"], "local/a.txt", "updated")

        with pytest.raises(RuntimeError, match="未コミットの変更があります"):
            await ctrl.run_convert_only()

    async def test_index_only_with_uncommitted_raises(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """index_only は未コミット変更があると RuntimeError."""
        ctrl, _, _ = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("first")
        await ctrl.run_incremental()

        _place_local_file(workspace["source"], "local/a.txt", "updated")

        with pytest.raises(RuntimeError, match="未コミットの変更があります"):
            await ctrl.run_index_only()

    async def test_uncommitted_check_scoped_by_source_type(
        self,
        controller: tuple[PipelineController, StubConverter, StubIndexer],
        workspace: dict[str, Path],
    ) -> None:
        """source_type 指定時、他の source_type の未コミット変更はエラーにならない."""
        ctrl, _, _ = controller
        _place_local_file(workspace["source"], "local/a.txt")
        ctrl.commit("first")
        await ctrl.run_incremental()

        # web に未コミット変更を追加（local は変更なし）
        _place_web_file(
            workspace["source"],
            "web/example.com/new.html",
            title="New Page",
            source_id="web/example.com/new.html",
        )

        # source_type="local" で rebuild → web の変更は無視されるので成功する
        summary = await ctrl.run_full_rebuild(source_type="local")
        assert summary.mode == PipelineMode.FULL_REBUILD

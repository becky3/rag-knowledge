"""ChangeDetector (RealChangeDetector) のテスト.

仕様: docs/specs/pipeline-controller.md
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from rag.pipeline.controller import PipelineController
from rag.pipeline.protocols import ConverterProtocol, IndexerProtocol
from rag.store.models import SourceMetadata, SourceType
from rag.store.source_store import SourceStore


class StubConverter:
    """ConverterProtocol スタブ."""

    def __init__(self) -> None:
        self.converted: list[str] = []
        self.deleted: list[str] = []
        self.cleared: list[Any] = []

    def convert(
        self,
        file_path: str,
        source_store_dir: Path,
        converted_store_dir: Path,
    ) -> Path:
        self.converted.append(file_path)
        return converted_store_dir / file_path

    def delete(self, file_path: str, converted_store_dir: Path) -> None:
        self.deleted.append(file_path)

    def clear(
        self,
        converted_store_dir: Path,
        source_type: SourceType | None = None,
        *,
        path: str | None = None,
    ) -> None:
        self.cleared.append((source_type, path))


class StubIndexer:
    """IndexerProtocol スタブ."""

    def __init__(self) -> None:
        self.added: list[str] = []
        self.updated: list[str] = []
        self.deleted: list[str] = []
        self.upserted: list[str] = []
        self.cleared: list[SourceType | None] = []
        self.cleared_calls: list[tuple[SourceType | None, str | None]] = []

    async def add(
        self,
        source_id: str,
        converted_path: Path,
        metadata: SourceMetadata,
    ) -> None:
        self.added.append(source_id)

    async def update(
        self,
        source_id: str,
        converted_path: Path,
        metadata: SourceMetadata,
    ) -> None:
        self.updated.append(source_id)

    async def delete(self, source_id: str) -> None:
        self.deleted.append(source_id)

    async def upsert_metadata(
        self,
        source_id: str,
        metadata: SourceMetadata,
    ) -> None:
        self.upserted.append(source_id)

    async def clear(
        self,
        source_type: SourceType | None = None,
        *,
        path: str | None = None,
    ) -> None:
        self.cleared.append(source_type)
        self.cleared_calls.append((source_type, path))

    @contextlib.contextmanager
    def batch_writes(self) -> Iterator[None]:
        yield


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
) -> tuple[PipelineController, ConverterProtocol, IndexerProtocol]:
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


class TestClassifyChanges:
    """ChangeDetector.classify_changes のテスト."""

    def test_data_and_meta_together(
        self,
        controller: tuple[PipelineController, ConverterProtocol, IndexerProtocol],
    ) -> None:
        ctrl, _, _ = controller
        raw = [
            ("M", "web/example.com/page.html", ""),
            ("M", "web/example.com/page.html.meta", ""),
        ]
        changes = ctrl._change_detector.classify_changes(raw)
        assert len(changes) == 1
        assert changes[0].file_path == "web/example.com/page.html"
        assert changes[0].status.value == "modified"

    def test_meta_only(
        self,
        controller: tuple[PipelineController, ConverterProtocol, IndexerProtocol],
    ) -> None:
        ctrl, _, _ = controller
        raw = [
            ("M", "web/example.com/page.html.meta", ""),
        ]
        changes = ctrl._change_detector.classify_changes(raw)
        assert len(changes) == 1
        assert changes[0].status.value == "meta_only"
        assert changes[0].file_path == "web/example.com/page.html"

    def test_gitignore_filtered(
        self,
        controller: tuple[PipelineController, ConverterProtocol, IndexerProtocol],
    ) -> None:
        ctrl, _, _ = controller
        raw = [
            ("M", ".gitignore", ""),
            ("A", "local/test.txt", ""),
        ]
        changes = ctrl._change_detector.classify_changes(raw)
        assert len(changes) == 1
        assert changes[0].file_path == "local/test.txt"


class TestClassifyChangesMedia:
    """ChangeDetector.classify_changes の media 親 JSON 検出テスト."""

    def test_media_added_triggers_parent_json_and_excludes_self(
        self,
        controller: tuple[PipelineController, ConverterProtocol, IndexerProtocol],
        workspace: dict[str, Path],
    ) -> None:
        """media 追加時、親 JSON のみが ChangeEntry として返ることを検証する（#597）."""
        ctrl, _, _ = controller
        _place_local_file(
            workspace["source"],
            "bluesky/did/2026/04/rkey1.json",
            '{"post": {}}',
        )
        raw = [
            ("A", "bluesky/did/2026/04/media/rkey1/image_0.webp", ""),
        ]
        changes = ctrl._change_detector.classify_changes(raw)
        paths = {c.file_path: c.status.value for c in changes}
        assert "bluesky/did/2026/04/media/rkey1/image_0.webp" not in paths
        assert paths == {"bluesky/did/2026/04/rkey1.json": "modified"}

    def test_media_added_no_parent_json_file_yields_no_changes(
        self,
        controller: tuple[PipelineController, ConverterProtocol, IndexerProtocol],
    ) -> None:
        """親 JSON が存在しない孤児 media は ChangeEntry を生成しない（#597）."""
        ctrl, _, _ = controller
        raw = [
            ("A", "bluesky/did/2026/04/media/rkey1/image_0.webp", ""),
        ]
        changes = ctrl._change_detector.classify_changes(raw)
        paths = {c.file_path for c in changes}
        assert "bluesky/did/2026/04/media/rkey1/image_0.webp" not in paths
        assert "bluesky/did/2026/04/rkey1.json" not in paths

    def test_media_modified_with_existing_parent_triggers_parent_only(
        self,
        controller: tuple[PipelineController, ConverterProtocol, IndexerProtocol],
        workspace: dict[str, Path],
    ) -> None:
        """media 変更時、親 JSON のみ MODIFIED として返り media 自身は除外される（#597）."""
        ctrl, _, _ = controller
        _place_local_file(
            workspace["source"],
            "bluesky/did/2026/04/rkey1.json",
            '{"post": {}}',
        )
        raw = [
            ("M", "bluesky/did/2026/04/media/rkey1/image_0.webp", ""),
        ]
        changes = ctrl._change_detector.classify_changes(raw)
        paths = {c.file_path: c.status.value for c in changes}
        assert "bluesky/did/2026/04/media/rkey1/image_0.webp" not in paths
        assert paths == {"bluesky/did/2026/04/rkey1.json": "modified"}

    def test_parent_json_already_in_diff(
        self,
        controller: tuple[PipelineController, ConverterProtocol, IndexerProtocol],
        workspace: dict[str, Path],
    ) -> None:
        ctrl, _, _ = controller
        _place_local_file(
            workspace["source"],
            "bluesky/did/2026/04/rkey1.json",
            '{"post": {}}',
        )
        raw = [
            ("A", "bluesky/did/2026/04/media/rkey1/image_0.webp", ""),
            ("M", "bluesky/did/2026/04/rkey1.json", ""),
        ]
        changes = ctrl._change_detector.classify_changes(raw)
        json_entries = [c for c in changes if c.file_path == "bluesky/did/2026/04/rkey1.json"]
        assert len(json_entries) == 1
        assert json_entries[0].status.value == "modified"

    def test_media_renamed_across_rkey_triggers_both_parents(
        self,
        controller: tuple[PipelineController, ConverterProtocol, IndexerProtocol],
        workspace: dict[str, Path],
    ) -> None:
        """media が別 rkey へリネームされた場合、新旧両方の親 JSON が再変換対象になる（#597 Copilot 指摘）."""
        ctrl, _, _ = controller
        _place_local_file(
            workspace["source"],
            "bluesky/did/2026/04/rkey_old.json",
            '{"post": {}}',
        )
        _place_local_file(
            workspace["source"],
            "bluesky/did/2026/04/rkey_new.json",
            '{"post": {}}',
        )
        raw = [
            (
                "R",
                "bluesky/did/2026/04/media/rkey_new/image_0.webp",
                "bluesky/did/2026/04/media/rkey_old/image_0.webp",
            ),
        ]
        changes = ctrl._change_detector.classify_changes(raw)
        paths = {c.file_path: c.status.value for c in changes}
        assert "bluesky/did/2026/04/media/rkey_new/image_0.webp" not in paths
        assert "bluesky/did/2026/04/media/rkey_old/image_0.webp" not in paths
        assert paths.get("bluesky/did/2026/04/rkey_old.json") == "modified"
        assert paths.get("bluesky/did/2026/04/rkey_new.json") == "modified"

    def test_media_renamed_to_nonmedia_preserves_new_path_processing(
        self,
        controller: tuple[PipelineController, ConverterProtocol, IndexerProtocol],
        workspace: dict[str, Path],
    ) -> None:
        """media → 非 media へのリネーム時、旧親 JSON は再変換対象、新パスは通常通り登録される."""
        ctrl, _, _ = controller
        _place_local_file(
            workspace["source"],
            "bluesky/did/2026/04/rkey_old.json",
            '{"post": {}}',
        )
        raw = [
            (
                "R",
                "local/moved_image.webp",
                "bluesky/did/2026/04/media/rkey_old/image_0.webp",
            ),
        ]
        changes = ctrl._change_detector.classify_changes(raw)
        paths = {c.file_path: c.status.value for c in changes}
        assert paths.get("bluesky/did/2026/04/rkey_old.json") == "modified"
        assert paths.get("local/moved_image.webp") == "renamed"


class TestScanAllAsAdded:
    """ChangeDetector.scan_all_as_added のテスト."""

    def test_excludes_bluesky_media(
        self,
        controller: tuple[PipelineController, ConverterProtocol, IndexerProtocol],
        workspace: dict[str, Path],
    ) -> None:
        """初回スキャン経路で bluesky media が独立 ChangeEntry として返らない（#597）."""
        ctrl, _, _ = controller
        _place_local_file(
            workspace["source"],
            "bluesky/did/2026/04/rkey1.json",
            '{"post": {}}',
        )
        _place_local_file(
            workspace["source"],
            "bluesky/did/2026/04/media/rkey1/image_0.webp",
            "dummy",
        )
        _place_local_file(
            workspace["source"],
            "bluesky/did/2026/04/media/rkey1/video_0.ts",
            "dummy",
        )
        ctrl.init_repo()
        ctrl.commit("initial")

        entries = ctrl._change_detector.scan_all_as_added()
        paths = {e.file_path for e in entries}
        assert "bluesky/did/2026/04/rkey1.json" in paths
        assert "bluesky/did/2026/04/media/rkey1/image_0.webp" not in paths
        assert "bluesky/did/2026/04/media/rkey1/video_0.ts" not in paths

    def test_excludes_meta_and_sidecar_files(
        self,
        controller: tuple[PipelineController, ConverterProtocol, IndexerProtocol],
        workspace: dict[str, Path],
    ) -> None:
        """初回スキャンで .meta・sidecar（aozora/catalog.csv 等）も除外する."""
        ctrl, _, _ = controller
        _place_local_file(workspace["source"], "local/a.txt", "content")
        _place_local_file(workspace["source"], "local/a.txt.meta", "{}")
        _place_local_file(workspace["source"], "aozora/catalog.csv", "data")
        ctrl.init_repo()
        ctrl.commit("initial")

        entries = ctrl._change_detector.scan_all_as_added()
        paths = {e.file_path for e in entries}
        assert "local/a.txt" in paths
        assert "local/a.txt.meta" not in paths
        assert "aozora/catalog.csv" not in paths

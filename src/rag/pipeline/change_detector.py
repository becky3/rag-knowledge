"""パイプライン制御 — 変更検出 Port + Adapter.

仕様: docs/specs/pipeline-controller.md

git diff からの変更ファイル列挙・分類を抽象化する。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from rag.pipeline.models import ChangeEntry, ChangeStatus
from rag.store.source_store import is_source_file

if TYPE_CHECKING:
    from rag.pipeline.git_ops import GitOperations
    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)


class ChangeDetector(Protocol):
    """変更検出 Port.

    git ベースの変更検出と、`is_source_file` / attachment 解決を組み合わせた
    分類処理を抽象化する。
    """

    def scan_all_as_added(self) -> list[ChangeEntry]:
        """全追跡ファイルを「追加」として返す."""
        ...

    def supplement_hidden_changes(
        self,
        raw_diff: list[tuple[str, str, str]],
        from_commit_id: str,
    ) -> list[tuple[str, str, str]]:
        """ネット差分で検出されない中間変更を補完する."""
        ...

    def classify_changes(
        self,
        raw_diff: list[tuple[str, str, str]],
    ) -> list[ChangeEntry]:
        """git diff の生出力を ChangeEntry に分類する."""
        ...


class RealChangeDetector:
    """ChangeDetector の本番実装."""

    def __init__(
        self,
        source_store: SourceStore,
        git: GitOperations,
    ) -> None:
        self._source_store = source_store
        self._git = git

    def scan_all_as_added(self) -> list[ChangeEntry]:
        """全追跡ファイルを「追加」として返す.

        `is_source_file` で独立ソースのみを対象とする（sidecar・ロック・
        attachment は除外）。
        """
        all_files = self._git.list_all_files()
        entries: list[ChangeEntry] = []
        for f in all_files:
            if not is_source_file(f):
                continue
            entries.append(
                ChangeEntry(status=ChangeStatus.ADDED, file_path=f),
            )
        return entries

    def supplement_hidden_changes(
        self,
        raw_diff: list[tuple[str, str, str]],
        from_commit_id: str,
    ) -> list[tuple[str, str, str]]:
        """ネット差分で検出されない中間変更を補完する."""
        touched = self._git.get_files_touched_in_range(from_commit_id)
        if not touched:
            return raw_diff

        net_files = {entry[1] for entry in raw_diff}
        hidden = touched - net_files
        if not hidden:
            return raw_diff

        head_files = set(self._git.list_all_files())
        supplemented = list(raw_diff)
        added_count = 0
        for file_path in sorted(hidden):
            if file_path not in head_files:
                continue
            if not is_source_file(file_path):
                continue
            logger.debug(
                "中間コミットで変更されたがネット差分に出ないファイルを"
                "MODIFIED として追加: %s",
                file_path,
            )
            supplemented.append(("M", file_path, ""))
            added_count += 1
        if added_count:
            logger.info(
                "中間コミットで変更されたがネット差分に出ないファイルを"
                "MODIFIED として %d 件追加",
                added_count,
            )
        return supplemented

    def classify_changes(
        self,
        raw_diff: list[tuple[str, str, str]],
    ) -> list[ChangeEntry]:
        """git diff の生出力を ChangeEntry に分類する.

        .meta ファイルのみの変更を meta_only として検出する。
        複合ソースの attachment（例: BlueSky の media/）の変更時は
        `find_existing_parent` で親ソースを解決し、親ソースを再変換対象に含める。
        """
        data_entries: dict[str, ChangeEntry] = {}
        meta_files: list[tuple[str, str, str]] = []
        attachment_parents: set[str] = set()

        for status_char, file_path, old_path in raw_diff:
            if file_path.endswith(".meta"):
                meta_files.append((status_char, file_path, old_path))
                continue
            if status_char == "R" and old_path:
                old_parent = self._source_store.find_existing_parent(old_path)
                if old_parent is not None:
                    attachment_parents.add(old_parent)
            parent = self._source_store.find_existing_parent(file_path)
            if parent is not None:
                attachment_parents.add(parent)
                continue
            if not is_source_file(file_path):
                continue
            entry = _map_status(status_char, file_path, old_path)
            data_entries[file_path] = entry

        for parent_path in attachment_parents:
            if parent_path in data_entries:
                continue
            data_entries[parent_path] = ChangeEntry(
                status=ChangeStatus.MODIFIED,
                file_path=parent_path,
            )

        for _status_char, meta_path, _old_path in meta_files:
            data_path = meta_path.removesuffix(".meta")
            if data_path in data_entries:
                continue
            if not is_source_file(data_path):
                continue
            data_entries[data_path] = ChangeEntry(
                status=ChangeStatus.META_ONLY,
                file_path=data_path,
            )

        return list(data_entries.values())


def _map_status(
    status_char: str,
    file_path: str,
    old_path: str,
) -> ChangeEntry:
    """git status 文字を ChangeStatus にマッピングする."""
    mapping = {
        "A": ChangeStatus.ADDED,
        "M": ChangeStatus.MODIFIED,
        "D": ChangeStatus.DELETED,
        "R": ChangeStatus.RENAMED,
    }
    status = mapping.get(status_char, ChangeStatus.MODIFIED)
    return ChangeEntry(status=status, file_path=file_path, old_path=old_path)

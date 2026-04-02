"""パイプライン制御データモデル.

仕様: docs/specs/pipeline-controller.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from rag.store.models import SourceType


class ChangeStatus(Enum):
    """git diff から検出された変更種別."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    META_ONLY = "meta_only"


@dataclass(frozen=True)
class ChangeEntry:
    """変更ファイルリストの1エントリ."""

    status: ChangeStatus
    file_path: str
    old_path: str = ""


class PipelineMode(Enum):
    """パイプライン実行モード."""

    INCREMENTAL = "incremental"
    FULL_REBUILD = "full_rebuild"
    CONVERT_ONLY = "convert_only"
    INDEX_ONLY = "index_only"


@dataclass
class PipelineSummary:
    """パイプライン実行結果サマリ."""

    mode: PipelineMode
    total_files: int
    processed: int
    skipped: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    from_commit_id: str = ""
    to_commit_id: str = ""


# プログレス通知のフェーズ名
PHASE_FETCH = "Fetch"
PHASE_CONVERT = "Convert"
PHASE_INDEX = "Index"
PHASE_CONVERT_AND_INDEX = "Convert & Index"


def detect_source_type(rel_path: str) -> SourceType:
    """相対パスから source_type を判定する."""
    if rel_path.startswith("web/"):
        return "web"
    if rel_path.startswith("bluesky/"):
        return "bluesky"
    if rel_path.startswith("zenn/"):
        return "zenn"
    if rel_path.startswith("youtube/"):
        return "youtube"
    if rel_path.startswith("aozora/"):
        return "aozora"
    if rel_path.startswith("journal/"):
        return "journal"
    return "local"

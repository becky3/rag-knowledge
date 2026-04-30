"""パイプライン制御データモデル.

仕様: docs/specs/pipeline-controller.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypedDict


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
    CONVERT_ONLY = "convert"
    INDEX_ONLY = "index"


class PipelinePhase(Enum):
    """パイプライン処理のフェーズ.

    値は `PipelineErrorEntry.phase` に記録される snake_case キー。
    progress 表示用のラベルは `PIPELINE_PHASE_DISPLAY` mapping で管理する。
    """

    FETCH = "fetch"
    CONVERT = "convert"
    INDEX = "index"
    CONVERT_AND_INDEX = "convert_and_index"

    @property
    def error_key(self) -> str:
        """PipelineErrorEntry.phase に記録される snake_case キー."""
        return self.value

    @property
    def display(self) -> str:
        """progress 表示・ログ出力用のフェーズ名."""
        return PIPELINE_PHASE_DISPLAY[self]


PIPELINE_PHASE_DISPLAY: dict[PipelinePhase, str] = {
    PipelinePhase.FETCH: "Fetch",
    PipelinePhase.CONVERT: "Convert",
    PipelinePhase.INDEX: "Index",
    PipelinePhase.CONVERT_AND_INDEX: "Convert & Index",
}


class PipelineErrorEntry(TypedDict):
    """PipelineSummary.errors の各エントリのスキーマ.

    仕様: docs/specs/pipeline-controller.md の `PipelineSummary.errors`
    """

    path: str
    size_bytes: int | None
    message: str
    phase: str


class PipelineWarningEntry(TypedDict):
    """PipelineSummary.warnings の各エントリのスキーマ.

    ConversionSkippedError 等の non-fatal な失敗を構造化して保持する。
    consumer が path 文字列のみで集計する場合は `warned_paths()` を使う。
    """

    path: str
    message: str
    phase: str


def format_pipeline_warning(entry: PipelineWarningEntry) -> str:
    """PipelineWarningEntry を表示用文字列に整形する.

    MCP レスポンス・CLI テキスト出力の両方で使用する共通フォーマッタ。
    `{path [phase]: message}` のフォーマットで返す。
    phase が空文字の場合は phase 表示を省略する。
    """
    phase_part = f" [{entry['phase']}]" if entry.get("phase") else ""
    return f"{entry['path']}{phase_part}: {entry['message']}"


def format_pipeline_error(entry: PipelineErrorEntry) -> str:
    """PipelineErrorEntry を表示用文字列に整形する.

    MCP レスポンス・CLI テキスト出力の両方で使用する共通フォーマッタ。
    `{path (size_bytes bytes) [phase]: message}` のフォーマットで返す。
    size_bytes が None の場合はバイト数表示を省略する。
    phase が空文字の場合は phase 表示を省略する（通常は常にセットされる）。
    """
    path = entry["path"]
    message = entry["message"]
    phase = entry["phase"]
    size_bytes = entry["size_bytes"]

    size_part = f" ({size_bytes} bytes)" if size_bytes is not None else ""
    phase_part = f" [{phase}]" if phase else ""
    return f"{path}{size_part}{phase_part}: {message}"


@dataclass
class PipelineSummary:
    """パイプライン実行結果サマリ.

    `errors` の各エントリは `PipelineErrorEntry` TypedDict で定義されたスキーマ
    （仕様: docs/specs/pipeline-controller.md の `PipelineSummary.errors`）。
    path 文字列のみが必要な consumer は `failed_paths()` を使う。
    """

    mode: PipelineMode
    total_files: int
    processed: int
    errors: list[PipelineErrorEntry] = field(default_factory=list)
    warnings: list[PipelineWarningEntry] = field(default_factory=list)
    from_commit_id: str = ""
    to_commit_id: str = ""

    def failed_paths(self) -> set[str]:
        """errors の各 dict から path を抽出した set を返すアクセサ.

        集合演算（convert 失敗分の index 除外等）で使う consumer 側が
        dict 構造を意識しないようにするため。
        """
        return {e["path"] for e in self.errors}

    def warned_paths(self) -> set[str]:
        """warnings の各 dict から path を抽出した set を返すアクセサ.

        集合演算（convert 警告分の index 除外等）で使う consumer 側が
        dict 構造を意識しないようにするため。
        """
        return {w["path"] for w in self.warnings}


@dataclass
class FullRebuildResult:
    """全再構築の2フェーズ結果.

    convert フェーズと index フェーズそれぞれの PipelineSummary を保持する。
    """

    convert: PipelineSummary
    index: PipelineSummary

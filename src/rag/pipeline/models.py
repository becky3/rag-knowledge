"""パイプライン制御データモデル.

仕様: docs/specs/pipeline-controller.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import NotRequired, TypedDict


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

    display は progress 通知のプレフィックス（人間向け表示）、
    error_key は `PipelineErrorEntry.phase` に記録される列挙値。
    """

    FETCH = ("Fetch", "fetch")
    CONVERT = ("Convert", "convert")
    INDEX = ("Index", "index")
    CONVERT_AND_INDEX = ("Convert & Index", "convert_and_index")

    def __init__(self, display: str, error_key: str) -> None:
        self._display = display
        self._error_key = error_key

    @property
    def display(self) -> str:
        """progress 表示・ログ出力用のフェーズ名."""
        return self._display

    @property
    def error_key(self) -> str:
        """PipelineErrorEntry.phase に記録される snake_case キー."""
        return self._error_key


class PipelineErrorEntry(TypedDict):
    """PipelineSummary.errors の各エントリのスキーマ.

    仕様: docs/specs/pipeline-controller.md の `PipelineSummary.errors`
    """

    path: str
    size_bytes: NotRequired[int | None]
    message: str
    phase: str


def format_pipeline_error(entry: PipelineErrorEntry) -> str:
    """PipelineErrorEntry を表示用文字列に整形する.

    MCP レスポンス・CLI テキスト出力の両方で使用する共通フォーマッタ。
    `{path (size_bytes bytes) [phase]: message}` のフォーマットで返す。
    size_bytes が None または未設定の場合はバイト数表示を省略する。
    phase が空文字の場合は phase 表示を省略する。
    """
    path = entry.get("path", "")
    message = entry.get("message", "")
    phase = entry.get("phase", "")
    size_bytes = entry.get("size_bytes")

    size_part = f" ({size_bytes} bytes)" if isinstance(size_bytes, int) else ""
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
    warnings: list[str] = field(default_factory=list)
    from_commit_id: str = ""
    to_commit_id: str = ""

    def failed_paths(self) -> set[str]:
        """errors の各 dict から path を抽出した set を返すアクセサ.

        集合演算（convert 失敗分の index 除外等）で使う consumer 側が
        dict 構造を意識しないようにするため。
        """
        return {e["path"] for e in self.errors}


@dataclass
class FullRebuildResult:
    """全再構築の2フェーズ結果.

    convert フェーズと index フェーズそれぞれの PipelineSummary を保持する。
    """

    convert: PipelineSummary
    index: PipelineSummary

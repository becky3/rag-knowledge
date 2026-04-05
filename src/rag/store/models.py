"""source_store データモデル.

仕様: docs/specs/source-store.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SourceType = Literal["web", "bluesky", "zenn", "youtube", "aozora", "local", "journal"]
SourceStatus = Literal["active", "deleted"]

# git null commit hash（初回パイプライン実行時の from_commit_id）
NULL_COMMIT_HASH = "0" * 40


@dataclass(frozen=True)
class SourceRecord:
    """sources テーブルのレコード.

    source_id は source_store 内の相対パス（file_path）と同一の値。
    source_store 層では file_path として、それより上位では source_id として扱う。
    """

    source_id: str
    source_type: SourceType
    title: str
    status: SourceStatus
    content_hash: str
    file_size: int
    collected_at: str
    updated_at: str
    published_at: str = ""


@dataclass(frozen=True)
class PipelineHistoryRecord:
    """pipeline_history テーブルのレコード."""

    id: int
    from_commit_id: str
    to_commit_id: str
    processed_at: str
    mode: str = "incremental"


@dataclass
class SourceMetadata:
    """ソースのメタデータ（.meta + DB 共通）."""

    source_id: str
    source_type: SourceType
    title: str
    collected_at: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FileData:
    """ファイル取得結果."""

    content: bytes
    file_path: str
    metadata: SourceMetadata

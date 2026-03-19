"""ソース識別・タイトル解決の共通ユーティリティ.

source_id / title の解決ロジックを一元化する。
SourceStore と PipelineController の両方から呼び出される。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def resolve_source_id(
    source_type: str,
    rel_path: str,
    metadata: dict[str, Any] | None,
) -> str:
    """source_id を決定する.

    .meta に source_id があればそれを使用し、
    なければ相対パスをフォールバックとして使用する。

    Args:
        source_type: 媒体種別
        rel_path: source_store 内の相対パス
        metadata: .meta から読み取ったメタデータ辞書

    Returns:
        解決された source_id
    """
    if metadata and "source_id" in metadata:
        return str(metadata["source_id"])
    return rel_path


def resolve_title(
    source_type: str,
    rel_path: str,
    metadata: dict[str, Any] | None,
) -> str:
    """タイトルを決定する.

    .meta に title があればそれを使用し、
    なければファイル名（拡張子除去）をフォールバックとして使用する。

    Args:
        source_type: 媒体種別
        rel_path: source_store 内の相対パス
        metadata: .meta から読み取ったメタデータ辞書

    Returns:
        解決されたタイトル
    """
    if metadata and "title" in metadata:
        return str(metadata["title"])
    return Path(rel_path).stem

"""チャンクメタデータ構築モジュール.

仕様: docs/specs/indexer.md

ChromaDB に格納するチャンクメタデータの構築を担当する。
"""

from __future__ import annotations

from rag.store.models import SourceMetadata


def build_chunk_metadata(
    source_metadata: SourceMetadata,
    chunk_index: int,
    total_chunks: int,
) -> dict[str, str | int | float | bool]:
    """チャンクに付与するメタデータを構築する.

    Args:
        source_metadata: ソースメタデータ
        chunk_index: チャンクの連番（0 始まり）
        total_chunks: 当該ソースのチャンク総数

    Returns:
        ChromaDB 格納用のメタデータ辞書
    """
    meta: dict[str, str | int | float | bool] = {
        "source_id": source_metadata.source_id,
        "source_type": source_metadata.source_type,
        "title": source_metadata.title,
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
        "collected_at": source_metadata.collected_at,
    }

    # カスタムフィールド: extra の各キーに custom: プレフィックスを付与
    for key, value in source_metadata.extra.items():
        converted = _convert_metadata_value(value)
        meta[f"custom:{key}"] = converted

    return meta


def _convert_metadata_value(value: object) -> str | int | float | bool:
    """ChromaDB 許容型に変換する.

    ChromaDB は str | int | float | bool のみ許容する。
    list は str() で変換し、その他も str() で変換する。
    """
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)

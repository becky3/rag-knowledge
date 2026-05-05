"""検索系のフィルタ構築ヘルパー.

ChromaDB の where 句構築 / BM25 フィルタ整形を集約する。
仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

from typing import Any


def build_where_clause(
    source_type: str | None,
    filters: dict[str, str] | None,
) -> dict[str, Any] | None:
    """ChromaDB の where 句を構築する.

    source_type と filters を統合する。filters のキーには custom: プレフィックスを
    自動付与する。複数条件の場合は ChromaDB の $and 演算子で結合する。

    Returns:
        単一条件: {"key": value}
        複数条件: {"$and": [{"key1": value1}, {"key2": value2}]}
        条件なし: None
    """
    conditions: dict[str, str | int | float | bool] = {}
    if source_type is not None:
        conditions["source_type"] = source_type
    if filters:
        for key, value in filters.items():
            conditions[f"custom:{key}"] = value

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions
    return {"$and": [{k: v} for k, v in conditions.items()]}


def build_bm25_filters(
    filters: dict[str, str],
) -> dict[str, str]:
    """BM25 用のフィルタ辞書を構築する.

    キーに custom: プレフィックスを付与する（ChromaDB メタデータのキーと一致させる）。
    """
    return {f"custom:{key}": value for key, value in filters.items()}

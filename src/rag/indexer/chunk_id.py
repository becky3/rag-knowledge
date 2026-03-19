"""チャンク ID 生成モジュール.

仕様: docs/specs/indexer.md

チャンク ID は source_id の SHA-256 ハッシュ先頭 16 文字 + "_" + chunk_index で構成する。
"""

from __future__ import annotations

import hashlib


def generate_chunk_id(source_id: str, chunk_index: int) -> str:
    """チャンク ID を生成する.

    Args:
        source_id: ソース識別子
        chunk_index: チャンクの連番（0 始まり）

    Returns:
        チャンク ID（例: a1b2c3d4e5f67890_0）
    """
    prefix = source_id_prefix(source_id)
    return f"{prefix}_{chunk_index}"


def source_id_prefix(source_id: str) -> str:
    """source_id のハッシュプレフィックスを返す.

    Args:
        source_id: ソース識別子

    Returns:
        SHA-256 の先頭 16 文字
    """
    return hashlib.sha256(source_id.encode()).hexdigest()[:16]


def parse_chunk_index(chunk_id: str) -> int:
    """チャンク ID からチャンクインデックスを抽出する.

    Args:
        chunk_id: チャンク ID（例: a1b2c3d4e5f67890_0）

    Returns:
        チャンクインデックス

    Raises:
        ValueError: チャンク ID の形式が不正な場合
    """
    parts = chunk_id.rsplit("_", 1)
    if len(parts) != 2:
        msg = f"不正なチャンク ID 形式: {chunk_id}"
        raise ValueError(msg)
    try:
        return int(parts[1])
    except ValueError:
        msg = f"チャンク ID からインデックスを解析できません: {chunk_id}"
        raise ValueError(msg) from None

""".meta サイドカーファイルの読み書き.

仕様: docs/specs/source-store.md

オリジナルデータファイルと同階層に配置する YAML 形式のメタデータファイル。
local 媒体は .meta を持たない。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def meta_path_for(file_path: Path) -> Path:
    """データファイルに対応する .meta ファイルのパスを返す.

    Args:
        file_path: データファイルのパス

    Returns:
        .meta ファイルのパス（同階層、ファイル名 + ``.meta``）
    """
    return file_path.with_name(file_path.name + ".meta")


def read_meta(file_path: Path) -> dict[str, Any]:
    """.meta ファイルを読み取る.

    Args:
        file_path: データファイルのパス（.meta ファイル自体ではない）

    Returns:
        メタデータの辞書

    Raises:
        FileNotFoundError: .meta ファイルが存在しない場合
    """
    mp = meta_path_for(file_path)
    with open(mp, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    if data is None:
        return {}
    return data


def write_meta(file_path: Path, metadata: dict[str, Any]) -> None:
    """.meta ファイルを書き込む.

    Args:
        file_path: データファイルのパス（.meta ファイル自体ではない）
        metadata: 書き込むメタデータの辞書
    """
    mp = meta_path_for(file_path)
    with open(mp, "w", encoding="utf-8") as f:
        yaml.dump(
            metadata,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )

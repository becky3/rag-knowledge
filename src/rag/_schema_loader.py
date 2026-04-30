"""enums.yml ローダー — 列挙値の属性を実行時に取得する.

`_schema/enums.yml` は列挙値の SSoT（仕様: docs/specs/architecture.md）。
列挙値の集合自体は Python 側の Literal / Enum で表現し、
CI（`scripts/validate_enums.py`）で同期検証されるが、
属性（`has_meta` 等）は本モジュールが実行時に YAML から取得する。

rag-knowledge はソースツリーからの editable install で運用するため、
`_schema/enums.yml` をリポジトリルートからの相対パスで解決できる。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENUMS_PATH = _REPO_ROOT / "_schema" / "enums.yml"


@lru_cache(maxsize=1)
def _load_enums() -> dict[str, Any]:
    """`_schema/enums.yml` を読み込みパース済み dict を返す（キャッシュ）."""
    with _ENUMS_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        msg = f"enums.yml のトップレベルが dict ではありません: {_ENUMS_PATH}"
        raise TypeError(msg)
    return data


def source_types_without_meta() -> frozenset[str]:
    """`.meta` サイドカーを持たない source_type の集合を返す.

    enums.yml の `source_type` カテゴリで `has_meta: false` の値を抽出する。
    """
    data = _load_enums()
    entry = data.get("source_type", {})
    values = entry.get("values", [])
    return frozenset(
        item["value"]
        for item in values
        if isinstance(item, dict) and item.get("has_meta") is False
    )

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
from typing import Any, cast, get_args

import yaml

from rag.config import PROJECT_ROOT
from rag.store.models import SourceType

_ENUMS_PATH = PROJECT_ROOT / "_schema" / "enums.yml"


@lru_cache(maxsize=1)
def _load_enums() -> dict[str, Any]:
    """`_schema/enums.yml` を読み込みパース済み dict を返す（キャッシュ）.

    rag-knowledge は editable install（uv sync）でリポジトリルートから運用する前提のため、
    通常はリポジトリ内に enums.yml が存在する。万一見つからない場合は、運用形態の誤りや
    パス解決の異常を示す可能性が高いため、対処の手がかりを含めた明示的エラーを送出する。
    """
    if not _ENUMS_PATH.exists():
        msg = (
            f"enums.yml が見つかりません: {_ENUMS_PATH}\n"
            f"rag-knowledge は editable install で運用してください "
            f"（uv sync 後、リポジトリルートで実行）。"
            f"詳細は docs/specs/architecture.md を参照。"
        )
        raise FileNotFoundError(msg)
    with _ENUMS_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        msg = f"enums.yml のトップレベルが dict ではありません: {_ENUMS_PATH}"
        raise TypeError(msg)
    return data


def source_types_without_meta() -> frozenset[SourceType]:
    """`.meta` サイドカーを持たない source_type の集合を返す.

    enums.yml の `source_type` カテゴリで `has_meta: false` の値を抽出する。
    enums.yml の構造異常（`source_type` キー欠落・`values` 不正・`has_meta` 欠落等）は
    `validate_enums.py` の CI 検証で防がれる前提だが、ランタイムでも構造を検証して
    異常時には説明的な例外を送出する。
    """
    data = _load_enums()
    entry = data.get("source_type")
    if not isinstance(entry, dict):
        msg = (
            f"enums.yml の 'source_type' エントリが dict ではありません: "
            f"{type(entry).__name__}"
        )
        raise TypeError(msg)
    values = entry.get("values")
    if not isinstance(values, list) or not values:
        msg = (
            f"enums.yml の 'source_type.values' が list でないか空です: "
            f"{type(values).__name__}"
        )
        raise TypeError(msg)
    valid_source_types: frozenset[str] = frozenset(get_args(SourceType))
    result: set[SourceType] = set()
    for i, item in enumerate(values):
        if not isinstance(item, dict):
            msg = f"enums.yml の 'source_type.values[{i}]' が dict ではありません"
            raise TypeError(msg)
        if "value" not in item:
            msg = f"enums.yml の 'source_type.values[{i}]' に 'value' キーがありません"
            raise KeyError(msg)
        if "has_meta" not in item:
            msg = (
                f"enums.yml の 'source_type.values[{i}]' に 'has_meta' 属性がありません"
            )
            raise KeyError(msg)
        value = item["value"]
        if value not in valid_source_types:
            msg = (
                f"enums.yml の 'source_type.values[{i}].value' が SourceType Literal に "
                f"含まれません: {value!r} "
                f"（CI: scripts/validate_enums.py で同期検証されます）"
            )
            raise ValueError(msg)
        if item["has_meta"] is False:
            result.add(cast("SourceType", value))
    return frozenset(result)

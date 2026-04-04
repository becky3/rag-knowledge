"""enums.yml と Python コード（Literal / Enum）の整合性を検証する.

enums.yml を SSoT として、Python 側の定義と値が一致することを確認する。
不一致時は差分を表示して exit 1 で終了する。
"""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path
from typing import get_args

import yaml


def _load_enums_yml(path: Path) -> dict:
    """enums.yml を読み込んで返す."""
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _extract_yml_values(data: dict, key: str) -> set[str]:
    """enums.yml の指定キーから値の集合を取得する."""
    entry = data.get(key)
    if entry is None:
        print(f"ERROR: enums.yml に '{key}' が定義されていません")
        sys.exit(1)
    values = entry.get("values")
    if not values:
        print(f"ERROR: enums.yml の '{key}.values' が空です")
        sys.exit(1)
    return {item["value"] for item in values}


def _extract_literal_values(literal_type: type) -> set[str]:
    """typing.Literal 型から値の集合を取得する."""
    return set(get_args(literal_type))


def _extract_enum_values(enum_class: type[Enum]) -> set[str]:
    """Enum クラスから値の集合を取得する."""
    return {member.value for member in enum_class}


def _check_match(name: str, yml_values: set[str], python_values: set[str]) -> bool:
    """値の一致を検証し、不一致があれば差分を表示する."""
    if yml_values == python_values:
        print(f"OK: {name} - {len(yml_values)} values match")
        return True

    print(f"MISMATCH: {name}")
    only_yml = yml_values - python_values
    only_python = python_values - yml_values
    if only_yml:
        print(f"  enums.yml only: {sorted(only_yml)}")
    if only_python:
        print(f"  Python only:    {sorted(only_python)}")
    return False


def main() -> None:
    """メインエントリポイント."""
    repo_root = Path(__file__).resolve().parent.parent
    enums_path = repo_root / "_schema" / "enums.yml"

    if not enums_path.exists():
        print(f"ERROR: {enums_path} が見つかりません")
        sys.exit(1)

    data = _load_enums_yml(enums_path)

    # Python 定義をインポート
    from rag.pipeline.models import PipelineMode
    from rag.store.models import SourceType

    ok = True

    # source_type: Literal 型
    yml_source_types = _extract_yml_values(data, "source_type")
    python_source_types = _extract_literal_values(SourceType)
    if not _check_match("source_type (SourceType)", yml_source_types, python_source_types):
        ok = False

    # pipeline_mode: Enum クラス
    yml_pipeline_modes = _extract_yml_values(data, "pipeline_mode")
    python_pipeline_modes = _extract_enum_values(PipelineMode)
    if not _check_match("pipeline_mode (PipelineMode)", yml_pipeline_modes, python_pipeline_modes):
        ok = False

    if not ok:
        print("\nValidation FAILED: enums.yml と Python コードが一致しません")
        sys.exit(1)

    print("\nValidation PASSED")


if __name__ == "__main__":
    main()

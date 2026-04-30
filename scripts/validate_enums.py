"""enums.yml と Python コード（Literal / Enum）の整合性を検証する.

enums.yml を SSoT として、Python 側の定義と値が一致することを確認する。
不一致時は差分を表示して exit 1 で終了する。

検証内容:
- カテゴリごとの値集合が Python 側（Literal / Enum）と一致すること
- 値の重複がないこと
- attributes が宣言されている場合、各 value にその属性が宣言された型で含まれていること
"""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path
from typing import Any, get_args

import yaml

_ATTR_TYPE_MAP: dict[str, type] = {
    "bool": bool,
    "str": str,
    "int": int,
}


def _load_enums_yml(path: Path) -> dict[str, Any]:
    """enums.yml を読み込んで返す."""
    with path.open(encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            print(f"ERROR: enums.yml の YAML パースに失敗しました: {e}")
            sys.exit(1)
    if not isinstance(data, dict):
        print(f"ERROR: enums.yml のトップレベルが dict ではありません: {type(data)}")
        sys.exit(1)
    return data


def _extract_yml_values(data: dict[str, Any], key: str) -> set[str]:
    """enums.yml の指定キーから値の集合を取得する.

    `attributes` が宣言されている場合、各 value がその属性を宣言された型で持つかも検証する。
    """
    entry = data.get(key)
    if entry is None:
        print(f"ERROR: enums.yml に '{key}' が定義されていません")
        sys.exit(1)
    if not isinstance(entry, dict):
        print(f"ERROR: enums.yml の '{key}' は dict ではありません: {type(entry)}")
        sys.exit(1)
    values = entry.get("values")
    if not isinstance(values, list) or not values:
        print(f"ERROR: enums.yml の '{key}.values' が空、または list ではありません: {values}")
        sys.exit(1)
    attr_specs = _parse_attributes(entry.get("attributes"), key)
    result: set[str] = set()
    duplicates: set[str] = set()
    for i, item in enumerate(values):
        if not isinstance(item, dict) or "value" not in item:
            print(f"ERROR: enums.yml の '{key}.values[{i}]' に 'value' キーがありません: {item}")
            sys.exit(1)
        value = item["value"]
        if not isinstance(value, str):
            print(
                f"ERROR: enums.yml の '{key}.values[{i}].value' は str ではありません: "
                f"{value!r} ({type(value)})"
            )
            sys.exit(1)
        if value in result:
            duplicates.add(value)
        result.add(value)
        for attr_name, attr_type in attr_specs.items():
            if attr_name not in item:
                print(
                    f"ERROR: enums.yml の '{key}.values[{i}]' に属性 '{attr_name}' が "
                    f"ありません: {item}"
                )
                sys.exit(1)
            attr_value = item[attr_name]
            if not isinstance(attr_value, attr_type):
                print(
                    f"ERROR: enums.yml の '{key}.values[{i}].{attr_name}' は "
                    f"{attr_type.__name__} ではありません: "
                    f"{attr_value!r} ({type(attr_value).__name__})"
                )
                sys.exit(1)
    if duplicates:
        print(f"ERROR: enums.yml の '{key}.values' に重複値があります: {sorted(duplicates)}")
        sys.exit(1)
    return result


def _parse_attributes(attrs: Any, key: str) -> dict[str, type]:
    """attributes 宣言をパースして属性名 → Python 型の dict を返す.

    attributes が未定義の場合は空 dict を返す。
    """
    if attrs is None:
        return {}
    if not isinstance(attrs, dict):
        print(f"ERROR: enums.yml の '{key}.attributes' は dict ではありません: {type(attrs)}")
        sys.exit(1)
    result: dict[str, type] = {}
    for attr_name, spec in attrs.items():
        if not isinstance(spec, dict) or "type" not in spec:
            print(
                f"ERROR: enums.yml の '{key}.attributes.{attr_name}' に 'type' キーが "
                f"ありません: {spec}"
            )
            sys.exit(1)
        type_name = spec["type"]
        if not isinstance(type_name, str):
            print(
                f"ERROR: enums.yml の '{key}.attributes.{attr_name}.type' は str で "
                f"指定してください: {type_name!r} ({type(type_name).__name__})"
            )
            sys.exit(1)
        if type_name not in _ATTR_TYPE_MAP:
            print(
                f"ERROR: enums.yml の '{key}.attributes.{attr_name}.type' が未対応です: "
                f"{type_name!r} (対応: {sorted(_ATTR_TYPE_MAP)})"
            )
            sys.exit(1)
        result[attr_name] = _ATTR_TYPE_MAP[type_name]
    return result


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
    from rag.errors import CliErrorCode  # type: ignore[import-untyped]
    from rag.pipeline.ingesters._common import (  # type: ignore[import-untyped]
        IngestErrorCategory,
    )
    from rag.pipeline.models import PipelineMode  # type: ignore[import-untyped]
    from rag.store.models import (  # type: ignore[import-untyped]
        SourceStatus,
        SourceType,
    )

    ok = True

    # source_type: Literal 型
    yml_source_types = _extract_yml_values(data, "source_type")
    python_source_types = _extract_literal_values(SourceType)
    if not _check_match("source_type (SourceType)", yml_source_types, python_source_types):
        ok = False

    # source_status: Enum クラス
    yml_source_status = _extract_yml_values(data, "source_status")
    python_source_status = _extract_enum_values(SourceStatus)
    if not _check_match("source_status (SourceStatus)", yml_source_status, python_source_status):
        ok = False

    # pipeline_mode: Enum クラス
    yml_pipeline_modes = _extract_yml_values(data, "pipeline_mode")
    python_pipeline_modes = _extract_enum_values(PipelineMode)
    if not _check_match("pipeline_mode (PipelineMode)", yml_pipeline_modes, python_pipeline_modes):
        ok = False

    # cli_error_code: Enum クラス
    yml_error_codes = _extract_yml_values(data, "cli_error_code")
    python_error_codes = _extract_enum_values(CliErrorCode)
    if not _check_match("cli_error_code (CliErrorCode)", yml_error_codes, python_error_codes):
        ok = False

    # ingest_error_category: Enum クラス
    yml_ingest_error_categories = _extract_yml_values(data, "ingest_error_category")
    python_ingest_error_categories = _extract_enum_values(IngestErrorCategory)
    if not _check_match(
        "ingest_error_category (IngestErrorCategory)",
        yml_ingest_error_categories,
        python_ingest_error_categories,
    ):
        ok = False

    if not ok:
        print("\nValidation FAILED: enums.yml と Python コードが一致しません")
        sys.exit(1)

    print("\nValidation PASSED")


if __name__ == "__main__":
    main()

"""validate_enums.py の属性付き value パーサ単体テスト.

仕様: docs/specs/architecture.md（SSoT 階層）
- attributes 宣言が無い従来形式は従来通り動作する
- attributes 宣言がある場合、各 value が宣言された型で属性を持つことを検証する
- attributes の type が未対応 / 値の型不一致 / 属性欠落で SystemExit する

scripts/ ディレクトリは sys.path に含まれないため、importlib で動的にロードする。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable


def _load_validate_enums() -> ModuleType:
    """scripts/validate_enums.py を動的にロードする."""
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "validate_enums.py"
    spec = importlib.util.spec_from_file_location("validate_enums", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_enums"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ve() -> ModuleType:
    """validate_enums モジュール."""
    return _load_validate_enums()


@pytest.fixture()
def extract_values(ve: ModuleType) -> Callable[..., set[str]]:
    """_extract_yml_values へのアクセスヘルパー."""
    return ve._extract_yml_values  # type: ignore[no-any-return]


def _make_data(values: list[dict[str, Any]], attributes: dict[str, Any] | None = None) -> dict[str, Any]:
    """テスト用 enums.yml ライクな dict を構築する."""
    entry: dict[str, Any] = {"description": "test", "values": values}
    if attributes is not None:
        entry["attributes"] = attributes
    return {"test_cat": entry}


class TestNoAttributes:
    """attributes 宣言なし（従来形式）が引き続き動作する."""

    def test_simple_values(self, extract_values: Callable[..., set[str]]) -> None:
        data = _make_data(values=[{"value": "a"}, {"value": "b"}])
        assert extract_values(data, "test_cat") == {"a", "b"}


class TestWithAttributes:
    """attributes 宣言ありのパーサ."""

    def test_bool_attribute_pass(self, extract_values: Callable[..., set[str]]) -> None:
        data = _make_data(
            values=[
                {"value": "a", "has_meta": True},
                {"value": "b", "has_meta": False},
            ],
            attributes={"has_meta": {"type": "bool", "description": "..."}},
        )
        assert extract_values(data, "test_cat") == {"a", "b"}

    def test_attribute_missing(self, extract_values: Callable[..., set[str]]) -> None:
        """value に宣言された属性が含まれていない場合は SystemExit."""
        data = _make_data(
            values=[{"value": "a"}],
            attributes={"has_meta": {"type": "bool", "description": "..."}},
        )
        with pytest.raises(SystemExit):
            extract_values(data, "test_cat")

    def test_attribute_type_mismatch(self, extract_values: Callable[..., set[str]]) -> None:
        """値の型が宣言と不一致の場合は SystemExit."""
        data = _make_data(
            values=[{"value": "a", "has_meta": "yes"}],  # str ではなく bool が必要
            attributes={"has_meta": {"type": "bool", "description": "..."}},
        )
        with pytest.raises(SystemExit):
            extract_values(data, "test_cat")

    def test_unsupported_type(self, extract_values: Callable[..., set[str]]) -> None:
        """attributes.type が未対応の場合は SystemExit."""
        data = _make_data(
            values=[{"value": "a", "x": [1, 2]}],
            attributes={"x": {"type": "list", "description": "..."}},
        )
        with pytest.raises(SystemExit):
            extract_values(data, "test_cat")

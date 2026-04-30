"""列挙値・属性スキーマ関連のテスト.

Covers:
- `_schema_loader.source_types_without_meta()` が enums.yml の `has_meta: false` 集合を返す
- `SourceStatus` / `IngestErrorCategory` / `PipelinePhase` の Enum 振る舞い
- PipelinePhase の `display` mapping / `error_key` プロパティ

仕様: docs/specs/architecture.md（SSoT 階層）
"""

from __future__ import annotations

from typing import Any

import pytest

from rag import _schema_loader
from rag._schema_loader import source_types_without_meta
from rag.pipeline.ingesters._common import IngestErrorCategory
from rag.pipeline.models import PIPELINE_PHASE_DISPLAY, PipelinePhase
from rag.store.models import SourceStatus


class TestSchemaLoader:
    """_schema_loader.source_types_without_meta のテスト."""

    def test_returns_local_only(self) -> None:
        """`has_meta: false` の集合は local のみを含む."""
        assert source_types_without_meta() == frozenset({"local"})

    def test_returns_typed_frozenset(self) -> None:
        """戻り値型は SourceType Literal の値のみを含む（type: ignore 不要を保証）."""
        from typing import get_args

        from rag.store.models import SourceType

        result = source_types_without_meta()
        valid = frozenset(get_args(SourceType))
        # 全ての値が SourceType Literal 値の部分集合であること
        assert result <= valid


class TestSourceStatus:
    """SourceStatus Enum のテスト."""

    def test_values_match_enums_yml(self) -> None:
        """active / deleted の 2 値を持つ."""
        assert {member.value for member in SourceStatus} == {"active", "deleted"}

    def test_round_trip_via_value(self) -> None:
        """value からの再構築が等価."""
        for member in SourceStatus:
            assert SourceStatus(member.value) is member


class TestIngestErrorCategory:
    """IngestErrorCategory Enum のテスト."""

    def test_values_match_enums_yml(self) -> None:
        """4 値を持ち enums.yml と一致."""
        assert {member.value for member in IngestErrorCategory} == {
            "metadata_fetch",
            "media_download",
            "placement",
            "delegation",
        }

    def test_round_trip_via_value(self) -> None:
        """value からの再構築が等価."""
        for member in IngestErrorCategory:
            assert IngestErrorCategory(member.value) is member


class TestPipelinePhase:
    """PipelinePhase Enum と display mapping のテスト."""

    def test_value_is_snake_case_error_key(self) -> None:
        """value は error_key と同等の snake_case 文字列."""
        assert PipelinePhase.FETCH.value == "fetch"
        assert PipelinePhase.CONVERT.value == "convert"
        assert PipelinePhase.INDEX.value == "index"
        assert PipelinePhase.CONVERT_AND_INDEX.value == "convert_and_index"

    def test_error_key_property(self) -> None:
        """error_key プロパティは value と一致."""
        for phase in PipelinePhase:
            assert phase.error_key == phase.value

    def test_display_mapping_covers_all_members(self) -> None:
        """全 Enum メンバが display mapping に含まれる."""
        assert set(PIPELINE_PHASE_DISPLAY.keys()) == set(PipelinePhase)

    def test_display_property(self) -> None:
        """display プロパティは PIPELINE_PHASE_DISPLAY からラベルを返す."""
        assert PipelinePhase.FETCH.display == "Fetch"
        assert PipelinePhase.CONVERT.display == "Convert"
        assert PipelinePhase.INDEX.display == "Index"
        assert PipelinePhase.CONVERT_AND_INDEX.display == "Convert & Index"


class TestSourceTypesWithoutMetaValidation:
    """source_types_without_meta() の構造検証テスト（enums.yml 異常時の説明的例外）."""

    def _patch_enums(
        self, monkeypatch: pytest.MonkeyPatch, data: Any,
    ) -> None:
        """`_load_enums` の戻り値を monkeypatch で差し替える."""
        _schema_loader._load_enums.cache_clear()
        monkeypatch.setattr(_schema_loader, "_load_enums", lambda: data)

    def test_source_type_entry_not_dict(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """source_type エントリが dict でない場合 TypeError."""
        self._patch_enums(monkeypatch, {"source_type": "not a dict"})
        with pytest.raises(TypeError, match="'source_type' エントリが dict ではありません"):
            source_types_without_meta()

    def test_values_not_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """values が list でない場合 TypeError."""
        self._patch_enums(monkeypatch, {"source_type": {"values": "not a list"}})
        with pytest.raises(TypeError, match="'source_type.values' が list でないか空"):
            source_types_without_meta()

    def test_values_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """values が空 list の場合 TypeError."""
        self._patch_enums(monkeypatch, {"source_type": {"values": []}})
        with pytest.raises(TypeError, match="空"):
            source_types_without_meta()

    def test_item_missing_value_key(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """value キー欠落時 KeyError."""
        self._patch_enums(
            monkeypatch,
            {"source_type": {"values": [{"has_meta": True}]}},
        )
        with pytest.raises(KeyError, match="'value' キーがありません"):
            source_types_without_meta()

    def test_item_missing_has_meta(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """has_meta 属性欠落時 KeyError."""
        self._patch_enums(
            monkeypatch,
            {"source_type": {"values": [{"value": "local"}]}},
        )
        with pytest.raises(KeyError, match="'has_meta' 属性がありません"):
            source_types_without_meta()

    def test_value_not_in_source_type_literal(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SourceType Literal に含まれない値の場合 ValueError."""
        self._patch_enums(
            monkeypatch,
            {"source_type": {"values": [{"value": "unknown_type", "has_meta": True}]}},
        )
        with pytest.raises(ValueError, match=r"SourceType Literal"):
            source_types_without_meta()

    def test_clear_cache_after_test(self) -> None:
        """テストの後始末で lru_cache をクリアして実 enums.yml の結果に戻す."""
        _schema_loader._load_enums.cache_clear()
        assert source_types_without_meta() == frozenset({"local"})

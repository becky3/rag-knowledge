"""列挙値・属性スキーマ関連のテスト.

Covers:
- `_schema_loader.source_types_without_meta()` が enums.yml の `has_meta: false` 集合を返す
- `SourceStatus` / `IngestErrorCategory` / `PipelinePhase` の Enum 振る舞い
- PipelinePhase の `display` mapping / `error_key` プロパティ

仕様: docs/specs/architecture.md（SSoT 階層）
"""

from __future__ import annotations

from rag._schema_loader import source_types_without_meta
from rag.pipeline.ingesters._common import IngestErrorCategory
from rag.pipeline.models import PIPELINE_PHASE_DISPLAY, PipelinePhase
from rag.store.models import SourceStatus


class TestSchemaLoader:
    """_schema_loader.source_types_without_meta のテスト."""

    def test_returns_local_only(self) -> None:
        """`has_meta: false` の集合は local のみを含む."""
        assert source_types_without_meta() == frozenset({"local"})


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

"""journal migration のファイル名日時パースのテスト.

テスト方針:
- YYYYMMDD-HHMMSS パターンのファイル名を JST として解釈し、UTC に変換した
  ISO 8601 文字列が返ること（Issue #795）
- パターンにマッチしないファイル名は None を返す
"""

from __future__ import annotations

from rag.pipeline.ingesters.journal import _parse_datetime_from_entry_id


class TestParseDatetimeFromEntryId:
    """_parse_datetime_from_entry_id のテスト."""

    def test_standard_entry_id_jst_to_utc(self) -> None:
        """YYYYMMDD-HHMMSS-topic 形式は JST 解釈で UTC に変換される."""
        # JST 2026-04-04 01:20:30 → UTC 2026-04-03 16:20:30
        result = _parse_datetime_from_entry_id("20260404-012030-some-topic")
        assert result == "2026-04-03T16:20:30+00:00"

    def test_entry_id_without_topic(self) -> None:
        """YYYYMMDD-HHMMSS- のみ（トピック部分が短い）でもパース可能."""
        # JST 2026-01-01 00:00:00 → UTC 2025-12-31 15:00:00
        result = _parse_datetime_from_entry_id("20260101-000000-x")
        assert result == "2025-12-31T15:00:00+00:00"

    def test_jst_to_utc_crosses_date_boundary(self) -> None:
        """JST 09:00:00 ちょうどは UTC 00:00:00 になる（日付境界の確認）."""
        result = _parse_datetime_from_entry_id("20260520-090000-noon-jst")
        assert result == "2026-05-20T00:00:00+00:00"

    def test_non_matching_entry_id(self) -> None:
        """パターンにマッチしないファイル名は None を返す."""
        result = _parse_datetime_from_entry_id("some-random-name")
        assert result is None

    def test_partial_date_pattern(self) -> None:
        """日付部分のみでハイフン区切り後がない場合は None."""
        result = _parse_datetime_from_entry_id("20260404")
        assert result is None

    def test_invalid_date_values(self) -> None:
        """正規表現にマッチするが無効な日時値の場合は None."""
        result = _parse_datetime_from_entry_id("99991399-999999-invalid")
        assert result is None

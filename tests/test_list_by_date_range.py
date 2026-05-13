"""rag_list_by_date_range / list-by-date-range のテスト.

仕様: docs/specs/infrastructure/content-listing.md

テスト方針 (計画ファイル: aidlc-docs/plan-work/issue-789.md):
- MetadataDB.list_sources_by_date_range / count_sources_by_date_range の単体テスト
- 範囲フィルタの JST 境界（date_from T00:00:00+09:00 〜 date_to T23:59:59.999999+09:00）
- UTC 表記の published_at（`Z` 終端）が JST 解釈で正しく振り分けられること
- source_type 指定有無（None で全種別横断）
- filters・order・limit・0 件・論理削除除外
- stats_port.list_sources_by_date_range 共通関数のフォーマット検証
- バリデーション（YYYY-MM-DD 形式・date_from > date_to）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag.admin.stats_port import (
    list_sources_by_date_range,
    to_jst_range_iso,
    validate_date_string,
)
from rag.store.metadata_db import MetadataDB
from rag.store.models import SourceStatus


@pytest.fixture()
def db(tmp_path: Path) -> MetadataDB:
    """テスト用 MetadataDB インスタンス."""
    mdb = MetadataDB(tmp_path / "metadata.db")
    mdb.initialize()
    return mdb


def _register(
    db: MetadataDB,
    source_id: str,
    source_type: str = "web",
    title: str = "Test",
    collected_at: str = "2026-01-01T00:00:00Z",
    published_at: str = "",
    file_size: int = 1024,
    status: str = "active",
    meta: str = "{}",
) -> None:
    """テスト用ソース登録ヘルパー."""
    db.register_source(
        source_id=source_id,
        source_type=source_type,
        title=title,
        content_hash="hash",
        file_size=file_size,
        collected_at=collected_at,
        updated_at=collected_at,
        published_at=published_at,
        meta=meta,
    )
    if status == "deleted":
        db.set_status(source_id, SourceStatus.DELETED)


class TestValidateDateString:
    """validate_date_string のテスト."""

    def test_valid_date(self) -> None:
        assert validate_date_string("2026-01-15", field_name="date_from") == "2026-01-15"

    @pytest.mark.parametrize("value", [
        "2026/01/15",
        "20260115",
        "2026-1-15",
        "2026-01-15T00:00:00",
        "",
        "abc",
    ])
    def test_invalid_format(self, value: str) -> None:
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            validate_date_string(value, field_name="date_from")

    def test_invalid_calendar(self) -> None:
        with pytest.raises(ValueError, match="不正"):
            validate_date_string("2026-13-01", field_name="date_from")


class TestToJstRangeIso:
    """to_jst_range_iso のテスト.

    境界文字列は MetadataDB.published_at の正規化書式（UTC マイクロ秒 6 桁固定 ISO 8601）
    に揃える。JST 2026-01-15 00:00:00.000000 → UTC 2026-01-14 15:00:00.000000、
    JST 2026-01-15 23:59:59.999999 → UTC 2026-01-15 14:59:59.999999。
    """

    def test_basic_range(self) -> None:
        date_from_iso, date_to_iso = to_jst_range_iso("2026-01-15", "2026-01-15")
        assert date_from_iso == "2026-01-14T15:00:00.000000+00:00"
        assert date_to_iso == "2026-01-15T14:59:59.999999+00:00"

    def test_date_from_after_date_to(self) -> None:
        with pytest.raises(ValueError, match="date_from"):
            to_jst_range_iso("2026-01-16", "2026-01-15")

    def test_invalid_format_propagates(self) -> None:
        with pytest.raises(ValueError):
            to_jst_range_iso("2026/01/15", "2026-01-15")


class TestListSourcesByDateRange:
    """MetadataDB.list_sources_by_date_range のテスト.

    Note:
        `_register` ヘルパは内部で `register_source` を呼ぶため、
        テストデータの `published_at` は書き込み時に
        `normalize_published_at()` で UTC マイクロ秒 6 桁固定 ISO 8601
        に正規化されて格納される（例: 入力 `2026-01-15T00:00:00+09:00`
        は格納時に `2026-01-14T15:00:00.000000+00:00` に変換）。
        範囲境界（`to_jst_range_iso` の出力）も同書式に揃っているため、
        SQLite の文字列比較で時系列順比較が成立する。
    """

    def test_empty(self, db: MetadataDB) -> None:
        result = db.list_sources_by_date_range(
            date_from_iso="2026-01-15T00:00:00.000000+09:00",
            date_to_iso="2026-01-15T23:59:59.999999+09:00",
            limit=10,
        )
        assert result == []

    def test_inclusive_boundary_jst(self, db: MetadataDB) -> None:
        """JST 境界の両端 inclusive を確認."""
        _register(db, "start", published_at="2026-01-15T00:00:00+09:00")
        _register(db, "mid", published_at="2026-01-15T12:00:00+09:00")
        _register(db, "end", published_at="2026-01-15T23:59:59+09:00")
        _register(db, "before", published_at="2026-01-14T23:59:59+09:00")
        _register(db, "after", published_at="2026-01-16T00:00:00+09:00")

        date_from_iso, date_to_iso = to_jst_range_iso("2026-01-15", "2026-01-15")
        result = db.list_sources_by_date_range(
            date_from_iso=date_from_iso,
            date_to_iso=date_to_iso,
            limit=10,
            ascending=True,
        )
        assert [r.source_id for r in result] == ["start", "mid", "end"]

    def test_utc_published_at_compared_at_jst_boundary(self, db: MetadataDB) -> None:
        """`Z` 終端の UTC タイムスタンプも JST 境界での比較で正しく振り分く.

        2026-01-15T00:00:00Z (= JST 2026-01-15 09:00) → 1/15 範囲に入る
        2026-01-14T14:59:59Z (= JST 2026-01-14 23:59:59) → 1/15 範囲外
        2026-01-15T15:00:00Z (= JST 2026-01-16 00:00) → 1/15 範囲外
        """
        _register(db, "early_utc_in", published_at="2026-01-15T00:00:00Z")
        _register(db, "edge_jst_outside", published_at="2026-01-14T14:59:59Z")
        _register(db, "edge_jst_next_day", published_at="2026-01-15T15:00:00Z")

        date_from_iso, date_to_iso = to_jst_range_iso("2026-01-15", "2026-01-15")
        result = db.list_sources_by_date_range(
            date_from_iso=date_from_iso,
            date_to_iso=date_to_iso,
            limit=10,
        )
        ids = sorted(r.source_id for r in result)
        assert ids == ["early_utc_in"]

    def test_source_type_filter(self, db: MetadataDB) -> None:
        _register(db, "j1", source_type="journal", published_at="2026-01-15T10:00:00+09:00")
        _register(db, "b1", source_type="bluesky", published_at="2026-01-15T11:00:00+09:00")
        _register(db, "z1", source_type="zenn", published_at="2026-01-15T12:00:00+09:00")

        date_from_iso, date_to_iso = to_jst_range_iso("2026-01-15", "2026-01-15")

        result_all = db.list_sources_by_date_range(
            date_from_iso=date_from_iso,
            date_to_iso=date_to_iso,
            limit=10,
        )
        assert sorted(r.source_id for r in result_all) == ["b1", "j1", "z1"]

        result_journal = db.list_sources_by_date_range(
            date_from_iso=date_from_iso,
            date_to_iso=date_to_iso,
            source_type="journal",
            limit=10,
        )
        assert [r.source_id for r in result_journal] == ["j1"]

    def test_order_desc_default(self, db: MetadataDB) -> None:
        _register(db, "a", published_at="2026-01-15T01:00:00+09:00")
        _register(db, "b", published_at="2026-01-15T05:00:00+09:00")
        _register(db, "c", published_at="2026-01-15T10:00:00+09:00")

        date_from_iso, date_to_iso = to_jst_range_iso("2026-01-15", "2026-01-15")
        result = db.list_sources_by_date_range(
            date_from_iso=date_from_iso,
            date_to_iso=date_to_iso,
            limit=10,
        )
        assert [r.source_id for r in result] == ["c", "b", "a"]

    def test_limit(self, db: MetadataDB) -> None:
        for i in range(5):
            _register(
                db, f"w{i}",
                published_at=f"2026-01-15T{i:02d}:00:00+09:00",
            )
        date_from_iso, date_to_iso = to_jst_range_iso("2026-01-15", "2026-01-15")
        result = db.list_sources_by_date_range(
            date_from_iso=date_from_iso,
            date_to_iso=date_to_iso,
            limit=2,
        )
        assert len(result) == 2

    def test_excludes_deleted(self, db: MetadataDB) -> None:
        _register(db, "active1", published_at="2026-01-15T10:00:00+09:00")
        _register(db, "deleted1", published_at="2026-01-15T11:00:00+09:00", status="deleted")

        date_from_iso, date_to_iso = to_jst_range_iso("2026-01-15", "2026-01-15")
        result = db.list_sources_by_date_range(
            date_from_iso=date_from_iso,
            date_to_iso=date_to_iso,
            limit=10,
        )
        assert [r.source_id for r in result] == ["active1"]

    def test_filters_by_meta(self, db: MetadataDB) -> None:
        _register(
            db, "j1",
            source_type="journal",
            published_at="2026-01-15T10:00:00+09:00",
            meta=json.dumps({"repository": "rag-knowledge"}),
        )
        _register(
            db, "j2",
            source_type="journal",
            published_at="2026-01-15T11:00:00+09:00",
            meta=json.dumps({"repository": "other-repo"}),
        )

        date_from_iso, date_to_iso = to_jst_range_iso("2026-01-15", "2026-01-15")
        result = db.list_sources_by_date_range(
            date_from_iso=date_from_iso,
            date_to_iso=date_to_iso,
            filters={"repository": "rag-knowledge"},
            limit=10,
        )
        assert [r.source_id for r in result] == ["j1"]

    def test_filters_invalid_key(self, db: MetadataDB) -> None:
        date_from_iso, date_to_iso = to_jst_range_iso("2026-01-15", "2026-01-15")
        with pytest.raises(ValueError, match="フィルタキー名"):
            db.list_sources_by_date_range(
                date_from_iso=date_from_iso,
                date_to_iso=date_to_iso,
                filters={"bad-key!": "x"},
                limit=10,
            )


class TestCountSourcesByDateRange:
    """MetadataDB.count_sources_by_date_range のテスト."""

    def test_count(self, db: MetadataDB) -> None:
        for i in range(3):
            _register(
                db, f"w{i}",
                published_at=f"2026-01-15T{i:02d}:00:00+09:00",
            )
        _register(db, "out", published_at="2026-01-16T00:00:00+09:00")

        date_from_iso, date_to_iso = to_jst_range_iso("2026-01-15", "2026-01-15")
        assert db.count_sources_by_date_range(
            date_from_iso=date_from_iso,
            date_to_iso=date_to_iso,
        ) == 3

    def test_count_with_source_type(self, db: MetadataDB) -> None:
        _register(db, "j1", source_type="journal", published_at="2026-01-15T10:00:00+09:00")
        _register(db, "b1", source_type="bluesky", published_at="2026-01-15T11:00:00+09:00")

        date_from_iso, date_to_iso = to_jst_range_iso("2026-01-15", "2026-01-15")
        assert db.count_sources_by_date_range(
            date_from_iso=date_from_iso,
            date_to_iso=date_to_iso,
            source_type="journal",
        ) == 1


class TestListSourcesByDateRangeFormatter:
    """stats_port.list_sources_by_date_range 共通関数のフォーマット検証."""

    def test_empty_response(self, tmp_path: Path) -> None:
        # 空 metadata.db を作成
        db = MetadataDB(tmp_path / "metadata.db")
        db.initialize()
        db.close()

        result = list_sources_by_date_range(
            source_store_dir=str(tmp_path),
            date_from="2026-01-15",
            date_to="2026-01-15",
            source_type=None,
            limit=10,
        )
        assert "date_range: 2026-01-15〜2026-01-15" in result
        assert "（all, 0件 / 全0件）" in result

    def test_no_metadata_db(self, tmp_path: Path) -> None:
        result = list_sources_by_date_range(
            source_store_dir=str(tmp_path / "nonexistent"),
            date_from="2026-01-15",
            date_to="2026-01-15",
            source_type=None,
            limit=10,
        )
        assert "0件 / 全0件" in result

    def test_invalid_date_format_returns_error(self, tmp_path: Path) -> None:
        db = MetadataDB(tmp_path / "metadata.db")
        db.initialize()
        db.close()

        result = list_sources_by_date_range(
            source_store_dir=str(tmp_path),
            date_from="2026/01/15",
            date_to="2026-01-15",
            source_type=None,
            limit=10,
        )
        assert result.startswith("エラー:")
        assert "YYYY-MM-DD" in result

    def test_formatted_entries_include_type(self, tmp_path: Path) -> None:
        db_path = tmp_path / "metadata.db"
        db = MetadataDB(db_path)
        db.initialize()
        db.register_source(
            source_id="journal/test.md",
            source_type="journal",
            title="JournalEntry",
            content_hash="h",
            file_size=2048,
            collected_at="2026-01-15T10:00:00+09:00",
            updated_at="2026-01-15T10:00:00+09:00",
            published_at="2026-01-15T10:00:00+09:00",
            meta="{}",
        )
        db.register_source(
            source_id="bluesky/post1",
            source_type="bluesky",
            title="Post",
            content_hash="h2",
            file_size=512,
            collected_at="2026-01-15T11:00:00+09:00",
            updated_at="2026-01-15T11:00:00+09:00",
            published_at="2026-01-15T11:00:00+09:00",
            meta="{}",
        )
        db.close()

        result = list_sources_by_date_range(
            source_store_dir=str(tmp_path),
            date_from="2026-01-15",
            date_to="2026-01-15",
            source_type=None,
            limit=10,
        )
        assert "date_range: 2026-01-15〜2026-01-15（all, 2件 / 全2件" in result
        assert "Type: journal" in result
        assert "Type: bluesky" in result
        assert "JournalEntry" in result
        assert "Post" in result

    def test_formatted_with_source_type_filter(self, tmp_path: Path) -> None:
        db_path = tmp_path / "metadata.db"
        db = MetadataDB(db_path)
        db.initialize()
        db.register_source(
            source_id="journal/test.md",
            source_type="journal",
            title="JournalEntry",
            content_hash="h",
            file_size=2048,
            collected_at="2026-01-15T10:00:00+09:00",
            updated_at="2026-01-15T10:00:00+09:00",
            published_at="2026-01-15T10:00:00+09:00",
            meta="{}",
        )
        db.close()

        result = list_sources_by_date_range(
            source_store_dir=str(tmp_path),
            date_from="2026-01-15",
            date_to="2026-01-15",
            source_type="journal",
            limit=10,
        )
        assert "（journal, 1件 / 全1件" in result
        assert "Type: journal" in result

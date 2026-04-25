"""YouTube コンバーターハンドラのテスト.

仕様: docs/specs/ingesters/youtube.md（コンバーター対応セクション）

テスト方針:
- スニペット結合（間隔分割・文字数分割）
- タイムスタンプフォーマット（MM:SS / HH:MM:SS）
- ヘッダー生成
- 空スニペット
- upload_date フォーマット
"""

from __future__ import annotations

from typing import Any

from rag.converter.handlers import (
    _format_timestamp_yt,
    _format_upload_date,
    convert_json_youtube,
)


class TestFormatTimestamp:
    def test_seconds_only(self) -> None:
        assert _format_timestamp_yt(45.0, has_hours=False) == "[00:45]"

    def test_minutes_and_seconds(self) -> None:
        assert _format_timestamp_yt(125.0, has_hours=False) == "[02:05]"

    def test_with_hours(self) -> None:
        assert _format_timestamp_yt(3661.0, has_hours=True) == "[1:01:01]"

    def test_zero(self) -> None:
        assert _format_timestamp_yt(0.0, has_hours=False) == "[00:00]"

    def test_exactly_one_hour(self) -> None:
        assert _format_timestamp_yt(3600.0, has_hours=True) == "[1:00:00]"


class TestFormatUploadDate:
    def test_valid_date(self) -> None:
        assert _format_upload_date("20240901") == "2024-09-01"

    def test_invalid_format_passthrough(self) -> None:
        assert _format_upload_date("2024-09-01") == "2024-09-01"

    def test_empty_string(self) -> None:
        assert _format_upload_date("") == ""


class TestConvertJsonYoutube:
    def _make_data(
        self,
        *,
        snippets: list[dict[str, Any]] | None = None,
        duration: int = 120,
    ) -> dict[str, Any]:
        return {
            "video_id": "TestVideo01",
            "title": "Test Video",
            "uploader": "Test Channel",
            "upload_date": "20240901",
            "duration": duration,
            "snippets": snippets or [],
        }

    def test_empty_snippets_returns_header_only(self) -> None:
        data = self._make_data(snippets=[])
        result = convert_json_youtube(data)
        assert result is not None
        assert "# Test Video" in result
        assert "投稿者: Test Channel" in result
        assert "公開日: 2024-09-01" in result
        assert "---" in result

    def test_single_snippet(self) -> None:
        data = self._make_data(snippets=[
            {"start": 3.0, "end": 7.0, "text": "Hello world"},
        ])
        result = convert_json_youtube(data)
        assert result is not None
        assert "[00:03] Hello world" in result

    def test_gap_based_paragraph_split(self) -> None:
        """間隔ベースの段落分割を検証する."""
        data = self._make_data(snippets=[
            {"start": 0.0, "end": 3.0, "text": "First"},
            {"start": 3.0, "end": 6.0, "text": " second"},
            {"start": 10.0, "end": 13.0, "text": "After gap"},
        ])
        result = convert_json_youtube(data, merge_gap_sec=2.0)
        assert result is not None
        # "First second" は 1 段落、"After gap" は別段落
        assert "[00:00] First second" in result
        assert "[00:10] After gap" in result

    def test_char_limit_paragraph_split(self) -> None:
        """文字数ベースの段落分割を検証する."""
        data = self._make_data(snippets=[
            {"start": 0.0, "end": 1.0, "text": "A" * 200},
            {"start": 1.0, "end": 2.0, "text": "B" * 200},
        ])
        result = convert_json_youtube(data, merge_max_chars=300, merge_gap_sec=10.0)
        assert result is not None
        # 200 + 200 = 400 > 300 なので分割される
        lines = [line for line in result.split("\n") if line.startswith("[")]
        assert len(lines) == 2

    def test_hour_timestamp_format(self) -> None:
        """1時間以上の動画で HH:MM:SS 形式になることを検証する."""
        data = self._make_data(
            snippets=[
                {"start": 3661.0, "end": 3665.0, "text": "Long video"},
            ],
            duration=7200,
        )
        result = convert_json_youtube(data)
        assert result is not None
        assert "[1:01:01]" in result

    def test_missing_title_fallback(self) -> None:
        """タイトルが空の場合のフォールバックを検証する."""
        data = self._make_data()
        data["title"] = ""
        result = convert_json_youtube(data)
        assert result is not None
        assert "# (Untitled)" in result

    def test_video_url_in_header(self) -> None:
        """ヘッダーに動画 URL が含まれることを検証する."""
        data = self._make_data()
        result = convert_json_youtube(data)
        assert result is not None
        assert "https://www.youtube.com/watch?v=TestVideo01" in result

    def test_continuous_snippets_merged(self) -> None:
        """連続するスニペットが結合されることを検証する."""
        data = self._make_data(snippets=[
            {"start": 0.0, "end": 1.0, "text": "One"},
            {"start": 1.0, "end": 2.0, "text": " two"},
            {"start": 2.0, "end": 3.0, "text": " three"},
        ])
        result = convert_json_youtube(data, merge_gap_sec=2.0, merge_max_chars=1000)
        assert result is not None
        assert "[00:00] One two three" in result

    def test_end_time_reversal_handled(self) -> None:
        """end 時刻が逆転しても gap 計算が壊れないことを検証する."""
        data = self._make_data(snippets=[
            {"start": 0.0, "end": 10.0, "text": "AAA"},
            {"start": 5.0, "end": 8.0, "text": "BBB"},   # end が前のより小さい（逆転）
            {"start": 15.0, "end": 18.0, "text": "CCC"},  # 実際の gap は 15-10=5
        ])
        # max(prev_end) により prev_end=10.0 が維持され、gap=15-10=5 >= 2.0 で分割
        result = convert_json_youtube(data, merge_gap_sec=2.0, merge_max_chars=1000)
        assert result is not None
        assert "[00:00] AAABBB" in result
        assert "[00:15] CCC" in result

    def test_end_time_reversal_without_max_would_fail(self) -> None:
        """end 時刻逆転時に max() がないと gap が変わることを間接的に検証する."""
        data = self._make_data(snippets=[
            {"start": 0.0, "end": 10.0, "text": "AAA"},
            {"start": 5.0, "end": 6.0, "text": "BBB"},   # end=6 < prev_end=10
            {"start": 11.5, "end": 14.0, "text": "CCC"},  # gap=11.5-10=1.5 (max), gap=11.5-6=5.5 (no max)
        ])
        # max(prev_end) により prev_end=10.0 → gap=1.5 < 2.0 → マージ
        result = convert_json_youtube(data, merge_gap_sec=2.0, merge_max_chars=1000)
        assert result is not None
        assert "[00:00] AAABBBCCC" in result

    def test_none_upload_date_and_duration(self) -> None:
        """upload_date や duration が None でもクラッシュしないことを検証する."""
        data = {
            "video_id": "test_id_0001",
            "title": "Test",
            "uploader": "Tester",
            "upload_date": None,
            "duration": None,
            "snippets": [{"start": 0.0, "end": 1.0, "text": "Hello"}],
        }
        result = convert_json_youtube(data)
        assert result is not None
        assert "# Test" in result
        assert "[00:00] Hello" in result

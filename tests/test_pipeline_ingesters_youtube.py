"""YouTube インジェスターのテスト.

仕様: docs/specs/ingesters/youtube.md

テスト方針:
- URL パース・バリデーション
- max_videos のバリデーション・クランプ
- 字幕取得（モック）
- Whisper フォールバック（モック）
- source_store 配置（JSON + .meta）
- プレイリスト展開（モック）
- エラーハンドリング
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rag.pipeline.ingesters._common import IngestResult
from rag.pipeline.ingesters.youtube import (
    MAX_VIDEOS_HARD_LIMIT,
    YoutubeIngester,
    _validate_max_videos,
    extract_playlist_id,
    extract_video_id,
)


# --- URL パーステスト ---


class TestExtractVideoId:
    def test_standard_url(self) -> None:
        assert extract_video_id("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs") == "JV3KOJ_Z4Vs"

    def test_short_url(self) -> None:
        assert extract_video_id("https://youtu.be/JV3KOJ_Z4Vs") == "JV3KOJ_Z4Vs"

    def test_url_with_extra_params(self) -> None:
        assert extract_video_id("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs&t=30s") == "JV3KOJ_Z4Vs"

    def test_mobile_url(self) -> None:
        assert extract_video_id("https://m.youtube.com/watch?v=JV3KOJ_Z4Vs") == "JV3KOJ_Z4Vs"

    def test_invalid_url_raises(self) -> None:
        with pytest.raises(ValueError, match="不正な YouTube URL"):
            extract_video_id("https://example.com/watch?v=test")

    def test_no_video_id_raises(self) -> None:
        with pytest.raises(ValueError, match="不正な YouTube URL"):
            extract_video_id("https://www.youtube.com/watch")

    def test_invalid_video_id_format_raises(self) -> None:
        with pytest.raises(ValueError, match="不正な YouTube URL"):
            extract_video_id("https://www.youtube.com/watch?v=short")


class TestExtractPlaylistId:
    def test_standard_playlist_url(self) -> None:
        assert extract_playlist_id(
            "https://www.youtube.com/playlist?list=PLi8SA3sbzYVSSlN3Ekb1nEpxnFuVzGz3_"
        ) == "PLi8SA3sbzYVSSlN3Ekb1nEpxnFuVzGz3_"

    def test_invalid_domain_raises(self) -> None:
        with pytest.raises(ValueError, match="不正な YouTube プレイリスト URL"):
            extract_playlist_id("https://example.com/playlist?list=test")

    def test_missing_list_param_raises(self) -> None:
        with pytest.raises(ValueError, match="プレイリスト ID が見つかりません"):
            extract_playlist_id("https://www.youtube.com/playlist")


# --- バリデーションテスト ---


class TestValidateMaxVideos:
    def test_valid_value(self) -> None:
        assert _validate_max_videos(100) == 100

    def test_clamp_over_limit(self) -> None:
        assert _validate_max_videos(600) == MAX_VIDEOS_HARD_LIMIT

    def test_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="1 以上"):
            _validate_max_videos(0)

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="1 以上"):
            _validate_max_videos(-1)

    def test_bool_raises(self) -> None:
        with pytest.raises(TypeError, match="整数"):
            _validate_max_videos(True)

    def test_string_raises(self) -> None:
        with pytest.raises(TypeError, match="整数"):
            _validate_max_videos("100")  # type: ignore[arg-type]

    def test_boundary_500(self) -> None:
        assert _validate_max_videos(500) == 500

    def test_boundary_501_clamped(self) -> None:
        assert _validate_max_videos(501) == MAX_VIDEOS_HARD_LIMIT


# --- source_store 配置テスト ---


@pytest.fixture()
def source_store(tmp_path: Path) -> Any:
    """テスト用の簡易 SourceStore モック."""
    mock = MagicMock()
    mock.root_dir = tmp_path
    return mock


def _make_metadata(*, channel_id: str = "UCtest123456789012345", duration: int = 120) -> dict[str, Any]:
    """テスト用メタデータを生成する."""
    return {
        "id": "JV3KOJ_Z4Vs",
        "title": "Test Video Title",
        "channel_id": channel_id,
        "uploader": "Test Channel",
        "upload_date": "20240901",
        "duration": duration,
        "description": "Test description",
    }


def _make_snippets() -> list[dict[str, Any]]:
    """テスト用スニペットを生成する."""
    return [
        {"start": 0.0, "end": 3.0, "text": "First snippet"},
        {"start": 3.0, "end": 6.0, "text": " second part"},
        {"start": 10.0, "end": 13.0, "text": "After gap"},
    ]


class TestIngestVideo:
    @pytest.mark.asyncio()
    async def test_successful_subtitle_ingest(self, source_store: Any) -> None:
        """字幕取得成功時のファイル配置を検証する."""
        ingester = YoutubeIngester(source_store, max_duration=14400)

        metadata = _make_metadata()
        snippets = _make_snippets()

        with (
            patch.object(ingester, "_fetch_metadata", new_callable=AsyncMock, return_value=metadata),
            patch.object(
                ingester,
                "_fetch_transcript",
                new_callable=AsyncMock,
                return_value=(snippets, "subtitle", "ja"),
            ),
        ):
            result = await ingester.ingest_video("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs")

        assert result.placed == 1
        assert result.errors == 0

        # JSON ファイルの検証
        json_path = source_store.root_dir / "youtube" / "UCtest123456789012345" / "JV3KOJ_Z4Vs.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["video_id"] == "JV3KOJ_Z4Vs"
        assert data["transcript_source"] == "subtitle"
        assert len(data["snippets"]) == 3

        # .meta ファイルの検証
        meta_path = json_path.parent / "JV3KOJ_Z4Vs.json.meta"
        assert meta_path.exists()
        meta_content = meta_path.read_text(encoding="utf-8")
        assert "source_type: youtube" in meta_content
        assert "video_id: JV3KOJ_Z4Vs" in meta_content

    @pytest.mark.asyncio()
    async def test_duration_exceeded_skipped(self, source_store: Any) -> None:
        """動画長上限超過時にスキップされることを検証する."""
        ingester = YoutubeIngester(source_store, max_duration=60)

        metadata = _make_metadata(duration=3600)

        with patch.object(ingester, "_fetch_metadata", new_callable=AsyncMock, return_value=metadata):
            result = await ingester.ingest_video("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs")

        assert result.placed == 0
        assert result.skipped == 1

    @pytest.mark.asyncio()
    async def test_metadata_error_handled(self, source_store: Any) -> None:
        """メタデータ取得失敗時のエラーハンドリングを検証する."""
        ingester = YoutubeIngester(source_store)

        with patch.object(
            ingester,
            "_fetch_metadata",
            new_callable=AsyncMock,
            side_effect=Exception("API error"),
        ):
            result = await ingester.ingest_video("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs")

        assert result.errors == 1
        assert "メタデータ取得失敗" in result.error_details[0]

    @pytest.mark.asyncio()
    async def test_unknown_channel_id_fallback(self, source_store: Any) -> None:
        """channel_id が取得できない場合のフォールバックを検証する."""
        ingester = YoutubeIngester(source_store)

        metadata = _make_metadata()
        metadata["channel_id"] = None
        snippets = _make_snippets()

        with (
            patch.object(ingester, "_fetch_metadata", new_callable=AsyncMock, return_value=metadata),
            patch.object(
                ingester,
                "_fetch_transcript",
                new_callable=AsyncMock,
                return_value=(snippets, "subtitle", "ja"),
            ),
        ):
            result = await ingester.ingest_video("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs")

        assert result.placed == 1
        json_path = source_store.root_dir / "youtube" / "unknown" / "JV3KOJ_Z4Vs.json"
        assert json_path.exists()

    @pytest.mark.asyncio()
    async def test_whisper_model_recorded(self, source_store: Any) -> None:
        """Whisper 使用時に whisper_model が JSON に記録されることを検証する."""
        ingester = YoutubeIngester(source_store, whisper_model="base")

        metadata = _make_metadata()
        snippets = _make_snippets()

        with (
            patch.object(ingester, "_fetch_metadata", new_callable=AsyncMock, return_value=metadata),
            patch.object(
                ingester,
                "_fetch_transcript",
                new_callable=AsyncMock,
                return_value=(snippets, "whisper", "ja"),
            ),
        ):
            result = await ingester.ingest_video("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs")

        assert result.placed == 1
        json_path = source_store.root_dir / "youtube" / "UCtest123456789012345" / "JV3KOJ_Z4Vs.json"
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["transcript_source"] == "whisper"
        assert data["whisper_model"] == "base"


    @pytest.mark.asyncio()
    async def test_overwritten_count_on_reingest(self, source_store: Any) -> None:
        """同一動画を 2 回取り込んだ時に overwritten がカウントされることを検証する."""
        ingester = YoutubeIngester(source_store, max_duration=14400)

        metadata = _make_metadata()
        snippets = _make_snippets()

        # 1 回目
        with (
            patch.object(ingester, "_fetch_metadata", new_callable=AsyncMock, return_value=metadata),
            patch.object(ingester, "_fetch_transcript", new_callable=AsyncMock, return_value=(snippets, "subtitle", "ja")),
        ):
            result1 = await ingester.ingest_video("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs")
        assert result1.placed == 1
        assert result1.overwritten == 0

        # 2 回目（同一動画）
        with (
            patch.object(ingester, "_fetch_metadata", new_callable=AsyncMock, return_value=metadata),
            patch.object(ingester, "_fetch_transcript", new_callable=AsyncMock, return_value=(snippets, "subtitle", "ja")),
        ):
            result2 = await ingester.ingest_video("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs")
        assert result2.placed == 1
        assert result2.overwritten == 1


    @pytest.mark.asyncio()
    async def test_api_error_does_not_fallback_to_whisper(self, source_store: Any) -> None:
        """API エラー（IP ブロック等）では Whisper フォールバックせずエラーになることを検証する.

        _fetch_subtitle をパッチして RequestBlocked を投げさせることで、
        _fetch_transcript 内の例外フィルタリング（TranscriptsDisabled/NoTranscriptFound のみ
        Whisper フォールバック）が正しく動作することを検証する。
        """
        ingester = YoutubeIngester(source_store, max_duration=14400)
        metadata = _make_metadata()

        from youtube_transcript_api import RequestBlocked

        with (
            patch.object(ingester, "_fetch_metadata", new_callable=AsyncMock, return_value=metadata),
            patch.object(
                ingester,
                "_fetch_subtitle",
                new_callable=AsyncMock,
                side_effect=RequestBlocked("JV3KOJ_Z4Vs"),
            ),
        ):
            result = await ingester.ingest_video("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs")

        # Whisper フォールバックされず、エラーとして処理される
        assert result.errors == 1
        assert result.placed == 0

    @pytest.mark.asyncio()
    async def test_transcripts_disabled_triggers_whisper_fallback(self, source_store: Any) -> None:
        """TranscriptsDisabled では Whisper フォールバックが発動することを検証する."""
        from youtube_transcript_api import TranscriptsDisabled

        ingester = YoutubeIngester(source_store, max_duration=14400)
        metadata = _make_metadata()
        snippets = _make_snippets()

        with (
            patch.object(ingester, "_fetch_metadata", new_callable=AsyncMock, return_value=metadata),
            patch.object(
                ingester,
                "_fetch_subtitle",
                new_callable=AsyncMock,
                side_effect=TranscriptsDisabled("JV3KOJ_Z4Vs"),
            ),
            patch.object(
                ingester,
                "_transcribe_with_whisper",
                new_callable=AsyncMock,
                return_value=(snippets, "ja"),
            ),
        ):
            result = await ingester.ingest_video("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs")

        assert result.placed == 1
        assert result.errors == 0


class TestRequestIntervalClamp:
    def test_interval_clamped_to_minimum(self) -> None:
        """MIN_REQUEST_INTERVAL 未満の値がクランプされることを検証する."""
        from rag.pipeline.ingesters.youtube import MIN_REQUEST_INTERVAL
        from unittest.mock import MagicMock

        store = MagicMock()
        ingester = YoutubeIngester(store, request_interval=0.01)
        assert ingester._request_interval == MIN_REQUEST_INTERVAL

    def test_interval_above_minimum_preserved(self) -> None:
        """MIN_REQUEST_INTERVAL 以上の値がそのまま保持されることを検証する."""
        from unittest.mock import MagicMock

        store = MagicMock()
        ingester = YoutubeIngester(store, request_interval=5.0)
        assert ingester._request_interval == 5.0


class TestCrawlPlaylist:
    @pytest.mark.asyncio()
    async def test_invalid_url_raises(self, source_store: Any) -> None:
        """不正なプレイリスト URL でエラーになることを検証する."""
        ingester = YoutubeIngester(source_store)

        with pytest.raises(ValueError, match="不正な YouTube プレイリスト URL"):
            await ingester.crawl_playlist("https://example.com/playlist?list=test")

    @pytest.mark.asyncio()
    async def test_successful_playlist_crawl(self, source_store: Any) -> None:
        """プレイリスト展開 + 各動画取り込みの正常系を検証する."""
        ingester = YoutubeIngester(source_store, max_videos=3)

        entries = [
            {"id": "video_id_0001", "url": "video_id_0001"},
            {"id": "video_id_0002", "url": "video_id_0002"},
        ]
        single_result = IngestResult(placed=1)

        with (
            patch.object(
                ingester,
                "_expand_playlist",
                new_callable=AsyncMock,
                return_value=entries,
            ),
            patch.object(
                ingester,
                "ingest_video",
                new_callable=AsyncMock,
                return_value=single_result,
            ),
        ):
            result = await ingester.crawl_playlist(
                "https://www.youtube.com/playlist?list=PLtest123"
            )

        assert result.placed == 2
        assert result.errors == 0

    @pytest.mark.asyncio()
    async def test_circuit_breaker_on_consecutive_errors(self, source_store: Any) -> None:
        """5 回連続失敗でサーキットブレーカーが発動することを検証する."""
        ingester = YoutubeIngester(source_store, max_videos=10)

        entries = [{"id": f"vid_{i:011d}", "url": f"vid_{i:011d}"} for i in range(10)]
        error_result = IngestResult(errors=1, error_details=["test error"])

        with (
            patch.object(
                ingester,
                "_expand_playlist",
                new_callable=AsyncMock,
                return_value=entries,
            ),
            patch.object(
                ingester,
                "ingest_video",
                new_callable=AsyncMock,
                return_value=error_result,
            ),
        ):
            result = await ingester.crawl_playlist(
                "https://www.youtube.com/playlist?list=PLtest123"
            )

        # サーキットブレーカー発動で 5 件で停止
        assert result.errors == 5
        assert result.placed == 0

    @pytest.mark.asyncio()
    async def test_empty_playlist(self, source_store: Any) -> None:
        """空プレイリストで placed=0 の正常終了を検証する."""
        ingester = YoutubeIngester(source_store)

        with patch.object(
            ingester,
            "_expand_playlist",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await ingester.crawl_playlist(
                "https://www.youtube.com/playlist?list=PLtest123"
            )

        assert result.placed == 0
        assert result.errors == 0

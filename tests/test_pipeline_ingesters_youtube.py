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
    _validate_max_videos,
    extract_playlist_id,
    extract_video_id,
)

from factories import make_youtube_ingester


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
        """字幕取得成功時に place_file が正しく呼ばれることを検証する."""
        ingester = make_youtube_ingester(source_store, max_duration=14400)

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

        # place_file の呼び出し検証
        source_store.place_file.assert_called_once()
        call_kwargs = source_store.place_file.call_args
        assert call_kwargs.kwargs["source_type"] == "youtube"
        assert call_kwargs.kwargs["rel_path"] == "youtube/UCtest123456789012345/JV3KOJ_Z4Vs.json"

        # JSON データの検証
        data = json.loads(call_kwargs.kwargs["data"].decode("utf-8"))
        assert data["video_id"] == "JV3KOJ_Z4Vs"
        assert data["transcript_source"] == "subtitle"
        assert len(data["snippets"]) == 3

        # メタデータの検証
        meta = call_kwargs.kwargs["metadata"]
        assert meta["source_type"] == "youtube"
        assert meta["video_id"] == "JV3KOJ_Z4Vs"

    @pytest.mark.asyncio()
    async def test_duration_exceeded_skipped(self, source_store: Any) -> None:
        """動画長上限超過時にスキップされることを検証する."""
        ingester = make_youtube_ingester(source_store, max_duration=60)

        metadata = _make_metadata(duration=3600)

        with patch.object(ingester, "_fetch_metadata", new_callable=AsyncMock, return_value=metadata):
            result = await ingester.ingest_video("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs")

        assert result.placed == 0
        assert result.skipped == 1

    @pytest.mark.asyncio()
    async def test_metadata_error_handled(self, source_store: Any) -> None:
        """メタデータ取得失敗時のエラーハンドリングを検証する."""
        ingester = make_youtube_ingester(source_store)

        with patch.object(
            ingester,
            "_fetch_metadata",
            new_callable=AsyncMock,
            side_effect=Exception("API error"),
        ):
            result = await ingester.ingest_video("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs")

        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "metadata_fetch"
        assert detail["target"] == "JV3KOJ_Z4Vs"
        assert "メタデータ取得失敗" in detail["message"]

    @pytest.mark.asyncio()
    async def test_missing_channel_id_causes_error(self, source_store: Any) -> None:
        """channel_id が取得できない場合にエラーになることを検証する."""
        ingester = make_youtube_ingester(source_store)

        metadata = _make_metadata()
        metadata["channel_id"] = None

        with patch.object(ingester, "_fetch_metadata", new_callable=AsyncMock, return_value=metadata):
            result = await ingester.ingest_video("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs")

        assert result.placed == 0
        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "metadata_fetch"
        assert detail["target"] == "JV3KOJ_Z4Vs"
        assert "channel_id" in detail["message"]

    @pytest.mark.asyncio()
    async def test_whisper_model_recorded(self, source_store: Any) -> None:
        """Whisper 使用時に whisper_model が JSON に記録されることを検証する."""
        ingester = make_youtube_ingester(source_store, whisper_model="base")

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
        call_kwargs = source_store.place_file.call_args
        data = json.loads(call_kwargs.kwargs["data"].decode("utf-8"))
        assert data["transcript_source"] == "whisper"
        assert data["whisper_model"] == "base"

    @pytest.mark.asyncio()
    async def test_overwritten_count_on_reingest(self, source_store: Any) -> None:
        """同一動画を 2 回取り込んだ時に overwritten がカウントされること（排他計上）を検証する.

        仕様: docs/specs/ingesters/common.md「placed と overwritten の排他関係」
        """
        ingester = make_youtube_ingester(source_store, max_duration=14400)

        metadata = _make_metadata()
        snippets = _make_snippets()

        # 1 回目: dest.exists() が False → placed=1, overwritten=0
        with (
            patch.object(ingester, "_fetch_metadata", new_callable=AsyncMock, return_value=metadata),
            patch.object(ingester, "_fetch_transcript", new_callable=AsyncMock, return_value=(snippets, "subtitle", "ja")),
        ):
            result1 = await ingester.ingest_video("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs")
        assert result1.placed == 1
        assert result1.overwritten == 0

        # 2 回目: dest に実ファイルを作成して exists() が True になるようにする
        dest = source_store.root_dir / "youtube" / "UCtest123456789012345" / "JV3KOJ_Z4Vs.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("{}", encoding="utf-8")

        with (
            patch.object(ingester, "_fetch_metadata", new_callable=AsyncMock, return_value=metadata),
            patch.object(ingester, "_fetch_transcript", new_callable=AsyncMock, return_value=(snippets, "subtitle", "ja")),
        ):
            result2 = await ingester.ingest_video("https://www.youtube.com/watch?v=JV3KOJ_Z4Vs")
        # 排他計上: 既存ファイル上書き時は placed=0, overwritten=1
        assert result2.placed == 0
        assert result2.overwritten == 1


    @pytest.mark.asyncio()
    async def test_api_error_does_not_fallback_to_whisper(self, source_store: Any) -> None:
        """API エラー（IP ブロック等）では Whisper フォールバックせずエラーになることを検証する.

        _fetch_subtitle をパッチして RequestBlocked を投げさせることで、
        _fetch_transcript 内の例外フィルタリング（TranscriptsDisabled/NoTranscriptFound のみ
        Whisper フォールバック）が正しく動作することを検証する。
        """
        ingester = make_youtube_ingester(source_store, max_duration=14400)
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

        ingester = make_youtube_ingester(source_store, max_duration=14400)
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
        ingester = make_youtube_ingester(store, request_interval=0.01)
        assert ingester._request_interval == MIN_REQUEST_INTERVAL

    def test_interval_above_minimum_preserved(self) -> None:
        """MIN_REQUEST_INTERVAL 以上の値がそのまま保持されることを検証する."""
        from unittest.mock import MagicMock

        store = MagicMock()
        ingester = make_youtube_ingester(store, request_interval=5.0)
        assert ingester._request_interval == 5.0


class TestCrawlPlaylist:
    @pytest.mark.asyncio()
    async def test_invalid_url_raises(self, source_store: Any) -> None:
        """不正なプレイリスト URL でエラーになることを検証する."""
        ingester = make_youtube_ingester(source_store)

        with pytest.raises(ValueError, match="不正な YouTube プレイリスト URL"):
            await ingester.crawl_playlist("https://example.com/playlist?list=test")

    @pytest.mark.asyncio()
    async def test_successful_playlist_crawl(self, source_store: Any) -> None:
        """プレイリスト展開 + 各動画取り込みの正常系を検証する."""
        ingester = make_youtube_ingester(source_store, max_videos=3, request_interval=0.1)

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
    async def test_sleep_uses_jitter(self, source_store: Any) -> None:
        """random.uniform がジッター範囲で呼ばれ、その値が asyncio.sleep に渡されることを検証する."""
        from rag.pipeline.ingesters.youtube import JITTER_MIN_RATIO

        interval = 10.0
        ingester = make_youtube_ingester(
            source_store, max_videos=3, request_interval=interval
        )

        entries = [
            {"id": "video_id_0001", "url": "video_id_0001"},
            {"id": "video_id_0002", "url": "video_id_0002"},
            {"id": "video_id_0003", "url": "video_id_0003"},
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
            patch("rag.pipeline.ingesters.youtube.random.uniform", return_value=7.5) as mock_uniform,
            patch("rag.pipeline.ingesters.youtube.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await ingester.crawl_playlist(
                "https://www.youtube.com/playlist?list=PLtest123"
            )

        assert mock_uniform.call_count == 2
        for call in mock_uniform.call_args_list:
            lo, hi = call.args
            assert lo == interval * JITTER_MIN_RATIO
            assert hi == interval

        assert mock_sleep.call_count == 2
        for call in mock_sleep.call_args_list:
            assert call.args[0] == 7.5

    @pytest.mark.asyncio()
    async def test_sleep_jitter_clamped_to_min_request_interval(self, source_store: Any) -> None:
        """ジッター値が MIN_REQUEST_INTERVAL 未満の場合にクランプされることを検証する."""
        from rag.pipeline.ingesters.youtube import MIN_REQUEST_INTERVAL

        ingester = make_youtube_ingester(
            source_store, max_videos=2, request_interval=MIN_REQUEST_INTERVAL
        )

        entries = [
            {"id": "video_id_0001", "url": "video_id_0001"},
            {"id": "video_id_0002", "url": "video_id_0002"},
        ]
        single_result = IngestResult(placed=1)
        jitter_below_min = MIN_REQUEST_INTERVAL * 0.01

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
            patch("rag.pipeline.ingesters.youtube.random.uniform", return_value=jitter_below_min),
            patch("rag.pipeline.ingesters.youtube.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await ingester.crawl_playlist(
                "https://www.youtube.com/playlist?list=PLtest123"
            )

        assert mock_sleep.call_count == 1
        assert mock_sleep.call_args.args[0] == MIN_REQUEST_INTERVAL

    @pytest.mark.asyncio()
    async def test_circuit_breaker_on_consecutive_errors(self, source_store: Any) -> None:
        """5 回連続失敗でサーキットブレーカーが発動することを検証する."""
        ingester = make_youtube_ingester(source_store, max_videos=10, request_interval=0.1)

        entries = [{"id": f"vid_{i:011d}", "url": f"vid_{i:011d}"} for i in range(10)]

        def _make_error_result() -> IngestResult:
            return IngestResult(
                errors=1,
                error_details=[{
                    "category": "metadata_fetch",
                    "target": "test",
                    "message": "test error",
                }],
            )

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
                side_effect=lambda *args, **kwargs: _make_error_result(),
            ),
        ):
            result = await ingester.crawl_playlist(
                "https://www.youtube.com/playlist?list=PLtest123"
            )

        # サーキットブレーカー発動で 5 件で停止
        assert result.errors == 5
        assert result.placed == 0
        assert result.aborted is True
        assert result.abort_reason == "circuit breaker"

    @pytest.mark.asyncio()
    async def test_empty_playlist(self, source_store: Any) -> None:
        """空プレイリストで placed=0 の正常終了を検証する."""
        ingester = make_youtube_ingester(source_store)

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

    @pytest.mark.asyncio()
    async def test_playlist_expand_error_dict(self, source_store: Any) -> None:
        """プレイリスト展開失敗時の error_details dict 構造を検証する."""
        ingester = make_youtube_ingester(source_store)

        with patch.object(
            ingester,
            "_expand_playlist",
            new_callable=AsyncMock,
            side_effect=Exception("Playlist API error"),
        ):
            result = await ingester.crawl_playlist(
                "https://www.youtube.com/playlist?list=PLtest123"
            )

        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "metadata_fetch"
        assert detail["target"] == "PLtest123"
        assert "Playlist API error" in detail["message"]

    @pytest.mark.asyncio()
    async def test_missing_video_id_in_entry_dict(self, source_store: Any) -> None:
        """entry に video_id がない場合の error_details dict."""
        ingester = make_youtube_ingester(source_store, max_videos=10)

        entries = [{"id": "", "url": ""}]

        with patch.object(
            ingester,
            "_expand_playlist",
            new_callable=AsyncMock,
            return_value=entries,
        ):
            result = await ingester.crawl_playlist(
                "https://www.youtube.com/playlist?list=PLtest123"
            )

        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "metadata_fetch"
        assert detail["target"] == "entry_0"
        assert "video_id missing" in detail["message"]

"""YouTube インジェスターのテスト.

仕様: docs/specs/ingesters/youtube.md
仕様: docs/specs/infrastructure/fake-adapters/youtube.md

テスト方針:
- URL パース・バリデーション
- URL 種別分類（video / malformed / not_youtube）
- max_videos のバリデーション・クランプ
- 字幕取得（FakeYoutubeFetcher 経由）
- Whisper フォールバック（FakeYoutubeFetcher の no_subtitle / transcripts_disabled シナリオ）
- source_store 配置（JSON + .meta）
- プレイリスト展開（FakeYoutubeFetcher の playlist_entries 注入）
- エラーハンドリング（FakeYoutubeFetcher のシナリオ切替）

外部ライブラリ（yt_dlp / youtube_transcript_api / faster_whisper）は
tests/conftest.py の autouse 安全網で _RaiseOnUse に差し替えられているため、
実 YouTube アクセスは発生しない。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rag.pipeline.ingesters._common import IngestResult
from rag.pipeline.ingesters._fake.youtube import FakeYoutubeFetcher
from rag.pipeline.ingesters.youtube import (
    MAX_VIDEOS_HARD_LIMIT,
    _validate_max_videos,
    classify_youtube_url,
    extract_playlist_id,
    extract_video_id,
    is_youtube_video_url,
)

from factories import make_youtube_ingester


_FIXTURE_DIR = (
    Path(__file__).parent.parent
    / "src" / "rag" / "pipeline" / "ingesters" / "_fake" / "youtube" / "data"
)


def _fake(**kwargs: Any) -> FakeYoutubeFetcher:
    """FakeYoutubeFetcher を fixture_dir 込みで構築するヘルパー."""
    return FakeYoutubeFetcher(fixture_dir=_FIXTURE_DIR, **kwargs)


# --- URL パーステスト ---


class TestExtractVideoId:
    def test_standard_url(self) -> None:
        assert extract_video_id("https://www.youtube.com/watch?v=TestVideo01") == "TestVideo01"

    def test_short_url(self) -> None:
        assert extract_video_id("https://youtu.be/TestVideo01") == "TestVideo01"

    def test_url_with_extra_params(self) -> None:
        assert extract_video_id("https://www.youtube.com/watch?v=TestVideo01&t=30s") == "TestVideo01"

    def test_mobile_url(self) -> None:
        assert extract_video_id("https://m.youtube.com/watch?v=TestVideo01") == "TestVideo01"

    def test_invalid_url_raises(self) -> None:
        with pytest.raises(ValueError, match="不正な YouTube URL"):
            extract_video_id("https://example.com/watch?v=test")

    def test_no_video_id_raises(self) -> None:
        with pytest.raises(ValueError, match="不正な YouTube URL"):
            extract_video_id("https://www.youtube.com/watch")

    def test_invalid_video_id_format_raises(self) -> None:
        with pytest.raises(ValueError, match="不正な YouTube URL"):
            extract_video_id("https://www.youtube.com/watch?v=short")

    def test_shorts_url(self) -> None:
        assert extract_video_id("https://www.youtube.com/shorts/TestVideo01") == "TestVideo01"

    def test_live_url(self) -> None:
        assert extract_video_id("https://www.youtube.com/live/TestVideo01") == "TestVideo01"

    def test_live_url_with_query(self) -> None:
        assert (
            extract_video_id("https://www.youtube.com/live/TestVideo01?si=abc123")
            == "TestVideo01"
        )

    def test_embed_url(self) -> None:
        assert extract_video_id("https://www.youtube.com/embed/TestVideo01") == "TestVideo01"

    def test_legacy_v_url(self) -> None:
        assert extract_video_id("https://www.youtube.com/v/TestVideo01") == "TestVideo01"

    def test_youtube_com_without_subdomain(self) -> None:
        assert extract_video_id("https://youtube.com/watch?v=TestVideo01") == "TestVideo01"

    def test_invalid_video_id_in_shorts_path_raises(self) -> None:
        with pytest.raises(ValueError, match="不正な YouTube URL"):
            extract_video_id("https://www.youtube.com/shorts/short")

    def test_playlist_url_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="不正な YouTube URL"):
            extract_video_id("https://www.youtube.com/playlist?list=PLtest")

    def test_channel_handle_url_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="不正な YouTube URL"):
            extract_video_id("https://www.youtube.com/@SampleHandle")

    def test_video_id_with_underscore(self) -> None:
        """`_` を含む video_id (11 文字) が抽出できること."""
        assert extract_video_id("https://www.youtube.com/watch?v=Test_Video1") == "Test_Video1"

    def test_video_id_with_hyphen(self) -> None:
        """`-` を含む video_id (11 文字) が抽出できること."""
        assert extract_video_id("https://www.youtube.com/watch?v=Test-Video1") == "Test-Video1"

    def test_video_id_with_underscore_in_shorts(self) -> None:
        """`_` を含む video_id が shorts URL から抽出できること."""
        assert (
            extract_video_id("https://www.youtube.com/shorts/Test_Video1") == "Test_Video1"
        )

    def test_video_id_case_is_preserved(self) -> None:
        """video_id の大文字小文字が保持されること（パス判定が大文字小文字無視でも）."""
        assert (
            extract_video_id("https://www.youtube.com/SHORTS/MixedCase11") == "MixedCase11"
        )


class TestIsYoutubeVideoUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=TestVideo01",
            "https://youtube.com/watch?v=TestVideo01",
            "https://m.youtube.com/watch?v=TestVideo01",
            "https://youtu.be/TestVideo01",
            "https://www.youtube.com/shorts/TestVideo01",
            "https://www.youtube.com/live/TestVideo01",
            "https://www.youtube.com/live/TestVideo01?si=abc",
            "https://www.youtube.com/embed/TestVideo01",
            "https://www.youtube.com/v/TestVideo01",
        ],
    )
    def test_video_urls_are_recognized(self, url: str) -> None:
        assert is_youtube_video_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/watch?v=TestVideo01",
            "https://www.youtube.com/playlist?list=PLtest",
            "https://www.youtube.com/@SampleHandle",
            "https://www.youtube.com/c/somechannel",
            "https://www.youtube.com/channel/UCxxxx",
            "https://www.youtube.com/watch?v=short",
            "https://www.youtube.com/watch",
        ],
    )
    def test_non_video_urls_are_rejected(self, url: str) -> None:
        assert is_youtube_video_url(url) is False


class TestClassifyYoutubeUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=TestVideo01",
            "https://youtu.be/TestVideo01",
            "https://www.youtube.com/shorts/TestVideo01",
            "https://www.youtube.com/live/TestVideo01",
            "https://www.youtube.com/embed/TestVideo01",
            "https://www.youtube.com/v/TestVideo01",
            "https://m.youtube.com/watch?v=TestVideo01",
        ],
    )
    def test_valid_video_urls(self, url: str) -> None:
        assert classify_youtube_url(url) == "video"

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=short",
            "https://www.youtube.com/watch?v=",
            "https://www.youtube.com/shorts/short",
            "https://www.youtube.com/live/short",
            "https://www.youtube.com/embed/short",
            "https://www.youtube.com/v/short",
            "https://youtu.be/short",
        ],
    )
    def test_malformed_video_urls(self, url: str) -> None:
        assert classify_youtube_url(url) == "malformed"

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/watch?v=TestVideo01",
            "https://www.youtube.com/playlist?list=PLtest",
            "https://www.youtube.com/@SampleHandle",
            "https://www.youtube.com/c/somechannel",
            "https://www.youtube.com/channel/UCxxxx",
            "https://www.youtube.com/watch",
            "https://youtu.be/",
        ],
    )
    def test_not_youtube_video_urls(self, url: str) -> None:
        assert classify_youtube_url(url) == "not_youtube"


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
        "id": "TestVideo01",
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
        metadata = _make_metadata()
        snippets = _make_snippets()
        fetcher = _fake(metadata=metadata, snippets=snippets, language="ja")
        ingester = make_youtube_ingester(source_store, fetcher=fetcher, max_duration=14400)

        result = await ingester.ingest_video("https://www.youtube.com/watch?v=TestVideo01")

        assert result.placed == 1
        assert result.errors == 0

        source_store.place_file.assert_called_once()
        call_kwargs = source_store.place_file.call_args
        assert call_kwargs.kwargs["source_type"] == "youtube"
        assert call_kwargs.kwargs["rel_path"] == "youtube/UCtest123456789012345/TestVideo01.json"

        data = json.loads(call_kwargs.kwargs["data"].decode("utf-8"))
        assert data["video_id"] == "TestVideo01"
        assert data["transcript_source"] == "subtitle"
        assert len(data["snippets"]) == 3

        meta = call_kwargs.kwargs["metadata"]
        assert meta["source_type"] == "youtube"
        assert meta["video_id"] == "TestVideo01"

    @pytest.mark.asyncio()
    async def test_duration_exceeded_skipped(self, source_store: Any) -> None:
        """動画長上限超過時にスキップされることを検証する."""
        metadata = _make_metadata(duration=3600)
        fetcher = _fake(metadata=metadata)
        ingester = make_youtube_ingester(source_store, fetcher=fetcher, max_duration=60)

        result = await ingester.ingest_video("https://www.youtube.com/watch?v=TestVideo01")

        assert result.placed == 0
        assert result.skipped == 1

    @pytest.mark.asyncio()
    async def test_metadata_error_handled(self, source_store: Any) -> None:
        """メタデータ取得失敗時のエラーハンドリングを検証する."""
        fetcher = _fake(scenario="metadata_error")
        ingester = make_youtube_ingester(source_store, fetcher=fetcher)

        result = await ingester.ingest_video("https://www.youtube.com/watch?v=TestVideo01")

        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "metadata_fetch"
        assert detail["target"] == "TestVideo01"
        assert "メタデータ取得失敗" in detail["message"]

    @pytest.mark.asyncio()
    async def test_missing_channel_id_causes_error(self, source_store: Any) -> None:
        """channel_id が取得できない場合にエラーになることを検証する."""
        metadata = _make_metadata()
        metadata["channel_id"] = None
        fetcher = _fake(metadata=metadata)
        ingester = make_youtube_ingester(source_store, fetcher=fetcher)

        result = await ingester.ingest_video("https://www.youtube.com/watch?v=TestVideo01")

        assert result.placed == 0
        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "metadata_fetch"
        assert detail["target"] == "TestVideo01"
        assert "channel_id" in detail["message"]

    @pytest.mark.asyncio()
    async def test_whisper_model_recorded(self, source_store: Any) -> None:
        """Whisper 使用時に whisper_model が JSON に記録されることを検証する."""
        metadata = _make_metadata()
        snippets = _make_snippets()
        # no_subtitle で Whisper フォールバックを発動 → カスタム snippets を whisper でも返す
        fetcher = _fake(scenario="no_subtitle", metadata=metadata, snippets=snippets, language="ja")
        ingester = make_youtube_ingester(
            source_store, fetcher=fetcher, whisper_model="medium",
        )

        result = await ingester.ingest_video("https://www.youtube.com/watch?v=TestVideo01")

        assert result.placed == 1
        call_kwargs = source_store.place_file.call_args
        data = json.loads(call_kwargs.kwargs["data"].decode("utf-8"))
        assert data["transcript_source"] == "whisper"
        assert data["whisper_model"] == "medium"

    @pytest.mark.asyncio()
    async def test_overwritten_count_on_reingest(self, source_store: Any) -> None:
        """同一動画を 2 回取り込んだ時に overwritten がカウントされること（排他計上）を検証する.

        仕様: docs/specs/ingesters/common.md「placed と overwritten の排他関係」
        """
        metadata = _make_metadata()
        snippets = _make_snippets()
        fetcher = _fake(metadata=metadata, snippets=snippets, language="ja")
        ingester = make_youtube_ingester(source_store, fetcher=fetcher, max_duration=14400)

        # 1 回目: dest.exists() が False → placed=1, overwritten=0
        result1 = await ingester.ingest_video("https://www.youtube.com/watch?v=TestVideo01")
        assert result1.placed == 1
        assert result1.overwritten == 0

        # 2 回目: dest に実ファイルを作成して exists() が True になるようにする
        dest = source_store.root_dir / "youtube" / "UCtest123456789012345" / "TestVideo01.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("{}", encoding="utf-8")

        result2 = await ingester.ingest_video("https://www.youtube.com/watch?v=TestVideo01")
        # 排他計上: 既存ファイル上書き時は placed=0, overwritten=1
        assert result2.placed == 0
        assert result2.overwritten == 1

    @pytest.mark.asyncio()
    async def test_api_error_does_not_fallback_to_whisper(self, source_store: Any) -> None:
        """API エラー（IP ブロック等）では Whisper フォールバックせずエラーになることを検証する.

        FakeYoutubeFetcher の ip_blocked シナリオは fetch_subtitle で RequestBlocked を発生させる。
        ingester の _fetch_transcript 内例外フィルタリング（TranscriptsDisabled/NoTranscriptFound のみ
        Whisper フォールバック）が正しく動作することを検証する。
        """
        metadata = _make_metadata()
        fetcher = _fake(scenario="ip_blocked", metadata=metadata)
        ingester = make_youtube_ingester(source_store, fetcher=fetcher, max_duration=14400)

        result = await ingester.ingest_video("https://www.youtube.com/watch?v=TestVideo01")

        # Whisper フォールバックされず、エラーとして処理される
        assert result.errors == 1
        assert result.placed == 0

    @pytest.mark.asyncio()
    async def test_transcripts_disabled_triggers_whisper_fallback(self, source_store: Any) -> None:
        """TranscriptsDisabled では Whisper フォールバックが発動することを検証する."""
        metadata = _make_metadata()
        snippets = _make_snippets()
        # transcripts_disabled シナリオ + カスタム snippets で Whisper の戻り値を制御
        fetcher = _fake(
            scenario="transcripts_disabled",
            metadata=metadata,
            snippets=snippets,
            language="ja",
        )
        ingester = make_youtube_ingester(source_store, fetcher=fetcher, max_duration=14400)

        result = await ingester.ingest_video("https://www.youtube.com/watch?v=TestVideo01")

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
        entries = [
            {"id": "video_id_01", "url": "video_id_01"},
            {"id": "video_id_02", "url": "video_id_02"},
        ]
        fetcher = _fake(playlist_entries=entries)
        ingester = make_youtube_ingester(
            source_store, fetcher=fetcher, max_videos=3, request_interval=0.1,
        )

        # ingest_video は public method なので patch.object 可（Fetcher 経由ではない）
        single_result = IngestResult(placed=1)
        with patch.object(
            ingester,
            "ingest_video",
            new_callable=AsyncMock,
            return_value=single_result,
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
        entries = [
            {"id": "video_id_01", "url": "video_id_01"},
            {"id": "video_id_02", "url": "video_id_02"},
            {"id": "video_id_03", "url": "video_id_03"},
        ]
        fetcher = _fake(playlist_entries=entries)
        ingester = make_youtube_ingester(
            source_store, fetcher=fetcher, max_videos=3, request_interval=interval,
        )

        single_result = IngestResult(placed=1)
        with (
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

        entries = [
            {"id": "video_id_01", "url": "video_id_01"},
            {"id": "video_id_02", "url": "video_id_02"},
        ]
        fetcher = _fake(playlist_entries=entries)
        ingester = make_youtube_ingester(
            source_store, fetcher=fetcher, max_videos=2, request_interval=MIN_REQUEST_INTERVAL,
        )

        single_result = IngestResult(placed=1)
        jitter_below_min = MIN_REQUEST_INTERVAL * 0.01

        with (
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
        entries = [{"id": f"vid_{i:011d}", "url": f"vid_{i:011d}"} for i in range(10)]
        fetcher = _fake(playlist_entries=entries)
        ingester = make_youtube_ingester(
            source_store, fetcher=fetcher, max_videos=10, request_interval=0.1,
        )

        def _make_error_result() -> IngestResult:
            return IngestResult(
                errors=1,
                error_details=[{
                    "category": "metadata_fetch",
                    "target": "test",
                    "message": "test error",
                }],
            )

        with patch.object(
            ingester,
            "ingest_video",
            new_callable=AsyncMock,
            side_effect=lambda *args, **kwargs: _make_error_result(),
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
        fetcher = _fake(scenario="empty_playlist")
        ingester = make_youtube_ingester(source_store, fetcher=fetcher)

        result = await ingester.crawl_playlist(
            "https://www.youtube.com/playlist?list=PLtest123"
        )

        assert result.placed == 0
        assert result.errors == 0

    @pytest.mark.asyncio()
    async def test_playlist_expand_error_dict(self, source_store: Any) -> None:
        """プレイリスト展開失敗時の error_details dict 構造を検証する."""
        fetcher = _fake(scenario="playlist_expand_error")
        ingester = make_youtube_ingester(source_store, fetcher=fetcher)

        result = await ingester.crawl_playlist(
            "https://www.youtube.com/playlist?list=PLtest123"
        )

        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "metadata_fetch"
        assert detail["target"] == "PLtest123"
        assert "Fake playlist expand error" in detail["message"]

    @pytest.mark.asyncio()
    async def test_missing_video_id_in_entry_dict(self, source_store: Any) -> None:
        """entry に video_id がない場合の error_details dict."""
        entries = [{"id": "", "url": ""}]
        fetcher = _fake(playlist_entries=entries)
        ingester = make_youtube_ingester(source_store, fetcher=fetcher, max_videos=10)

        result = await ingester.crawl_playlist(
            "https://www.youtube.com/playlist?list=PLtest123"
        )

        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "metadata_fetch"
        assert detail["target"] == "entry_0"
        assert "video_id missing" in detail["message"]


class TestWhisperLifecycle:
    """Whisper モデルの遅延ロード/明示アンロード機構を検証する.

    仕様: docs/specs/ingesters/youtube.md「Whisper モデルライフサイクル」
    """

    @pytest.mark.asyncio()
    async def test_ingest_videos_unloads_whisper_once(self, source_store: Any) -> None:
        """ingest_videos の bulk 取り込み完了時に unload_whisper が 1 度だけ呼ばれることを検証する."""
        fetcher = _fake()
        fetcher.unload_whisper = MagicMock()  # type: ignore[method-assign]
        ingester = make_youtube_ingester(
            source_store, fetcher=fetcher, max_duration=14400,
        )

        single_result = IngestResult(placed=1)
        with patch.object(
            ingester,
            "ingest_video",
            new_callable=AsyncMock,
            return_value=single_result,
        ):
            results = await ingester.ingest_videos([
                "https://www.youtube.com/watch?v=TestVideo01",
                "https://www.youtube.com/watch?v=TestVideo02",
                "https://www.youtube.com/watch?v=TestVideo03",
            ])

        assert len(results) == 3
        assert all(r.placed == 1 for r in results)
        assert fetcher.unload_whisper.call_count == 1

    @pytest.mark.asyncio()
    async def test_ingest_videos_unloads_on_exception(self, source_store: Any) -> None:
        """ingest_videos の途中で例外が発生しても unload_whisper が呼ばれることを検証する."""
        fetcher = _fake()
        fetcher.unload_whisper = MagicMock()  # type: ignore[method-assign]
        ingester = make_youtube_ingester(
            source_store, fetcher=fetcher, max_duration=14400,
        )

        # 2 本目で例外、3 本目は per-item エラーに変換されて継続する。
        # AsyncMock の side_effect は Exception インスタンスを自動的に raise する。
        with patch.object(
            ingester,
            "ingest_video",
            new_callable=AsyncMock,
            side_effect=[
                IngestResult(placed=1),
                RuntimeError("boom"),
                IngestResult(placed=1),
            ],
        ):
            results = await ingester.ingest_videos([
                "https://www.youtube.com/watch?v=TestVideo01",
                "https://www.youtube.com/watch?v=TestVideo02",
                "https://www.youtube.com/watch?v=TestVideo03",
            ])

        assert len(results) == 3
        assert results[0].placed == 1
        assert results[1].errors == 1
        assert "boom" in results[1].error_details[0]["message"]
        assert results[2].placed == 1
        assert fetcher.unload_whisper.call_count == 1

    @pytest.mark.asyncio()
    async def test_ingest_videos_propagates_programming_errors(self, source_store: Any) -> None:
        """ingest_videos がプログラミングエラー（TypeError/AttributeError/ImportError）を per-item 変換せず伝播することを検証する.

        ingest_video 内部の「プログラミングエラーは伝播させる」設計と整合させ、
        バグをサイレントに成功扱いにしないことを保証する。
        """
        fetcher = _fake()
        fetcher.unload_whisper = MagicMock()  # type: ignore[method-assign]
        ingester = make_youtube_ingester(
            source_store, fetcher=fetcher, max_duration=14400,
        )

        with patch.object(
            ingester,
            "ingest_video",
            new_callable=AsyncMock,
            side_effect=TypeError("programming error"),
        ):
            with pytest.raises(TypeError, match="programming error"):
                await ingester.ingest_videos([
                    "https://www.youtube.com/watch?v=TestVideo01",
                ])

        # 例外伝播時も bulk 末尾の unload_whisper は呼ばれる（try/finally 配置）
        assert fetcher.unload_whisper.call_count == 1

    @pytest.mark.asyncio()
    async def test_crawl_playlist_unloads_whisper(self, source_store: Any) -> None:
        """crawl_playlist 完了時に unload_whisper が呼ばれることを検証する."""
        entries = [
            {"id": "video_id_01", "url": "video_id_01"},
            {"id": "video_id_02", "url": "video_id_02"},
        ]
        fetcher = _fake(playlist_entries=entries)
        fetcher.unload_whisper = MagicMock()  # type: ignore[method-assign]
        ingester = make_youtube_ingester(
            source_store, fetcher=fetcher, max_videos=3, request_interval=0.1,
        )

        single_result = IngestResult(placed=1)
        with patch.object(
            ingester,
            "ingest_video",
            new_callable=AsyncMock,
            return_value=single_result,
        ):
            await ingester.crawl_playlist(
                "https://www.youtube.com/playlist?list=PLtest123"
            )

        assert fetcher.unload_whisper.call_count == 1

    @pytest.mark.asyncio()
    async def test_crawl_playlist_unloads_on_expand_error(self, source_store: Any) -> None:
        """crawl_playlist の playlist 展開失敗時も unload_whisper が呼ばれることを検証する."""
        fetcher = _fake(scenario="playlist_expand_error")
        fetcher.unload_whisper = MagicMock()  # type: ignore[method-assign]
        ingester = make_youtube_ingester(source_store, fetcher=fetcher)

        result = await ingester.crawl_playlist(
            "https://www.youtube.com/playlist?list=PLtest123"
        )

        assert result.errors == 1
        assert fetcher.unload_whisper.call_count == 1

    def test_fake_unload_whisper_is_noop(self) -> None:
        """FakeYoutubeFetcher の unload_whisper が例外を出さないことを検証する."""
        fetcher = _fake()
        # 2 回呼んでも安全（no-op）
        fetcher.unload_whisper()
        fetcher.unload_whisper()

    def test_real_unload_whisper_is_idempotent(self) -> None:
        """RealYoutubeFetcher の unload_whisper が未ロード状態でも安全に呼べることを検証する."""
        from rag.pipeline.ingesters.youtube_fetcher import RealYoutubeFetcher

        fetcher = RealYoutubeFetcher()
        # _whisper_model_instance is None の状態で 2 回連続呼んでも例外なし
        fetcher.unload_whisper()
        fetcher.unload_whisper()
        assert fetcher._whisper_model_instance is None

    @pytest.mark.asyncio()
    async def test_crawl_playlist_unloads_on_invalid_url(self, source_store: Any) -> None:
        """crawl_playlist が不正 URL で ValueError を raise しても unload_whisper が呼ばれることを検証する."""
        fetcher = _fake()
        fetcher.unload_whisper = MagicMock()  # type: ignore[method-assign]
        ingester = make_youtube_ingester(source_store, fetcher=fetcher)

        with pytest.raises(ValueError, match="不正な YouTube プレイリスト URL"):
            await ingester.crawl_playlist("https://example.com/playlist?list=test")

        assert fetcher.unload_whisper.call_count == 1

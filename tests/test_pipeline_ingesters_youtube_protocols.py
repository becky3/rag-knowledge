"""YouTube Protocol 群（Real 実装 + factory）のテスト.

仕様: docs/specs/architecture.md
仕様: docs/specs/ingesters/bluesky.md（YouTube への委譲経路）
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from rag.pipeline.ingesters._common import IngestResult
from rag.pipeline.ingesters.youtube_protocols import (
    RealYoutubeClassifier,
    RealYoutubeDelegator,
    YoutubeClassifier,
    YoutubeDelegator,
    create_youtube_classifier,
    create_youtube_delegator,
)


class TestRealYoutubeClassifier:
    """RealYoutubeClassifier は SSoT 関数 (classify_youtube_url) と同一結果を返す."""

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "video"),
            ("https://youtu.be/dQw4w9WgXcQ", "video"),
            ("https://www.youtube.com/watch?v=", "malformed"),
            ("https://youtu.be/short", "malformed"),
            ("https://example.com/page", "not_youtube"),
            ("https://www.youtube.com/@channel", "not_youtube"),
        ],
    )
    def test_classify_matches_youtube_module_ssot(
        self, url: str, expected: str,
    ) -> None:
        classifier = RealYoutubeClassifier()
        assert classifier.classify(url) == expected


class TestRealYoutubeDelegator:
    @pytest.mark.asyncio
    async def test_ingest_video_delegates_to_ingester(self) -> None:
        ingester = AsyncMock()
        expected = IngestResult()
        ingester.ingest_video.return_value = expected
        delegator = RealYoutubeDelegator(ingester)

        result = await delegator.ingest_video(
            "https://youtu.be/test", playlist_id="PLtest",
        )

        assert result is expected
        ingester.ingest_video.assert_awaited_once_with(
            "https://youtu.be/test", playlist_id="PLtest",
        )

    @pytest.mark.asyncio
    async def test_ingest_video_default_playlist_id_is_none(self) -> None:
        ingester = AsyncMock()
        ingester.ingest_video.return_value = IngestResult()
        delegator = RealYoutubeDelegator(ingester)

        await delegator.ingest_video("https://youtu.be/test")

        ingester.ingest_video.assert_awaited_once_with(
            "https://youtu.be/test", playlist_id=None,
        )


class TestFactories:
    def test_create_youtube_classifier_returns_real(self) -> None:
        classifier = create_youtube_classifier()
        assert isinstance(classifier, RealYoutubeClassifier)

    def test_create_youtube_delegator_returns_real(self) -> None:
        ingester = AsyncMock()
        delegator = create_youtube_delegator(ingester)
        assert isinstance(delegator, RealYoutubeDelegator)


class TestProtocolContract:
    def test_protocols_are_importable(self) -> None:
        # smoke: 型として参照できる
        assert YoutubeClassifier is not None
        assert YoutubeDelegator is not None

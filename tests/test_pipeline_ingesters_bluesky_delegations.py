"""delegations.py のテスト（投稿内 URL の YouTube / site_ingest 委譲）.

仕様: docs/specs/ingesters/bluesky.md「投稿内 URL の自動取り込み」
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.pipeline.ingesters._common import IngestResult
from rag.pipeline.ingesters.bluesky.delegations import follow_urls
from rag.pipeline.ingesters.youtube_protocols import (
    RealYoutubeClassifier,
)
from rag.pipeline.site_ingest_runner import SiteIngestExecution
from rag.scrapy.bridge import BridgeResult


def _item_with_urls(
    urls: list[str], *, suppress: bool = False,
) -> dict[str, Any]:
    """指定 URL を含むフィードアイテム（facets 経由）を生成."""
    return {
        "post": {
            "record": {
                "facets": [
                    {"features": [{
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": u,
                    }]}
                    for u in urls
                ],
            },
        },
        "_suppress_youtube_reingest": suppress,
    }


def _make_execution(
    *, placed: int = 0, errors: int = 0,
    error_details: list[dict[str, Any]] | None = None,
    parse_errors: int = 0,
) -> SiteIngestExecution:
    """site_ingest 結果オブジェクトを生成."""
    bridge = BridgeResult()
    bridge.ingest.placed = placed
    bridge.ingest.errors = errors
    if error_details:
        bridge.ingest.error_details = list(error_details)
    bridge.parse_errors = parse_errors
    execution = SiteIngestExecution(bridge=bridge, scrapy_success=True)
    execution.crawl_result = None
    return execution


class TestFollowUrlsClassification:
    @pytest.mark.asyncio
    async def test_skips_bluesky_urls(self) -> None:
        runner = AsyncMock()
        runner.run_for_urls = AsyncMock(return_value=_make_execution())
        items = [_item_with_urls([
            "https://bsky.app/profile/x.bsky.social/post/r1",
        ])]
        stats = await follow_urls(
            items,
            classifier=RealYoutubeClassifier(),
            youtube_delegator=None,
            site_ingest_runner=runner,
            source_store=MagicMock(),
            settings=MagicMock(),
            youtube_request_interval=0.0,
        )
        assert stats["skipped"] == 1
        runner.run_for_urls.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_youtube_url_recorded_as_error(self) -> None:
        result = IngestResult()
        items = [_item_with_urls(["https://www.youtube.com/watch?v="])]
        stats = await follow_urls(
            items,
            classifier=RealYoutubeClassifier(),
            youtube_delegator=None,
            site_ingest_runner=AsyncMock(),
            source_store=MagicMock(),
            settings=MagicMock(),
            youtube_request_interval=0.0,
            result=result,
        )
        assert stats["errors"] == 1
        assert result.errors == 1
        assert result.error_details[0]["category"] == "delegation"


class TestFollowUrlsWebDelegation:
    @pytest.mark.asyncio
    async def test_web_url_passed_to_runner(self) -> None:
        runner = AsyncMock()
        runner.run_for_urls = AsyncMock(
            return_value=_make_execution(placed=1),
        )
        items = [_item_with_urls(["https://example.com/article"])]
        stats = await follow_urls(
            items,
            classifier=RealYoutubeClassifier(),
            youtube_delegator=None,
            site_ingest_runner=runner,
            source_store=MagicMock(),
            settings=MagicMock(),
            youtube_request_interval=0.0,
        )
        assert stats["web_placed"] == 1
        runner.run_for_urls.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_web_dedup_across_items(self) -> None:
        runner = AsyncMock()
        runner.run_for_urls = AsyncMock(
            return_value=_make_execution(placed=1),
        )
        items = [
            _item_with_urls(["https://example.com/dup"]),
            _item_with_urls(["https://example.com/dup"]),
        ]
        await follow_urls(
            items,
            classifier=RealYoutubeClassifier(),
            youtube_delegator=None,
            site_ingest_runner=runner,
            source_store=MagicMock(),
            settings=MagicMock(),
            youtube_request_interval=0.0,
        )
        # run_for_urls には 1 件だけ
        called_urls = runner.run_for_urls.await_args[0][0]
        assert called_urls == ["https://example.com/dup"]

    @pytest.mark.asyncio
    async def test_runner_failure_recorded(self) -> None:
        runner = AsyncMock()
        runner.run_for_urls = AsyncMock(
            side_effect=RuntimeError("scrapy failed"),
        )
        result = IngestResult()
        items = [_item_with_urls(["https://example.com/x"])]
        stats = await follow_urls(
            items,
            classifier=RealYoutubeClassifier(),
            youtube_delegator=None,
            site_ingest_runner=runner,
            source_store=MagicMock(),
            settings=MagicMock(),
            youtube_request_interval=0.0,
            result=result,
        )
        assert stats["errors"] >= 1
        assert any(
            d["category"] == "delegation"
            for d in result.error_details
        )


class TestFollowUrlsYoutubeDelegation:
    @pytest.mark.asyncio
    async def test_youtube_url_calls_delegator(self) -> None:
        delegator = AsyncMock()
        yt_result = IngestResult()
        yt_result.placed = 1
        delegator.ingest_video = AsyncMock(return_value=yt_result)
        runner = AsyncMock()
        runner.run_for_urls = AsyncMock(return_value=_make_execution())
        items = [_item_with_urls(["https://youtu.be/abcdEFG1234"])]
        stats = await follow_urls(
            items,
            classifier=RealYoutubeClassifier(),
            youtube_delegator=delegator,
            site_ingest_runner=runner,
            source_store=MagicMock(),
            settings=MagicMock(),
            youtube_request_interval=0.0,
        )
        assert stats["youtube_placed"] == 1
        delegator.ingest_video.assert_awaited_once_with("https://youtu.be/abcdEFG1234")

    @pytest.mark.asyncio
    async def test_youtube_skipped_when_delegator_none(self) -> None:
        runner = AsyncMock()
        runner.run_for_urls = AsyncMock(return_value=_make_execution())
        items = [_item_with_urls(["https://youtu.be/abcdEFG1234"])]
        stats = await follow_urls(
            items,
            classifier=RealYoutubeClassifier(),
            youtube_delegator=None,
            site_ingest_runner=runner,
            source_store=MagicMock(),
            settings=MagicMock(),
            youtube_request_interval=0.0,
        )
        assert stats["skipped"] >= 1
        assert stats["youtube_placed"] == 0

    @pytest.mark.asyncio
    async def test_suppressed_youtube_skipped_when_force_false(self) -> None:
        delegator = AsyncMock()
        runner = AsyncMock()
        runner.run_for_urls = AsyncMock(return_value=_make_execution())
        items = [_item_with_urls(
            ["https://youtu.be/abcdEFG1234"], suppress=True,
        )]
        stats = await follow_urls(
            items,
            classifier=RealYoutubeClassifier(),
            youtube_delegator=delegator,
            site_ingest_runner=runner,
            source_store=MagicMock(),
            settings=MagicMock(),
            youtube_request_interval=0.0,
            force_youtube_reingest=False,
        )
        delegator.ingest_video.assert_not_called()
        assert stats["skipped"] >= 1

    @pytest.mark.asyncio
    async def test_suppressed_youtube_taken_when_force_true(self) -> None:
        delegator = AsyncMock()
        yt_result = IngestResult()
        yt_result.placed = 1
        delegator.ingest_video = AsyncMock(return_value=yt_result)
        runner = AsyncMock()
        runner.run_for_urls = AsyncMock(return_value=_make_execution())
        items = [_item_with_urls(
            ["https://youtu.be/abcdEFG1234"], suppress=True,
        )]
        stats = await follow_urls(
            items,
            classifier=RealYoutubeClassifier(),
            youtube_delegator=delegator,
            site_ingest_runner=runner,
            source_store=MagicMock(),
            settings=MagicMock(),
            youtube_request_interval=0.0,
            force_youtube_reingest=True,
        )
        assert stats["youtube_placed"] == 1

    @pytest.mark.asyncio
    async def test_youtube_delegator_failure_recorded(self) -> None:
        delegator = AsyncMock()
        delegator.ingest_video = AsyncMock(
            side_effect=RuntimeError("yt failed"),
        )
        runner = AsyncMock()
        runner.run_for_urls = AsyncMock(return_value=_make_execution())
        result = IngestResult()
        items = [_item_with_urls(["https://youtu.be/abcdEFG1234"])]
        stats = await follow_urls(
            items,
            classifier=RealYoutubeClassifier(),
            youtube_delegator=delegator,
            site_ingest_runner=runner,
            source_store=MagicMock(),
            settings=MagicMock(),
            youtube_request_interval=0.0,
            result=result,
        )
        assert stats["errors"] >= 1
        assert any(
            "youtube delegation failed" in d.get("message", "")
            for d in result.error_details
        )

    @pytest.mark.asyncio
    async def test_dedup_across_suppress_and_unsuppress_safer_side(
        self,
    ) -> None:
        # 同一 URL が「抑制対象」と「抑制対象外」両方に存在 → 安全側で取り込み
        delegator = AsyncMock()
        yt_result = IngestResult()
        yt_result.placed = 1
        delegator.ingest_video = AsyncMock(return_value=yt_result)
        runner = AsyncMock()
        runner.run_for_urls = AsyncMock(return_value=_make_execution())
        items = [
            _item_with_urls(["https://youtu.be/abcdEFG1234"], suppress=True),
            _item_with_urls(["https://youtu.be/abcdEFG1234"], suppress=False),
        ]
        await follow_urls(
            items,
            classifier=RealYoutubeClassifier(),
            youtube_delegator=delegator,
            site_ingest_runner=runner,
            source_store=MagicMock(),
            settings=MagicMock(),
            youtube_request_interval=0.0,
            force_youtube_reingest=False,
        )
        # 1 回呼ばれる（安全側で suppress=False と扱われる）
        delegator.ingest_video.assert_awaited_once()


class TestFollowUrlsEmpty:
    @pytest.mark.asyncio
    async def test_no_items_returns_zero_stats(self) -> None:
        stats = await follow_urls(
            [],
            classifier=RealYoutubeClassifier(),
            youtube_delegator=None,
            site_ingest_runner=AsyncMock(),
            source_store=MagicMock(),
            settings=MagicMock(),
            youtube_request_interval=0.0,
        )
        assert stats == {
            "web_placed": 0, "youtube_placed": 0, "skipped": 0, "errors": 0,
        }

    @pytest.mark.asyncio
    async def test_no_urls_in_items_returns_zero(self) -> None:
        stats = await follow_urls(
            [_item_with_urls([])],
            classifier=RealYoutubeClassifier(),
            youtube_delegator=None,
            site_ingest_runner=AsyncMock(),
            source_store=MagicMock(),
            settings=MagicMock(),
            youtube_request_interval=0.0,
        )
        assert stats == {
            "web_placed": 0, "youtube_placed": 0, "skipped": 0, "errors": 0,
        }

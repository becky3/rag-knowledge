"""BlueskyIngester facade のテスト（オーケストレーション + バリデーション）.

仕様: docs/specs/ingesters/bluesky.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rag.pipeline.ingesters._fake.bluesky import (
    FakeBlueskyFetcher,
    FakeBlueskyMediaDownloader,
)
from rag.pipeline.ingesters.bluesky import (
    MAX_POSTS_HARD_LIMIT,
)
from rag.pipeline.ingesters.bluesky._facade import _validate_max_posts
from rag.store.source_store import SourceStore

from factories import make_bluesky_ingester


@pytest.fixture()
def source_store(tmp_path: Path) -> SourceStore:
    store = SourceStore(tmp_path / "source_store")
    store.initialize()
    return store


@pytest.fixture()
def fixture_dir() -> Path:
    return (
        Path(__file__).parent.parent
        / "src" / "rag" / "pipeline" / "ingesters" / "_fake" / "bluesky" / "data"
    )


def _make_feed_item(
    did: str = "did:plc:abc123",
    handle: str = "alice.bsky.social",
    rkey: str = "xyz789",
    text: str = "Sample post text",
    created_at: str = "2026-01-15T09:00:00Z",
    *,
    is_repost: bool = False,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "post": {
            "uri": f"at://{did}/app.bsky.feed.post/{rkey}",
            "cid": "test-cid",
            "author": {"did": did, "handle": handle},
            "record": {"text": text, "createdAt": created_at},
        },
    }
    item["reason"] = (
        {
            "$type": "app.bsky.feed.defs#reasonRepost",
            "by": {"did": "did:plc:r", "handle": "r.bsky.social"},
            "indexedAt": "2026-01-16T10:00:00Z",
        }
        if is_repost
        else None
    )
    return item


class TestValidateMaxPosts:
    def test_valid_value(self) -> None:
        assert _validate_max_posts(50) == 50

    def test_clamps_to_hard_limit(self) -> None:
        assert _validate_max_posts(MAX_POSTS_HARD_LIMIT + 100) == MAX_POSTS_HARD_LIMIT

    def test_at_hard_limit(self) -> None:
        assert _validate_max_posts(MAX_POSTS_HARD_LIMIT) == MAX_POSTS_HARD_LIMIT

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="1 以上"):
            _validate_max_posts(0)

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="1 以上"):
            _validate_max_posts(-5)

    def test_non_integer_rejected(self) -> None:
        with pytest.raises(TypeError, match="整数"):
            _validate_max_posts("100")

    def test_bool_rejected(self) -> None:
        with pytest.raises(TypeError, match="整数"):
            _validate_max_posts(True)

    def test_float_rejected(self) -> None:
        with pytest.raises(TypeError, match="整数"):
            _validate_max_posts(100.0)


class TestCrawlBlueskyValidation:
    @pytest.mark.asyncio
    async def test_empty_handle_raises(self, source_store: SourceStore) -> None:
        ingester = make_bluesky_ingester(source_store)
        with pytest.raises(ValueError, match="handle が空"):
            await ingester.crawl_bluesky("")

    @pytest.mark.asyncio
    async def test_did_handle_rejected(self, source_store: SourceStore) -> None:
        ingester = make_bluesky_ingester(source_store)
        with pytest.raises(ValueError, match="DID 形式"):
            await ingester.crawl_bluesky("did:plc:abc123")


class TestCrawlBlueskyFlow:
    @pytest.mark.asyncio
    async def test_places_posts_from_fixture(
        self, source_store: SourceStore, fixture_dir: Path,
    ) -> None:
        ingester = make_bluesky_ingester(
            source_store,
            fetcher=FakeBlueskyFetcher(fixture_dir, scenario="happy"),
            media_downloader=FakeBlueskyMediaDownloader(),
            max_posts=10,
        )
        result, items = await ingester.crawl_bluesky("test.bsky.social")
        # fixture: 4 posts (1通常 + 1リプライ + 1リポスト + 1外部リンク)
        # include_reposts=True デフォルトなのでリポストも配置される
        assert result.placed == 4
        assert len(items) == 4

    @pytest.mark.asyncio
    async def test_skips_repost_when_include_reposts_false(
        self, source_store: SourceStore, fixture_dir: Path,
    ) -> None:
        ingester = make_bluesky_ingester(
            source_store,
            fetcher=FakeBlueskyFetcher(fixture_dir, scenario="happy"),
            media_downloader=FakeBlueskyMediaDownloader(),
            include_reposts=False,
            max_posts=10,
        )
        result, items = await ingester.crawl_bluesky("test.bsky.social")
        # リポスト 1 件除外 → 3 件配置
        assert result.placed == 3
        assert len(items) == 3

    @pytest.mark.asyncio
    async def test_max_posts_limits_iteration(
        self, source_store: SourceStore, fixture_dir: Path,
    ) -> None:
        ingester = make_bluesky_ingester(
            source_store,
            fetcher=FakeBlueskyFetcher(fixture_dir, scenario="happy"),
            media_downloader=FakeBlueskyMediaDownloader(),
            max_posts=2,
        )
        result, _ = await ingester.crawl_bluesky("test.bsky.social", max_posts=2)
        assert result.placed == 2

    @pytest.mark.asyncio
    async def test_progress_callback_called_per_post(
        self, source_store: SourceStore, fixture_dir: Path,
    ) -> None:
        ingester = make_bluesky_ingester(
            source_store,
            fetcher=FakeBlueskyFetcher(fixture_dir, scenario="happy"),
            media_downloader=FakeBlueskyMediaDownloader(),
            max_posts=10,
        )
        calls: list[tuple[int, int, str]] = []

        def cb(processed: int, total: int, current: str) -> None:
            calls.append((processed, total, current))

        await ingester.crawl_bluesky("test.bsky.social", progress_callback=cb)
        assert len(calls) == 4
        assert all(call[1] == 10 for call in calls)  # total = max_posts

    @pytest.mark.asyncio
    async def test_force_overwrites(
        self, source_store: SourceStore, fixture_dir: Path,
    ) -> None:
        # 1 回目（force=False）
        ingester = make_bluesky_ingester(
            source_store,
            fetcher=FakeBlueskyFetcher(fixture_dir, scenario="happy"),
            media_downloader=FakeBlueskyMediaDownloader(),
            max_posts=10,
        )
        await ingester.crawl_bluesky("test.bsky.social")
        # 2 回目（force=True）
        result, _ = await ingester.crawl_bluesky("test.bsky.social", force=True)
        assert result.overwritten == 4
        assert result.placed == 0

    @pytest.mark.asyncio
    async def test_circuit_breaker_propagates(
        self, source_store: SourceStore, fixture_dir: Path,
    ) -> None:
        ingester = make_bluesky_ingester(
            source_store,
            fetcher=FakeBlueskyFetcher(fixture_dir, scenario="circuit_breaker"),
            media_downloader=FakeBlueskyMediaDownloader(),
            max_posts=10,
        )
        with pytest.raises(RuntimeError, match="circuit breaker"):
            await ingester.crawl_bluesky("test.bsky.social")


class TestIngestPosts:
    @pytest.mark.asyncio
    async def test_invalid_url_recorded_as_error(
        self, source_store: SourceStore, fixture_dir: Path,
    ) -> None:
        ingester = make_bluesky_ingester(
            source_store,
            fetcher=FakeBlueskyFetcher(fixture_dir),
            media_downloader=FakeBlueskyMediaDownloader(),
        )
        result, items = await ingester.ingest_posts(["https://example.com/x"])
        assert result.errors == 1
        assert items == []

    @pytest.mark.asyncio
    async def test_empty_url_list_returns_empty(
        self, source_store: SourceStore,
    ) -> None:
        ingester = make_bluesky_ingester(source_store)
        result, items = await ingester.ingest_posts([])
        assert result.placed == 0
        assert items == []

    @pytest.mark.asyncio
    async def test_resolve_handle_failure_recorded_as_error(
        self, source_store: SourceStore, fixture_dir: Path,
    ) -> None:
        # not_found_handle シナリオで resolve が 404
        fetcher = FakeBlueskyFetcher(fixture_dir, scenario="not_found_handle")
        ingester = make_bluesky_ingester(
            source_store,
            fetcher=fetcher,
            media_downloader=FakeBlueskyMediaDownloader(),
        )
        result, _ = await ingester.ingest_posts(
            ["https://bsky.app/profile/missing.bsky.social/post/r1"],
        )
        assert result.errors == 1

    @pytest.mark.asyncio
    async def test_not_found_post_recorded_as_error(
        self, source_store: SourceStore, fixture_dir: Path,
    ) -> None:
        # resolve は通る、posts が空
        fetcher = FakeBlueskyFetcher(
            fixture_dir,
            did_map={"alice.bsky.social": "did:plc:alice"},
            scenario="not_found_post",
        )
        ingester = make_bluesky_ingester(
            source_store,
            fetcher=fetcher,
            media_downloader=FakeBlueskyMediaDownloader(),
        )
        result, items = await ingester.ingest_posts(
            ["https://bsky.app/profile/alice.bsky.social/post/r1"],
        )
        assert result.errors == 1
        assert items == []

    @pytest.mark.asyncio
    async def test_successful_ingest_marks_no_suppress(
        self, source_store: SourceStore, fixture_dir: Path,
    ) -> None:
        # alice の resolve は did_map で許可、posts は fixture 経由
        fetcher = FakeBlueskyFetcher(
            fixture_dir,
            did_map={"test.bsky.social": "did:plc:test01234567"},
        )
        ingester = make_bluesky_ingester(
            source_store,
            fetcher=fetcher,
            media_downloader=FakeBlueskyMediaDownloader(),
        )
        result, items = await ingester.ingest_posts(
            ["https://bsky.app/profile/test.bsky.social/post/testrkey00001"],
        )
        # 1 投稿 placed
        assert result.placed == 1
        # ピンポイント修復用途: _suppress_youtube_reingest=False
        assert items[0]["_suppress_youtube_reingest"] is False

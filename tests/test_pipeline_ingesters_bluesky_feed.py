"""feed_fetcher.py のテスト（pagination + DID 解決）.

仕様: docs/specs/ingesters/bluesky.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rag.pipeline.ingesters._fake.bluesky import FakeBlueskyFetcher
from rag.pipeline.ingesters.bluesky.feed_fetcher import (
    fetch_posts_by_uris,
    iterate_author_feed,
    resolve_handles_to_dids,
)


@pytest.fixture()
def fixture_dir() -> Path:
    return (
        Path(__file__).parent.parent
        / "src" / "rag" / "pipeline" / "ingesters" / "_fake" / "bluesky" / "data"
    )


def _make_fetcher(
    fixture_dir: Path, **overrides: Any,
) -> FakeBlueskyFetcher:
    return FakeBlueskyFetcher(fixture_dir=fixture_dir, **overrides)


class TestIterateAuthorFeed:
    @pytest.mark.asyncio
    async def test_yields_each_feed_item(self, fixture_dir: Path) -> None:
        fetcher = _make_fetcher(fixture_dir)
        items = []
        async for item in iterate_author_feed(
            fetcher, "test.bsky.social", page_size=100, max_posts=10,
        ):
            items.append(item)
        # fixture には 4 アイテム
        assert len(items) == 4

    @pytest.mark.asyncio
    async def test_respects_max_posts_limit(self, fixture_dir: Path) -> None:
        fetcher = _make_fetcher(fixture_dir)
        count = 0
        async for _ in iterate_author_feed(
            fetcher, "test.bsky.social", page_size=100, max_posts=2,
        ):
            count += 1
        assert count == 2

    @pytest.mark.asyncio
    async def test_stops_on_empty_feed(self, fixture_dir: Path) -> None:
        fetcher = _make_fetcher(fixture_dir, feed={"feed": [], "cursor": None})
        items = [
            item async for item in iterate_author_feed(
                fetcher, "test.bsky.social", page_size=100, max_posts=10,
            )
        ]
        assert items == []

    @pytest.mark.asyncio
    async def test_stops_on_no_cursor(self, fixture_dir: Path) -> None:
        # fixture は cursor=None のため 1 ページで停止する
        fetcher = _make_fetcher(fixture_dir)
        items = [
            item async for item in iterate_author_feed(
                fetcher, "test.bsky.social", page_size=100, max_posts=100,
            )
        ]
        assert len(items) == 4  # fixture の全件（1 ページ完了）

    @pytest.mark.asyncio
    async def test_circuit_breaker_propagates(self, fixture_dir: Path) -> None:
        fetcher = _make_fetcher(fixture_dir, scenario="circuit_breaker")
        with pytest.raises(RuntimeError, match="circuit breaker"):
            async for _ in iterate_author_feed(
                fetcher, "test.bsky.social", page_size=100, max_posts=10,
            ):
                pass


class TestFetchPostsByUris:
    @pytest.mark.asyncio
    async def test_returns_posts_list(self, fixture_dir: Path) -> None:
        fetcher = _make_fetcher(fixture_dir)
        posts = await fetch_posts_by_uris(
            fetcher, ["at://did:plc:test01234567/app.bsky.feed.post/testrkey00001"],
        )
        assert len(posts) == 1
        assert posts[0]["author"]["handle"] == "test.bsky.social"

    @pytest.mark.asyncio
    async def test_empty_uris_returns_empty(self, fixture_dir: Path) -> None:
        fetcher = _make_fetcher(fixture_dir)
        posts = await fetch_posts_by_uris(fetcher, [])
        assert posts == []

    @pytest.mark.asyncio
    async def test_not_found_post_returns_empty(self, fixture_dir: Path) -> None:
        fetcher = _make_fetcher(fixture_dir, scenario="not_found_post")
        posts = await fetch_posts_by_uris(
            fetcher, ["at://did:plc:test01234567/app.bsky.feed.post/missing"],
        )
        assert posts == []


class TestResolveHandlesToDids:
    @pytest.mark.asyncio
    async def test_resolves_handles(self, fixture_dir: Path) -> None:
        fetcher = _make_fetcher(
            fixture_dir,
            did_map={
                "alice.bsky.social": "did:plc:alice",
                "bob.bsky.social": "did:plc:bob",
            },
        )
        result = await resolve_handles_to_dids(
            fetcher, ["alice.bsky.social", "bob.bsky.social"],
        )
        assert result == {
            "alice.bsky.social": "did:plc:alice",
            "bob.bsky.social": "did:plc:bob",
        }

    @pytest.mark.asyncio
    async def test_dedup_handles(self, fixture_dir: Path) -> None:
        # 重複した handle は 1 回しか resolve されない（同じキーの dict 結果）
        fetcher = _make_fetcher(
            fixture_dir, did_map={"a.bsky.social": "did:plc:a"},
        )
        result = await resolve_handles_to_dids(
            fetcher, ["a.bsky.social", "a.bsky.social"],
        )
        assert result == {"a.bsky.social": "did:plc:a"}

    @pytest.mark.asyncio
    async def test_resolve_failure_is_skipped(self, fixture_dir: Path) -> None:
        fetcher = _make_fetcher(fixture_dir, scenario="not_found_handle")
        result = await resolve_handles_to_dids(fetcher, ["missing.bsky.social"])
        assert result == {}

    @pytest.mark.asyncio
    async def test_empty_did_skipped(self, fixture_dir: Path) -> None:
        # did が空文字列の場合は dict に含まれない
        fetcher = _make_fetcher(
            fixture_dir, did_map={"empty.bsky.social": ""},
        )
        result = await resolve_handles_to_dids(fetcher, ["empty.bsky.social"])
        # did_map に空文字列を入れると Fake の did_map 経由で dict は handle なし扱い
        # (空 did は warning 出力 + 結果に含まれない)
        assert result == {}

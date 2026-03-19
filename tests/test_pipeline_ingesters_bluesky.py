"""BlueSky インジェスターのテスト.

仕様: docs/specs/ingesters/bluesky.md

テスト方針:
- max_posts バリデーション（型チェック、0/負数拒否、クランプ）
- crawl_bluesky のフロー（ページネーション、重複検出、リポストフィルタ）
- .meta サイドカーファイルの生成
- ファイル構造の検証
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.pipeline.ingesters.bluesky import (
    MAX_POSTS_HARD_LIMIT,
    BlueskyIngester,
    _escape_did,
    _make_title,
    _validate_max_posts,
)
from rag.store.source_store import SourceStore


@pytest.fixture()
def source_store(tmp_path: Path) -> SourceStore:
    """テスト用 SourceStore を生成する."""
    store = SourceStore(tmp_path / "source_store")
    store.initialize()
    return store


def _make_feed_item(
    did: str = "did:plc:abc123",
    handle: str = "alice.bsky.social",
    rkey: str = "xyz789",
    text: str = "Sample post text",
    created_at: str = "2026-01-15T09:00:00Z",
    *,
    is_repost: bool = False,
    embed: dict | None = None,
    reply: dict | None = None,
) -> dict:
    """テスト用フィードアイテムを生成する."""
    record: dict = {
        "text": text,
        "createdAt": created_at,
    }
    if embed:
        record["embed"] = embed
    if reply:
        record["reply"] = reply

    item: dict = {
        "post": {
            "uri": f"at://{did}/app.bsky.feed.post/{rkey}",
            "cid": "test-cid",
            "author": {
                "did": did,
                "handle": handle,
                "displayName": "Alice",
            },
            "record": record,
        },
    }
    if is_repost:
        item["reason"] = {
            "$type": "app.bsky.feed.defs#reasonRepost",
            "by": {"did": "did:plc:reposter", "handle": "bob.bsky.social"},
            "indexedAt": "2026-01-16T10:00:00Z",
        }
    else:
        item["reason"] = None

    return item


def _make_mock_client(pages: list[dict]) -> AsyncMock:
    """ページネーション対応のモック ConstrainedClient を生成する."""
    client = AsyncMock()
    responses = []
    for page_data in pages:
        resp = MagicMock()
        resp.json.return_value = page_data
        responses.append(resp)
    client.get = AsyncMock(side_effect=responses)
    return client


class TestValidateMaxPosts:
    """_validate_max_posts のテスト."""

    def test_valid_value(self) -> None:
        """有効な値がそのまま返ること."""
        assert _validate_max_posts(100) == 100

    def test_clamp_above_limit(self) -> None:
        """上限超過がクランプされること."""
        assert _validate_max_posts(2000) == MAX_POSTS_HARD_LIMIT

    def test_reject_zero(self) -> None:
        """0 が拒否されること."""
        with pytest.raises(ValueError, match="1 以上"):
            _validate_max_posts(0)

    def test_reject_negative(self) -> None:
        """負数が拒否されること."""
        with pytest.raises(ValueError, match="1 以上"):
            _validate_max_posts(-1)

    def test_reject_bool(self) -> None:
        """bool が拒否されること."""
        with pytest.raises(TypeError, match="整数"):
            _validate_max_posts(True)

    def test_reject_float(self) -> None:
        """float が拒否されること."""
        with pytest.raises(TypeError, match="整数"):
            _validate_max_posts(1.5)

    def test_reject_string(self) -> None:
        """文字列が拒否されること."""
        with pytest.raises(TypeError, match="整数"):
            _validate_max_posts("100")


class TestEscapeDid:
    """_escape_did のテスト."""

    def test_escape_colons(self) -> None:
        """コロンが全角に置換されること."""
        assert _escape_did("did:plc:abc123") == "did\uff1aplc\uff1aabc123"


class TestMakeTitle:
    """_make_title のテスト."""

    def test_short_text(self) -> None:
        """50 文字以下のテキストがそのまま返ること."""
        assert _make_title("short") == "short"

    def test_long_text_truncated(self) -> None:
        """50 文字超のテキストが切り詰められること."""
        long_text = "a" * 60
        result = _make_title(long_text)
        assert result == "a" * 50 + "..."
        assert len(result) == 53


@pytest.mark.asyncio()
class TestCrawlBluesky:
    """crawl_bluesky のテスト."""

    async def test_empty_handle_raises(
        self, source_store: SourceStore
    ) -> None:
        """空のハンドルでエラーになること."""
        ingester = BlueskyIngester(source_store)
        with pytest.raises(ValueError, match="空"):
            await ingester.crawl_bluesky("", client=AsyncMock())

    async def test_did_handle_raises(
        self, source_store: SourceStore
    ) -> None:
        """DID 形式のハンドルでエラーになること."""
        ingester = BlueskyIngester(source_store)
        with pytest.raises(ValueError, match="DID"):
            await ingester.crawl_bluesky("did:plc:abc", client=AsyncMock())

    async def test_no_client_raises(
        self, source_store: SourceStore
    ) -> None:
        """client 未指定でエラーになること."""
        ingester = BlueskyIngester(source_store)
        with pytest.raises(ValueError, match="client"):
            await ingester.crawl_bluesky("alice.bsky.social")

    async def test_basic_crawl(self, source_store: SourceStore) -> None:
        """基本的なクロールが正常に動作すること."""
        item = _make_feed_item()
        client = _make_mock_client([{"feed": [item]}])

        ingester = BlueskyIngester(source_store, max_posts=3)
        result = await ingester.crawl_bluesky(
            "alice.bsky.social", client=client
        )

        assert result.placed == 1
        assert result.errors == 0

        # ファイル構造の検証
        escaped = _escape_did("did:plc:abc123")
        expected_path = (
            source_store.root_dir / "bluesky" / escaped / "2026" / "01" / "xyz789.json"
        )
        assert expected_path.exists()

        # JSON 内容の検証
        data = json.loads(expected_path.read_text(encoding="utf-8"))
        assert data["post"]["uri"] == "at://did:plc:abc123/app.bsky.feed.post/xyz789"

    async def test_dedup_skips_existing(
        self, source_store: SourceStore
    ) -> None:
        """既存ファイルがスキップされること."""
        item = _make_feed_item()
        client = _make_mock_client([
            {"feed": [item]},
        ])

        ingester = BlueskyIngester(source_store, max_posts=3)
        # 1 回目
        await ingester.crawl_bluesky("alice.bsky.social", client=client)

        # 2 回目（同じアイテム）
        client2 = _make_mock_client([{"feed": [item]}])
        result = await ingester.crawl_bluesky(
            "alice.bsky.social", client=client2
        )

        assert result.skipped == 1
        assert result.placed == 0

    async def test_repost_excluded(self, source_store: SourceStore) -> None:
        """リポストが include_reposts=false で除外されること."""
        repost_item = _make_feed_item(rkey="repost1", is_repost=True)
        normal_item = _make_feed_item(rkey="normal1")
        client = _make_mock_client([
            {"feed": [repost_item, normal_item]},
        ])

        ingester = BlueskyIngester(source_store, max_posts=10)
        result = await ingester.crawl_bluesky(
            "alice.bsky.social",
            include_reposts=False,
            client=client,
        )

        assert result.placed == 1  # リポストが除外され、通常投稿のみ

    async def test_repost_included(self, source_store: SourceStore) -> None:
        """リポストが include_reposts=true で含まれること."""
        repost_item = _make_feed_item(rkey="repost1", is_repost=True)
        normal_item = _make_feed_item(rkey="normal1")
        client = _make_mock_client([
            {"feed": [repost_item, normal_item]},
        ])

        ingester = BlueskyIngester(source_store, max_posts=10)
        result = await ingester.crawl_bluesky(
            "alice.bsky.social",
            include_reposts=True,
            client=client,
        )

        assert result.placed == 2

    async def test_meta_fields(self, source_store: SourceStore) -> None:
        """.meta サイドカーファイルのフィールドが正しいこと."""
        item = _make_feed_item(
            text="Sample post text",
            embed={"$type": "app.bsky.embed.images"},
        )
        item["post"]["record"]["reply"] = {"parent": {}}
        client = _make_mock_client([{"feed": [item]}])

        ingester = BlueskyIngester(source_store, max_posts=3)
        await ingester.crawl_bluesky("alice.bsky.social", client=client)

        # .meta ファイルの検証
        escaped = _escape_did("did:plc:abc123")
        meta_path = (
            source_store.root_dir
            / "bluesky"
            / escaped
            / "2026"
            / "01"
            / "xyz789.json.meta"
        )
        assert meta_path.exists()

        import yaml

        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        assert meta["source_type"] == "bluesky"
        assert meta["handle"] == "alice.bsky.social"
        assert meta["did"] == "did:plc:abc123"
        assert meta["rkey"] == "xyz789"
        assert meta["has_images"] is True
        assert meta["is_reply"] is True
        assert meta["is_repost"] is False

    async def test_empty_feed(self, source_store: SourceStore) -> None:
        """空のフィードで正常終了すること."""
        client = _make_mock_client([{"feed": []}])

        ingester = BlueskyIngester(source_store, max_posts=3)
        result = await ingester.crawl_bluesky(
            "alice.bsky.social", client=client
        )

        assert result.placed == 0
        assert result.errors == 0

    async def test_max_posts_limit(self, source_store: SourceStore) -> None:
        """max_posts で取得数が制限されること."""
        items = [
            _make_feed_item(rkey=f"post{i}")
            for i in range(5)
        ]
        client = _make_mock_client([{"feed": items}])

        ingester = BlueskyIngester(source_store)
        result = await ingester.crawl_bluesky(
            "alice.bsky.social",
            max_posts=2,
            client=client,
        )

        assert result.placed == 2

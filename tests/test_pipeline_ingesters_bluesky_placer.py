"""post_placer.py のテスト（純関数 + place_post）.

仕様: docs/specs/ingesters/bluesky.md
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rag.pipeline.ingesters._common import IngestResult
from rag.pipeline.ingesters._fake.bluesky import FakeBlueskyMediaDownloader
from rag.pipeline.ingesters.bluesky.post_placer import (
    _ext_from_content_type,
    _extract_link_card,
    _extract_media_urls,
    _escape_did,
    _make_title,
    place_post,
)
from rag.store.source_store import SourceStore


@pytest.fixture()
def source_store(tmp_path: Path) -> SourceStore:
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
    embed: dict[str, Any] | None = None,
    reply: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {"text": text, "createdAt": created_at}
    if embed:
        record["embed"] = embed
    if reply:
        record["reply"] = reply
    item: dict[str, Any] = {
        "post": {
            "uri": f"at://{did}/app.bsky.feed.post/{rkey}",
            "cid": "test-cid",
            "author": {"did": did, "handle": handle, "displayName": "Alice"},
            "record": record,
        },
    }
    item["reason"] = (
        {
            "$type": "app.bsky.feed.defs#reasonRepost",
            "by": {"did": "did:plc:reposter", "handle": "bob.bsky.social"},
            "indexedAt": "2026-01-16T10:00:00Z",
        }
        if is_repost
        else None
    )
    return item


class TestEscapeDid:
    def test_replaces_colons_with_fullwidth(self) -> None:
        assert _escape_did("did:plc:abc123") == "did：plc：abc123"

    def test_no_colons_passthrough(self) -> None:
        assert _escape_did("simple") == "simple"


class TestMakeTitle:
    def test_short_text_returned_as_is(self) -> None:
        assert _make_title("short") == "short"

    def test_50_chars_no_truncation(self) -> None:
        text = "a" * 50
        assert _make_title(text) == text

    def test_51_chars_truncated_with_ellipsis(self) -> None:
        text = "a" * 51
        assert _make_title(text) == "a" * 50 + "..."


class TestExtFromContentType:
    @pytest.mark.parametrize(
        "ct,expected",
        [
            ("image/webp", ".webp"),
            ("image/jpeg", ".jpg"),
            ("image/png", ".png"),
            ("image/gif", ".gif"),
            ("image/avif", ".avif"),
            ("video/mp2t", ".ts"),
            ("image/webp; charset=utf-8", ".webp"),
            ("IMAGE/PNG", ".png"),
        ],
    )
    def test_known_content_types(self, ct: str, expected: str) -> None:
        assert _ext_from_content_type(ct) == expected

    def test_unknown_falls_back_to_webp(self) -> None:
        assert _ext_from_content_type("application/octet-stream") == ".webp"


class TestExtractLinkCard:
    def test_extracts_full_card(self) -> None:
        result = _extract_link_card({
            "uri": "https://x.test",
            "title": "Title",
            "description": "Desc",
        })
        assert result == {
            "uri": "https://x.test",
            "title": "Title",
            "description": "Desc",
        }

    def test_partial_fields_ok(self) -> None:
        result = _extract_link_card({"uri": "https://x.test"})
        assert result == {"uri": "https://x.test"}

    def test_empty_returns_none(self) -> None:
        assert _extract_link_card({}) is None
        assert _extract_link_card("not a dict") is None
        assert _extract_link_card(None) is None


class TestExtractMediaUrls:
    def test_no_embed(self) -> None:
        item = {"post": {"record": {}}}
        assert _extract_media_urls(item) == ([], None)

    def test_extracts_image_urls(self) -> None:
        item = {
            "post": {
                "embed": {
                    "$type": "app.bsky.embed.images#view",
                    "images": [
                        {"fullsize": "https://cdn/x1.jpg"},
                        {"fullsize": "https://cdn/x2.jpg"},
                    ],
                },
            },
        }
        urls, playlist = _extract_media_urls(item)
        assert urls == ["https://cdn/x1.jpg", "https://cdn/x2.jpg"]
        assert playlist is None

    def test_extracts_playlist_url(self) -> None:
        item = {
            "post": {
                "embed": {
                    "$type": "app.bsky.embed.video#view",
                    "playlist": "https://video/p.m3u8",
                },
            },
        }
        urls, playlist = _extract_media_urls(item)
        assert urls == []
        assert playlist == "https://video/p.m3u8"

    def test_record_with_media_view(self) -> None:
        item = {
            "post": {
                "embed": {
                    "$type": "app.bsky.embed.recordWithMedia#view",
                    "media": {
                        "$type": "app.bsky.embed.images#view",
                        "images": [{"fullsize": "https://cdn/m.jpg"}],
                        "playlist": None,
                    },
                },
            },
        }
        urls, playlist = _extract_media_urls(item)
        assert urls == ["https://cdn/m.jpg"]
        assert playlist is None


class TestPlacePost:
    @pytest.mark.asyncio
    async def test_places_new_post_with_meta(
        self, source_store: SourceStore,
    ) -> None:
        item = _make_feed_item()
        result = IngestResult()
        outcome = await place_post(
            item,
            is_repost=False,
            force=False,
            store=source_store,
            media_downloader=FakeBlueskyMediaDownloader(),
            result=result,
        )
        assert outcome == (True, False)
        assert result.placed == 1
        assert result.overwritten == 0
        # ファイルが配置された
        json_path = (
            source_store.root_dir
            / "bluesky" / "did：plc：abc123" / "2026" / "01" / "xyz789.json"
        )
        assert json_path.exists()
        meta_path = json_path.with_suffix(".json.meta")
        assert meta_path.exists()
        # JSON の中身は元のフィードアイテム
        content = json.loads(json_path.read_text(encoding="utf-8"))
        assert content["post"]["author"]["handle"] == "alice.bsky.social"

    @pytest.mark.asyncio
    async def test_skips_existing_when_not_force(
        self, source_store: SourceStore,
    ) -> None:
        item = _make_feed_item()
        result = IngestResult()
        # 1 回目
        await place_post(
            item, is_repost=False, force=False,
            store=source_store, media_downloader=FakeBlueskyMediaDownloader(),
            result=result,
        )
        # 2 回目（force=False）
        outcome = await place_post(
            item, is_repost=False, force=False,
            store=source_store, media_downloader=FakeBlueskyMediaDownloader(),
            result=result,
        )
        assert outcome is None
        assert result.skipped == 1

    @pytest.mark.asyncio
    async def test_overwrites_existing_when_force(
        self, source_store: SourceStore,
    ) -> None:
        item = _make_feed_item()
        result = IngestResult()
        await place_post(
            item, is_repost=False, force=False,
            store=source_store, media_downloader=FakeBlueskyMediaDownloader(),
            result=result,
        )
        outcome = await place_post(
            item, is_repost=False, force=True,
            store=source_store, media_downloader=FakeBlueskyMediaDownloader(),
            result=result,
        )
        assert outcome == (True, True)
        assert result.overwritten == 1

    @pytest.mark.asyncio
    async def test_seen_paths_dedup(
        self, source_store: SourceStore,
    ) -> None:
        item = _make_feed_item()
        result = IngestResult()
        seen: set[str] = set()
        outcome1 = await place_post(
            item, is_repost=False, force=False,
            store=source_store, media_downloader=FakeBlueskyMediaDownloader(),
            result=result, seen_paths=seen,
        )
        # 同一アイテム再投入（seen で弾かれる）
        outcome2 = await place_post(
            item, is_repost=False, force=False,
            store=source_store, media_downloader=FakeBlueskyMediaDownloader(),
            result=result, seen_paths=seen,
        )
        assert outcome1 == (True, False)
        assert outcome2 is None
        assert result.skipped == 1

    @pytest.mark.asyncio
    async def test_invalid_created_at_falls_back_to_unknown(
        self, source_store: SourceStore,
    ) -> None:
        item = _make_feed_item(created_at="not-a-date")
        result = IngestResult()
        outcome = await place_post(
            item, is_repost=False, force=False,
            store=source_store, media_downloader=FakeBlueskyMediaDownloader(),
            result=result,
        )
        assert outcome == (True, False)
        # ファイルパスに unknown/00 が含まれる
        path = (
            source_store.root_dir
            / "bluesky" / "did：plc：abc123" / "unknown" / "00" / "xyz789.json"
        )
        assert path.exists()

    @pytest.mark.asyncio
    async def test_image_download_via_protocol(
        self, source_store: SourceStore,
    ) -> None:
        item = _make_feed_item(
            embed={
                "$type": "app.bsky.embed.images",
                "images": [{}],
            },
        )
        # Real から見える item.post.embed (view 版) も画像を含む
        item["post"]["embed"] = {
            "$type": "app.bsky.embed.images#view",
            "images": [{"fullsize": "https://cdn.bsky.app/img/x1.jpg"}],
        }
        result = IngestResult()
        downloader = FakeBlueskyMediaDownloader(image_content_type="image/png")
        outcome = await place_post(
            item, is_repost=False, force=False,
            store=source_store, media_downloader=downloader, result=result,
        )
        assert outcome == (True, False)
        media_dir = (
            source_store.root_dir
            / "bluesky" / "did：plc：abc123" / "2026" / "01" / "media" / "xyz789"
        )
        # PNG なので image_0.png
        assert (media_dir / "image_0.png").exists()

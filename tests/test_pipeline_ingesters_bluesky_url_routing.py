"""URL 抽出 / 種別判定 / パース（純関数）のテスト.

仕様: docs/specs/ingesters/bluesky.md
"""

from __future__ import annotations

from typing import Any

import pytest

from rag.pipeline.ingesters.bluesky.url_routing import (
    classify_url,
    extract_urls_from_item,
    parse_bluesky_url,
)
from rag.pipeline.ingesters.youtube_protocols import RealYoutubeClassifier


@pytest.fixture()
def classifier() -> RealYoutubeClassifier:
    return RealYoutubeClassifier()


def _post(record: dict[str, Any]) -> dict[str, Any]:
    return {"post": {"record": record}}


class TestExtractUrlsFromItem:
    def test_facets_link_extraction(self) -> None:
        item = _post({
            "text": "see https://example.com",
            "facets": [{
                "features": [{
                    "$type": "app.bsky.richtext.facet#link",
                    "uri": "https://example.com/article",
                }],
            }],
        })
        assert extract_urls_from_item(item) == ["https://example.com/article"]

    def test_external_embed_extraction(self) -> None:
        item = _post({
            "text": "see link",
            "embed": {
                "$type": "app.bsky.embed.external",
                "external": {"uri": "https://example.com/page"},
            },
        })
        assert extract_urls_from_item(item) == ["https://example.com/page"]

    def test_record_with_media_external(self) -> None:
        item = _post({
            "text": "media + link",
            "embed": {
                "$type": "app.bsky.embed.recordWithMedia",
                "media": {
                    "$type": "app.bsky.embed.external",
                    "external": {"uri": "https://example.com/m"},
                },
            },
        })
        assert extract_urls_from_item(item) == ["https://example.com/m"]

    def test_dedup_preserves_order(self) -> None:
        item = _post({
            "text": "dup",
            "facets": [
                {"features": [{
                    "$type": "app.bsky.richtext.facet#link",
                    "uri": "https://a.test",
                }]},
                {"features": [{
                    "$type": "app.bsky.richtext.facet#link",
                    "uri": "https://a.test",
                }]},
            ],
        })
        assert extract_urls_from_item(item) == ["https://a.test"]

    def test_non_http_scheme_skipped(self) -> None:
        item = _post({
            "text": "x",
            "facets": [{"features": [{
                "$type": "app.bsky.richtext.facet#link",
                "uri": "ftp://example.com",
            }]}],
        })
        assert extract_urls_from_item(item) == []

    def test_no_record_returns_empty(self) -> None:
        assert extract_urls_from_item({"post": {}}) == []
        assert extract_urls_from_item({}) == []


class TestClassifyUrl:
    def test_youtube_video(self, classifier: RealYoutubeClassifier) -> None:
        assert classify_url(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", classifier,
        ) == "youtube"

    def test_youtube_short(self, classifier: RealYoutubeClassifier) -> None:
        assert classify_url("https://youtu.be/dQw4w9WgXcQ", classifier) == "youtube"

    def test_invalid_youtube(self, classifier: RealYoutubeClassifier) -> None:
        assert classify_url(
            "https://www.youtube.com/watch?v=", classifier,
        ) == "invalid_youtube"

    def test_bluesky_skip(self, classifier: RealYoutubeClassifier) -> None:
        assert classify_url(
            "https://bsky.app/profile/x.bsky.social/post/r1", classifier,
        ) == "skip"

    def test_general_web(self, classifier: RealYoutubeClassifier) -> None:
        assert classify_url("https://example.com/article", classifier) == "web"

    def test_youtube_channel_classified_as_web(
        self, classifier: RealYoutubeClassifier,
    ) -> None:
        assert classify_url(
            "https://www.youtube.com/@channel", classifier,
        ) == "web"


class TestParseBlueskyUrl:
    def test_valid_url(self) -> None:
        assert parse_bluesky_url(
            "https://bsky.app/profile/alice.bsky.social/post/abc123",
        ) == ("alice.bsky.social", "abc123")

    def test_with_query_string(self) -> None:
        assert parse_bluesky_url(
            "https://bsky.app/profile/alice.bsky.social/post/abc123?ref=x",
        ) == ("alice.bsky.social", "abc123")

    def test_with_fragment(self) -> None:
        assert parse_bluesky_url(
            "https://bsky.app/profile/alice.bsky.social/post/abc123#frag",
        ) == ("alice.bsky.social", "abc123")

    def test_http_scheme(self) -> None:
        assert parse_bluesky_url(
            "http://bsky.app/profile/alice.bsky.social/post/abc123",
        ) == ("alice.bsky.social", "abc123")

    def test_invalid_returns_none(self) -> None:
        assert parse_bluesky_url("https://example.com/x") is None
        assert parse_bluesky_url("not a url") is None
        assert parse_bluesky_url("https://bsky.app/x/y") is None

"""BlueSky MediaDownloader（Real / Fake / factory / 純関数）のテスト.

仕様: docs/specs/infrastructure/fake-adapters/bluesky.md
仕様: docs/specs/ingesters/bluesky.md
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from rag.config import RAGSettings
from rag.pipeline.ingesters._fake.bluesky import FakeBlueskyMediaDownloader
from rag.pipeline.ingesters.bluesky_media_downloader import (
    BlueskyMediaDownloader,
    HlsPlaylistEmptyError,
    HlsVariantNotSelectableError,
    RealBlueskyMediaDownloader,
    _base_domain,
    _get_following_same_origin_redirect,
    _select_hls_variant,
    create_bluesky_media_downloader,
)
from settings_defaults import TEST_SETTINGS_DEFAULTS


def _make_settings(**overrides: Any) -> RAGSettings:
    data = dict(TEST_SETTINGS_DEFAULTS)
    data.update(overrides)
    return RAGSettings(**data)


class TestCreateBlueskyMediaDownloader:
    def test_returns_fake_when_fake_mode_true(self) -> None:
        settings = _make_settings(rag_bluesky_fake_mode=True)
        downloader = create_bluesky_media_downloader(settings, client=None)  # type: ignore[arg-type]
        assert isinstance(downloader, FakeBlueskyMediaDownloader)

    def test_returns_real_when_fake_mode_false(self) -> None:
        settings = _make_settings(rag_bluesky_fake_mode=False)
        client = AsyncMock()
        downloader = create_bluesky_media_downloader(settings, client=client)
        assert isinstance(downloader, RealBlueskyMediaDownloader)


class TestFakeBlueskyMediaDownloader:
    def test_unknown_scenario_rejected(self) -> None:
        with pytest.raises(ValueError, match="未知のシナリオ"):
            FakeBlueskyMediaDownloader(scenario="unknown")

    @pytest.mark.asyncio
    async def test_happy_image_returns_synthetic_png_with_content_type(self) -> None:
        downloader = FakeBlueskyMediaDownloader()
        data, content_type = await downloader.download_image(
            "https://cdn.bsky.app/img/feed_fullsize/test"
        )
        assert data.startswith(b"\x89PNG")
        assert content_type == "image/png"

    @pytest.mark.asyncio
    async def test_happy_video_returns_synthetic_ts(self) -> None:
        downloader = FakeBlueskyMediaDownloader()
        data = await downloader.download_hls_video(
            "https://video.bsky.app/playlist.m3u8"
        )
        assert data.startswith(b"\x47")

    @pytest.mark.asyncio
    async def test_download_error_scenario_raises_runtime_error(self) -> None:
        downloader = FakeBlueskyMediaDownloader(scenario="download_error")
        with pytest.raises(RuntimeError, match="media download error"):
            await downloader.download_image("https://x")
        with pytest.raises(RuntimeError, match="media download error"):
            await downloader.download_hls_video("https://x")

    @pytest.mark.asyncio
    async def test_custom_image_data_overrides_default(self) -> None:
        downloader = FakeBlueskyMediaDownloader(
            image_data=b"custom-image", image_content_type="image/jpeg",
        )
        data, ct = await downloader.download_image("https://x")
        assert data == b"custom-image"
        assert ct == "image/jpeg"


class TestHlsVariantSelection:
    """Real 実装が利用する純関数の単体テスト."""

    def test_selects_lowest_bandwidth_variant(self) -> None:
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
            "high.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=500000\n"
            "low.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1000000\n"
            "mid.m3u8\n"
        )
        result = _select_hls_variant(master, "https://video.bsky.app/master.m3u8")
        assert result == "https://video.bsky.app/low.m3u8"

    def test_falls_back_to_first_variant_when_bandwidth_missing(self) -> None:
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:CODECS=\"avc1\"\n"
            "first.m3u8\n"
            "#EXT-X-STREAM-INF:CODECS=\"avc1\"\n"
            "second.m3u8\n"
        )
        result = _select_hls_variant(master, "https://video.bsky.app/master.m3u8")
        assert result == "https://video.bsky.app/first.m3u8"

    def test_returns_none_when_no_variants(self) -> None:
        master = "#EXTM3U\n#EXT-X-VERSION:3\n"
        result = _select_hls_variant(master, "https://video.bsky.app/master.m3u8")
        assert result is None


class TestBaseDomain:
    @pytest.mark.parametrize(
        "host,expected",
        [
            ("video.cdn.bsky.app", "bsky.app"),
            ("bsky.app", "bsky.app"),
            ("evil.example.com", "example.com"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_base_domain_returns_etld_plus_one_like(
        self, host: str | None, expected: str,
    ) -> None:
        assert _base_domain(host) == expected


class TestGetFollowingSameOriginRedirect:
    @pytest.mark.asyncio
    async def test_returns_response_directly_when_2xx(self) -> None:
        request = httpx.Request("GET", "https://video.bsky.app/x")
        response = httpx.Response(200, content=b"ok", request=request)
        client = AsyncMock()
        client.get.return_value = response
        result = await _get_following_same_origin_redirect(
            client, "https://video.bsky.app/x",
        )
        assert result.content == b"ok"

    @pytest.mark.asyncio
    async def test_follows_same_origin_redirect(self) -> None:
        request1 = httpx.Request("GET", "https://video.bsky.app/old")
        redirect = httpx.Response(
            302,
            headers={"location": "https://cdn.bsky.app/new"},
            request=request1,
        )
        request2 = httpx.Request("GET", "https://cdn.bsky.app/new")
        ok = httpx.Response(200, content=b"final", request=request2)
        client = AsyncMock()
        client.get.side_effect = [redirect, ok]
        result = await _get_following_same_origin_redirect(
            client, "https://video.bsky.app/old",
        )
        assert result.content == b"final"

    @pytest.mark.asyncio
    async def test_rejects_cross_domain_redirect(self) -> None:
        request = httpx.Request("GET", "https://video.bsky.app/x")
        redirect = httpx.Response(
            302,
            headers={"location": "https://evil.example.com/payload"},
            request=request,
        )
        client = AsyncMock()
        client.get.return_value = redirect
        with pytest.raises(httpx.HTTPStatusError, match="cross-domain"):
            await _get_following_same_origin_redirect(
                client, "https://video.bsky.app/x",
            )

    @pytest.mark.asyncio
    async def test_redirect_limit_exceeded(self) -> None:
        request = httpx.Request("GET", "https://video.bsky.app/x")
        redirect = httpx.Response(
            302,
            headers={"location": "https://video.bsky.app/y"},
            request=request,
        )
        client = AsyncMock()
        client.get.return_value = redirect
        with pytest.raises(httpx.HTTPStatusError, match="redirect limit"):
            await _get_following_same_origin_redirect(
                client, "https://video.bsky.app/x", max_redirects=2,
            )

    @pytest.mark.asyncio
    async def test_redirect_without_location_raises(self) -> None:
        request = httpx.Request("GET", "https://video.bsky.app/x")
        bad = httpx.Response(302, headers={}, request=request)
        client = AsyncMock()
        client.get.return_value = bad
        with pytest.raises(
            httpx.HTTPStatusError, match="redirect response without location",
        ):
            await _get_following_same_origin_redirect(
                client, "https://video.bsky.app/x",
            )

    @pytest.mark.asyncio
    async def test_non_2xx_raises(self) -> None:
        request = httpx.Request("GET", "https://video.bsky.app/x")
        bad = httpx.Response(500, request=request)
        client = AsyncMock()
        client.get.return_value = bad
        with pytest.raises(httpx.HTTPStatusError):
            await _get_following_same_origin_redirect(
                client, "https://video.bsky.app/x",
            )


class TestRealBlueskyMediaDownloader:
    @pytest.mark.asyncio
    async def test_download_image_returns_content_and_content_type(self) -> None:
        request = httpx.Request("GET", "https://cdn.bsky.app/img/x.webp")
        response = httpx.Response(
            200,
            content=b"webp-binary",
            headers={"content-type": "image/webp"},
            request=request,
        )
        client = AsyncMock()
        client.get.return_value = response
        downloader = RealBlueskyMediaDownloader(client)
        data, ct = await downloader.download_image("https://cdn.bsky.app/img/x.webp")
        assert data == b"webp-binary"
        assert ct == "image/webp"

    @pytest.mark.asyncio
    async def test_download_hls_master_then_variant_then_segments(self) -> None:
        master_url = "https://video.bsky.app/master.m3u8"
        variant_url = "https://video.bsky.app/low.m3u8"

        master_text = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=500000\n"
            "low.m3u8\n"
        )
        variant_text = (
            "#EXTM3U\n"
            "seg0.ts\n"
            "seg1.ts\n"
        )

        request = httpx.Request("GET", master_url)
        responses = [
            httpx.Response(200, content=master_text.encode(), request=request),
            httpx.Response(200, content=variant_text.encode(), request=request),
            httpx.Response(200, content=b"AAA", request=request),
            httpx.Response(200, content=b"BBB", request=request),
        ]
        client = AsyncMock()
        client.get.side_effect = responses
        downloader = RealBlueskyMediaDownloader(client)

        data = await downloader.download_hls_video(master_url)
        assert data == b"AAABBB"

        # 4 requests: master, variant, seg0, seg1
        assert client.get.call_count == 4
        called_urls = [c[0][0] for c in client.get.call_args_list]
        assert called_urls[0] == master_url
        assert called_urls[1] == variant_url
        assert called_urls[2] == "https://video.bsky.app/seg0.ts"
        assert called_urls[3] == "https://video.bsky.app/seg1.ts"

    @pytest.mark.asyncio
    async def test_download_hls_variant_only_playlist(self) -> None:
        playlist_url = "https://video.bsky.app/variant.m3u8"
        playlist_text = "#EXTM3U\nonly.ts\n"
        request = httpx.Request("GET", playlist_url)
        responses = [
            httpx.Response(200, content=playlist_text.encode(), request=request),
            httpx.Response(200, content=b"single", request=request),
        ]
        client = AsyncMock()
        client.get.side_effect = responses
        downloader = RealBlueskyMediaDownloader(client)
        data = await downloader.download_hls_video(playlist_url)
        assert data == b"single"

    @pytest.mark.asyncio
    async def test_download_hls_raises_when_variant_not_selectable(self) -> None:
        # マスタープレイリストだがバリアント URL なし
        master_text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
        request = httpx.Request("GET", "https://video.bsky.app/master.m3u8")
        client = AsyncMock()
        client.get.return_value = httpx.Response(
            200, content=master_text.encode(), request=request,
        )
        downloader = RealBlueskyMediaDownloader(client)
        with pytest.raises(HlsVariantNotSelectableError):
            await downloader.download_hls_video("https://video.bsky.app/master.m3u8")

    @pytest.mark.asyncio
    async def test_download_hls_raises_when_no_segments(self) -> None:
        playlist_text = "#EXTM3U\n#EXT-X-VERSION:3\n"
        request = httpx.Request("GET", "https://video.bsky.app/empty.m3u8")
        client = AsyncMock()
        client.get.return_value = httpx.Response(
            200, content=playlist_text.encode(), request=request,
        )
        downloader = RealBlueskyMediaDownloader(client)
        with pytest.raises(HlsPlaylistEmptyError):
            await downloader.download_hls_video("https://video.bsky.app/empty.m3u8")


class TestProtocolContract:
    """BlueskyMediaDownloader Protocol が import 可能で意味のある型."""

    def test_protocol_is_importable(self) -> None:
        # smoke: 型として参照できる
        assert BlueskyMediaDownloader is not None

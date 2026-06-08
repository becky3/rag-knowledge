"""BlueSky Fetcher / MediaDownloader / 関連 Protocol および Settings のテスト.

仕様: docs/specs/infrastructure/fake-mode.md
仕様: docs/specs/infrastructure/fake-adapters/bluesky.md

テスト範囲:
- U1: Protocol 型の公開、Settings 統合、log_fake_mode_status の bluesky 対応
- U2: Real / Fake Adapter の契約検証、factory の Real/Fake 切替、Fake シナリオ動作
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from rag.config import RAGSettings, _EnvLoader, log_fake_mode_status
from rag.pipeline.ingesters._fake.bluesky import (
    FakeBlueskyFetcher,
    FakeBlueskyMediaDownloader,
)
from rag.pipeline.ingesters.bluesky_fetcher import (
    BlueskyFetcher,
    RealBlueskyFetcher,
    create_bluesky_fetcher,
)
from rag.pipeline.ingesters.bluesky_media_downloader import BlueskyMediaDownloader
from rag.pipeline.ingesters.web import WebDelegator
from rag.pipeline.ingesters.youtube_protocols import (
    YoutubeClassifier,
    YoutubeDelegator,
)
from settings_defaults import TEST_SETTINGS_DEFAULTS


def _make_settings(**overrides: Any) -> RAGSettings:
    data = dict(TEST_SETTINGS_DEFAULTS)
    data.update(overrides)
    return RAGSettings(**data)


class TestProtocolDefinitions:
    """U1 で公開された 5 Protocol の構造確認."""

    def test_bluesky_fetcher_has_three_async_methods(self) -> None:
        # AT Protocol API の 3 エンドポイント分が async メソッドとして公開される
        names = {
            name
            for name, member in inspect.getmembers(BlueskyFetcher)
            if not name.startswith("_") and inspect.isfunction(member)
        }
        assert names == {"get_author_feed", "get_posts", "resolve_handle"}

    def test_bluesky_media_downloader_has_two_async_methods(self) -> None:
        names = {
            name
            for name, member in inspect.getmembers(BlueskyMediaDownloader)
            if not name.startswith("_") and inspect.isfunction(member)
        }
        assert names == {"download_image", "download_hls_video"}

    def test_youtube_classifier_protocol_has_classify(self) -> None:
        names = {
            name
            for name, member in inspect.getmembers(YoutubeClassifier)
            if not name.startswith("_") and inspect.isfunction(member)
        }
        assert names == {"classify"}

    def test_youtube_delegator_protocol_has_expected_methods(self) -> None:
        names = {
            name
            for name, member in inspect.getmembers(YoutubeDelegator)
            if not name.startswith("_") and inspect.isfunction(member)
        }
        # ingest_videos が VRAM 解放保証を含む公開 API、unload_whisper は保険的な明示呼び出し用
        # 仕様: docs/specs/ingesters/youtube.md「Whisper モデルライフサイクル」
        assert names == {"ingest_videos", "unload_whisper"}

    def test_web_delegator_protocol_has_only_fetch_urls(self) -> None:
        """WebDelegator は fetch_urls のみを公開する（Issue #797: クロール委譲を提供しない）."""
        names = {
            name
            for name, member in inspect.getmembers(WebDelegator)
            if not name.startswith("_") and inspect.isfunction(member)
        }
        assert names == {"fetch_urls"}


class TestBlueskyFakeModeSettings:
    """U1 で追加された bluesky fake mode の Settings 統合."""

    def test_env_loader_has_bluesky_fake_mode_field(self) -> None:
        assert "rag_bluesky_fake_mode" in _EnvLoader.model_fields
        assert "rag_bluesky_fake_fixture_dir" in _EnvLoader.model_fields

    def test_env_loader_default_is_safe_side(self) -> None:
        # .env が無くても安全側（fake 有効）で起動する
        loader = _EnvLoader(
            embedding_provider="local",
            lmstudio_base_url="http://localhost:1234",
            chromadb_persist_dir="./chroma_db",
            bm25_persist_dir="./bm25_index",
            source_store_dir="./source_store",
            converted_store_dir="./converted_store",
            rag_transport="stdio",
            rag_http_host="127.0.0.1",
            rag_http_port=8081,
            rag_dns_rebinding_protection=True,
            rag_debug_log_enabled=False,
        )  # type: ignore[call-arg]
        assert loader.rag_bluesky_fake_mode is True
        assert loader.rag_bluesky_fake_fixture_dir == (
            "src/rag/pipeline/ingesters/_fake/bluesky/data"
        )

    def test_rag_settings_has_bluesky_fake_mode_field(self) -> None:
        assert "rag_bluesky_fake_mode" in RAGSettings.model_fields
        assert "rag_bluesky_fake_fixture_dir" in RAGSettings.model_fields


class TestLogFakeModeStatusBluesky:
    """log_fake_mode_status が bluesky の状態を出力する."""

    def test_bluesky_fake_mode_emits_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = _make_settings(rag_bluesky_fake_mode=True)
        with caplog.at_level(logging.WARNING, logger="rag.config"):
            log_fake_mode_status(settings)
        assert any(
            "[FAKE MODE: bluesky]" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_bluesky_real_mode_emits_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = _make_settings(
            rag_youtube_fake_mode=False,
            rag_bluesky_fake_mode=False,
            rag_embedding_fake_mode=False,
        )
        with caplog.at_level(logging.INFO, logger="rag.config"):
            log_fake_mode_status(settings)
        records = [r for r in caplog.records if r.name == "rag.config"]
        assert any(
            "BlueSky は REAL モードで起動中" in r.message
            and r.levelno == logging.INFO
            for r in records
        )
        assert not any("[FAKE MODE: bluesky]" in r.message for r in records)


class TestCreateBlueskyFetcher:
    """U2: factory の Real / Fake 切替."""

    def test_returns_fake_fetcher_when_fake_mode_true(self) -> None:
        settings = _make_settings(rag_bluesky_fake_mode=True)
        # fake モード時は client=None 許容（型注釈と実挙動が一致）
        fetcher = create_bluesky_fetcher(settings, client=None)
        assert isinstance(fetcher, FakeBlueskyFetcher)

    def test_returns_real_fetcher_when_fake_mode_false(self) -> None:
        settings = _make_settings(rag_bluesky_fake_mode=False)
        # client は Real 側のみ参照されるため、smoke 用のダミーで OK
        client = AsyncMock()
        fetcher = create_bluesky_fetcher(settings, client=client)
        assert isinstance(fetcher, RealBlueskyFetcher)

    def test_real_mode_with_none_client_raises(self) -> None:
        # REAL モードで client=None を渡した場合は明示的に弾く
        settings = _make_settings(rag_bluesky_fake_mode=False)
        with pytest.raises(ValueError, match="REAL モード"):
            create_bluesky_fetcher(settings, client=None)

    def test_raises_when_fixture_dir_missing(self, tmp_path: Path) -> None:
        non_existent = tmp_path / "does_not_exist"
        settings = _make_settings(
            rag_bluesky_fake_mode=True,
            rag_bluesky_fake_fixture_dir=str(non_existent),
        )
        with pytest.raises(FileNotFoundError, match="fake fixture"):
            create_bluesky_fetcher(settings, client=None)


class TestRealBlueskyFetcher:
    """U2: RealBlueskyFetcher の URL 組み立てとステータスチェック."""

    @pytest.mark.asyncio
    async def test_get_author_feed_builds_url_with_actor_and_limit(self) -> None:
        client = AsyncMock()
        request = httpx.Request(
            "GET",
            "https://example.test/xrpc/app.bsky.feed.getAuthorFeed",
        )
        client.get.return_value = httpx.Response(
            200,
            json={"feed": [], "cursor": None},
            request=request,
        )
        fetcher = RealBlueskyFetcher(client, appview_url="https://example.test")

        result = await fetcher.get_author_feed("test.bsky.social", limit=50)

        assert result == {"feed": [], "cursor": None}
        called_url = client.get.call_args[0][0]
        assert "actor=test.bsky.social" in called_url
        assert "limit=50" in called_url
        assert "cursor=" not in called_url

    @pytest.mark.asyncio
    async def test_get_author_feed_includes_cursor_when_provided(self) -> None:
        client = AsyncMock()
        request = httpx.Request(
            "GET",
            "https://example.test/xrpc/app.bsky.feed.getAuthorFeed",
        )
        client.get.return_value = httpx.Response(
            200, json={"feed": []}, request=request,
        )
        fetcher = RealBlueskyFetcher(client, appview_url="https://example.test/")

        await fetcher.get_author_feed(
            "test.bsky.social", limit=10, cursor="abc123",
        )

        called_url = client.get.call_args[0][0]
        # appview_url の末尾 / が rstrip されている
        assert called_url.startswith("https://example.test/xrpc/")
        assert "cursor=abc123" in called_url

    @pytest.mark.asyncio
    async def test_get_posts_passes_uris_param(self) -> None:
        client = AsyncMock()
        request = httpx.Request(
            "GET", "https://example.test/xrpc/app.bsky.feed.getPosts",
        )
        client.get.return_value = httpx.Response(
            200, json={"posts": []}, request=request,
        )
        fetcher = RealBlueskyFetcher(client, appview_url="https://example.test")

        await fetcher.get_posts(["at://did:plc:a/app.bsky.feed.post/r1"])

        called_url = client.get.call_args[0][0]
        assert "uris=at" in called_url

    @pytest.mark.asyncio
    async def test_resolve_handle_passes_handle_param(self) -> None:
        client = AsyncMock()
        request = httpx.Request(
            "GET",
            "https://example.test/xrpc/com.atproto.identity.resolveHandle",
        )
        client.get.return_value = httpx.Response(
            200,
            json={"did": "did:plc:abc"},
            request=request,
        )
        fetcher = RealBlueskyFetcher(client, appview_url="https://example.test")

        result = await fetcher.resolve_handle("test.bsky.social")

        assert result == {"did": "did:plc:abc"}
        called_url = client.get.call_args[0][0]
        assert "handle=test.bsky.social" in called_url

    @pytest.mark.asyncio
    async def test_raises_http_error_on_non_2xx(self) -> None:
        client = AsyncMock()
        request = httpx.Request("GET", "https://example.test")
        client.get.return_value = httpx.Response(
            500, request=request,
        )
        fetcher = RealBlueskyFetcher(client, appview_url="https://example.test")

        with pytest.raises(httpx.HTTPStatusError):
            await fetcher.get_author_feed("test.bsky.social", limit=10)


class TestFakeBlueskyFetcher:
    """U2: FakeBlueskyFetcher の 4 シナリオ + カスタム注入."""

    @pytest.fixture
    def fixture_dir(self) -> Path:
        return Path(__file__).parent.parent / (
            "src/rag/pipeline/ingesters/_fake/bluesky/data"
        )

    def test_unknown_scenario_rejected(self, fixture_dir: Path) -> None:
        with pytest.raises(ValueError, match="未知のシナリオ"):
            FakeBlueskyFetcher(fixture_dir, scenario="unknown_scenario")

    def test_missing_fixture_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Fake fixture"):
            FakeBlueskyFetcher(tmp_path / "missing")

    @pytest.mark.asyncio
    async def test_happy_get_author_feed_returns_fixture(
        self, fixture_dir: Path,
    ) -> None:
        fetcher = FakeBlueskyFetcher(fixture_dir, scenario="happy")
        result = await fetcher.get_author_feed("test.bsky.social", limit=10)
        assert "feed" in result
        assert isinstance(result["feed"], list)
        assert len(result["feed"]) >= 1

    @pytest.mark.asyncio
    async def test_happy_get_posts_returns_fixture(
        self, fixture_dir: Path,
    ) -> None:
        fetcher = FakeBlueskyFetcher(fixture_dir, scenario="happy")
        result = await fetcher.get_posts(["at://did:plc:test01234567/app.bsky.feed.post/testrkey00001"])
        assert result["posts"][0]["author"]["handle"] == "test.bsky.social"

    @pytest.mark.asyncio
    async def test_happy_resolve_handle_returns_fixture(
        self, fixture_dir: Path,
    ) -> None:
        fetcher = FakeBlueskyFetcher(fixture_dir, scenario="happy")
        result = await fetcher.resolve_handle("test.bsky.social")
        assert result["did"].startswith("did:plc:test")

    @pytest.mark.asyncio
    async def test_not_found_handle_raises_404(self, fixture_dir: Path) -> None:
        fetcher = FakeBlueskyFetcher(fixture_dir, scenario="not_found_handle")
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await fetcher.resolve_handle("missing.bsky.social")
        assert exc_info.value.response.status_code == 404

    @pytest.mark.asyncio
    async def test_not_found_post_returns_empty_posts(
        self, fixture_dir: Path,
    ) -> None:
        fetcher = FakeBlueskyFetcher(fixture_dir, scenario="not_found_post")
        result = await fetcher.get_posts(["at://did:plc:x/app.bsky.feed.post/y"])
        assert result == {"posts": []}

    @pytest.mark.asyncio
    async def test_circuit_breaker_raises_runtime_error(
        self, fixture_dir: Path,
    ) -> None:
        fetcher = FakeBlueskyFetcher(fixture_dir, scenario="circuit_breaker")
        with pytest.raises(RuntimeError, match="circuit breaker"):
            await fetcher.get_author_feed("any.bsky.social", limit=10)
        with pytest.raises(RuntimeError, match="circuit breaker"):
            await fetcher.get_posts(["at://x"])
        with pytest.raises(RuntimeError, match="circuit breaker"):
            await fetcher.resolve_handle("x.bsky.social")

    @pytest.mark.asyncio
    async def test_custom_feed_overrides_fixture(self, fixture_dir: Path) -> None:
        custom = {"feed": [{"post": {"uri": "custom"}}], "cursor": "next"}
        fetcher = FakeBlueskyFetcher(fixture_dir, feed=custom)
        result = await fetcher.get_author_feed("any", limit=1)
        assert result == custom

    @pytest.mark.asyncio
    async def test_custom_did_map_overrides_fixture(
        self, fixture_dir: Path,
    ) -> None:
        fetcher = FakeBlueskyFetcher(
            fixture_dir,
            did_map={"alice.bsky.social": "did:plc:alice"},
        )
        result = await fetcher.resolve_handle("alice.bsky.social")
        assert result == {"did": "did:plc:alice"}


class TestProtocolContractFakeAndReal:
    """U2: Real / Fake が同じ Protocol を満たす（属性存在チェック）."""

    @pytest.fixture
    def fixture_dir(self) -> Path:
        return Path(__file__).parent.parent / (
            "src/rag/pipeline/ingesters/_fake/bluesky/data"
        )

    def test_real_and_fake_fetcher_share_method_set(
        self, fixture_dir: Path,
    ) -> None:
        protocol_methods = {"get_author_feed", "get_posts", "resolve_handle"}
        real = RealBlueskyFetcher(client=AsyncMock(), appview_url="https://x")
        fake = FakeBlueskyFetcher(fixture_dir)
        for method in protocol_methods:
            assert hasattr(real, method)
            assert hasattr(fake, method)

    def test_real_and_fake_media_downloader_share_method_set(self) -> None:
        protocol_methods = {"download_image", "download_hls_video"}
        from rag.pipeline.ingesters.bluesky_media_downloader import (
            RealBlueskyMediaDownloader,
        )
        real = RealBlueskyMediaDownloader(client=AsyncMock())
        fake = FakeBlueskyMediaDownloader()
        for method in protocol_methods:
            assert hasattr(real, method)
            assert hasattr(fake, method)

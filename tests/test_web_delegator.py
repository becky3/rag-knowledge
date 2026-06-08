"""WebDelegator Protocol / RealWebDelegator / factory のテスト.

仕様: docs/specs/architecture.md §3.3
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from settings_defaults import TEST_SETTINGS_DEFAULTS

from rag.config import RAGSettings
from rag.pipeline.ingesters.web import (
    RealWebDelegator,
    SiteIngestExecution,
    WebDelegator,
    WebIngester,
    create_web_delegator,
)
from rag.store.source_store import SourceStore


class TestRealWebDelegator:
    @pytest.mark.asyncio
    async def test_fetch_urls_delegates_to_web_ingester(self) -> None:
        """RealWebDelegator.fetch_urls が WebIngester.fetch_urls に委譲すること.

        Issue #797: WebDelegator はクロール委譲を提供しない設計（投稿内 URL
        自動取り込みでサイト全体クロールへ意図せず化けることを構造的に防ぐ）。
        """
        expected = SiteIngestExecution()
        web_ingester = AsyncMock(spec=WebIngester)
        web_ingester.fetch_urls = AsyncMock(return_value=expected)
        delegator = RealWebDelegator(web_ingester=web_ingester)
        urls = ["https://a.example", "https://b.example"]

        result = await delegator.fetch_urls(urls)

        assert result is expected
        web_ingester.fetch_urls.assert_awaited_once_with(urls=urls)


class TestProtocolContract:
    def test_protocol_is_importable(self) -> None:
        assert WebDelegator is not None

    def test_protocol_has_no_crawl_method(self) -> None:
        """WebDelegator Protocol に crawl 系の委譲メソッドが存在しないこと.

        BlueSky 等の委譲経路から意図しないクロールを構造的に防ぐ不変条件
        （Issue #797）。
        """
        assert hasattr(WebDelegator, "fetch_urls")
        # crawl 系メソッド・旧名 run_for_urls は公開しない
        assert not hasattr(WebDelegator, "crawl_url")
        assert not hasattr(WebDelegator, "crawl_urls")
        assert not hasattr(WebDelegator, "run_for_urls")


class TestCreateWebDelegator:
    """factory 関数 ``create_web_delegator`` のテスト."""

    def test_returns_real_delegator(self, tmp_path: Path) -> None:
        """factory が ``RealWebDelegator`` を返すこと."""
        settings = RAGSettings(**TEST_SETTINGS_DEFAULTS)
        store = SourceStore(tmp_path / "source_store")
        store.initialize()

        delegator = create_web_delegator(settings, store)

        assert isinstance(delegator, RealWebDelegator)

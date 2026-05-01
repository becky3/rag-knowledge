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
    async def test_run_for_urls_delegates_to_web_ingester(self) -> None:
        expected = SiteIngestExecution()
        web_ingester = AsyncMock(spec=WebIngester)
        web_ingester.crawl_urls = AsyncMock(return_value=expected)
        delegator = RealWebDelegator(web_ingester=web_ingester)
        urls = ["https://a.example", "https://b.example"]

        result = await delegator.run_for_urls(urls)

        assert result is expected
        web_ingester.crawl_urls.assert_awaited_once_with(urls=urls)


class TestProtocolContract:
    def test_protocol_is_importable(self) -> None:
        assert WebDelegator is not None


class TestCreateWebDelegator:
    """factory 関数 ``create_web_delegator`` のテスト."""

    def test_returns_real_delegator(self, tmp_path: Path) -> None:
        """factory が ``RealWebDelegator`` を返すこと."""
        settings = RAGSettings(**TEST_SETTINGS_DEFAULTS)
        store = SourceStore(tmp_path / "source_store")
        store.initialize()

        delegator = create_web_delegator(settings, store)

        assert isinstance(delegator, RealWebDelegator)

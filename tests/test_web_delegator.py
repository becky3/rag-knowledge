"""WebDelegator Protocol / RealWebDelegator / factory のテスト.

仕様: docs/specs/architecture.md §3.3
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from rag.pipeline.ingesters.web import (
    RealWebDelegator,
    SiteIngestExecution,
    WebDelegator,
    WebIngester,
)


class TestRealWebDelegator:
    @pytest.mark.asyncio
    async def test_run_for_urls_delegates_to_web_ingester(self) -> None:
        expected = SiteIngestExecution()
        web_ingester = AsyncMock(spec=WebIngester)
        web_ingester.crawl_urls = AsyncMock(return_value=expected)
        delegator = RealWebDelegator(web_ingester=web_ingester)
        source_store = AsyncMock()
        settings = AsyncMock()
        urls = ["https://a.example", "https://b.example"]

        result = await delegator.run_for_urls(
            urls, source_store=source_store, settings=settings,
        )

        assert result is expected
        web_ingester.crawl_urls.assert_awaited_once_with(
            urls=urls, source_store=source_store, settings=settings,
        )


class TestProtocolContract:
    def test_protocol_is_importable(self) -> None:
        assert WebDelegator is not None

"""SiteIngestRunner Protocol / RealSiteIngestRunner / factory のテスト.

仕様: docs/specs/architecture.md
仕様: docs/specs/site-ingest.md
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from rag.pipeline.site_ingest_runner import (
    RealSiteIngestRunner,
    SiteIngestExecution,
    SiteIngestRunner,
    create_site_ingest_runner,
)


class TestCreateSiteIngestRunner:
    def test_returns_real_runner(self) -> None:
        runner = create_site_ingest_runner()
        assert isinstance(runner, RealSiteIngestRunner)


class TestRealSiteIngestRunner:
    @pytest.mark.asyncio
    async def test_run_for_urls_delegates_to_execute_site_ingest(self) -> None:
        runner = RealSiteIngestRunner()
        expected = SiteIngestExecution()
        with patch(
            "rag.pipeline.site_ingest_runner.execute_site_ingest",
            new=AsyncMock(return_value=expected),
        ) as mock_execute:
            source_store = AsyncMock()
            settings = AsyncMock()
            urls = ["https://a.example", "https://b.example"]
            result = await runner.run_for_urls(
                urls, source_store=source_store, settings=settings,
            )
            assert result is expected
            mock_execute.assert_awaited_once_with(
                urls=urls, source_store=source_store, settings=settings,
            )


class TestProtocolContract:
    def test_protocol_is_importable(self) -> None:
        assert SiteIngestRunner is not None

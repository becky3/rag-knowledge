"""site_ingest_runner（site-ingest コア処理）のテスト.

仕様: docs/specs/site-ingest.md

テスト方針:
- execute_site_ingest が ScrapyRunner と Bridge を正しく呼び出すこと
- 単一 URL（クロールモード）と複数 URL（複数 URL モード）で正しい引数が渡ること
- JSONL 未出力時の早期 return（no_output=True）
- 空 URL リストの拒否
- crawl_result が SiteIngestExecution に保持されること（cleanup 責務は呼び出し元）

背景: #686 — site-ingest コア処理を Python API として切り出し、bluesky から
subprocess を介さず直接呼び出せるようにした。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from settings_defaults import TEST_SETTINGS_DEFAULTS

from rag.config import RAGSettings
from rag.pipeline.ingesters._common import IngestResult
from rag.pipeline.site_ingest_runner import execute_site_ingest
from rag.scrapy.bridge import BridgeResult
from rag.scrapy.runner import CrawlResult


def _make_settings(**overrides: object) -> RAGSettings:
    """site-ingest 関連設定を実 RAGSettings インスタンスとして返す.

    pydantic Field の制約に追随させるため、共通の TEST_SETTINGS_DEFAULTS を
    ベースに必要な site_ingest_* のみ上書きする。
    """
    return RAGSettings(**{**TEST_SETTINGS_DEFAULTS, **overrides})


@pytest.mark.asyncio
class TestExecuteSiteIngest:
    async def test_empty_urls_raises(self) -> None:
        """URL 0 件で ValueError を送出すること."""
        with pytest.raises(ValueError, match="at least one URL"):
            await execute_site_ingest(
                urls=[],
                source_store=MagicMock(),
                settings=_make_settings(),
            )

    async def test_single_url_dispatches_crawl_mode(self) -> None:
        """1 URL でクロールモード（start_url）として ScrapyRunner.run を呼ぶこと."""
        crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/out"),
            jsonl_path=Path("/tmp/out/nonexistent.jsonl"),
            success=True,
        )
        with (
            patch(
                "rag.pipeline.site_ingest_runner.create_scrapy_runner",
                return_value=MagicMock(run=AsyncMock(return_value=crawl_result)),
            ) as mock_factory,
        ):
            execution = await execute_site_ingest(
                urls=["https://example.com/page"],
                source_store=MagicMock(),
                settings=_make_settings(),
                url_pattern="^https://example\\.com/",
                max_pages=10,
                force=True,
            )

        kwargs = mock_factory.return_value.run.await_args.kwargs
        assert kwargs["start_url"] == "https://example.com/page"
        assert kwargs["allowed_domains"] == "example.com"
        assert kwargs["url_pattern"] == "^https://example\\.com/"
        assert kwargs["max_pages"] == 10
        assert kwargs["force"] is True
        assert "start_urls" not in kwargs
        assert execution.no_output is True
        assert execution.crawl_result is crawl_result

    async def test_multi_url_dispatches_multi_mode(self) -> None:
        """2 URL 以上で複数 URL モード（start_urls）として呼ぶこと."""
        crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/out"),
            jsonl_path=Path("/tmp/out/nonexistent.jsonl"),
            success=True,
        )
        runner_mock = MagicMock()
        runner_mock.run = AsyncMock(return_value=crawl_result)
        with patch(
            "rag.pipeline.site_ingest_runner.create_scrapy_runner",
            return_value=runner_mock,
        ) as mock_factory:
            execution = await execute_site_ingest(
                urls=["https://a.example.com/x", "https://b.example.com/y"],
                source_store=MagicMock(),
                settings=_make_settings(),
            )

        kwargs = runner_mock.run.await_args.kwargs
        assert kwargs["start_urls"] == [
            "https://a.example.com/x", "https://b.example.com/y",
        ]
        # ドメインの和集合
        assert set(kwargs["allowed_domains"].split(",")) == {
            "a.example.com", "b.example.com",
        }
        assert "start_url" not in kwargs
        assert "url_pattern" not in kwargs
        assert execution.no_output is True

    async def test_invokes_bridge_when_jsonl_exists(self, tmp_path: Path) -> None:
        """JSONL が存在する場合、Bridge を呼び出して bridge 結果を保持すること."""
        jsonl_path = tmp_path / "metadata.jsonl"
        jsonl_path.write_text("", encoding="utf-8")

        crawl_result = CrawlResult(
            exit_code=0,
            output_dir=tmp_path,
            jsonl_path=jsonl_path,
            success=True,
        )
        bridge_result = BridgeResult(
            ingest=IngestResult(placed=2, overwritten=1),
            total_lines=3,
            parse_errors=0,
        )
        source_store = MagicMock()

        with (
            patch(
                "rag.pipeline.site_ingest_runner.create_scrapy_runner",
                return_value=MagicMock(run=AsyncMock(return_value=crawl_result)),
            ),
            patch(
                "rag.pipeline.site_ingest_runner.import_to_source_store",
                return_value=bridge_result,
            ) as mock_bridge,
        ):
            execution = await execute_site_ingest(
                urls=["https://example.com/page"],
                source_store=source_store,
                settings=_make_settings(),
            )

        mock_bridge.assert_called_once_with(
            jsonl_path=jsonl_path,
            html_dir=tmp_path,
            source_store=source_store,
        )
        assert execution.no_output is False
        assert execution.ingest is bridge_result.ingest
        assert execution.total_lines == bridge_result.total_lines
        assert execution.parse_errors == bridge_result.parse_errors
        assert execution.scrapy_success is True

    async def test_does_not_cleanup_internally(self, tmp_path: Path) -> None:
        """execute_site_ingest 自体は cleanup を呼ばない（呼び出し元の責務）."""
        jsonl_path = tmp_path / "metadata.jsonl"
        jsonl_path.write_text("", encoding="utf-8")
        crawl_result = MagicMock(spec=CrawlResult)
        crawl_result.exit_code = 0
        crawl_result.output_dir = tmp_path
        crawl_result.jsonl_path = jsonl_path
        crawl_result.success = True

        with (
            patch(
                "rag.pipeline.site_ingest_runner.create_scrapy_runner",
                return_value=MagicMock(run=AsyncMock(return_value=crawl_result)),
            ),
            patch(
                "rag.pipeline.site_ingest_runner.import_to_source_store",
                return_value=BridgeResult(),
            ),
        ):
            execution = await execute_site_ingest(
                urls=["https://example.com/p"],
                source_store=MagicMock(),
                settings=_make_settings(),
            )

        crawl_result.cleanup.assert_not_called()
        assert execution.crawl_result is crawl_result

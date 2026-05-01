"""WebIngester（site-ingest コア処理）のテスト.

仕様: docs/specs/architecture.md §3.3
仕様: docs/specs/site-ingest.md

テスト方針:
- WebIngester.crawl_urls が ScrapyRunner と Bridge を正しく呼び出すこと
- 単一 URL（クロールモード）と複数 URL（複数 URL モード）で正しい引数が渡ること
- JSONL 未出力時の早期 return（no_output=True）
- 空 URL リストの拒否
- crawl_result が SiteIngestExecution に保持されること（cleanup 責務は呼び出し元）

背景: #686 — site-ingest コア処理を Python API として切り出し、bluesky から
subprocess を介さず直接呼び出せるようにした。
#706 — site_ingest_runner.py を ingesters/web/ パッケージに統合し WebIngester に
リネーム。ScrapyRunner Protocol を Fetcher 相当依存として注入する構造に整理。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from settings_defaults import TEST_SETTINGS_DEFAULTS

from rag.config import RAGSettings
from rag.pipeline.ingesters._common import IngestResult
from rag.pipeline.ingesters.web import WebIngester
from rag.scrapy.bridge import BridgeResult
from rag.scrapy.runner import CrawlResult


def _make_settings(**overrides: object) -> RAGSettings:
    """site-ingest 関連設定を実 RAGSettings インスタンスとして返す."""
    return RAGSettings(**{**TEST_SETTINGS_DEFAULTS, **overrides})


def _make_web_ingester(
    crawl_result: CrawlResult,
    *,
    source_store: MagicMock | None = None,
) -> tuple[WebIngester, MagicMock, MagicMock]:
    """テスト用 WebIngester を生成し、注入された scrapy_runner / source_store mock を返す."""
    scrapy_runner = MagicMock()
    scrapy_runner.run = AsyncMock(return_value=crawl_result)
    store = source_store if source_store is not None else MagicMock()
    return WebIngester(store, scrapy_runner=scrapy_runner), scrapy_runner, store


@pytest.mark.asyncio
class TestWebIngesterCrawlUrls:
    async def test_empty_urls_raises(self) -> None:
        """URL 0 件で ValueError を送出すること."""
        scrapy_runner = MagicMock()
        ingester = WebIngester(MagicMock(), scrapy_runner=scrapy_runner)
        with pytest.raises(ValueError, match="at least one URL"):
            await ingester.crawl_urls(
                urls=[],
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
        ingester, scrapy_runner, _ = _make_web_ingester(crawl_result)

        execution = await ingester.crawl_urls(
            urls=["https://example.com/page"],
            settings=_make_settings(),
            url_pattern="^https://example\\.com/",
            max_pages=10,
            force=True,
        )

        kwargs = scrapy_runner.run.await_args.kwargs
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
        ingester, scrapy_runner, _ = _make_web_ingester(crawl_result)

        execution = await ingester.crawl_urls(
            urls=["https://a.example.com/x", "https://b.example.com/y"],
            settings=_make_settings(),
        )

        kwargs = scrapy_runner.run.await_args.kwargs
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
        ingester, _, _ = _make_web_ingester(crawl_result, source_store=source_store)

        with patch(
            "rag.pipeline.ingesters.web._facade.import_to_source_store",
            return_value=bridge_result,
        ) as mock_bridge:
            execution = await ingester.crawl_urls(
                urls=["https://example.com/page"],
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
        """WebIngester 自体は cleanup を呼ばない（呼び出し元の責務）."""
        jsonl_path = tmp_path / "metadata.jsonl"
        jsonl_path.write_text("", encoding="utf-8")
        crawl_result = MagicMock(spec=CrawlResult)
        crawl_result.exit_code = 0
        crawl_result.output_dir = tmp_path
        crawl_result.jsonl_path = jsonl_path
        crawl_result.success = True

        ingester, _, _ = _make_web_ingester(crawl_result)

        with patch(
            "rag.pipeline.ingesters.web._facade.import_to_source_store",
            return_value=BridgeResult(),
        ):
            execution = await ingester.crawl_urls(
                urls=["https://example.com/p"],
                settings=_make_settings(),
            )

        crawl_result.cleanup.assert_not_called()
        assert execution.crawl_result is crawl_result

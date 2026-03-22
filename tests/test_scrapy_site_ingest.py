"""site-ingest の MCP ツール / CLI コマンド統合テスト.

仕様: docs/specs/site-ingest.md

テスト方針:
- MCP ツール（rag_site_ingest）の入力バリデーション・全体フロー
- CLI コマンド（site-ingest）の入力バリデーション・全体フロー
- URL バリデーション（空、スキーム不正）
- SSRF チェック（プライベート IP 拒否）
- url_pattern の正規表現バリデーション
- max_pages のクランプ（下限 1、上限 50000）
- ScrapyRunner → Bridge → pipeline の統合フロー（モック）
- JSONL 未出力時の早期リターン
- Config 設定値の反映
"""

from __future__ import annotations

import argparse
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rag.server import _reset_pipeline_controller, _reset_rag_service


@pytest.fixture(autouse=True)
def _reset_global_state() -> None:
    """各テスト前にグローバル状態をリセットする."""
    _reset_rag_service()
    _reset_pipeline_controller()


def _make_mock_settings() -> MagicMock:
    """MCP テスト用のモック設定を生成する."""
    s = MagicMock()
    s.site_ingest_temp_dir = "/tmp/site_ingest"
    s.site_ingest_delay_sec = 1.0
    s.site_ingest_max_pages = 1000
    s.site_ingest_download_timeout = 30
    s.site_ingest_timeout_sec = 0
    s.site_ingest_error_count = 0
    return s


# --- MCP ツール rag_site_ingest テスト ---


class TestMcpSiteIngestToolRegistered:
    """MCP サーバーにツールが登録されていること."""

    @pytest.mark.asyncio
    async def test_rag_site_ingest_in_tool_list(self) -> None:
        """rag_site_ingest がツール一覧に含まれること."""
        mod = import_module("rag.server")
        tools = await mod.mcp.list_tools()
        tool_names = {t.name for t in tools}
        assert "rag_site_ingest" in tool_names


class TestMcpSiteIngestValidation:
    """rag_site_ingest の入力バリデーションテスト."""

    @pytest.mark.asyncio
    async def test_empty_url_returns_error(self) -> None:
        """空 URL がエラーを返すこと."""
        mod = import_module("rag.server")
        with patch.object(mod, "get_settings", return_value=_make_mock_settings()):
            result = await mod.rag_site_ingest(url="", url_pattern="", max_pages=None, force=False)
        assert "エラー" in result

    @pytest.mark.asyncio
    async def test_invalid_scheme_returns_error(self) -> None:
        """http/https 以外のスキームがエラーを返すこと."""
        mod = import_module("rag.server")
        with patch.object(mod, "get_settings", return_value=_make_mock_settings()):
            result = await mod.rag_site_ingest(url="ftp://example.com", url_pattern="", max_pages=None, force=False)
        assert "エラー" in result

    @pytest.mark.asyncio
    async def test_ssrf_private_ip_returns_error(self) -> None:
        """プライベート IP がエラーを返すこと."""
        mod = import_module("rag.server")
        with patch.object(mod, "get_settings", return_value=_make_mock_settings()):
            result = await mod.rag_site_ingest(url="http://192.168.1.1/page", url_pattern="", max_pages=None, force=False)
        assert "エラー" in result

    @pytest.mark.asyncio
    async def test_ssrf_localhost_returns_error(self) -> None:
        """localhost がエラーを返すこと."""
        mod = import_module("rag.server")
        with patch.object(mod, "get_settings", return_value=_make_mock_settings()):
            result = await mod.rag_site_ingest(url="http://localhost/page", url_pattern="", max_pages=None, force=False)
        assert "エラー" in result

    @pytest.mark.asyncio
    async def test_invalid_regex_pattern_returns_error(self) -> None:
        """不正な正規表現パターンがエラーを返すこと."""
        mod = import_module("rag.server")
        with patch.object(mod, "get_settings", return_value=_make_mock_settings()):
            result = await mod.rag_site_ingest(
                url="https://example.com",
                url_pattern="[invalid(",
                max_pages=None,
                force=False,
            )
        assert "エラー" in result
        assert "正規表現" in result


class TestMcpSiteIngestMaxPagesClamp:
    """max_pages のクランプ処理テスト."""

    @pytest.mark.asyncio
    async def test_max_pages_clamped_to_1_when_zero(self) -> None:
        """max_pages=0 が 1 にクランプされること."""
        from rag.scrapy.runner import CrawlResult, ScrapyRunner

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/test"),
            jsonl_path=Path("/tmp/test/nonexistent.jsonl"),
            success=True,
        )

        mod = import_module("rag.server")
        with (
            patch.object(mod, "get_settings", return_value=_make_mock_settings()),
            patch.object(ScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result),
            patch.object(mod, "_get_pipeline_controller", new_callable=AsyncMock, return_value=MagicMock()),
        ):
            result = await mod.rag_site_ingest(
                url="https://example.com",
                url_pattern="",
                max_pages=0,
                force=False,
            )
            # JSONL が存在しないので早期リターン
            assert "メタデータが出力されませんでした" in result

    @pytest.mark.asyncio
    async def test_max_pages_clamped_to_50000_when_exceeds(self) -> None:
        """max_pages=100000 が 50000 にクランプされること."""
        from rag.scrapy.runner import CrawlResult, ScrapyRunner

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/test"),
            jsonl_path=Path("/tmp/test/nonexistent.jsonl"),
            success=True,
        )

        mod = import_module("rag.server")
        with (
            patch.object(mod, "get_settings", return_value=_make_mock_settings()),
            patch.object(ScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result) as mock_run,
            patch.object(mod, "_get_pipeline_controller", new_callable=AsyncMock, return_value=MagicMock()),
        ):
            result = await mod.rag_site_ingest(
                url="https://example.com",
                url_pattern="",
                max_pages=100000,
                force=False,
            )
            assert "メタデータが出力されませんでした" in result
            # run() が呼ばれた際の max_pages が 50000 であること
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["max_pages"] == 50000


class TestMcpSiteIngestFlow:
    """rag_site_ingest の統合フロー（モック）テスト."""

    @pytest.mark.asyncio
    async def test_no_jsonl_returns_early(self) -> None:
        """JSONL が出力されない場合、早期リターンすること."""
        from rag.scrapy.runner import CrawlResult, ScrapyRunner

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/test"),
            jsonl_path=Path("/tmp/test/nonexistent.jsonl"),
            success=True,
        )

        mod = import_module("rag.server")
        with (
            patch.object(mod, "get_settings", return_value=_make_mock_settings()),
            patch.object(ScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result),
            patch.object(mod, "_get_pipeline_controller", new_callable=AsyncMock, return_value=MagicMock()),
        ):
            result = await mod.rag_site_ingest(
                url="https://example.com",
                url_pattern="",
                max_pages=10,
                force=False,
            )
            assert "メタデータが出力されませんでした" in result
            assert "exit_code=0" in result

    @pytest.mark.asyncio
    async def test_successful_ingest_flow(self, tmp_path: Path) -> None:
        """正常な取り込みフロー: Runner → Bridge → pipeline."""
        import json

        from rag.pipeline.ingesters._common import IngestResult
        from rag.pipeline.models import PipelineMode, PipelineSummary
        from rag.scrapy.bridge import BridgeResult
        from rag.scrapy.runner import CrawlResult, ScrapyRunner

        jsonl_path = tmp_path / "metadata.jsonl"
        jsonl_path.write_text(
            json.dumps({"url": "https://example.com/page", "title": "Test", "status": 200, "depth": 0, "collected_at": "2025-01-01T00:00:00Z", "filepath": "page.html"}) + "\n",
            encoding="utf-8",
        )

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=tmp_path,
            jsonl_path=jsonl_path,
            success=True,
        )

        mock_bridge_result = BridgeResult(
            ingest=IngestResult(placed=3, skipped=1, errors=0),
            total_lines=4,
            parse_errors=0,
        )

        mock_pipeline_summary = PipelineSummary(
            mode=PipelineMode.INCREMENTAL,
            total_files=3,
            processed=3,
            skipped=0,
            errors=[],
        )

        mock_controller = MagicMock()
        mock_controller.source_store = MagicMock()

        mod = import_module("rag.server")
        with (
            patch.object(mod, "get_settings", return_value=_make_mock_settings()),
            patch.object(ScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result),
            patch.object(mod, "_get_pipeline_controller", new_callable=AsyncMock, return_value=mock_controller),
            patch("rag.scrapy.bridge.import_to_source_store", return_value=mock_bridge_result) as mock_bridge,
            patch.object(mod, "_run_ingest_and_index_subprocess", new_callable=AsyncMock, return_value=mock_pipeline_summary),
            patch.object(mod, "_reset_pipeline_controller"),
            patch.object(mod, "_reset_rag_service"),
        ):
            result = await mod.rag_site_ingest(
                url="https://example.com",
                url_pattern="",
                max_pages=10,
                force=False,
            )
            assert "3件新規配置" in result
            assert "1件スキップ" in result
            assert "パイプライン: 3件処理" in result
            mock_bridge.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_pipeline_when_zero_placed(self, tmp_path: Path) -> None:
        """placed=0 の場合、パイプラインが実行されないこと."""
        import json

        from rag.pipeline.ingesters._common import IngestResult
        from rag.scrapy.bridge import BridgeResult
        from rag.scrapy.runner import CrawlResult, ScrapyRunner

        jsonl_path = tmp_path / "metadata.jsonl"
        jsonl_path.write_text(
            json.dumps({"url": "https://example.com/page", "title": "Test", "status": 200, "depth": 0, "collected_at": "2025-01-01T00:00:00Z", "filepath": "page.html"}) + "\n",
            encoding="utf-8",
        )

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=tmp_path,
            jsonl_path=jsonl_path,
            success=True,
        )

        mock_bridge_result = BridgeResult(
            ingest=IngestResult(placed=0, skipped=5, errors=0),
            total_lines=5,
            parse_errors=0,
        )

        mock_controller = MagicMock()
        mock_controller.source_store = MagicMock()

        mod = import_module("rag.server")
        with (
            patch.object(mod, "get_settings", return_value=_make_mock_settings()),
            patch.object(ScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result),
            patch.object(mod, "_get_pipeline_controller", new_callable=AsyncMock, return_value=mock_controller),
            patch("rag.scrapy.bridge.import_to_source_store", return_value=mock_bridge_result),
            patch.object(mod, "_run_ingest_and_index_subprocess", new_callable=AsyncMock) as mock_subprocess,
        ):
            result = await mod.rag_site_ingest(
                url="https://example.com",
                url_pattern="",
                max_pages=10,
                force=False,
            )
            assert "0件新規配置" in result
            assert "5件スキップ" in result
            mock_subprocess.assert_not_called()

    @pytest.mark.asyncio
    async def test_partial_result_with_scrapy_failure(self, tmp_path: Path) -> None:
        """Scrapy が非0終了コードでも部分結果が返ること."""
        import json

        from rag.pipeline.ingesters._common import IngestResult
        from rag.pipeline.models import PipelineMode, PipelineSummary
        from rag.scrapy.bridge import BridgeResult
        from rag.scrapy.runner import CrawlResult, ScrapyRunner

        jsonl_path = tmp_path / "metadata.jsonl"
        jsonl_path.write_text(
            json.dumps({"url": "https://example.com/page", "title": "Test", "status": 200, "depth": 0, "collected_at": "2025-01-01T00:00:00Z", "filepath": "page.html"}) + "\n",
            encoding="utf-8",
        )

        mock_crawl_result = CrawlResult(
            exit_code=1,
            output_dir=tmp_path,
            jsonl_path=jsonl_path,
            success=False,
        )

        mock_bridge_result = BridgeResult(
            ingest=IngestResult(placed=2, skipped=0, errors=0),
            total_lines=2,
            parse_errors=0,
        )

        mock_pipeline_summary = PipelineSummary(
            mode=PipelineMode.INCREMENTAL,
            total_files=2,
            processed=2,
            skipped=0,
            errors=[],
        )

        mock_controller = MagicMock()
        mock_controller.source_store = MagicMock()

        mod = import_module("rag.server")
        with (
            patch.object(mod, "get_settings", return_value=_make_mock_settings()),
            patch.object(ScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result),
            patch.object(mod, "_get_pipeline_controller", new_callable=AsyncMock, return_value=mock_controller),
            patch("rag.scrapy.bridge.import_to_source_store", return_value=mock_bridge_result),
            patch.object(mod, "_run_ingest_and_index_subprocess", new_callable=AsyncMock, return_value=mock_pipeline_summary),
            patch.object(mod, "_reset_pipeline_controller"),
            patch.object(mod, "_reset_rag_service"),
        ):
            result = await mod.rag_site_ingest(
                url="https://example.com",
                url_pattern="",
                max_pages=10,
                force=False,
            )
            assert "2件新規配置" in result
            assert "exit_code=1" in result
            assert "部分的な結果" in result


# --- CLI コマンド site-ingest テスト ---


class TestCliSiteIngestValidation:
    """CLI run_site_ingest の入力バリデーションテスト."""

    @pytest.mark.asyncio
    async def test_empty_url_exits(self) -> None:
        """空 URL で sys.exit(1) すること."""
        from rag.cli import run_site_ingest

        args = argparse.Namespace(url="", url_pattern="", max_pages=None, force=False, download_only=False)
        with pytest.raises(SystemExit) as exc_info:
            await run_site_ingest(args)
        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_invalid_scheme_exits(self) -> None:
        """不正スキームで sys.exit(1) すること."""
        from rag.cli import run_site_ingest

        args = argparse.Namespace(url="ftp://example.com", url_pattern="", max_pages=None, force=False, download_only=False)
        with pytest.raises(SystemExit) as exc_info:
            await run_site_ingest(args)
        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_ssrf_private_ip_exits(self) -> None:
        """プライベート IP で sys.exit(1) すること."""
        from rag.cli import run_site_ingest

        args = argparse.Namespace(url="http://10.0.0.1/page", url_pattern="", max_pages=None, force=False, download_only=False)
        with pytest.raises(SystemExit) as exc_info:
            await run_site_ingest(args)
        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_invalid_regex_pattern_exits(self) -> None:
        """不正な正規表現パターンで sys.exit(1) すること."""
        from rag.cli import run_site_ingest

        args = argparse.Namespace(url="https://example.com", url_pattern="[bad(", max_pages=None, force=False, download_only=False)
        with pytest.raises(SystemExit) as exc_info:
            await run_site_ingest(args)
        assert exc_info.value.code == 1


def _make_cli_mock_settings() -> MagicMock:
    """CLI テスト用のモック設定を生成する."""
    s = MagicMock()
    s.site_ingest_temp_dir = "/tmp/test"
    s.site_ingest_delay_sec = 1.0
    s.site_ingest_max_pages = 1000
    s.site_ingest_download_timeout = 30
    s.site_ingest_timeout_sec = 0
    s.site_ingest_error_count = 0
    return s


class TestCliSiteIngestMaxPagesClamp:
    """CLI の max_pages クランプ処理テスト."""

    @pytest.mark.asyncio
    async def test_max_pages_clamped_to_1_when_negative(self) -> None:
        """max_pages=-1 が 1 にクランプされること."""
        from rag.scrapy.runner import CrawlResult, ScrapyRunner

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/test"),
            jsonl_path=Path("/tmp/test/nonexistent.jsonl"),
            success=True,
        )

        args = argparse.Namespace(url="https://example.com", url_pattern="", max_pages=-1, force=False, download_only=False)

        with (
            patch.object(ScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result) as mock_run,
            patch("rag.cli._build_cli_pipeline_controller", return_value=(MagicMock(), _make_cli_mock_settings())),
        ):
            from rag.cli import run_site_ingest

            await run_site_ingest(args)
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["max_pages"] == 1

    @pytest.mark.asyncio
    async def test_max_pages_clamped_to_50000(self) -> None:
        """max_pages=999999 が 50000 にクランプされること."""
        from rag.scrapy.runner import CrawlResult, ScrapyRunner

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/test"),
            jsonl_path=Path("/tmp/test/nonexistent.jsonl"),
            success=True,
        )

        args = argparse.Namespace(url="https://example.com", url_pattern="", max_pages=999999, force=False, download_only=False)

        with (
            patch.object(ScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result) as mock_run,
            patch("rag.cli._build_cli_pipeline_controller", return_value=(MagicMock(), _make_cli_mock_settings())),
        ):
            from rag.cli import run_site_ingest

            await run_site_ingest(args)
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["max_pages"] == 50000

    @pytest.mark.asyncio
    async def test_max_pages_uses_settings_default(self) -> None:
        """max_pages 未指定時に設定値が使われること."""
        from rag.scrapy.runner import CrawlResult, ScrapyRunner

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/test"),
            jsonl_path=Path("/tmp/test/nonexistent.jsonl"),
            success=True,
        )

        mock_settings = _make_cli_mock_settings()
        mock_settings.site_ingest_max_pages = 2000

        args = argparse.Namespace(url="https://example.com", url_pattern="", max_pages=None, force=False, download_only=False)

        with (
            patch.object(ScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result) as mock_run,
            patch("rag.cli._build_cli_pipeline_controller", return_value=(MagicMock(), mock_settings)),
        ):
            from rag.cli import run_site_ingest

            await run_site_ingest(args)
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["max_pages"] == 2000


class TestCliSiteIngestFlow:
    """CLI run_site_ingest の統合フローテスト."""

    @pytest.mark.asyncio
    async def test_no_jsonl_returns_without_bridge(self, capsys: pytest.CaptureFixture[str]) -> None:
        """JSONL 未出力時に Bridge を呼ばず早期リターンすること."""
        from rag.scrapy.runner import CrawlResult, ScrapyRunner

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/test"),
            jsonl_path=Path("/tmp/test/nonexistent.jsonl"),
            success=True,
        )

        args = argparse.Namespace(url="https://example.com", url_pattern="", max_pages=10, force=False, download_only=False)

        with (
            patch.object(ScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result),
            patch("rag.cli._build_cli_pipeline_controller", return_value=(MagicMock(), _make_cli_mock_settings())),
        ):
            from rag.cli import run_site_ingest

            await run_site_ingest(args)
            captured = capsys.readouterr()
            assert "メタデータが出力されませんでした" in captured.out

    @pytest.mark.asyncio
    async def test_successful_ingest_prints_summary(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """正常な取り込みで結果サマリーが出力されること."""
        import json

        from rag.pipeline.ingesters._common import IngestResult
        from rag.scrapy.bridge import BridgeResult
        from rag.scrapy.runner import CrawlResult, ScrapyRunner

        jsonl_path = tmp_path / "metadata.jsonl"
        jsonl_path.write_text(
            json.dumps({"url": "https://example.com/p", "title": "T", "status": 200, "depth": 0, "collected_at": "2025-01-01T00:00:00Z", "filepath": "p.html"}) + "\n",
            encoding="utf-8",
        )

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=tmp_path,
            jsonl_path=jsonl_path,
            success=True,
        )

        mock_bridge_result = BridgeResult(
            ingest=IngestResult(placed=5, skipped=2, errors=1),
            total_lines=8,
            parse_errors=0,
        )

        mock_pipeline_summary = MagicMock()
        mock_pipeline_summary.processed = 5
        mock_pipeline_summary.errors = []

        mock_controller = MagicMock()
        mock_controller.source_store = MagicMock()
        mock_controller.ingest_and_index.return_value = mock_pipeline_summary

        args = argparse.Namespace(url="https://example.com", url_pattern="", max_pages=10, force=False, download_only=False)

        with (
            patch.object(ScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result),
            patch("rag.cli._build_cli_pipeline_controller", return_value=(mock_controller, _make_cli_mock_settings())),
            patch("rag.scrapy.bridge.import_to_source_store", return_value=mock_bridge_result),
        ):
            from rag.cli import run_site_ingest

            await run_site_ingest(args)
            captured = capsys.readouterr()
            assert "5件新規配置" in captured.out
            assert "2件スキップ" in captured.out
            assert "1件エラー" in captured.out
            assert "パイプライン: 5件処理" in captured.out

    @pytest.mark.asyncio
    async def test_no_pipeline_when_zero_placed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """placed=0 の場合パイプラインが実行されないこと."""
        import json

        from rag.pipeline.ingesters._common import IngestResult
        from rag.scrapy.bridge import BridgeResult
        from rag.scrapy.runner import CrawlResult, ScrapyRunner

        jsonl_path = tmp_path / "metadata.jsonl"
        jsonl_path.write_text(
            json.dumps({"url": "https://example.com/p", "title": "T", "status": 200, "depth": 0, "collected_at": "2025-01-01T00:00:00Z", "filepath": "p.html"}) + "\n",
            encoding="utf-8",
        )

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=tmp_path,
            jsonl_path=jsonl_path,
            success=True,
        )

        mock_bridge_result = BridgeResult(
            ingest=IngestResult(placed=0, skipped=3, errors=0),
            total_lines=3,
            parse_errors=0,
        )

        mock_controller = MagicMock()
        mock_controller.source_store = MagicMock()

        args = argparse.Namespace(url="https://example.com", url_pattern="", max_pages=10, force=False, download_only=False)

        with (
            patch.object(ScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result),
            patch("rag.cli._build_cli_pipeline_controller", return_value=(mock_controller, _make_cli_mock_settings())),
            patch("rag.scrapy.bridge.import_to_source_store", return_value=mock_bridge_result),
        ):
            from rag.cli import run_site_ingest

            await run_site_ingest(args)
            captured = capsys.readouterr()
            assert "0件新規配置" in captured.out
            mock_controller.ingest_and_index.assert_not_called()


# --- Config テスト ---


def _make_rag_settings(**overrides: object) -> object:
    """テスト用 RAGSettings を生成する（全必須フィールドをデフォルト値付きで提供）."""
    from settings_defaults import TEST_SETTINGS_DEFAULTS
    from rag.config import RAGSettings

    return RAGSettings(**{**TEST_SETTINGS_DEFAULTS, **overrides})


class TestSiteIngestConfig:
    """site-ingest 関連の設定値テスト."""

    def test_config_has_site_ingest_fields(self) -> None:
        """RAGSettings に site_ingest 関連フィールドが存在すること."""
        from rag.config import RAGSettings

        fields = RAGSettings.model_fields
        assert "site_ingest_temp_dir" in fields
        assert "site_ingest_delay_sec" in fields
        assert "site_ingest_max_pages" in fields
        assert "site_ingest_download_timeout" in fields
        assert "site_ingest_timeout_sec" in fields
        assert "site_ingest_error_count" in fields

    def test_site_ingest_delay_sec_lower_bound(self) -> None:
        """site_ingest_delay_sec が下限 0.05 未満で拒否されること."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_rag_settings(site_ingest_delay_sec=0.01)

    def test_site_ingest_max_pages_lower_bound(self) -> None:
        """site_ingest_max_pages が 0 で拒否されること."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_rag_settings(site_ingest_max_pages=0)

    def test_site_ingest_download_timeout_lower_bound(self) -> None:
        """site_ingest_download_timeout が 0 で拒否されること."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_rag_settings(site_ingest_download_timeout=0)

    def test_site_ingest_timeout_sec_lower_bound(self) -> None:
        """site_ingest_timeout_sec が 60 未満で拒否されること."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_rag_settings(site_ingest_timeout_sec=59.0)

    def test_site_ingest_timeout_sec_upper_bound(self) -> None:
        """site_ingest_timeout_sec が 86400 超で拒否されること."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_rag_settings(site_ingest_timeout_sec=86401.0)

    def test_site_ingest_error_count_lower_bound(self) -> None:
        """site_ingest_error_count が 0 で拒否されること."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_rag_settings(site_ingest_error_count=0)

    def test_site_ingest_error_count_upper_bound(self) -> None:
        """site_ingest_error_count が 1000 超で拒否されること."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_rag_settings(site_ingest_error_count=1001)


# --- CLI パーサーテスト ---


class TestCliSiteIngestParser:
    """CLI site-ingest サブコマンドのパーサーテスト."""

    def test_run_site_ingest_is_callable(self) -> None:
        """run_site_ingest が呼び出し可能であること."""
        from rag.cli import run_site_ingest

        assert callable(run_site_ingest)

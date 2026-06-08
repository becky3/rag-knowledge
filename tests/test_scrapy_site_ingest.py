"""site-ingest / site-crawl の MCP ツール / CLI コマンド統合テスト.

仕様: docs/specs/site-ingest.md

Issue #797 で入口を 2 つに物理分離した:
- MCP ``rag_site_ingest`` / CLI ``site-ingest`` — 取得入口（リンク辿りなし、複数 URL OK）
- MCP ``rag_site_crawl`` / CLI ``site-crawl`` — クロール入口（リンク辿りあり、単一 URL）

テスト方針:
- MCP ツールの入力バリデーション・CLI 委譲フロー
- CLI コマンドの入力バリデーション・全体フロー
- URL バリデーション（空、スキーム不正）
- SSRF チェック（プライベート IP 拒否）
- url_pattern の正規表現バリデーション（site-crawl のみ）
- max_pages のクランプ（下限 1、上限 1000）— CLI レイヤーでテスト（site-crawl のみ）
- MCP は _run_cli_subprocess に委譲 → 引数とフォーマットを検証
- ScrapyRunner → Bridge → pipeline の統合フロー（CLI テスト、モック）
- site-ingest は単一 URL でもリンク辿りクロールに化けないこと（Issue #797 回帰）
"""

from __future__ import annotations

import argparse
import contextlib
import json
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rag.server.safe_browsing_wiring import _reset_safe_browsing_client


@pytest.fixture(autouse=True)
def _reset_global_state() -> None:
    """各テスト前にグローバル状態をリセットする."""
    _reset_safe_browsing_client()


@pytest.fixture(autouse=True)
def _bypass_write_lock():
    """CLI の write_lock 取得を no-op にする.

    本ファイルの CLI 統合テストは ``_build_cli_pipeline_controller`` を
    MagicMock で置き換える都合上、controller.source_store.root_dir が
    MagicMock オブジェクトになる。そのまま ``_write_lock_or_exit`` を通すと
    実際に ``Path(MagicMock)`` から ``MagicMock/mock.source_store.root_dir/...``
    ディレクトリが作成されてしまうため、ヘルパーを no-op に差し替える。
    """
    with patch(
        "rag.cli._write_lock_or_exit",
        new=lambda *args, **kwargs: contextlib.nullcontext(),
    ):
        yield


def _make_cli_mock_settings() -> MagicMock:
    """CLI テスト用のモック設定を生成する.

    既存テストは ``patch.object(RealScrapyRunner, "run")`` で Real 実装の
    run メソッドを差し替える前提のため、ここでは fake モードを無効化する
    （factory が Real を返すようにする）。Fake 経路の検証は L2 E2E
    （tests/e2e/test_ingest_mcp_scrapy.py）で行う。
    """
    s = MagicMock()
    s.site_ingest_temp_dir = "/tmp/test"
    s.site_ingest_delay_sec = 1.0
    s.site_ingest_max_pages = 1000
    s.site_ingest_download_timeout = 30
    s.site_ingest_timeout_sec = 0
    s.site_ingest_error_count = 0
    s.rag_scrapy_fake_mode = False
    s.rag_embedding_concurrency = 1
    return s


# --- MCP ツール登録テスト ---


class TestMcpToolsRegistered:
    """MCP サーバーに 2 ツールが登録されていること."""

    @pytest.mark.asyncio
    async def test_rag_site_ingest_in_tool_list(self) -> None:
        """rag_site_ingest がツール一覧に含まれること."""
        mod = import_module("rag.server")
        tools = await mod.mcp.list_tools()
        tool_names = {t.name for t in tools}
        assert "rag_site_ingest" in tool_names

    @pytest.mark.asyncio
    async def test_rag_site_crawl_in_tool_list(self) -> None:
        """rag_site_crawl がツール一覧に含まれること（Issue #797 で新設）."""
        mod = import_module("rag.server")
        tools = await mod.mcp.list_tools()
        tool_names = {t.name for t in tools}
        assert "rag_site_crawl" in tool_names


# --- MCP ツール rag_site_ingest（取得入口）テスト ---


class TestMcpSiteIngestValidation:
    """rag_site_ingest の入力バリデーションテスト."""

    @pytest.mark.asyncio
    async def test_empty_urls_returns_error(self) -> None:
        """urls が空リストでエラーを返すこと."""
        mod = import_module("rag.server")
        result = await mod.rag_site_ingest(urls=[])
        assert "エラー" in result

    @pytest.mark.asyncio
    async def test_invalid_scheme_returns_error(self) -> None:
        """http/https 以外のスキームがエラーを返すこと."""
        mod = import_module("rag.server")
        result = await mod.rag_site_ingest(urls=["ftp://example.com"])
        assert "エラー" in result

    @pytest.mark.asyncio
    async def test_ssrf_private_ip_returns_error(self) -> None:
        """プライベート IP がエラーを返すこと."""
        mod = import_module("rag.server")
        result = await mod.rag_site_ingest(urls=["http://192.168.1.1/page"])
        assert "エラー" in result


class TestMcpSiteIngestCliDelegation:
    """rag_site_ingest が _run_cli_subprocess に正しく委譲すること."""

    @pytest.mark.asyncio
    async def test_single_url_delegates_to_cli(self) -> None:
        """単一 URL でも CLI site-ingest コマンドに委譲され、クロール固有引数が付与されないこと."""
        mock_result = {"placed": 1, "skipped": 0, "overwritten": 0, "errors": 0}

        mod = import_module("rag.server")
        with (
            patch("rag.server.cli_subprocess._run_cli_subprocess", new_callable=AsyncMock, return_value=mock_result) as mock_cli,
            patch("rag.server.cli_subprocess._format_cli_ingest_result", return_value="OK"),
        ):
            result = await mod.rag_site_ingest(urls=["https://example.com"])

        mock_cli.assert_called_once()
        call_args = mock_cli.call_args
        assert call_args[0][0] == "site-ingest"
        cli_args = call_args[0][1]
        assert "https://example.com" in cli_args
        # クロール固有オプションは存在しない（Issue #797 で site-ingest からは消滅）
        assert "--url-pattern" not in cli_args
        assert "--max-pages" not in cli_args
        assert "--force" not in cli_args
        assert "--restart" not in cli_args
        assert result.endswith("OK")

    @pytest.mark.asyncio
    async def test_multi_url_delegates_to_cli(self) -> None:
        """複数 URL で全 URL が CLI 引数に含まれること."""
        mock_result = {"placed": 2, "skipped": 0, "overwritten": 0, "errors": 0}

        mod = import_module("rag.server")
        with (
            patch("rag.server.cli_subprocess._run_cli_subprocess", new_callable=AsyncMock, return_value=mock_result) as mock_cli,
            patch("rag.server.cli_subprocess._format_cli_ingest_result", return_value="OK"),
        ):
            await mod.rag_site_ingest(
                urls=["https://example.com/a", "https://example.com/b"],
            )
        cli_args = mock_cli.call_args[0][1]
        assert "https://example.com/a" in cli_args
        assert "https://example.com/b" in cli_args

    @pytest.mark.asyncio
    async def test_skip_pipeline_flag_passed(self) -> None:
        """skip_pipeline=True で --skip-pipeline が CLI 引数に含まれること."""
        mock_result = {"placed": 1, "skipped": 0, "overwritten": 0, "errors": 0}

        mod = import_module("rag.server")
        with (
            patch("rag.server.cli_subprocess._run_cli_subprocess", new_callable=AsyncMock, return_value=mock_result) as mock_cli,
            patch("rag.server.cli_subprocess._format_cli_ingest_result", return_value="OK"),
        ):
            await mod.rag_site_ingest(
                urls=["https://example.com"], skip_pipeline=True,
            )
        cli_args = mock_cli.call_args[0][1]
        assert "--skip-pipeline" in cli_args

    @pytest.mark.asyncio
    async def test_cli_subprocess_error_returns_error_message(self) -> None:
        """CLISubprocessError が適切なエラーメッセージに変換されること."""
        from rag.server.cli_subprocess import CLISubprocessError

        mod = import_module("rag.server")
        with patch(
            "rag.server.cli_subprocess._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError("failed"),
        ):
            result = await mod.rag_site_ingest(urls=["https://example.com"])
        assert "エラー" in result
        assert "失敗" in result


# --- MCP ツール rag_site_crawl（クロール入口）テスト ---


class TestMcpSiteCrawlValidation:
    """rag_site_crawl の入力バリデーションテスト."""

    @pytest.mark.asyncio
    async def test_empty_url_returns_error(self) -> None:
        """url が空文字でエラーを返すこと."""
        mod = import_module("rag.server")
        result = await mod.rag_site_crawl(url="")
        assert "エラー" in result

    @pytest.mark.asyncio
    async def test_invalid_scheme_returns_error(self) -> None:
        """http/https 以外のスキームがエラーを返すこと."""
        mod = import_module("rag.server")
        result = await mod.rag_site_crawl(url="ftp://example.com")
        assert "エラー" in result

    @pytest.mark.asyncio
    async def test_ssrf_private_ip_returns_error(self) -> None:
        """プライベート IP がエラーを返すこと."""
        mod = import_module("rag.server")
        result = await mod.rag_site_crawl(url="http://192.168.1.1/page")
        assert "エラー" in result

    @pytest.mark.asyncio
    async def test_invalid_regex_pattern_returns_error(self) -> None:
        """不正な正規表現パターンがエラーを返すこと."""
        mod = import_module("rag.server")
        with patch(
            "rag.server.tools.ingest_site.safe_browsing_wiring._get_safe_browsing_client",
            return_value=None,
        ):
            result = await mod.rag_site_crawl(
                url="https://example.com",
                url_pattern="[invalid(",
            )
        assert "エラー" in result
        assert "正規表現" in result


class TestMcpSiteCrawlCliDelegation:
    """rag_site_crawl が _run_cli_subprocess に正しく委譲すること."""

    @pytest.mark.asyncio
    async def test_delegates_to_cli_with_options(self) -> None:
        """url_pattern / max_pages / restart が CLI 引数に含まれること."""
        mock_result = {"placed": 5, "skipped": 0, "overwritten": 0, "errors": 0}

        mod = import_module("rag.server")
        with (
            patch("rag.server.tools.ingest_site.safe_browsing_wiring._get_safe_browsing_client", return_value=None),
            patch("rag.server.cli_subprocess._run_cli_subprocess", new_callable=AsyncMock, return_value=mock_result) as mock_cli,
            patch("rag.server.cli_subprocess._format_cli_ingest_result", return_value="OK"),
        ):
            await mod.rag_site_crawl(
                url="https://example.com",
                url_pattern=r"/docs/.*",
                max_pages=50,
                restart=True,
            )
        call_args = mock_cli.call_args
        assert call_args[0][0] == "site-crawl"
        cli_args = call_args[0][1]
        assert "https://example.com" in cli_args
        assert "--url-pattern" in cli_args
        assert r"/docs/.*" in cli_args
        assert "--max-pages" in cli_args
        assert "50" in cli_args
        assert "--restart" in cli_args

    @pytest.mark.asyncio
    async def test_minimal_invocation_omits_optional_args(self) -> None:
        """オプション未指定時に CLI 引数が最小限になること."""
        mock_result = {"placed": 1, "skipped": 0, "overwritten": 0, "errors": 0}

        mod = import_module("rag.server")
        with (
            patch("rag.server.tools.ingest_site.safe_browsing_wiring._get_safe_browsing_client", return_value=None),
            patch("rag.server.cli_subprocess._run_cli_subprocess", new_callable=AsyncMock, return_value=mock_result) as mock_cli,
            patch("rag.server.cli_subprocess._format_cli_ingest_result", return_value="OK"),
        ):
            await mod.rag_site_crawl(url="https://example.com")
        cli_args = mock_cli.call_args[0][1]
        assert "--url-pattern" not in cli_args
        assert "--max-pages" not in cli_args
        assert "--restart" not in cli_args


# --- CLI コマンド site-ingest（取得入口）テスト ---


class TestCliSiteIngestValidation:
    """CLI run_site_ingest の入力バリデーションテスト."""

    @pytest.mark.asyncio
    async def test_empty_url_exits(self) -> None:
        """空 URL で sys.exit(1) すること."""
        from rag.cli import run_site_ingest

        args = argparse.Namespace(url=[""], skip_pipeline=False)
        with pytest.raises(SystemExit) as exc_info:
            await run_site_ingest(args)
        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_invalid_scheme_exits(self) -> None:
        """不正スキームで sys.exit(1) すること."""
        from rag.cli import run_site_ingest

        args = argparse.Namespace(url=["ftp://example.com"], skip_pipeline=False)
        with pytest.raises(SystemExit) as exc_info:
            await run_site_ingest(args)
        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_ssrf_private_ip_exits(self) -> None:
        """プライベート IP で sys.exit(1) すること."""
        from rag.cli import run_site_ingest

        args = argparse.Namespace(url=["http://10.0.0.1/page"], skip_pipeline=False)
        with pytest.raises(SystemExit) as exc_info:
            await run_site_ingest(args)
        assert exc_info.value.code == 1


class TestCliSiteIngestFlow:
    """CLI run_site_ingest の統合フローテスト."""

    @pytest.mark.asyncio
    async def test_single_url_does_not_invoke_crawl_mode(self) -> None:
        """**Issue #797 回帰**: 単一 URL でもリンク辿りクロールを発動しない.

        WebIngester.fetch_urls 経由（start_urls 系統、no_follow=True）で呼ばれる
        ことを ScrapyRunner.run の kwargs で確認する。
        """
        from rag.scrapy.runner import CrawlResult, RealScrapyRunner

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/test"),
            jsonl_path=Path("/tmp/test/nonexistent.jsonl"),
            success=True,
        )
        args = argparse.Namespace(
            url=["https://example.com/page"], skip_pipeline=False,
        )
        with (
            patch.object(RealScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result) as mock_run,
            patch("rag.cli._build_cli_pipeline_controller", return_value=(MagicMock(), _make_cli_mock_settings())),
        ):
            from rag.cli import run_site_ingest

            await run_site_ingest(args)
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs.get("start_urls") == ["https://example.com/page"]
            assert "start_url" not in call_kwargs

    @pytest.mark.asyncio
    async def test_no_jsonl_returns_without_bridge(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """JSONL 未出力時に Bridge を呼ばず早期リターンすること."""
        from rag.scrapy.runner import CrawlResult, RealScrapyRunner

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/test"),
            jsonl_path=Path("/tmp/test/nonexistent.jsonl"),
            success=True,
        )
        args = argparse.Namespace(
            url=["https://example.com"], skip_pipeline=False,
        )
        with (
            patch.object(RealScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result),
            patch("rag.cli._build_cli_pipeline_controller", return_value=(MagicMock(), _make_cli_mock_settings())),
        ):
            from rag.cli import run_site_ingest

            await run_site_ingest(args)
            captured = capsys.readouterr()
            assert "メタデータが出力されませんでした" in captured.out

    @pytest.mark.asyncio
    async def test_text_output_uses_ingest_result_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """site-ingest の text 出力が IngestResult.summary() を経由して partial_failures を可視化すること.

        他 ingest コマンドと同じく `_print_ingest_result` 経由で出力される
        ことにより、`partial_failures` / `aborted` 等の観測性フィールドが
        欠落なく表示される。`_print_ingest_result` 内で partial_failures だけ
        抜け落ちる退化を防ぐための observability 不変条件テスト。
        """
        from rag.pipeline.ingesters._common import IngestResult
        from rag.scrapy.bridge import BridgeResult
        from rag.scrapy.runner import CrawlResult, RealScrapyRunner

        jsonl_path = tmp_path / "metadata.jsonl"
        jsonl_path.write_text(
            json.dumps({
                "url": "https://example.com/p", "title": "T", "status": 200,
                "depth": 0, "collected_at": "2025-01-01T00:00:00Z",
                "filepath": "p.html",
            }) + "\n",
            encoding="utf-8",
        )
        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=tmp_path,
            jsonl_path=jsonl_path,
            success=True,
        )
        mock_bridge_result = BridgeResult(
            ingest=IngestResult(
                placed=2,
                skipped=0,
                partial_failures=1,
                partial_failure_details=[
                    {"target": "https://example.com/img.png", "category": "media_download"},
                ],
            ),
            total_lines=2,
            parse_errors=0,
        )
        mock_pipeline_summary = MagicMock()
        mock_pipeline_summary.processed = 2
        mock_pipeline_summary.errors = []
        mock_pipeline_summary.warnings = []
        mock_controller = MagicMock()
        mock_controller.source_store = MagicMock()
        mock_controller.ingest_and_index = AsyncMock(
            return_value=mock_pipeline_summary,
        )
        args = argparse.Namespace(
            url=["https://example.com"], skip_pipeline=False,
        )
        with (
            patch.object(RealScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result),
            patch("rag.cli._build_cli_pipeline_controller", return_value=(mock_controller, _make_cli_mock_settings())),
            patch("rag.pipeline.ingesters.web._facade.import_to_source_store", return_value=mock_bridge_result),
        ):
            from rag.cli import run_site_ingest

            await run_site_ingest(args)
            captured = capsys.readouterr()
            # summary() の特徴的な出力（partial_failures が IngestResult.summary で可視化される）
            assert "部分失敗: 1件" in captured.out
            # context（サイト: ...）が summary に含まれる
            assert "サイト:" in captured.out


# --- CLI コマンド site-crawl（クロール入口）テスト ---


class TestCliSiteCrawlValidation:
    """CLI run_site_crawl の入力バリデーションテスト."""

    @pytest.mark.asyncio
    async def test_invalid_scheme_exits(self) -> None:
        """不正スキームで sys.exit(1) すること."""
        from rag.cli import run_site_crawl

        args = argparse.Namespace(
            url="ftp://example.com",
            url_pattern="",
            max_pages=None,
            restart=False,
            skip_pipeline=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            await run_site_crawl(args)
        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_ssrf_private_ip_exits(self) -> None:
        """プライベート IP で sys.exit(1) すること."""
        from rag.cli import run_site_crawl

        args = argparse.Namespace(
            url="http://10.0.0.1/page",
            url_pattern="",
            max_pages=None,
            restart=False,
            skip_pipeline=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            await run_site_crawl(args)
        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_invalid_regex_pattern_exits(self) -> None:
        """不正な正規表現パターンで sys.exit(1) すること."""
        from rag.cli import run_site_crawl

        args = argparse.Namespace(
            url="https://example.com",
            url_pattern="[bad(",
            max_pages=None,
            restart=False,
            skip_pipeline=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            await run_site_crawl(args)
        assert exc_info.value.code == 1


class TestCliSiteCrawlMaxPagesClamp:
    """CLI run_site_crawl の max_pages クランプ処理テスト."""

    @pytest.mark.asyncio
    async def test_max_pages_clamped_to_1_when_negative(self) -> None:
        """max_pages=-1 が 1 にクランプされること."""
        from rag.scrapy.runner import CrawlResult, RealScrapyRunner

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/test"),
            jsonl_path=Path("/tmp/test/nonexistent.jsonl"),
            success=True,
        )
        args = argparse.Namespace(
            url="https://example.com",
            url_pattern="",
            max_pages=-1,
            restart=False,
            skip_pipeline=False,
        )
        with (
            patch.object(RealScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result) as mock_run,
            patch("rag.cli._build_cli_pipeline_controller", return_value=(MagicMock(), _make_cli_mock_settings())),
        ):
            from rag.cli import run_site_crawl

            await run_site_crawl(args)
            assert mock_run.call_args.kwargs["max_pages"] == 1

    @pytest.mark.asyncio
    async def test_max_pages_clamped_to_1000(self) -> None:
        """max_pages=999999 が 1000 にクランプされること."""
        from rag.scrapy.runner import CrawlResult, RealScrapyRunner

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/test"),
            jsonl_path=Path("/tmp/test/nonexistent.jsonl"),
            success=True,
        )
        args = argparse.Namespace(
            url="https://example.com",
            url_pattern="",
            max_pages=999999,
            restart=False,
            skip_pipeline=False,
        )
        with (
            patch.object(RealScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result) as mock_run,
            patch("rag.cli._build_cli_pipeline_controller", return_value=(MagicMock(), _make_cli_mock_settings())),
        ):
            from rag.cli import run_site_crawl

            await run_site_crawl(args)
            assert mock_run.call_args.kwargs["max_pages"] == 1000

    @pytest.mark.asyncio
    async def test_max_pages_uses_settings_default(self) -> None:
        """max_pages 未指定時に設定値が使われること."""
        from rag.scrapy.runner import CrawlResult, RealScrapyRunner

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/test"),
            jsonl_path=Path("/tmp/test/nonexistent.jsonl"),
            success=True,
        )
        mock_settings = _make_cli_mock_settings()
        mock_settings.site_ingest_max_pages = 800
        args = argparse.Namespace(
            url="https://example.com",
            url_pattern="",
            max_pages=None,
            restart=False,
            skip_pipeline=False,
        )
        with (
            patch.object(RealScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result) as mock_run,
            patch("rag.cli._build_cli_pipeline_controller", return_value=(MagicMock(), mock_settings)),
        ):
            from rag.cli import run_site_crawl

            await run_site_crawl(args)
            assert mock_run.call_args.kwargs["max_pages"] == 800


class TestCliSiteCrawlFlow:
    """CLI run_site_crawl の統合フローテスト."""

    @pytest.mark.asyncio
    async def test_no_jsonl_returns_without_bridge(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """JSONL 未出力時に Bridge を呼ばず早期リターンすること."""
        from rag.scrapy.runner import CrawlResult, RealScrapyRunner

        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=Path("/tmp/test"),
            jsonl_path=Path("/tmp/test/nonexistent.jsonl"),
            success=True,
        )
        args = argparse.Namespace(
            url="https://example.com",
            url_pattern="",
            max_pages=10,
            restart=False,
            skip_pipeline=False,
        )
        with (
            patch.object(RealScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result),
            patch("rag.cli._build_cli_pipeline_controller", return_value=(MagicMock(), _make_cli_mock_settings())),
        ):
            from rag.cli import run_site_crawl

            await run_site_crawl(args)
            captured = capsys.readouterr()
            assert "メタデータが出力されませんでした" in captured.out

    @pytest.mark.asyncio
    async def test_successful_crawl_prints_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """正常なクロールで結果サマリーが出力されること."""
        from rag.pipeline.ingesters._common import IngestResult
        from rag.scrapy.bridge import BridgeResult
        from rag.scrapy.runner import CrawlResult, RealScrapyRunner

        jsonl_path = tmp_path / "metadata.jsonl"
        jsonl_path.write_text(
            json.dumps({
                "url": "https://example.com/p", "title": "T", "status": 200,
                "depth": 0, "collected_at": "2025-01-01T00:00:00Z",
                "filepath": "p.html",
            }) + "\n",
            encoding="utf-8",
        )
        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=tmp_path,
            jsonl_path=jsonl_path,
            success=True,
        )
        mock_bridge_result = BridgeResult(
            ingest=IngestResult(placed=5, skipped=2, overwritten=3, errors=1),
            total_lines=8,
            parse_errors=0,
        )
        mock_pipeline_summary = MagicMock()
        mock_pipeline_summary.processed = 5
        mock_pipeline_summary.errors = []
        mock_pipeline_summary.warnings = []
        mock_controller = MagicMock()
        mock_controller.source_store = MagicMock()
        mock_controller.ingest_and_index = AsyncMock(
            return_value=mock_pipeline_summary,
        )
        args = argparse.Namespace(
            url="https://example.com",
            url_pattern="",
            max_pages=10,
            restart=False,
            skip_pipeline=False,
        )
        with (
            patch.object(RealScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result),
            patch("rag.cli._build_cli_pipeline_controller", return_value=(mock_controller, _make_cli_mock_settings())),
            patch("rag.pipeline.ingesters.web._facade.import_to_source_store", return_value=mock_bridge_result),
        ):
            from rag.cli import run_site_crawl

            await run_site_crawl(args)
            captured = capsys.readouterr()
            assert "完了: 5件配置" in captured.out
            assert "上書き: 3件" in captured.out
            assert "スキップ: 2件" in captured.out
            assert "エラー: 1件" in captured.out
            assert "パイプライン: 5件処理" in captured.out

    @pytest.mark.asyncio
    async def test_text_output_uses_ingest_result_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """site-crawl の text 出力が IngestResult.summary() を経由して partial_failures を可視化すること.

        他 ingest コマンドと同じく `_print_ingest_result` 経由で出力される
        ことにより、`partial_failures` / `aborted` 等の観測性フィールドが
        欠落なく表示される。`_print_ingest_result` 内で partial_failures だけ
        抜け落ちる退化を防ぐための observability 不変条件テスト。
        """
        from rag.pipeline.ingesters._common import IngestResult
        from rag.scrapy.bridge import BridgeResult
        from rag.scrapy.runner import CrawlResult, RealScrapyRunner

        jsonl_path = tmp_path / "metadata.jsonl"
        jsonl_path.write_text(
            json.dumps({
                "url": "https://example.com/p", "title": "T", "status": 200,
                "depth": 0, "collected_at": "2025-01-01T00:00:00Z",
                "filepath": "p.html",
            }) + "\n",
            encoding="utf-8",
        )
        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=tmp_path,
            jsonl_path=jsonl_path,
            success=True,
        )
        mock_bridge_result = BridgeResult(
            ingest=IngestResult(
                placed=2,
                skipped=0,
                partial_failures=1,
                partial_failure_details=[
                    {"target": "https://example.com/img.png", "category": "media_download"},
                ],
            ),
            total_lines=2,
            parse_errors=0,
        )
        mock_pipeline_summary = MagicMock()
        mock_pipeline_summary.processed = 2
        mock_pipeline_summary.errors = []
        mock_pipeline_summary.warnings = []
        mock_controller = MagicMock()
        mock_controller.source_store = MagicMock()
        mock_controller.ingest_and_index = AsyncMock(
            return_value=mock_pipeline_summary,
        )
        args = argparse.Namespace(
            url="https://example.com",
            url_pattern="",
            max_pages=10,
            restart=False,
            skip_pipeline=False,
        )
        with (
            patch.object(RealScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result),
            patch("rag.cli._build_cli_pipeline_controller", return_value=(mock_controller, _make_cli_mock_settings())),
            patch("rag.pipeline.ingesters.web._facade.import_to_source_store", return_value=mock_bridge_result),
        ):
            from rag.cli import run_site_crawl

            await run_site_crawl(args)
            captured = capsys.readouterr()
            assert "部分失敗: 1件" in captured.out
            assert "サイト:" in captured.out

    @pytest.mark.asyncio
    async def test_cleanup_on_success(self, tmp_path: Path) -> None:
        """正常完了時にクロールディレクトリが削除されること."""
        from rag.pipeline.ingesters._common import IngestResult
        from rag.scrapy.bridge import BridgeResult
        from rag.scrapy.runner import CrawlResult, RealScrapyRunner

        crawl_dir = tmp_path / "crawl"
        crawl_dir.mkdir()
        (crawl_dir / "dummy.txt").write_text("data", encoding="utf-8")
        jsonl_path = tmp_path / "metadata.jsonl"
        jsonl_path.write_text(
            json.dumps({
                "url": "https://example.com/p", "title": "T", "status": 200,
                "depth": 0, "collected_at": "2025-01-01T00:00:00Z",
                "filepath": "p.html",
            }) + "\n",
            encoding="utf-8",
        )
        mock_crawl_result = CrawlResult(
            exit_code=0,
            output_dir=tmp_path,
            jsonl_path=jsonl_path,
            success=True,
            crawl_dir=crawl_dir,
        )
        mock_bridge_result = BridgeResult(
            ingest=IngestResult(placed=1, skipped=0, errors=0),
            total_lines=1,
            parse_errors=0,
        )
        mock_pipeline_summary = MagicMock()
        mock_pipeline_summary.processed = 1
        mock_pipeline_summary.errors = []
        mock_controller = MagicMock()
        mock_controller.source_store = MagicMock()
        mock_controller.ingest_and_index = AsyncMock(
            return_value=mock_pipeline_summary,
        )
        args = argparse.Namespace(
            url="https://example.com",
            url_pattern="",
            max_pages=10,
            restart=False,
            skip_pipeline=False,
        )
        with (
            patch.object(RealScrapyRunner, "run", new_callable=AsyncMock, return_value=mock_crawl_result),
            patch("rag.cli._build_cli_pipeline_controller", return_value=(mock_controller, _make_cli_mock_settings())),
            patch("rag.pipeline.ingesters.web._facade.import_to_source_store", return_value=mock_bridge_result),
        ):
            from rag.cli import run_site_crawl

            await run_site_crawl(args)
            assert not crawl_dir.exists()

"""再構築・統計ツールのテスト.

仕様: docs/specs/rebuild-stats.md

rag_rebuild MCP ツール・CLI、rag_stats 拡張の振る舞いを検証する。
"""

from __future__ import annotations

from importlib import import_module
from unittest.mock import AsyncMock, patch

import pytest

from rag.cli import _format_elapsed
from rag.pipeline.models import PipelineMode, PipelineSummary
from rag.rag_knowledge import format_file_size
from rag.server import (
    CLISubprocessError,
    _format_rebuild_summary,
)


# --- format_file_size テスト ---


class TestFormatSize:
    """サイズ表示の自動単位変換テスト."""

    def test_bytes(self) -> None:
        assert format_file_size(0) == "0 B"
        assert format_file_size(512) == "512 B"
        assert format_file_size(1023) == "1023 B"

    def test_kilobytes(self) -> None:
        assert format_file_size(1024) == "1.0 KB"
        assert format_file_size(1536) == "1.5 KB"

    def test_megabytes(self) -> None:
        assert format_file_size(1024 * 1024) == "1.0 MB"
        assert format_file_size(int(45.6 * 1024 * 1024)) == "45.6 MB"

    def test_gigabytes(self) -> None:
        assert format_file_size(1024 * 1024 * 1024) == "1.0 GB"
        assert format_file_size(int(2.5 * 1024 * 1024 * 1024)) == "2.5 GB"


# --- _format_rebuild_summary テスト ---


class TestFormatRebuildSummary:
    """再構築サマリのフォーマットテスト."""

    def test_success_summary(self) -> None:
        summary = PipelineSummary(
            mode=PipelineMode.FULL_REBUILD,
            total_files=10,
            processed=8,
            skipped=2,
        )
        result = _format_rebuild_summary(summary, 12.5)
        assert "全再構築" in result
        assert "処理件数: 8" in result
        assert "スキップ: 2" in result
        assert "エラー: 0" in result
        assert "12.5 秒" in result

    def test_with_errors(self) -> None:
        summary = PipelineSummary(
            mode=PipelineMode.INDEX_ONLY,
            total_files=5,
            processed=3,
            skipped=2,
            errors=["file1.txt", "file2.txt"],
        )
        result = _format_rebuild_summary(summary, 5.0)
        assert "インデックスのみ再構築" in result
        assert "エラー: 2" in result
        assert "file1.txt" in result
        assert "file2.txt" in result

    def test_all_modes(self) -> None:
        mode_labels = {
            PipelineMode.FULL_REBUILD: "全再構築",
            PipelineMode.CONVERT_ONLY: "コンバートのみ再実行",
            PipelineMode.INDEX_ONLY: "インデックスのみ再構築",
            PipelineMode.INCREMENTAL: "差分更新",
        }
        for mode, label in mode_labels.items():
            summary = PipelineSummary(
                mode=mode, total_files=0, processed=0, skipped=0,
            )
            result = _format_rebuild_summary(summary, 0.0)
            assert label in result


# --- rag_rebuild MCP ツールテスト ---


class TestRagRebuild:
    """rag_rebuild MCP ツールのテスト（_run_cli_subprocess 経由）."""

    @pytest.mark.asyncio
    async def test_full_rebuild_success(self) -> None:
        """CLI サブプロセス経由の full rebuild が正常結果を返すこと."""
        from rag.server import rag_rebuild

        mock_cli_result: dict[str, object] = {
            "type": "result",
            "mode": "full",
            "total_files": 5,
            "processed": 5,
            "skipped": 0,
            "errors": [],
            "elapsed": 1.2,
        }

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=mock_cli_result):
            result = await rag_rebuild(mode="full")

        assert "再構築完了" in result
        assert "全再構築" in result
        assert "処理件数: 5" in result

    @pytest.mark.asyncio
    async def test_rebuild_with_source_type(self) -> None:
        """source_type 指定の rebuild が正しい引数で CLI を呼ぶこと."""
        from rag.server import rag_rebuild

        mock_cli_result: dict[str, object] = {
            "type": "result",
            "mode": "convert",
            "total_files": 3,
            "processed": 3,
            "skipped": 0,
            "errors": [],
            "elapsed": 0.5,
        }

        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            return_value=mock_cli_result,
        ) as mock_subprocess:
            result = await rag_rebuild(mode="convert", source_type="web")

        assert "再構築完了" in result
        mock_subprocess.assert_called_once_with(
            "rebuild", ["--mode", "convert", "--source-type", "web"], ctx=None,
        )

    @pytest.mark.asyncio
    async def test_incremental_mode(self) -> None:
        """incremental モードが正常に動作すること."""
        from rag.server import rag_rebuild

        mock_cli_result: dict[str, object] = {
            "type": "result",
            "mode": "incremental",
            "total_files": 2,
            "processed": 2,
            "skipped": 0,
            "errors": [],
            "elapsed": 0.3,
        }

        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            return_value=mock_cli_result,
        ) as mock_subprocess:
            result = await rag_rebuild(mode="incremental")

        assert "差分更新" in result
        mock_subprocess.assert_called_once_with(
            "rebuild", ["--mode", "incremental"], ctx=None,
        )

    @pytest.mark.asyncio
    async def test_lock_conflict_returns_error(self) -> None:
        """ロック競合時にエラーメッセージを返すこと."""
        from rag.server import rag_rebuild

        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError("ロック競合", lock_conflict=True),
        ):
            result = await rag_rebuild(mode="full")

        assert "別の再構築が実行中" in result

    @pytest.mark.asyncio
    async def test_cli_error_returns_error(self) -> None:
        """CLI サブプロセスエラー時にエラーメッセージを返すこと."""
        from rag.server import rag_rebuild

        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError("subprocess failed"),
        ):
            result = await rag_rebuild(mode="full")

        assert "エラー" in result

    @pytest.mark.asyncio
    async def test_unexpected_exception_returns_error(self) -> None:
        """予期しない例外時にエラーメッセージを返すこと."""
        from rag.server import rag_rebuild

        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected"),
        ):
            result = await rag_rebuild(mode="full")

        assert "エラー" in result


# --- rag_stats MCP ツールテスト ---


class TestRagStats:
    """rag_stats MCP ツール（CLI 委譲版）のテスト."""

    @pytest.mark.asyncio
    async def test_stats_output_format(self) -> None:
        """4セクション構成の出力フォーマット."""
        from rag.server import rag_stats

        mock_cli_result: dict[str, object] = {
            "source_store": {"total_files": 5, "total_size": 1024},
            "converted_store": {"total_files": 3, "total_size": 512},
            "index": {"total_chunks": 100, "source_count": 10},
            "pipeline": {"last_processed_at": None, "run_count": 0, "last_commit_id": None, "deleted_count": 0},
        }

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=mock_cli_result):
            result = await rag_stats()

        assert "📊 RAG Knowledge 統計" in result
        assert "■ source_store" in result
        assert "■ converted_store" in result
        assert "■ インデックス" in result
        assert "■ パイプライン" in result

    @pytest.mark.asyncio
    async def test_stats_source_store_unset(self) -> None:
        """source_store 未設定時は「未設定」表示."""
        from rag.server import rag_stats

        mock_cli_result: dict[str, object] = {
            "source_store": {"status": "unconfigured"},
            "converted_store": {"status": "unconfigured"},
            "index": {"total_chunks": 0, "source_count": 0},
            "pipeline": {"status": "unconfigured"},
        }

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=mock_cli_result):
            result = await rag_stats()

        lines = result.split("\n")
        ss_idx = next(
            i for i, line in enumerate(lines) if "■ source_store" in line
        )
        assert "未設定" in lines[ss_idx + 1]

    @pytest.mark.asyncio
    async def test_stats_with_source_store_data(self) -> None:
        """source_store に実データがある場合の統計表示."""
        from rag.server import rag_stats

        mock_cli_result: dict[str, object] = {
            "source_store": {
                "total_files": 1,
                "total_size": 100,
                "by_type": {"web": {"files": 1, "size": 100}},
            },
            "converted_store": {"total_files": 0, "total_size": 0},
            "index": {"total_chunks": 0, "source_count": 0},
            "pipeline": {"status": "uninitialized"},
        }

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=mock_cli_result):
            result = await rag_stats()

        assert "総ファイル数: 1" in result
        assert "web:" in result

    @pytest.mark.asyncio
    async def test_stats_index_section(self) -> None:
        """インデックスセクションの統計表示."""
        from rag.server import rag_stats

        mock_cli_result: dict[str, object] = {
            "source_store": {"status": "unconfigured"},
            "converted_store": {"status": "unconfigured"},
            "index": {"total_chunks": 1234, "source_count": 120},
            "pipeline": {"status": "unconfigured"},
        }

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=mock_cli_result):
            result = await rag_stats()

        assert "総チャンク数: 1,234" in result
        assert "ソース数: 120" in result

    @pytest.mark.asyncio
    async def test_stats_pipeline_section(self) -> None:
        """パイプラインセクションの統計表示."""
        from rag.server import rag_stats

        mock_cli_result: dict[str, object] = {
            "source_store": {"total_files": 0, "total_size": 0},
            "converted_store": {"total_files": 0, "total_size": 0},
            "index": {"total_chunks": 0, "source_count": 0},
            "pipeline": {
                "last_processed_at": "2026-03-19T10:30:00+09:00",
                "run_count": 1,
                "last_commit_id": "bbbcccc",
                "deleted_count": 0,
            },
        }

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=mock_cli_result):
            result = await rag_stats()

        assert "最終処理: 2026-03-19T10:30:00+09:00" in result
        assert "実行回数: 1" in result
        assert "last_commit_id: bbbcccc" in result
        assert "論理削除: 0 件" in result

    @pytest.mark.asyncio
    async def test_stats_pipeline_uninitialized(self) -> None:
        """metadata.db が存在しない場合「未初期化」表示."""
        from rag.server import rag_stats

        mock_cli_result: dict[str, object] = {
            "source_store": {"total_files": 0, "total_size": 0},
            "converted_store": {"total_files": 0, "total_size": 0},
            "index": {"total_chunks": 0, "source_count": 0},
            "pipeline": {"status": "uninitialized"},
        }

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=mock_cli_result):
            result = await rag_stats()

        lines = result.split("\n")
        pl_idx = next(
            i for i, line in enumerate(lines) if "■ パイプライン" in line
        )
        assert "未初期化" in lines[pl_idx + 1]

    @pytest.mark.asyncio
    async def test_stats_cli_error(self) -> None:
        """CLI サブプロセスエラー時にエラーメッセージを返すこと."""
        from rag.server import rag_stats

        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError("subprocess failed"),
        ):
            result = await rag_stats()

        assert "エラー" in result


# --- ツール数テスト ---


@pytest.mark.asyncio
async def test_rag_server_exposes_tools() -> None:
    """RAG MCPサーバーが18個のツールを公開すること."""
    mod = import_module("rag.server")
    server = mod.mcp

    tools = await server.list_tools()
    tool_names = {t.name for t in tools}

    expected = {
        "rag_search", "rag_get_document",
        "rag_crawl_zenn", "rag_crawl_bluesky",
        "rag_add_youtube", "rag_crawl_youtube",
        "rag_add_document", "rag_add_journal", "rag_crawl_documents",
        "rag_site_ingest",
        "rag_update_aozora_catalog", "rag_search_aozora",
        "rag_add_aozora", "rag_crawl_aozora",
        "rag_delete", "rag_rebuild", "rag_stats",
        "rag_list_recent",
    }
    assert tool_names == expected, f"Expected {expected}, got {tool_names}"


# --- _format_elapsed テスト ---


class TestFormatElapsed:
    """所要時間の時分秒表記テスト."""

    def test_seconds_only(self) -> None:
        assert _format_elapsed(3.2) == "3.2 秒"
        assert _format_elapsed(59.9) == "59.9 秒"

    def test_minutes_and_seconds(self) -> None:
        assert _format_elapsed(60.0) == "1 分 0.0 秒"
        assert _format_elapsed(135.5) == "2 分 15.5 秒"

    def test_hours_minutes_seconds(self) -> None:
        assert _format_elapsed(3930.0) == "1 時間 5 分 30.0 秒"

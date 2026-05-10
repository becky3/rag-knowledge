"""Aozora 取り込みの L2 Mock E2E テスト.

仕様: docs/specs/workflows/qa-strategy.md
仕様: docs/specs/infrastructure/fake-adapters/aozora.md

MCP server / CLI を実プロセスで起動し、青空文庫インジェスト → search の
パイプライン全体が subprocess 越境環境で正しく動作することを検証する。

外部 HTTP（青空文庫カタログ ZIP / GitHub Raw XHTML）は FakeAozoraFetcher、
Embedding は FakeEmbedding を DI ファクトリ経由で注入する。
"""

from __future__ import annotations

import subprocess

import pytest

from ._mcp_helpers import call_mcp_tool
from .conftest import run_cli


pytestmark = pytest.mark.e2e


# Fake Fetcher の happy fixture が含む synthetic ID（fake-mode.md の規約）
_FAKE_BOOK_ID = "999900"
_FAKE_PERSON_ID = "99999"


class TestMcpAozoraIngest:
    """MCP 経由の Aozora インジェスト動作確認."""

    async def test_update_catalog_then_add_work(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """rag_update_aozora_catalog → rag_add_aozora の連続動作."""
        catalog_response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_update_aozora_catalog",
            {},
        )
        assert "カタログ" in catalog_response, (
            f"カタログ更新応答が不正: {catalog_response[:500]}"
        )

        add_response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_add_aozora",
            {"book_ids": [_FAKE_BOOK_ID]},
        )
        assert "完了" in add_response or "placed" in add_response.lower(), (
            f"作品取り込みが完了していない: {add_response[:500]}"
        )

    async def test_response_contains_fake_mode_label(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """fake モード時、Aozora 応答冒頭に [FAKE MODE: aozora] ラベルが付与される."""
        response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_update_aozora_catalog",
            {},
        )
        assert "[FAKE MODE: aozora]" in response, (
            f"fake モードラベルが応答に含まれていない: {response[:500]}"
        )


class TestCliAozoraIngest:
    """CLI 経由の Aozora インジェスト動作確認."""

    def test_cli_update_catalog_then_ingest_aozora(
        self,
        e2e_subprocess_env: dict[str, str],
        e2e_mcp_server: str,  # noqa: ARG002 - ChromaDB auto_start のため依存
    ) -> None:
        """CLI で update-aozora-catalog → ingest-aozora が連続実行できる."""
        update_result: subprocess.CompletedProcess[str] = run_cli(
            ["update-aozora-catalog"],
            env=e2e_subprocess_env,
            timeout=60.0,
        )
        assert update_result.returncode == 0, (
            f"カタログ更新が失敗: returncode={update_result.returncode}\n"
            f"stdout:\n{update_result.stdout}\nstderr:\n{update_result.stderr}"
        )

        ingest_result: subprocess.CompletedProcess[str] = run_cli(
            ["ingest-aozora", _FAKE_BOOK_ID],
            env=e2e_subprocess_env,
            timeout=120.0,
        )
        assert ingest_result.returncode == 0, (
            f"作品取り込みが失敗: returncode={ingest_result.returncode}\n"
            f"stdout:\n{ingest_result.stdout}\nstderr:\n{ingest_result.stderr}"
        )
        combined = ingest_result.stdout + ingest_result.stderr
        assert "完了" in combined or "placed" in combined.lower(), (
            f"CLI 出力に取り込み完了の証跡なし:\n{combined[:1000]}"
        )

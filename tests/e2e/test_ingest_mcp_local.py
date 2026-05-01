"""Local 取り込みの L2 Mock E2E テスト.

仕様: docs/specs/workflows/qa-strategy.md
仕様: docs/specs/infrastructure/fake-adapters/local.md

MCP server / CLI を実プロセスで起動し、Local インジェスト（ファイル添付）
→ search のパイプライン全体が subprocess 越境環境で正しく動作することを検証する。

LocalFetcher は filesystem 抽象化のみを担い、PDF/AsciiDoc 抽出は converter 層の責務。
本 e2e は filesystem アクセスの fake 注入経路（環境変数強制）と MCP 応答ラベルの
検証に絞る。converter Fake 化（QA 速度向上）は #713 で対応する。
"""

from __future__ import annotations

import pytest

from ._mcp_helpers import call_mcp_tool


pytestmark = pytest.mark.e2e


_SAMPLE_MARKDOWN_CONTENT = """# Test Document

E2E synthetic Markdown content for Local ingester verification.
"""


class TestMcpLocalIngest:
    """MCP 経由の Local インジェスト動作確認."""

    async def test_add_document_succeeds(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """rag_add_document で stdin 越しにファイルを取り込める."""
        response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_add_document",
            {
                "content": _SAMPLE_MARKDOWN_CONTENT,
                "filename": "e2e_test_document.md",
                "encoding": "text",
                "upload_mode": "replace",
            },
        )
        assert "完了" in response or "placed" in response.lower(), (
            f"取り込み完了を示すテキストが応答に含まれていない: {response[:500]}"
        )

    async def test_response_contains_fake_mode_label(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """fake モード時、Local 応答冒頭に [FAKE MODE: local] ラベルが付与される."""
        response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_add_document",
            {
                "content": _SAMPLE_MARKDOWN_CONTENT,
                "filename": "e2e_label_test.md",
                "encoding": "text",
                "upload_mode": "replace",
            },
        )
        assert "[FAKE MODE: local]" in response, (
            f"fake モードラベルが応答に含まれていない: {response[:500]}"
        )

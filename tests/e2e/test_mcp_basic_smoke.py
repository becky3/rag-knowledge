"""MCP server の基本疎通テスト（外部アクセス完全ゼロ）.

rag_stats は read-only で ChromaDB / SQLite を読むだけ、外部 API 接続なし、
Fake モード判定にも依存しない。MCP → CLI subprocess → 応答返却の経路を
最小コストで検証する目的。

このテストが hang する場合は、MCP/CLI subprocess 通信そのものに問題が
あることを示す（Fake モード設定や個別 fetcher の問題ではない）。
"""

from __future__ import annotations

import pytest

from ._mcp_helpers import call_mcp_tool


pytestmark = pytest.mark.e2e


class TestMcpBasicSmoke:
    async def test_rag_stats_returns_response(
        self, e2e_mcp_server: str,
    ) -> None:
        """rag_stats（外部アクセスゼロ・read-only）が応答を返す."""
        response = await call_mcp_tool(e2e_mcp_server, "rag_stats", {})
        assert response.strip(), (
            f"rag_stats が空応答（MCP/CLI subprocess 通信の問題）: {response[:300]}"
        )

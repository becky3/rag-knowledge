"""e2e テスト共通の MCP ツール呼び出しヘルパー.

仕様: docs/specs/workflows/qa-strategy.md

L2 Mock E2E テスト（``test_ingest_mcp_*.py`` / ``test_mcp_basic_smoke.py``）から
共通利用される。MCP server を HTTP モードで起動し、Streamable HTTP クライアントで
ツールを呼び出してテキスト応答を返す。

各テストファイルでヘルパーを再定義していた DRY 違反を解消するため、
本モジュールに集約する（PR #704 のレビュー指摘 R-C4 対応）。
"""

from __future__ import annotations

from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def call_mcp_tool(
    base_url: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """MCP server (HTTP モード) で指定ツールを呼び出し、テキスト応答を返す.

    Args:
        base_url: MCP server のベース URL（``e2e_mcp_server`` fixture から渡される）
        tool_name: 呼び出す MCP ツール名
        arguments: ツール引数

    Returns:
        応答テキスト（複数 content block がある場合は改行連結）
    """
    mcp_url = f"{base_url}/mcp"
    async with streamablehttp_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments=arguments)
            texts: list[str] = []
            for block in result.content:
                if hasattr(block, "text"):
                    texts.append(block.text)
            return "\n".join(texts)

"""e2e テスト共通の MCP ツール呼び出しヘルパー.

仕様: docs/specs/workflows/qa-strategy.md
"""

from __future__ import annotations

from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


# Unicode REPLACEMENT CHARACTER。文字化け検出のマーカーとして使う。
_MOJIBAKE_MARKER = "�"


def assert_no_mojibake(text: str, *, context: str = "response") -> None:
    """応答テキストに文字化けマーカー（U+FFFD）が含まれないこと.

    PYTHONUTF8=1 環境では本来発生しないため、検出時は subprocess 越境で
    encoding 違反が silent に発生していることを示す。
    """
    if _MOJIBAKE_MARKER in text:
        raise AssertionError(
            f"mojibake detected in {context} (U+FFFD found): {text[:500]}"
        )


async def call_mcp_tool(
    base_url: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """MCP server (HTTP モード) で指定ツールを呼び出し、テキスト応答を返す.

    応答テキストの mojibake 検出を自動適用する。
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
            response = "\n".join(texts)

    assert_no_mojibake(response, context=f"{tool_name} response")
    return response

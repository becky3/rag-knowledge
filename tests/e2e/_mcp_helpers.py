"""e2e テスト共通の MCP ツール呼び出しヘルパー.

仕様: docs/specs/workflows/qa-strategy.md
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


# Unicode REPLACEMENT CHARACTER。文字化け検出のマーカーとして使う。
_MOJIBAKE_MARKER = "�"

# 子プロセス stderr 監視の対象ログレベル正規表現。
# logging.basicConfig のフォーマット（`LEVEL - message` / `LEVEL:logger:message`）
# 末尾位置にあるレベル文字列のみを拾うことで、message 本文に "WARNING" 等の
# 単語が含まれる INFO ログでの誤検出を防ぐ。
_STDERR_VIOLATION_RE = re.compile(
    r"(?:^|\] |- )(WARNING|ERROR|CRITICAL)(?: - |:)"
)

# allowlist: 仕様上 WARNING レベルで出力されるが silent regression ではないログ。
# FAKE MODE 起動通知は fake-mode.md 仕様で WARNING 出力が必須なため除外する。
_STDERR_ALLOWLIST = (
    "[FAKE MODE:",
)


class McpServerHandle(NamedTuple):
    """MCP server fixture が yield するハンドル.

    Attributes:
        base_url: MCP server のベース URL（例: http://127.0.0.1:8081）
        stderr_lines: 子プロセス stderr の蓄積バッファ。drain thread によって
            常時追記される。テストは len(stderr_lines) でスナップショットを
            取り、ツール呼び出し前後の差分を assertion 対象にする
    """

    base_url: str
    stderr_lines: list[str]


def assert_no_mojibake(text: str, *, context: str = "response") -> None:
    """応答テキストに文字化けマーカー（U+FFFD）が含まれないこと.

    PYTHONUTF8=1 環境では本来発生しないため、検出時は subprocess 越境で
    encoding 違反が silent に発生していることを示す。
    """
    if _MOJIBAKE_MARKER in text:
        raise AssertionError(
            f"mojibake detected in {context} (U+FFFD found): {text[:500]}"
        )


def assert_no_stderr_warnings(
    new_lines: list[str], *, context: str = "tool call"
) -> None:
    """指定行範囲に WARNING / ERROR / CRITICAL ログが含まれないこと.

    fail-fast 環境下で想定外のログレベルが混入した場合は silent regression の
    兆候のため、テスト失敗で検出する。allowlist のパターンを含む行は除外する。
    """
    violations = [
        line
        for line in new_lines
        if _STDERR_VIOLATION_RE.search(line)
        and not any(allowed in line for allowed in _STDERR_ALLOWLIST)
    ]
    if violations:
        joined = "".join(violations)
        raise AssertionError(
            f"unexpected WARNING/ERROR in subprocess stderr during {context}:\n"
            f"{joined}"
        )


async def call_mcp_tool(
    server: McpServerHandle,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """MCP server (HTTP モード) で指定ツールを呼び出し、テキスト応答を返す.

    応答テキストの mojibake 検出 + 呼び出し中の子プロセス stderr
    WARNING/ERROR 監視を自動適用する。
    """
    stderr_snapshot = len(server.stderr_lines)

    mcp_url = f"{server.base_url}/mcp"
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
    new_lines = server.stderr_lines[stderr_snapshot:]
    assert_no_stderr_warnings(new_lines, context=f"{tool_name} call")

    return response

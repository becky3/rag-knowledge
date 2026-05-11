"""サーバー側の CLISubprocessError パーステスト.

仕様: docs/specs/infrastructure/content-upload.md (ロック種別の伝搬)

`_run_cli_subprocess` が CLI の error JSON（stdout）から
`details.lock_type` を読み取り、`CLISubprocessError.lock_type` 属性に
正しく詰めることを検証する。この伝搬が Upload HTTP API の 429/503 分岐と
MCP ツールのメッセージ切替の前提となる。

テスト方針:
- subprocess 起動は asyncio.create_subprocess_exec をモック
- stdout に error JSON 行を返し、exit_code=1 にする
- CLISubprocessError が送出され、lock_type が期待値になるか検証
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rag.server.cli_subprocess import (
    _STDOUT_LINE_BUFFER_LIMIT,
    CLISubprocessError,
    _run_cli_subprocess,
)


def _make_mock_process(stdout_lines: list[str], exit_code: int = 1) -> MagicMock:
    """asyncio.subprocess.Process をモックする.

    readline() で 1 行ずつ返し、stdout を使い切ったら空バイト列を返す。
    stderr は空、returncode は指定値。
    """
    mock_proc = MagicMock()
    mock_proc.returncode = exit_code

    # stdout.readline() を呼び出すたびに次の行を返す
    remaining_lines = [line.encode("utf-8") + b"\n" for line in stdout_lines] + [b""]

    async def _readline() -> bytes:
        return remaining_lines.pop(0) if remaining_lines else b""

    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline = _readline

    # stderr.read() は空を返す
    async def _stderr_read() -> bytes:
        return b""

    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read = _stderr_read

    # wait() / stdin
    async def _wait() -> int:
        return exit_code

    mock_proc.wait = _wait
    mock_proc.stdin = None  # stdin_data 未指定想定

    return mock_proc


def _make_error_line(
    *, code: str, message: str, lock_type: str | None = None,
) -> str:
    """CLI の error JSON 1 行を生成する."""
    payload: dict[str, object] = {
        "type": "error",
        "error": True,
        "code": code,
        "message": message,
    }
    if lock_type is not None:
        payload["details"] = {"lock_type": lock_type}
    return json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
class TestCLISubprocessParseLockType:
    """_run_cli_subprocess が error JSON から lock_type を拾うテスト."""

    async def test_parses_rebuild_lock_type(self) -> None:
        """lock_type='rebuild' を持つ error JSON → CLISubprocessError.lock_type='rebuild'."""
        error_line = _make_error_line(
            code="LOCK_CONFLICT",
            message="別の再構築が実行中です（ロック競合）",
            lock_type="rebuild",
        )
        mock_proc = _make_mock_process([error_line])
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            with pytest.raises(CLISubprocessError) as exc_info:
                await _run_cli_subprocess("add-journal", [])
        assert exc_info.value.code == "LOCK_CONFLICT"
        assert exc_info.value.lock_type == "rebuild"
        assert exc_info.value.lock_conflict is True

    async def test_parses_write_lock_type(self) -> None:
        """lock_type='write' を持つ error JSON → CLISubprocessError.lock_type='write'."""
        error_line = _make_error_line(
            code="LOCK_CONFLICT",
            message="別のインジェストが実行中です（ロック競合）",
            lock_type="write",
        )
        mock_proc = _make_mock_process([error_line])
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            with pytest.raises(CLISubprocessError) as exc_info:
                await _run_cli_subprocess("add-journal", [])
        assert exc_info.value.code == "LOCK_CONFLICT"
        assert exc_info.value.lock_type == "write"
        assert exc_info.value.lock_conflict is True

    async def test_no_details_defaults_to_none(self) -> None:
        """details キー欠落（後方互換）→ lock_type=None."""
        error_line = _make_error_line(
            code="LOCK_CONFLICT",
            message="ロック競合",
        )
        mock_proc = _make_mock_process([error_line])
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            with pytest.raises(CLISubprocessError) as exc_info:
                await _run_cli_subprocess("add-journal", [])
        assert exc_info.value.code == "LOCK_CONFLICT"
        assert exc_info.value.lock_type is None

    async def test_invalid_lock_type_value_defaults_to_none(self) -> None:
        """不正な lock_type 値（'rebuild'/'write' 以外）→ lock_type=None（ガード）."""
        # details.lock_type に不正値 "bogus" を入れる
        payload = {
            "type": "error",
            "error": True,
            "code": "LOCK_CONFLICT",
            "message": "ロック競合",
            "details": {"lock_type": "bogus"},
        }
        error_line = json.dumps(payload)
        mock_proc = _make_mock_process([error_line])
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            with pytest.raises(CLISubprocessError) as exc_info:
                await _run_cli_subprocess("add-journal", [])
        assert exc_info.value.lock_type is None


@pytest.mark.asyncio
class TestCLISubprocessStdoutReadLimit:
    """_run_cli_subprocess が asyncio StreamReader の行バッファ上限を引き上げるテスト.

    rag_get_document が 64KiB 超のドキュメントで asyncio のデフォルト
    `_DEFAULT_LIMIT` (65536) を超え `ValueError: Separator is found, but chunk
    is longer than limit` で失敗する問題 (Issue #777) の回避を保証する。
    """

    async def test_stdout_read_limit_is_at_least_10_mib(self) -> None:
        """`_STDOUT_LINE_BUFFER_LIMIT` 定数が 10 MiB 以上に設定されている."""
        assert _STDOUT_LINE_BUFFER_LIMIT >= 10 * 1024 * 1024

    async def test_passes_stdout_read_limit_to_subprocess_exec(self) -> None:
        """`create_subprocess_exec` に `limit=_STDOUT_LINE_BUFFER_LIMIT` を渡している."""
        result_line = json.dumps({"type": "result", "placed": 0})
        mock_proc = _make_mock_process([result_line], exit_code=0)
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ) as mock_exec:
            await _run_cli_subprocess("get-document", ["some-id"])
        assert mock_exec.await_count == 1
        assert mock_exec.await_args is not None
        assert mock_exec.await_args.kwargs.get("limit") == _STDOUT_LINE_BUFFER_LIMIT

    async def test_wraps_readline_value_error_as_cli_subprocess_error(self) -> None:
        """readline() の ValueError（行バッファ上限超過）を CLISubprocessError にラップする.

        asyncio.StreamReader.readline() は 1 行が limit を超えると
        `Separator is found, but chunk is longer than limit` の ValueError を投げる。
        MCP ツール側のエラー整形経路に乗せるため、CLISubprocessError へラップし、
        CLI `--output-file` での全文取得を案内するメッセージにする。

        例外経路でも子プロセスを kill()+wait() で確実に後始末することも検証する
        （ゾンビ化防止）。
        """
        mock_proc = MagicMock()
        mock_proc.returncode = None  # 未終了状態 → kill()+wait() 経路を通る

        async def _readline_raises() -> bytes:
            raise ValueError(
                "Separator is found, but chunk is longer than limit",
            )

        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline = _readline_raises

        async def _stderr_read() -> bytes:
            return b""

        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = _stderr_read

        wait_called = False

        async def _wait() -> int:
            nonlocal wait_called
            wait_called = True
            return 0

        mock_proc.wait = _wait
        mock_proc.stdin = None

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            with pytest.raises(CLISubprocessError) as exc_info:
                await _run_cli_subprocess("get-document", ["some-id"])

        # 期待メッセージは定数から算出して将来の上限変更に追従する
        expected_mib = _STDOUT_LINE_BUFFER_LIMIT // (1024 * 1024)
        message = str(exc_info.value)
        assert f"{expected_mib}MiB" in message
        assert "--output-file" in message

        # 子プロセスの後始末（ゾンビ化防止）が行われたこと
        mock_proc.kill.assert_called_once()
        assert wait_called is True


class TestCLISubprocessErrorFormatMcpError:
    """CLISubprocessError.format_mcp_error の lock_type 別メッセージテスト."""

    def test_rebuild_lock_returns_rebuild_message(self) -> None:
        """lock_type='rebuild' → '再構築処理中' メッセージ."""
        e = CLISubprocessError(
            "ロック競合", code="LOCK_CONFLICT", lock_type="rebuild",
        )
        msg = e.format_mcp_error()
        assert "再構築処理中" in msg
        assert "再試行" not in msg

    def test_write_lock_returns_ingest_message(self) -> None:
        """lock_type='write' → '別の取り込み' メッセージ."""
        e = CLISubprocessError(
            "ロック競合", code="LOCK_CONFLICT", lock_type="write",
        )
        msg = e.format_mcp_error()
        assert "別の取り込み" in msg
        assert "再試行" not in msg

    def test_no_lock_type_returns_generic_message(self) -> None:
        """lock_type=None → 汎用メッセージ."""
        e = CLISubprocessError("ロック競合", code="LOCK_CONFLICT")
        msg = e.format_mcp_error()
        # ロック競合の generic メッセージ
        assert "ロック" in msg

    def test_non_lock_conflict_returns_context_message(self) -> None:
        """非ロック競合エラー → context 付きエラーメッセージ."""
        e = CLISubprocessError("unexpected", code="INTERNAL_ERROR")
        msg = e.format_mcp_error("処理失敗")
        assert "処理失敗" in msg
        assert "unexpected" in msg

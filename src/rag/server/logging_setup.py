"""ログ設定ユーティリティ.

仕様: docs/specs/rag-knowledge.md「アプリケーションログ」「server 構造」
"""

from __future__ import annotations

import logging

LOG_FILE_PREFIX = "rag-server-"


def _sanitize_log_value(value: str, *, max_length: int = 200) -> str:
    """ログ出力用に文字列をサニタイズする（制御文字除去 + 長さ制限）."""
    sanitized = value.replace("\r", "").replace("\n", " ")
    if len(sanitized) > max_length:
        return sanitized[:max_length] + "..."
    return sanitized


_CLI_PASSTHROUGH_FORMATTER = logging.Formatter("%(message)s")


def _write_cli_lines_to_handlers(lines: list[str]) -> None:
    """CLI stderr 行を [CLI] プレフィックス付きでハンドラに emit する.

    logger.debug() を経由するとサーバー側フォーマッタ（[MCP] プレフィックス）が
    適用されてタイムスタンプが二重になるため、フォーマッタを一時的に差し替えて
    handler.emit() を呼ぶ。emit 経由にすることで SessionRotatingFileHandler の
    ロールオーバーや stream 再オープンの恩恵を受ける。

    フォーマッタ差し替えと emit は handler.acquire() / release() のロック内で
    実施し、他スレッドが同じハンドラに書き込んでも [MCP] フォーマッタと
    [CLI] パススルーが干渉しないようにする。handler.handle() を使うと二重
    acquire になるため、ここではあえて emit（ロックなし版）を直接呼ぶ。
    """
    rag_logger = logging.getLogger("rag")
    for handler in rag_logger.handlers:
        handler.acquire()
        try:
            original_formatter = handler.formatter
            handler.formatter = _CLI_PASSTHROUGH_FORMATTER
            try:
                for line in lines:
                    record = logging.LogRecord(
                        name="rag.cli",
                        level=logging.DEBUG,
                        pathname="",
                        lineno=0,
                        msg=f"[CLI] {line}",
                        args=None,
                        exc_info=None,
                    )
                    handler.emit(record)
            finally:
                handler.formatter = original_formatter
        finally:
            handler.release()

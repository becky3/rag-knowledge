"""セッション単位ログファイルハンドラのテスト.

仕様: docs/specs/rag-knowledge.md (MCP サーバーのロガー設定 / ログファイル出力)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

import pytest

from py_common_lib.logging import (
    SessionRotatingFileHandler,
    build_session_filename,
)
from rag.server import LOG_FILE_PREFIX, _write_cli_lines_to_handlers


def _make_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="rag.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


class TestBuildSessionFilename:
    def test_format_uses_session_timestamp_and_zero_padded_sequence(self) -> None:
        started_at = datetime(2026, 4, 17, 8, 30, 0)
        assert (
            build_session_filename(LOG_FILE_PREFIX, started_at, 1)
            == "rag-server-20260417-083000-00001.log"
        )

    def test_sequence_increments_preserve_five_digit_padding(self) -> None:
        started_at = datetime(2026, 4, 17, 8, 30, 0)
        assert (
            build_session_filename(LOG_FILE_PREFIX, started_at, 42)
            == "rag-server-20260417-083000-00042.log"
        )


class TestSessionRotatingFileHandler:
    def test_initial_file_is_created_with_sequence_one(
        self, tmp_path: Path
    ) -> None:
        started_at = datetime(2026, 4, 17, 8, 30, 0)
        handler = SessionRotatingFileHandler(
            log_dir=tmp_path, prefix=LOG_FILE_PREFIX, started_at=started_at, max_bytes=1_000_000
        )
        try:
            handler.emit(_make_record("hello"))
            handler.flush()
        finally:
            handler.close()
        expected = tmp_path / "rag-server-20260417-083000-00001.log"
        assert expected.exists()
        assert "hello" in expected.read_text(encoding="utf-8")

    def test_rollover_opens_new_file_while_keeping_previous(
        self, tmp_path: Path
    ) -> None:
        started_at = datetime(2026, 4, 17, 8, 30, 0)
        handler = SessionRotatingFileHandler(
            log_dir=tmp_path, prefix=LOG_FILE_PREFIX, started_at=started_at, max_bytes=50
        )
        try:
            handler.emit(_make_record("seed-line" + "x" * 40))
            for i in range(10):
                handler.emit(_make_record(f"line-{i:02d}" + "x" * 40))
            handler.flush()
        finally:
            handler.close()
        first = tmp_path / "rag-server-20260417-083000-00001.log"
        second = tmp_path / "rag-server-20260417-083000-00002.log"
        assert first.exists()
        assert second.exists()
        # 旧ファイルは削除されず、最初に書いた内容が残っている（truncate 防止の検証）
        first_content = first.read_text(encoding="utf-8")
        assert "seed-line" in first_content
        assert second.stat().st_size > 0

    def test_existing_initial_file_raises_file_exists_error(
        self, tmp_path: Path
    ) -> None:
        started_at = datetime(2026, 4, 17, 8, 30, 0)
        conflicting = tmp_path / "rag-server-20260417-083000-00001.log"
        conflicting.write_text("preexisting", encoding="utf-8")
        with pytest.raises(FileExistsError):
            SessionRotatingFileHandler(
                log_dir=tmp_path,
                prefix=LOG_FILE_PREFIX,
                started_at=started_at,
                max_bytes=1_000_000,
            )

    def test_rollover_raises_when_next_file_already_exists(
        self, tmp_path: Path
    ) -> None:
        """ロールオーバー先ファイルが既存の場合、emit 経由で handleError に委ねる."""
        started_at = datetime(2026, 4, 17, 8, 30, 0)
        # 次の連番ファイルを事前に作成して衝突を発生させる
        conflicting = tmp_path / "rag-server-20260417-083000-00002.log"
        conflicting.write_text("preexisting", encoding="utf-8")
        handler = SessionRotatingFileHandler(
            log_dir=tmp_path, prefix=LOG_FILE_PREFIX, started_at=started_at, max_bytes=50
        )
        errors: list[BaseException] = []
        handler.handleError = (  # type: ignore[method-assign]
            lambda record: errors.append(  # noqa: ARG005
                FileExistsError("rollover collision")
            )
        )
        try:
            for i in range(10):
                handler.emit(_make_record(f"line-{i}" + "x" * 40))
        finally:
            handler.close()
        assert errors, "handleError should capture rollover FileExistsError"
        # 旧ファイルは保護され、上書きされていない
        assert conflicting.read_text(encoding="utf-8") == "preexisting"

    def test_emit_reopens_stream_in_append_mode(self, tmp_path: Path) -> None:
        """stream=None 状態で emit されると mode='a' で再オープンされ既存内容を保持する.

        close() 後に再度 emit されても既存ファイルが truncate されず追記されることを
        検証する（mode='w' で再オープンされた場合は既存内容が失われる）。
        """
        started_at = datetime(2026, 4, 17, 8, 30, 0)
        handler = SessionRotatingFileHandler(
            log_dir=tmp_path, prefix=LOG_FILE_PREFIX, started_at=started_at, max_bytes=1_000_000
        )
        handler.emit(_make_record("first"))
        handler.flush()
        # stream を閉じて None 状態にする（emit 再オープン経路を通すため）
        assert handler.stream is not None
        handler.stream.close()
        handler.stream = None  # type: ignore[assignment]

        try:
            handler.emit(_make_record("second"))
            handler.flush()
        finally:
            handler.close()

        assert handler.mode == "a"
        log_path = tmp_path / "rag-server-20260417-083000-00001.log"
        content = log_path.read_text(encoding="utf-8")
        assert "first" in content
        assert "second" in content


class TestWriteCliLinesToHandlers:
    """server._write_cli_lines_to_handlers の挙動検証."""

    def test_cli_lines_bypass_mcp_formatter_and_get_cli_prefix(
        self, tmp_path: Path
    ) -> None:
        """CLI 行はサーバーフォーマッタを通さず [CLI] プレフィックス付きで出力される.

        logger.debug() 経由だと [MCP] プレフィックス・タイムスタンプが二重付加される。
        _write_cli_lines_to_handlers はフォーマッタを一時差し替えしてパススルー出力する。
        """
        rag_logger = logging.getLogger("rag")
        original_handlers = list(rag_logger.handlers)
        original_propagate = rag_logger.propagate
        rag_logger.handlers = []
        rag_logger.propagate = False

        log_path = tmp_path / "cli-passthrough.log"
        handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        mcp_formatter = logging.Formatter(
            "[MCP] %(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        handler.setFormatter(mcp_formatter)
        rag_logger.addHandler(handler)
        try:
            cli_line = "2026-04-17 08:30:00,123 - rag.cli - INFO - ingest done"
            _write_cli_lines_to_handlers([cli_line])
            handler.flush()
            # CLI 行出力後にサーバーフォーマッタが元に戻っていることを確認
            assert handler.formatter is mcp_formatter
        finally:
            handler.close()
            rag_logger.removeHandler(handler)
            rag_logger.handlers = original_handlers
            rag_logger.propagate = original_propagate

        content = log_path.read_text(encoding="utf-8")
        lines = [line for line in content.splitlines() if line]
        assert len(lines) == 1
        expected = "[CLI] 2026-04-17 08:30:00,123 - rag.cli - INFO - ingest done"
        assert lines[0] == expected
        # [MCP] プレフィックスが付与されていないこと（二重フォーマット防止の検証）
        assert "[MCP]" not in content

    def test_multiple_cli_lines_each_get_cli_prefix(self, tmp_path: Path) -> None:
        """複数行を渡したとき、全ての行に [CLI] プレフィックスが付与される."""
        rag_logger = logging.getLogger("rag")
        original_handlers = list(rag_logger.handlers)
        original_propagate = rag_logger.propagate
        rag_logger.handlers = []
        rag_logger.propagate = False

        log_path = tmp_path / "cli-multi.log"
        handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        rag_logger.addHandler(handler)
        try:
            _write_cli_lines_to_handlers(["line-A", "line-B", "line-C"])
            handler.flush()
        finally:
            handler.close()
            rag_logger.removeHandler(handler)
            rag_logger.handlers = original_handlers
            rag_logger.propagate = original_propagate

        content = log_path.read_text(encoding="utf-8")
        lines = [line for line in content.splitlines() if line]
        assert lines == ["[CLI] line-A", "[CLI] line-B", "[CLI] line-C"]


class TestServerFormatter:
    """サーバーフォーマッタの [MCP] プレフィックス付与検証."""

    def test_mcp_formatter_produces_expected_shape(self, tmp_path: Path) -> None:
        """server.py で使用するフォーマッタが [MCP] プレフィックス付きで出力する.

        フォーマット: [MCP] YYYY-MM-DD HH:MM:SS,mmm - name - LEVEL - message
        """
        log_path = tmp_path / "mcp-format.log"
        handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        mcp_formatter = logging.Formatter(
            "[MCP] %(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        handler.setFormatter(mcp_formatter)
        try:
            record = logging.LogRecord(
                name="rag.server",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg="Starting MCP server: transport=%s",
                args=("http",),
                exc_info=None,
            )
            handler.emit(record)
            handler.flush()
        finally:
            handler.close()

        content = log_path.read_text(encoding="utf-8").strip()
        pattern = (
            r"^\[MCP\] \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} "
            r"- rag\.server - INFO - Starting MCP server: transport=http$"
        )
        assert re.match(pattern, content), (
            f"log line does not match expected MCP format: {content!r}"
        )

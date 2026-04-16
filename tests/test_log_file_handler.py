"""セッション単位ログファイルハンドラのテスト.

仕様: docs/specs/rag-knowledge.md (MCP サーバーのロガー設定 / ログファイル出力)
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pytest

from rag.infrastructure.log_file_handler import (
    SessionRotatingFileHandler,
    build_session_filename,
)


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
            build_session_filename(started_at, 1)
            == "rag-server-20260417-083000-00001.log"
        )

    def test_sequence_increments_preserve_five_digit_padding(self) -> None:
        started_at = datetime(2026, 4, 17, 8, 30, 0)
        assert (
            build_session_filename(started_at, 42)
            == "rag-server-20260417-083000-00042.log"
        )


class TestSessionRotatingFileHandler:
    def test_initial_file_is_created_with_sequence_one(
        self, tmp_path: Path
    ) -> None:
        started_at = datetime(2026, 4, 17, 8, 30, 0)
        handler = SessionRotatingFileHandler(
            log_dir=tmp_path, started_at=started_at, max_bytes=1_000_000
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
            log_dir=tmp_path, started_at=started_at, max_bytes=50
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
            log_dir=tmp_path, started_at=started_at, max_bytes=50
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

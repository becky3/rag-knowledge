"""セッション単位で切り替わるログファイルハンドラ.

仕様: docs/specs/rag-knowledge.md（MCP サーバーのロガー設定 / ログファイル出力）

ファイル名は `rag-server-YYYYMMDD-HHMMSS-NNNNN.log` 形式。
NNNNN は 5 桁ゼロパディングの連番で、セッション開始時は 00001 から始まる。
書き込み時にファイルサイズが max_bytes 以上であれば連番をインクリメントし
新ファイルを開く（旧ファイルは保持）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

_SEQUENCE_WIDTH = 5
_FILE_NAME_PREFIX = "rag-server-"


def build_session_filename(started_at: datetime, sequence: int) -> str:
    """セッション開始時刻と連番からログファイル名を組み立てる."""
    timestamp = started_at.strftime("%Y%m%d-%H%M%S")
    return f"{_FILE_NAME_PREFIX}{timestamp}-{sequence:0{_SEQUENCE_WIDTH}d}.log"


class SessionRotatingFileHandler(logging.FileHandler):
    """サイズ超過時に新ファイルへ切り替える FileHandler.

    logging.handlers.RotatingFileHandler は旧ファイルを rename して削除対象にするため、
    旧ファイルを保持したまま新ファイルへ移行する本用途には合致しない。
    このハンドラは Python 標準の FileHandler を継承し、emit 前にサイズを確認して
    閾値超過時に新ファイルを開く。
    """

    def __init__(
        self,
        log_dir: Path,
        started_at: datetime,
        max_bytes: int,
        encoding: str = "utf-8",
    ) -> None:
        self._log_dir = log_dir
        self._started_at = started_at
        self._max_bytes = max_bytes
        self._sequence = 1
        self._raise_if_current_exists()
        super().__init__(self._current_path(), mode="w", encoding=encoding, delay=False)

    def _current_path(self) -> Path:
        return self._log_dir / build_session_filename(self._started_at, self._sequence)

    def _raise_if_current_exists(self) -> None:
        current = self._current_path()
        if current.exists():
            msg = f"Log file already exists: {current}"
            raise FileExistsError(msg)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._should_rollover():
                self._do_rollover()
        except Exception:
            self.handleError(record)
            return
        super().emit(record)

    def _should_rollover(self) -> bool:
        if self.stream is None:
            return False
        # TextIOWrapper.tell() はエンコーディング状態を含む不透明値のためサイズ判定に使えない。
        # 実ファイルサイズで判定する（親クラスの emit が書き込み後に flush するため
        # 次回 emit 時点でファイルサイズは正確）
        return Path(self.baseFilename).stat().st_size >= self._max_bytes

    def _do_rollover(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        self._sequence += 1
        self._raise_if_current_exists()
        self.baseFilename = str(self._current_path())
        self.stream = self._open()

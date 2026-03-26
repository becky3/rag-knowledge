"""ChromaDB サーバーのライフサイクル管理.

仕様: docs/specs/rag-knowledge.md (ChromaDB client/server 構成)

MCP サーバー起動時にヘルスチェック → 未起動なら自動起動を行う。
MCP サーバー終了時は、自分が起動したプロセスのみ停止する
（既存サーバーに接続した場合は停止しない）。
CLI からの接続は既存サーバーへの接続のみ（手動起動 or MCP 経由で起動済み前提）。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx  # safety:allowed — ChromaDB サーバーへのローカルヘルスチェック用

logger = logging.getLogger(__name__)

# ヘルスチェックのデフォルトタイムアウト（秒）
_HEALTH_CHECK_TIMEOUT = 5.0

# 起動待機のデフォルトタイムアウト（秒）
_WAIT_FOR_READY_TIMEOUT = 30.0

# 起動待機のリトライ間隔（秒）
_WAIT_RETRY_INTERVAL = 1.0

# shutdown 時の terminate → kill 待機タイムアウト（秒）
_SHUTDOWN_TIMEOUT = 5.0


class ChromaDBServerManager:
    """ChromaDB サーバーのライフサイクル管理.

    MCP サーバー起動時に使用する。ヘルスチェックで既存サーバーの存在を確認し、
    未起動かつ auto_start が有効な場合は `chroma run` でサブプロセス起動する。
    自分が起動したプロセスのみ shutdown() で停止する。
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8000,
        persist_dir: str = "./chroma_db",
        auto_start: bool = True,
    ) -> None:
        """ChromaDBServerManager を初期化する.

        Args:
            host: ChromaDB サーバーのホスト
            port: ChromaDB サーバーのポート
            persist_dir: ChromaDB の永続化ディレクトリ
            auto_start: 未起動時に自動起動するか
        """
        self._host = host
        self._port = port
        self._persist_dir = persist_dir
        self._auto_start = auto_start
        self._process: subprocess.Popen[str] | None = None
        self._started_by_us = False

    @property
    def _base_url(self) -> str:
        """ChromaDB サーバーのベース URL."""
        return f"http://{self._host}:{self._port}"

    def health_check(self, timeout: float = _HEALTH_CHECK_TIMEOUT) -> bool:
        """ChromaDB サーバーの HTTP ヘルスチェック.

        Args:
            timeout: リクエストタイムアウト（秒）

        Returns:
            サーバーが応答すれば True
        """
        try:
            resp = httpx.get(  # safety:allowed — ローカルヘルスチェック
                f"{self._base_url}/api/v1/heartbeat",
                timeout=timeout,
            )
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False
        except Exception:
            logger.warning(
                "Health check failed unexpectedly",
                exc_info=True,
            )
            return False

    def ensure_server_running(self) -> bool:
        """ChromaDB サーバーが稼働していることを保証する.

        1. ヘルスチェックで既存サーバーの存在を確認
        2. 未起動かつ auto_start 有効なら自動起動
        3. 起動待機（タイムアウト付き）

        Returns:
            サーバーが利用可能なら True。
            False でも MCP サーバーは稼働を継続し、ツール呼び出し時にエラーを返す
            （グレースフルデグレード）。
        """
        if self.health_check():
            logger.info(
                "ChromaDB server is already running at %s:%d",
                self._host,
                self._port,
            )
            return True

        if not self._auto_start:
            logger.warning(
                "ChromaDB server is not running at %s:%d and auto_start is disabled. "
                "Start manually with: chroma run --path <persist_dir> --port %d",
                self._host,
                self._port,
                self._port,
            )
            return False

        logger.info(
            "ChromaDB server not found at %s:%d, starting...",
            self._host,
            self._port,
        )
        return self._start_server()

    def shutdown(self) -> None:
        """自分が起動した ChromaDB サーバープロセスを停止する.

        既存サーバーに接続した場合（自分が起動していない場合）は何もしない。
        """
        if not self._started_by_us or self._process is None:
            return

        if self._process.poll() is not None:
            logger.info(
                "ChromaDB server process already exited (code: %d)",
                self._process.returncode,
            )
            self._process = None
            self._started_by_us = False
            return

        logger.info(
            "Shutting down ChromaDB server (PID: %d)...",
            self._process.pid,
        )
        self._process.terminate()
        try:
            self._process.wait(timeout=_SHUTDOWN_TIMEOUT)
            logger.info("ChromaDB server stopped gracefully")
        except subprocess.TimeoutExpired:
            logger.warning(
                "ChromaDB server did not stop within %.0fs, killing...",
                _SHUTDOWN_TIMEOUT,
            )
            self._process.kill()
            self._process.wait(timeout=_SHUTDOWN_TIMEOUT)
            logger.info("ChromaDB server killed")

        self._process = None
        self._started_by_us = False

    def _start_server(self) -> bool:
        """ChromaDB サーバーをサブプロセスとして起動する.

        Returns:
            サーバーの起動待機が成功すれば True
        """
        persist_path = str(Path(self._persist_dir).resolve())
        cmd = [
            "chroma",
            "run",
            "--path",
            persist_path,
            "--port",
            str(self._port),
            "--host",
            self._host,
        ]

        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
            "text": True,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            self._process = subprocess.Popen(cmd, **popen_kwargs)
            self._started_by_us = True
            logger.info(
                "Started ChromaDB server process (PID: %d, path: %s)",
                self._process.pid,
                persist_path,
            )
        except FileNotFoundError:
            logger.error(
                "'chroma' command not found. "
                "Ensure chromadb is installed: pip install chromadb"
            )
            return False
        except OSError:
            logger.exception("Failed to start ChromaDB server")
            return False

        return self._wait_for_ready()

    def _wait_for_ready(
        self, timeout: float = _WAIT_FOR_READY_TIMEOUT
    ) -> bool:
        """サーバーが応答するまでリトライする.

        Args:
            timeout: 最大待機時間（秒）

        Returns:
            サーバーが応答すれば True
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            # プロセスが予期せず終了していないか確認
            if self._process is not None and self._process.poll() is not None:
                stderr_output = ""
                if self._process.stderr:
                    stderr_output = self._process.stderr.read()
                logger.error(
                    "ChromaDB server process exited unexpectedly "
                    "(code: %d): %s",
                    self._process.returncode,
                    stderr_output[:500] if stderr_output else "(no output)",
                )
                return False

            if self.health_check(timeout=2.0):
                elapsed = time.monotonic() - start
                logger.info("ChromaDB server ready (%.1fs)", elapsed)
                return True

            time.sleep(_WAIT_RETRY_INTERVAL)

        logger.error(
            "ChromaDB server did not become ready within %.0fs", timeout
        )
        return False

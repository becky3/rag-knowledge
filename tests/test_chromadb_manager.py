"""ChromaDB Server Manager のテスト (Issue #407).

仕様: docs/specs/rag-knowledge.md (ChromaDB client/server 構成)

テスト方針:
- ヘルスチェック: httpx.get をモックし、成功/接続エラー/タイムアウトを検証
- 自動起動: subprocess.Popen をモックし、起動シーケンスを検証
- 起動待機: タイムアウト・プロセス異常終了時の動作を検証
- 自動起動フラグ OFF: サーバー未起動時に起動を試みないことを検証
- シャットダウン: 自分が起動したプロセスのみ停止、既存サーバーは停止しないことを検証
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import httpx
import pytest

from rag.infrastructure.chromadb_manager import ChromaDBServerManager


@pytest.fixture
def manager() -> ChromaDBServerManager:
    """デフォルト設定の ChromaDBServerManager."""
    return ChromaDBServerManager(
        host="localhost",
        port=8000,
        persist_dir="./test_chroma_db",
        auto_start=True,
    )


@pytest.fixture
def manager_no_autostart() -> ChromaDBServerManager:
    """auto_start=False の ChromaDBServerManager."""
    return ChromaDBServerManager(
        host="localhost",
        port=8000,
        persist_dir="./test_chroma_db",
        auto_start=False,
    )


class TestHealthCheck:
    """health_check() のテスト."""

    def test_success(self, manager: ChromaDBServerManager) -> None:
        """サーバーが正常応答する場合 True を返す."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        with patch("rag.infrastructure.chromadb_manager.httpx.get", return_value=mock_response):
            assert manager.health_check() is True

    def test_connection_error(self, manager: ChromaDBServerManager) -> None:
        """接続エラー時に False を返す."""
        with patch(
            "rag.infrastructure.chromadb_manager.httpx.get",
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            assert manager.health_check() is False

    def test_timeout(self, manager: ChromaDBServerManager) -> None:
        """タイムアウト時に False を返す."""
        with patch(
            "rag.infrastructure.chromadb_manager.httpx.get",
            side_effect=httpx.TimeoutException("Timeout"),
        ):
            assert manager.health_check() is False

    def test_non_200_status(self, manager: ChromaDBServerManager) -> None:
        """200 以外のステータスコードで False を返す."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        with patch("rag.infrastructure.chromadb_manager.httpx.get", return_value=mock_response):
            assert manager.health_check() is False

    def test_unexpected_exception(self, manager: ChromaDBServerManager) -> None:
        """予期しない例外時に False を返す."""
        with patch(
            "rag.infrastructure.chromadb_manager.httpx.get",
            side_effect=RuntimeError("Unexpected"),
        ):
            assert manager.health_check() is False

    def test_requests_correct_url(self, manager: ChromaDBServerManager) -> None:
        """正しい URL にリクエストする."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        with patch("rag.infrastructure.chromadb_manager.httpx.get", return_value=mock_response) as mock_get:
            manager.health_check(timeout=3.0)
            mock_get.assert_called_once_with(
                "http://localhost:8000/api/v1/heartbeat",
                timeout=3.0,
            )


class TestEnsureServerRunning:
    """ensure_server_running() のテスト."""

    def test_server_already_running(self, manager: ChromaDBServerManager) -> None:
        """サーバーが既に稼働中なら True を返し、起動しない."""
        with patch.object(manager, "health_check", return_value=True):
            with patch.object(manager, "_start_server") as mock_start:
                assert manager.ensure_server_running() is True
                mock_start.assert_not_called()

    def test_auto_start_disabled_server_not_running(
        self, manager_no_autostart: ChromaDBServerManager
    ) -> None:
        """auto_start=False でサーバー未起動時に False を返す."""
        with patch.object(manager_no_autostart, "health_check", return_value=False):
            with patch.object(manager_no_autostart, "_start_server") as mock_start:
                assert manager_no_autostart.ensure_server_running() is False
                mock_start.assert_not_called()

    def test_auto_start_enabled_starts_server(
        self, manager: ChromaDBServerManager
    ) -> None:
        """auto_start=True でサーバー未起動時に _start_server を呼ぶ."""
        with patch.object(manager, "health_check", return_value=False):
            with patch.object(manager, "_start_server", return_value=True) as mock_start:
                assert manager.ensure_server_running() is True
                mock_start.assert_called_once()

    def test_auto_start_server_fails(
        self, manager: ChromaDBServerManager
    ) -> None:
        """サーバー起動に失敗した場合 False を返す."""
        with patch.object(manager, "health_check", return_value=False):
            with patch.object(manager, "_start_server", return_value=False):
                assert manager.ensure_server_running() is False


class TestStartServer:
    """_start_server() のテスト."""

    def test_command_not_found(self, manager: ChromaDBServerManager) -> None:
        """chroma コマンドが見つからない場合 False を返す."""
        with patch(
            "rag.infrastructure.chromadb_manager.subprocess.Popen",
            side_effect=FileNotFoundError("chroma not found"),
        ):
            assert manager._start_server() is False

    def test_popen_exception(self, manager: ChromaDBServerManager) -> None:
        """Popen で予期しない例外が発生した場合 False を返す."""
        with patch(
            "rag.infrastructure.chromadb_manager.subprocess.Popen",
            side_effect=OSError("Permission denied"),
        ):
            assert manager._start_server() is False

    def test_successful_start(self, manager: ChromaDBServerManager) -> None:
        """正常にプロセス起動 → 起動待機に進む."""
        mock_process = MagicMock()
        mock_process.pid = 12345
        with patch(
            "rag.infrastructure.chromadb_manager.subprocess.Popen",
            return_value=mock_process,
        ):
            with patch.object(manager, "_wait_for_ready", return_value=True):
                assert manager._start_server() is True

    def test_start_command_includes_host_and_port(
        self, manager: ChromaDBServerManager
    ) -> None:
        """起動コマンドに host と port が含まれる."""
        mock_process = MagicMock()
        mock_process.pid = 12345
        with patch(
            "rag.infrastructure.chromadb_manager.subprocess.Popen",
            return_value=mock_process,
        ) as mock_popen:
            with patch.object(manager, "_wait_for_ready", return_value=True):
                manager._start_server()
                call_args = mock_popen.call_args
                cmd = call_args[0][0]
                assert "chroma" == cmd[0]
                assert "run" == cmd[1]
                assert "--port" in cmd
                assert "8000" in cmd
                assert "--host" in cmd
                assert "localhost" in cmd


class TestWaitForReady:
    """_wait_for_ready() のテスト."""

    def test_immediately_ready(self, manager: ChromaDBServerManager) -> None:
        """最初のヘルスチェックで成功する場合."""
        manager._process = MagicMock()
        manager._process.poll.return_value = None
        with patch.object(manager, "health_check", return_value=True):
            assert manager._wait_for_ready(timeout=5.0) is True

    def test_ready_after_retries(self, manager: ChromaDBServerManager) -> None:
        """数回のリトライ後に成功する場合."""
        manager._process = MagicMock()
        manager._process.poll.return_value = None
        call_count = 0

        def health_check_side_effect(timeout: float = 5.0) -> bool:
            nonlocal call_count
            call_count += 1
            return call_count >= 3

        with patch.object(manager, "health_check", side_effect=health_check_side_effect):
            with patch("rag.infrastructure.chromadb_manager.time.sleep"):
                assert manager._wait_for_ready(timeout=30.0) is True

    def test_timeout(self, manager: ChromaDBServerManager) -> None:
        """タイムアウトまでサーバーが応答しない場合 False を返す."""
        manager._process = MagicMock()
        manager._process.poll.return_value = None
        with patch.object(manager, "health_check", return_value=False):
            with patch("rag.infrastructure.chromadb_manager.time.sleep"):
                with patch(
                    "rag.infrastructure.chromadb_manager.time.monotonic",
                    side_effect=[0.0, 0.0, 1.0, 2.0, 31.0],
                ):
                    assert manager._wait_for_ready(timeout=30.0) is False

    def test_process_exits_unexpectedly(
        self, manager: ChromaDBServerManager
    ) -> None:
        """プロセスが予期せず終了した場合 False を返す."""
        mock_process = MagicMock()
        mock_process.poll.return_value = 1
        mock_process.returncode = 1
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = "Error: port already in use"
        mock_process.stderr = mock_stderr
        manager._process = mock_process

        with patch(
            "rag.infrastructure.chromadb_manager.time.monotonic",
            side_effect=[0.0, 0.0],
        ):
            assert manager._wait_for_ready(timeout=30.0) is False

    def test_process_exits_no_stderr(
        self, manager: ChromaDBServerManager
    ) -> None:
        """プロセスが予期せず終了し stderr が None の場合."""
        mock_process = MagicMock()
        mock_process.poll.return_value = 1
        mock_process.returncode = 1
        mock_process.stderr = None
        manager._process = mock_process

        with patch(
            "rag.infrastructure.chromadb_manager.time.monotonic",
            side_effect=[0.0, 0.0],
        ):
            assert manager._wait_for_ready(timeout=30.0) is False


class TestShutdown:
    """shutdown() のテスト."""

    def test_not_started_by_us(self, manager: ChromaDBServerManager) -> None:
        """自分が起動していない場合は何もしない."""
        manager._started_by_us = False
        manager._process = MagicMock()
        manager.shutdown()
        manager._process.terminate.assert_not_called()

    def test_no_process(self, manager: ChromaDBServerManager) -> None:
        """プロセスが None の場合は何もしない."""
        manager._started_by_us = True
        manager._process = None
        manager.shutdown()

    def test_process_already_exited(
        self, manager: ChromaDBServerManager
    ) -> None:
        """プロセスが既に終了している場合は terminate しない."""
        mock_process = MagicMock()
        mock_process.poll.return_value = 0
        mock_process.returncode = 0
        manager._process = mock_process
        manager._started_by_us = True

        manager.shutdown()
        mock_process.terminate.assert_not_called()
        assert manager._process is None
        assert manager._started_by_us is False

    def test_graceful_shutdown(
        self, manager: ChromaDBServerManager
    ) -> None:
        """正常にプロセスを停止できる場合."""
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        mock_process.pid = 12345
        manager._process = mock_process
        manager._started_by_us = True

        manager.shutdown()
        mock_process.terminate.assert_called_once()
        mock_process.wait.assert_called_once()
        mock_process.kill.assert_not_called()
        assert manager._process is None
        assert manager._started_by_us is False

    def test_force_kill_on_timeout(
        self, manager: ChromaDBServerManager
    ) -> None:
        """terminate 後にタイムアウトした場合 kill する."""
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        mock_process.pid = 12345
        mock_process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="chroma", timeout=5.0),
            None,
        ]
        manager._process = mock_process
        manager._started_by_us = True

        manager.shutdown()
        mock_process.terminate.assert_called_once()
        mock_process.kill.assert_called_once()
        assert manager._process is None
        assert manager._started_by_us is False

    def test_existing_server_not_terminated(
        self, manager: ChromaDBServerManager
    ) -> None:
        """既存サーバーに接続した場合（_started_by_us=False）は停止しない."""
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        manager._process = mock_process
        manager._started_by_us = False

        manager.shutdown()
        mock_process.terminate.assert_not_called()
        mock_process.kill.assert_not_called()

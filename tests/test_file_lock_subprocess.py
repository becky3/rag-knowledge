"""ファイルベースロックのサブプロセス間テスト.

仕様: docs/specs/infrastructure/content-upload.md (排他制御セクション)

本ファイルは pytest-xdist の並列実行と相性が悪いため、``-n0`` シリアル
実行を前提とする。また、各テストは subprocess を起動するので他のテストより
実行時間がかかる。

検証対象:
- 真のプロセス間排他（メインプロセスとサブプロセスで別 PID の排他）
- プロセス強制終了（kill / SIGKILL）時の OS レベル自動解放
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from rag.infrastructure.file_lock import (
    REBUILD_LOCK_FILENAME,
    WRITE_LOCK_FILENAME,
    LockAcquisitionError,
    rebuild_lock,
    write_lock,
)

# ワーカースクリプト: 指定パスで lock_kind のロックを取得し、"LOCKED" を
# stdout に出力した後 sleep する。親プロセスは "LOCKED" を読み取ることで
# ロック取得完了を同期する
_WORKER_SCRIPT = """
import sys
from pathlib import Path
import time

# venv の site-packages を参照できるように sys.path を設定する必要がある場合
# この script は uv run 経由で実行されるため通常は不要だが念のため
from rag.infrastructure.file_lock import rebuild_lock, write_lock

path = Path(sys.argv[1])
kind = sys.argv[2]
duration = float(sys.argv[3])

factory = rebuild_lock if kind == "rebuild" else write_lock
with factory(path):
    print("LOCKED", flush=True)
    time.sleep(duration)
print("RELEASED", flush=True)
"""


def _start_worker(
    lock_path: Path, kind: str, duration: float,
) -> subprocess.Popen[bytes]:
    """ロックを保持するワーカープロセスを起動.

    子プロセスが "LOCKED" を出力するまで待機してから返す。
    """
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _WORKER_SCRIPT,
            str(lock_path),
            kind,
            str(duration),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # "LOCKED" 出力を待つ（ロック取得確認）
    assert worker.stdout is not None
    line = worker.stdout.readline().decode().strip()
    if line != "LOCKED":
        stderr = worker.stderr.read().decode() if worker.stderr else ""
        worker.terminate()
        pytest.fail(
            f"worker did not acquire lock (kind={kind}): stdout={line!r}, "
            f"stderr={stderr}",
        )
    return worker


def _wait_for_exit(worker: subprocess.Popen[bytes], timeout: float = 10) -> None:
    """ワーカーの終了を待つ."""
    try:
        worker.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=5)


@pytest.mark.slow
class TestCrossProcessMutualExclusion:
    """別プロセスとの相互排他検証."""

    def test_subprocess_rebuild_blocks_main_write(self, tmp_path: Path) -> None:
        """subprocess で rebuild 保持中、メインから write 試行 → kind='rebuild'."""
        worker = _start_worker(tmp_path, "rebuild", duration=3.0)
        try:
            with pytest.raises(LockAcquisitionError) as exc_info:
                write_lock(tmp_path).acquire()
            assert exc_info.value.kind == "rebuild"
        finally:
            _wait_for_exit(worker)

    def test_subprocess_rebuild_blocks_main_rebuild(self, tmp_path: Path) -> None:
        """subprocess で rebuild 保持中、メインから rebuild 試行 → kind='rebuild'."""
        worker = _start_worker(tmp_path, "rebuild", duration=3.0)
        try:
            with pytest.raises(LockAcquisitionError) as exc_info:
                rebuild_lock(tmp_path).acquire()
            assert exc_info.value.kind == "rebuild"
        finally:
            _wait_for_exit(worker)

    def test_subprocess_write_blocks_main_write(self, tmp_path: Path) -> None:
        """subprocess で write 保持中、メインから write 試行 → kind='write'."""
        worker = _start_worker(tmp_path, "write", duration=3.0)
        try:
            with pytest.raises(LockAcquisitionError) as exc_info:
                write_lock(tmp_path).acquire()
            assert exc_info.value.kind == "write"
        finally:
            _wait_for_exit(worker)

    def test_subprocess_write_blocks_main_rebuild(self, tmp_path: Path) -> None:
        """subprocess で write 保持中、メインから rebuild 試行 → kind='write'."""
        worker = _start_worker(tmp_path, "write", duration=3.0)
        try:
            with pytest.raises(LockAcquisitionError) as exc_info:
                rebuild_lock(tmp_path).acquire()
            assert exc_info.value.kind == "write"
        finally:
            _wait_for_exit(worker)


@pytest.mark.slow
class TestLockReleaseOnProcessTermination:
    """プロセス強制終了時の OS レベル自動解放を検証."""

    def test_rebuild_lock_released_on_subprocess_kill(
        self, tmp_path: Path,
    ) -> None:
        """rebuild 保持中のサブプロセスを kill → 両ロックが解放される.

        OS ファイルロック（fcntl / msvcrt）はプロセス終了時に
        自動解放されるため、kill 後は即座に再取得できる。
        """
        worker = _start_worker(tmp_path, "rebuild", duration=60.0)
        try:
            # 強制終了（Windows: TerminateProcess, Unix: SIGKILL）
            worker.kill()
            worker.wait(timeout=5)
            # ロックファイル自体は残るが、flock 状態は OS が解放している
            # 少し待つ（Windows の OS レベル解放にラグがある場合に備える）
            time.sleep(0.1)

            # 両ロックとも再取得できる
            with rebuild_lock(tmp_path):
                pass
            with write_lock(tmp_path):
                pass
        finally:
            _wait_for_exit(worker)

    def test_write_lock_released_on_subprocess_kill(
        self, tmp_path: Path,
    ) -> None:
        """write 保持中のサブプロセスを kill → write_lock が解放される."""
        worker = _start_worker(tmp_path, "write", duration=60.0)
        try:
            worker.kill()
            worker.wait(timeout=5)
            time.sleep(0.1)

            with write_lock(tmp_path):
                pass
            with rebuild_lock(tmp_path):
                pass
        finally:
            _wait_for_exit(worker)


@pytest.mark.slow
class TestLockFileArtifacts:
    """ロックファイルが期待通りに source_store 直下に作成されることを検証."""

    def test_subprocess_creates_lock_files_at_expected_paths(
        self, tmp_path: Path,
    ) -> None:
        """サブプロセスが rebuild_lock 取得中、両ロックファイルが存在する."""
        worker = _start_worker(tmp_path, "rebuild", duration=2.0)
        try:
            assert (tmp_path / REBUILD_LOCK_FILENAME).exists()
            assert (tmp_path / WRITE_LOCK_FILENAME).exists()
        finally:
            _wait_for_exit(worker)

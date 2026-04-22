"""ファイルベースロックのテスト（修正版案I）.

仕様: docs/specs/infrastructure/content-upload.md (排他制御セクション)

設計:
- rebuild は rebuild_lock + write_lock を両方保持する
- write は rebuild_lock を NB プリチェックし、write_lock のみ保持する
- 全 4 パターンの競合で ``LockAcquisitionError.kind`` は実際の相手の
  操作種別と一致する

プロセス間のロック排他とプロセス強制終了時の自動解放は
test_file_lock_subprocess.py で検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rag.infrastructure.file_lock import (
    REBUILD_LOCK_FILENAME,
    WRITE_LOCK_FILENAME,
    FileLock,
    LockAcquisitionError,
    PipelineLock,
    rebuild_lock,
    write_lock,
)


class TestFileLock:
    """FileLock の基本動作テスト（低レベル primitive）."""

    def test_acquire_and_release(self, tmp_path: Path) -> None:
        lock = FileLock(tmp_path / "test.lock")
        lock.acquire()
        assert lock._fd is not None
        lock.release()
        assert lock._fd is None

    def test_context_manager(self, tmp_path: Path) -> None:
        lock = FileLock(tmp_path / "test.lock")
        with lock:
            assert lock._fd is not None
        assert lock._fd is None

    def test_lock_creates_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        assert not lock_path.exists()
        with FileLock(lock_path):
            assert lock_path.exists()

    def test_lock_creates_parent_directories(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "sub" / "dir" / "test.lock"
        with FileLock(lock_path):
            assert lock_path.exists()

    def test_release_without_acquire_is_noop(self, tmp_path: Path) -> None:
        lock = FileLock(tmp_path / "test.lock")
        lock.release()  # should not raise

    def test_double_release_is_safe(self, tmp_path: Path) -> None:
        lock = FileLock(tmp_path / "test.lock")
        lock.acquire()
        lock.release()
        lock.release()  # should not raise

    def test_lock_path_property(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        lock = FileLock(lock_path)
        assert lock.lock_path == lock_path

    def test_context_manager_releases_on_exception(self, tmp_path: Path) -> None:
        lock = FileLock(tmp_path / "test.lock")
        with pytest.raises(RuntimeError):
            with lock:
                raise RuntimeError("test error")
        assert lock._fd is None


class TestFileLockContention:
    """低レベル FileLock のロック競合テスト."""

    def test_second_lock_fails_nonblocking(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        lock1 = FileLock(lock_path)
        lock2 = FileLock(lock_path)

        lock1.acquire()
        try:
            with pytest.raises(LockAcquisitionError):
                lock2.acquire()
        finally:
            lock1.release()

    def test_low_level_error_has_no_kind(self, tmp_path: Path) -> None:
        """FileLock から直接送出される LockAcquisitionError の kind は None."""
        lock_path = tmp_path / "test.lock"
        lock1 = FileLock(lock_path)
        lock2 = FileLock(lock_path)

        lock1.acquire()
        try:
            with pytest.raises(LockAcquisitionError) as exc_info:
                lock2.acquire()
            assert exc_info.value.kind is None
            assert exc_info.value.lock_path == lock_path
        finally:
            lock1.release()

    def test_lock_released_allows_reacquire(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        lock1 = FileLock(lock_path)
        lock2 = FileLock(lock_path)

        with lock1:
            pass  # released

        # Now lock2 should succeed
        with lock2:
            assert lock2._fd is not None

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="Windows-specific: same-process lock contention via separate fd",
    )
    def test_windows_same_process_contention(self, tmp_path: Path) -> None:
        """Windows では同一プロセス内でも別 fd なら競合する."""
        lock_path = tmp_path / "test.lock"
        lock1 = FileLock(lock_path)
        lock2 = FileLock(lock_path)

        lock1.acquire()
        try:
            with pytest.raises(LockAcquisitionError):
                lock2.acquire()
        finally:
            lock1.release()


class TestLockFactoryFunctions:
    """write_lock / rebuild_lock ファクトリ関数のテスト."""

    def test_write_lock_is_pipeline_lock(self, tmp_path: Path) -> None:
        lock = write_lock(tmp_path)
        assert isinstance(lock, PipelineLock)
        assert lock.operation == "write"

    def test_rebuild_lock_is_pipeline_lock(self, tmp_path: Path) -> None:
        lock = rebuild_lock(tmp_path)
        assert isinstance(lock, PipelineLock)
        assert lock.operation == "rebuild"


class TestPipelineLockHoldingStrategy:
    """修正版案I の保持戦略（非対称）の検証."""

    def test_rebuild_holds_both_lock_files(self, tmp_path: Path) -> None:
        """rebuild は rebuild_lock と write_lock の両方のファイルを作成する."""
        with rebuild_lock(tmp_path):
            assert (tmp_path / REBUILD_LOCK_FILENAME).exists()
            assert (tmp_path / WRITE_LOCK_FILENAME).exists()

    def test_write_holds_only_write_lock_file(self, tmp_path: Path) -> None:
        """write は write_lock を保持、rebuild_lock はプリチェック後解放.

        プリチェック中に rebuild_lock ファイルは作成されるが、with 脱出時には
        rebuild_lock は他者から再取得可能な状態である。
        """
        with write_lock(tmp_path):
            assert (tmp_path / WRITE_LOCK_FILENAME).exists()
            # rebuild_lock ファイル自体はプリチェックで作成される
            assert (tmp_path / REBUILD_LOCK_FILENAME).exists()
            # ただし write 保持中でも rebuild_lock は他者から取得可能
            probe = FileLock(tmp_path / REBUILD_LOCK_FILENAME)
            probe.acquire()
            probe.release()


class TestPipelineLockMutualExclusion:
    """修正版案I: 全 4 パターンでの相互排他と kind の正確性を検証.

    Issue #668: rebuild 実行中の incremental ingest による
    ``last_commit_id`` 誤読の修正に伴い、write / rebuild ロックは
    相互排他に変更された。以下のテストで 4 パターン全ての kind が
    実際の相手の操作種別と一致することを検証する。
    """

    def test_rebuild_blocks_write_kind_is_rebuild(self, tmp_path: Path) -> None:
        """rebuild 取得中に write はプリチェックで失敗し kind='rebuild'.

        rebuild-A が rebuild_lock を保持中 → write-B のプリチェック
        （rebuild_lock NB 試行）で失敗 → ``kind="rebuild"``。
        """
        with rebuild_lock(tmp_path):
            write_b = write_lock(tmp_path)
            with pytest.raises(LockAcquisitionError) as exc_info:
                write_b.acquire()
            assert exc_info.value.kind == "rebuild"

    def test_rebuild_blocks_another_rebuild_kind_is_rebuild(
        self, tmp_path: Path,
    ) -> None:
        """rebuild 取得中に別 rebuild は primary 取得で失敗し kind='rebuild'.

        rebuild-A が rebuild_lock を保持中 → rebuild-B の primary
        （rebuild_lock）取得で失敗 → ``kind="rebuild"``。
        """
        with rebuild_lock(tmp_path):
            rebuild_b = rebuild_lock(tmp_path)
            with pytest.raises(LockAcquisitionError) as exc_info:
                rebuild_b.acquire()
            assert exc_info.value.kind == "rebuild"

    def test_write_blocks_another_write_kind_is_write(
        self, tmp_path: Path,
    ) -> None:
        """write 取得中に別 write は primary 取得で失敗し kind='write'.

        write-A が write_lock のみ保持中 → write-B のプリチェックは成功
        （rebuild_lock は保持されていない） → write-B の primary
        （write_lock）取得で失敗 → ``kind="write"``。
        """
        with write_lock(tmp_path):
            write_b = write_lock(tmp_path)
            with pytest.raises(LockAcquisitionError) as exc_info:
                write_b.acquire()
            assert exc_info.value.kind == "write"

    def test_write_blocks_rebuild_kind_is_write(self, tmp_path: Path) -> None:
        """write 取得中に rebuild は secondary 取得で失敗し kind='write'.

        write-A が write_lock のみ保持中 → rebuild-B の primary
        （rebuild_lock）取得は成功 → secondary（write_lock）取得で失敗
        → primary を release → ``kind="write"``。
        """
        with write_lock(tmp_path):
            rebuild_b = rebuild_lock(tmp_path)
            with pytest.raises(LockAcquisitionError) as exc_info:
                rebuild_b.acquire()
            assert exc_info.value.kind == "write"


class TestPipelineLockRelease:
    """ロック解放の正常/異常パスの検証."""

    def test_release_after_normal_acquire(self, tmp_path: Path) -> None:
        """正常 acquire/release 後に他者が再取得できる."""
        lock = rebuild_lock(tmp_path)
        lock.acquire()
        lock.release()
        with rebuild_lock(tmp_path):
            pass

    def test_context_manager_releases_on_exception(
        self, tmp_path: Path,
    ) -> None:
        """with 内で例外が起きても両ロック解放."""
        with pytest.raises(RuntimeError):
            with rebuild_lock(tmp_path):
                raise RuntimeError("test error")
        with rebuild_lock(tmp_path):
            pass
        with write_lock(tmp_path):
            pass

    def test_context_manager_releases_on_keyboard_interrupt(
        self, tmp_path: Path,
    ) -> None:
        """with 内の KeyboardInterrupt でも両ロック解放."""
        with pytest.raises(KeyboardInterrupt):
            with rebuild_lock(tmp_path):
                raise KeyboardInterrupt("simulated Ctrl+C")
        with rebuild_lock(tmp_path):
            pass
        with write_lock(tmp_path):
            pass

    def test_rebuild_release_frees_both_locks(self, tmp_path: Path) -> None:
        """rebuild の release 後、両ロックファイルが他者から取得可能."""
        with rebuild_lock(tmp_path):
            pass
        # 両ロックとも取れる
        with rebuild_lock(tmp_path):
            pass
        with write_lock(tmp_path):
            pass

    def test_write_release_frees_write_lock(self, tmp_path: Path) -> None:
        """write の release 後、write_lock が他者から取得可能."""
        with write_lock(tmp_path):
            pass
        with write_lock(tmp_path):
            pass
        # rebuild_lock はプリチェック時に既に release されているので先も取れる
        with rebuild_lock(tmp_path):
            pass


class TestPipelineLockInterruptSafety:
    """acquire 中間の KeyboardInterrupt で取得済みロックがリークしないことの検証.

    修正版案I + BaseException 保護により、acquire 内のどの段階で
    KeyboardInterrupt が発生しても、primary/secondary のロックは
    確実に解放される。
    """

    def test_interrupt_after_primary_releases_primary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """rebuild.acquire の primary 成功後・secondary 前に KeyboardInterrupt.

        FileLock.acquire を monkeypatch し、2 回目の呼び出しで
        KeyboardInterrupt を発生させる。primary（rebuild_lock）は
        既に取得されているが、BaseException 保護により解放される。
        """
        lock = rebuild_lock(tmp_path)

        original_acquire = FileLock.acquire
        call_count = [0]

        def mock_acquire(self: FileLock) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                # primary の acquire は成功させる
                original_acquire(self)
            elif call_count[0] == 2:
                # secondary の acquire 前に KeyboardInterrupt
                raise KeyboardInterrupt(
                    "simulated Ctrl+C after primary.acquire",
                )
            else:
                original_acquire(self)  # それ以降（再取得テスト）は成功

        monkeypatch.setattr(FileLock, "acquire", mock_acquire)

        with pytest.raises(KeyboardInterrupt):
            lock.acquire()

        # primary（rebuild_lock）がリークしていないことを確認
        # monkeypatch を戻してから再取得を試みる
        monkeypatch.setattr(FileLock, "acquire", original_acquire)
        with rebuild_lock(tmp_path):
            pass

    def test_interrupt_during_precheck_acquire_does_not_leak_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """プリチェックの probe.acquire 内で KeyboardInterrupt → probe の fd リークなし.

        FileLock.acquire の低レベル flock 呼び出しで KeyboardInterrupt が
        発生するケース。``FileLock.acquire`` は BaseException 保護により
        fd を確実に close するため、次の rebuild_lock 取得は成功する。
        """
        lock = write_lock(tmp_path)
        original_lock_unix = FileLock._lock_unix_fd
        original_lock_windows = FileLock._lock_windows_fd
        call_count = [0]

        def mock_lock_unix(self: FileLock, fd: int) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                # プリチェック probe の flock 中に KeyboardInterrupt
                raise KeyboardInterrupt(
                    "simulated Ctrl+C during probe.flock",
                )
            original_lock_unix(self, fd)

        def mock_lock_windows(self: FileLock, fd: int) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                raise KeyboardInterrupt(
                    "simulated Ctrl+C during probe.flock",
                )
            original_lock_windows(self, fd)

        monkeypatch.setattr(FileLock, "_lock_unix_fd", mock_lock_unix)
        monkeypatch.setattr(FileLock, "_lock_windows_fd", mock_lock_windows)

        with pytest.raises(KeyboardInterrupt):
            lock.acquire()

        # BaseException 保護で probe の fd が close される
        monkeypatch.setattr(FileLock, "_lock_unix_fd", original_lock_unix)
        monkeypatch.setattr(FileLock, "_lock_windows_fd", original_lock_windows)
        with rebuild_lock(tmp_path):
            pass

    def test_interrupt_in_secondary_releases_primary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """rebuild.acquire の secondary 内で KeyboardInterrupt → primary 解放.

        FileLock._lock_unix_fd / _lock_windows_fd を monkeypatch して、
        secondary の flock 呼び出しで KeyboardInterrupt を発生させる。
        primary は既に acquire 済みだが、PipelineLock の BaseException
        保護で確実に release される。
        """
        lock = rebuild_lock(tmp_path)
        original_acquire = FileLock.acquire
        call_count = [0]

        def mock_acquire(self: FileLock) -> None:
            call_count[0] += 1
            if call_count[0] == 2:
                # secondary の acquire 内で fd open 成功後に KeyboardInterrupt
                raise KeyboardInterrupt("simulated during secondary.acquire")
            original_acquire(self)

        monkeypatch.setattr(FileLock, "acquire", mock_acquire)

        with pytest.raises(KeyboardInterrupt):
            lock.acquire()

        # primary がリークしていなければ rebuild_lock は再取得可能
        monkeypatch.setattr(FileLock, "acquire", original_acquire)
        with rebuild_lock(tmp_path):
            pass


class TestPipelineLockFileNames:
    """PipelineLock が使用するロックファイル名の検証."""

    def test_write_lock_filename(self) -> None:
        assert WRITE_LOCK_FILENAME == ".write.lock"

    def test_rebuild_lock_filename(self) -> None:
        assert REBUILD_LOCK_FILENAME == ".rebuild.lock"

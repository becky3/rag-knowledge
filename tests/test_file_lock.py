"""ファイルベースロックのテスト.

仕様: docs/specs/infrastructure/content-upload.md (排他制御セクション)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rag.infrastructure.file_lock import (
    INGEST_LOCK_FILENAME,
    REBUILD_LOCK_FILENAME,
    FileLock,
    LockAcquisitionError,
    ingest_lock,
    rebuild_lock,
)


class TestFileLock:
    """FileLock の基本動作テスト."""

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
    """ロック競合テスト."""

    def test_second_lock_fails_nonblocking(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        lock1 = FileLock(lock_path)
        lock2 = FileLock(lock_path)

        lock1.acquire()
        try:
            with pytest.raises(LockAcquisitionError, match="別のプロセスが実行中"):
                lock2.acquire()
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
    """ingest_lock / rebuild_lock ファクトリ関数のテスト."""

    def test_ingest_lock_path(self, tmp_path: Path) -> None:
        lock = ingest_lock(tmp_path)
        assert lock.lock_path == tmp_path / INGEST_LOCK_FILENAME

    def test_rebuild_lock_path(self, tmp_path: Path) -> None:
        lock = rebuild_lock(tmp_path)
        assert lock.lock_path == tmp_path / REBUILD_LOCK_FILENAME

    def test_ingest_and_rebuild_locks_are_independent(self, tmp_path: Path) -> None:
        ilock = ingest_lock(tmp_path)
        rlock = rebuild_lock(tmp_path)

        # Both should be acquirable simultaneously
        with ilock:
            with rlock:
                assert ilock._fd is not None
                assert rlock._fd is not None

    def test_ingest_lock_contention(self, tmp_path: Path) -> None:
        lock1 = ingest_lock(tmp_path)
        lock2 = ingest_lock(tmp_path)

        with lock1:
            with pytest.raises(LockAcquisitionError):
                lock2.acquire()

    def test_rebuild_lock_contention(self, tmp_path: Path) -> None:
        lock1 = rebuild_lock(tmp_path)
        lock2 = rebuild_lock(tmp_path)

        with lock1:
            with pytest.raises(LockAcquisitionError):
                lock2.acquire()

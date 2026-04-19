"""ファイルベースロック — OS ファイルロックによるプロセス間排他制御.

仕様: docs/specs/infrastructure/content-upload.md (排他制御セクション)

インジェストロックと rebuild ロックを source_store ディレクトリ直下に配置し、
ノンブロッキングで取得する。取得できない場合は即座にエラーを返す。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType

# ロックファイル名
INGEST_LOCK_FILENAME = ".ingest.lock"
REBUILD_LOCK_FILENAME = ".rebuild.lock"


class LockAcquisitionError(Exception):
    """ロック取得に失敗した場合の例外."""


class FileLock:
    """OS ファイルロックによるプロセス間排他制御.

    コンテキストマネージャとして使用する::

        lock = FileLock(source_store_dir / ".ingest.lock")
        with lock:
            # 排他区間
            ...

    ノンブロッキングで取得を試み、取得できない場合は
    ``LockAcquisitionError`` を送出する。
    """

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fd: int | None = None

    @property
    def lock_path(self) -> Path:
        """ロックファイルのパス."""
        return self._lock_path

    def acquire(self) -> None:
        """ロックをノンブロッキングで取得する.

        Raises:
            LockAcquisitionError: 別プロセスがロックを保持している場合
        """
        # ロックファイルの親ディレクトリが存在することを確認
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)

        # ロックファイルを開く（なければ作成）
        self._fd = os.open(
            str(self._lock_path),
            os.O_CREAT | os.O_RDWR,
        )

        try:
            if sys.platform == "win32":
                self._lock_windows()
            else:
                self._lock_unix()
        except LockAcquisitionError:
            # ロック取得失敗時はファイルディスクリプタを閉じる
            os.close(self._fd)
            self._fd = None
            raise

    def release(self) -> None:
        """ロックを解放する."""
        if self._fd is None:
            return

        try:
            if sys.platform == "win32":
                self._unlock_windows()
            else:
                self._unlock_unix()
        finally:
            os.close(self._fd)
            self._fd = None

    def _lock_unix(self) -> None:
        """Unix 系 OS でのロック取得."""
        import errno
        import fcntl

        assert self._fd is not None
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise LockAcquisitionError(
                    f"ロックを取得できません（別のプロセスが実行中）: {self._lock_path}"
                ) from e
            raise  # 権限エラー等はそのまま伝播

    def _unlock_unix(self) -> None:
        """Unix 系 OS でのロック解放."""
        import fcntl

        assert self._fd is not None
        fcntl.flock(self._fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]

    def _lock_windows(self) -> None:
        """Windows でのロック取得."""
        import errno
        import msvcrt

        assert self._fd is not None
        try:
            msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EDEADLOCK):
                raise LockAcquisitionError(
                    f"ロックを取得できません（別のプロセスが実行中）: {self._lock_path}"
                ) from e
            raise

    def _unlock_windows(self) -> None:
        """Windows でのロック解放."""
        import msvcrt

        assert self._fd is not None
        # ファイルポインタを先頭に戻してから解放
        os.lseek(self._fd, 0, os.SEEK_SET)
        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()


def ingest_lock(source_store_dir: Path) -> FileLock:
    """インジェスト用ロックを返す.

    Args:
        source_store_dir: source_store のディレクトリパス

    Returns:
        インジェストロック用の FileLock インスタンス
    """
    return FileLock(source_store_dir / INGEST_LOCK_FILENAME)


def rebuild_lock(source_store_dir: Path) -> FileLock:
    """rebuild 用ロックを返す.

    Args:
        source_store_dir: source_store のディレクトリパス

    Returns:
        rebuild ロック用の FileLock インスタンス
    """
    return FileLock(source_store_dir / REBUILD_LOCK_FILENAME)

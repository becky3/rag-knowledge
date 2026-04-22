"""ファイルベースロック — OS ファイルロックによるプロセス間排他制御.

仕様: docs/specs/infrastructure/content-upload.md (排他制御セクション)

write 操作（ingest / delete 等の書き込み系）と rebuild 操作は、
``last_commit_id`` の整合性を保つために相互排他とする。
修正版案I による非対称設計:

- **rebuild**: ``rebuild_lock`` → ``write_lock`` の順で両方保持する
- **write**: ``rebuild_lock`` を NB プリチェック → 即 release し、``write_lock`` のみ保持する

いずれも自側ロックを先に NB 試行するため、競合相手の種別（rebuild / write）
を ``LockAcquisitionError.kind`` で正しく識別できる。全ロックが
ノンブロッキング（``LOCK_NB``）で取得されるため古典的な deadlock は起きない。

KeyboardInterrupt や例外発生時に取得途中のロックがリークしないよう、
``PipelineLock.acquire`` は ``BaseException`` で包んで原子的に動作する。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType
from typing import Literal, assert_never

# ロックファイル名
WRITE_LOCK_FILENAME = ".write.lock"
REBUILD_LOCK_FILENAME = ".rebuild.lock"

LockKind = Literal["rebuild", "write"]


class LockAcquisitionError(Exception):
    """ロック取得に失敗した場合の例外.

    ``kind`` は競合相手の操作種別（rebuild / write）を示す。
    低レベルの :class:`FileLock` から直接送出される場合は ``kind=None``。
    :class:`PipelineLock` 経由の場合は ``"rebuild"`` または ``"write"`` が
    設定される。修正版案I の設計により、全 4 パターンで ``kind`` は
    実際の相手の操作種別と一致する。
    """

    def __init__(
        self,
        lock_path: Path,
        kind: LockKind | None = None,
    ) -> None:
        if kind is None:
            super().__init__(
                f"ロックを取得できません（別のプロセスが実行中）: {lock_path}"
            )
        else:
            super().__init__(
                f"ロックを取得できません（kind={kind}）: {lock_path}"
            )
        self.kind: LockKind | None = kind
        self.lock_path = lock_path


class FileLock:
    """OS ファイルロックによるプロセス間排他制御（低レベル primitive）.

    単一のロックファイルに対するノンブロッキング排他取得を提供する。
    相互排他を含む高レベルの排他制御は :class:`PipelineLock` を使う。

    コンテキストマネージャとして使用する::

        lock = FileLock(source_store_dir / ".write.lock")
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

        ``os.open`` と flock の両方が成功した時のみ ``self._fd`` に値を設定する。
        途中で例外（``KeyboardInterrupt`` 含む）が発生した場合、fd を確実に
        close してから送出する。

        Raises:
            LockAcquisitionError: 別プロセスがロックを保持している場合
                （``kind=None`` で送出される。操作種別の付与は
                :class:`PipelineLock` が担当する）
        """
        # ロックファイルの親ディレクトリが存在することを確認
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)

        # ロックファイルを開く（なければ作成）
        fd = os.open(
            str(self._lock_path),
            os.O_CREAT | os.O_RDWR,
        )

        try:
            if sys.platform == "win32":
                self._lock_windows_fd(fd)
            else:
                self._lock_unix_fd(fd)
        except BaseException:
            # LockAcquisitionError / KeyboardInterrupt / その他を問わず、
            # ロック取得に失敗した場合は fd を確実に閉じる
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        # ここまで到達したらロック取得成功。self._fd に代入する
        self._fd = fd

    def release(self) -> None:
        """ロックを解放する."""
        if self._fd is None:
            return

        fd = self._fd
        self._fd = None
        try:
            if sys.platform == "win32":
                self._unlock_windows_fd(fd)
            else:
                self._unlock_unix_fd(fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _lock_unix_fd(self, fd: int) -> None:
        """Unix 系 OS でのロック取得."""
        import errno
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise LockAcquisitionError(self._lock_path) from e
            raise  # 権限エラー等はそのまま伝播

    def _unlock_unix_fd(self, fd: int) -> None:
        """Unix 系 OS でのロック解放."""
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]

    def _lock_windows_fd(self, fd: int) -> None:
        """Windows でのロック取得."""
        import errno
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EDEADLOCK):
                raise LockAcquisitionError(self._lock_path) from e
            raise

    def _unlock_windows_fd(self, fd: int) -> None:
        """Windows でのロック解放."""
        import msvcrt

        # ファイルポインタを先頭に戻してから解放
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]

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


class PipelineLock:
    """write / rebuild の相互排他を実現する合成ロック（修正版案I）.

    設計上の非対称性により、``LockAcquisitionError.kind`` は相手プロセスの
    実操作種別を正確に示す:

    - **rebuild**: ``rebuild_lock`` → ``write_lock`` の順に両方保持する
    - **write**: ``rebuild_lock`` を NB プリチェック → 即 release し、
      ``write_lock`` のみ保持する

    自側ロックを先に NB 試行することで、同種衝突は第 1 ステップで検出される。
    cross-type 衝突は適切なロックを取得しようとした段階で検出される。

    全 4 パターンでの判定:

    ===========  =========  ==============================  ============
    先行         後続       後続の失敗箇所                  ``kind``
    ===========  =========  ==============================  ============
    rebuild      write      ① rebuild_lock プリチェック    ``"rebuild"``
    rebuild      rebuild    ① rebuild_lock 本取得          ``"rebuild"``
    write        write      ② write_lock 本取得            ``"write"``
    write        rebuild    ② write_lock 本取得            ``"write"``
    ===========  =========  ==============================  ============

    全てのロック取得は NB なので古典的 deadlock は発生しない。

    中断耐性: :meth:`acquire` は ``BaseException`` で保護されており、
    KeyboardInterrupt やその他の例外が取得中間で発生した場合も、
    既に取得済みのロックを確実に release する。
    """

    def __init__(
        self, source_store_dir: Path, operation: LockKind,
    ) -> None:
        self._operation: LockKind = operation
        self._write_path = source_store_dir / WRITE_LOCK_FILENAME
        self._rebuild_path = source_store_dir / REBUILD_LOCK_FILENAME

        # 取得順序と保持戦略は operation によって異なる（修正版案I）
        # rebuild: rebuild_lock → write_lock（両方保持）
        # write:   rebuild_lock プリチェック → write_lock（write_lock のみ保持）
        if operation == "rebuild":
            self._primary: FileLock = FileLock(self._rebuild_path)
            self._primary_kind: LockKind = "rebuild"
            self._secondary: FileLock | None = FileLock(self._write_path)
            self._secondary_kind: LockKind | None = "write"
        elif operation == "write":
            self._primary = FileLock(self._write_path)
            self._primary_kind = "write"
            self._secondary = None
            self._secondary_kind = None
        else:
            # LockKind の全メンバーを網羅していることを型チェッカーに示す。
            # 新しい LockKind メンバーを追加した場合、この分岐が mypy エラーとなり
            # __init__ の漏れを検出できる
            assert_never(operation)

    @property
    def operation(self) -> LockKind:
        """この PipelineLock が表す自操作の種別."""
        return self._operation

    def _precheck_rebuild_lock(self) -> None:
        """write 操作時の rebuild_lock プリチェック.

        rebuild_lock を NB 取得 → 即 release する。取得できない場合は
        ``kind="rebuild"`` で送出し、呼び出し元は相手が rebuild であることを
        識別できる。``with`` 文で囲むことで KeyboardInterrupt 中断時も
        probe の fd リークを防ぐ。

        Raises:
            LockAcquisitionError: rebuild プロセスが rebuild_lock を保持している場合
        """
        try:
            with FileLock(self._rebuild_path):
                pass  # 取得成功を確認できれば良い。即 release
        except LockAcquisitionError as e:
            raise LockAcquisitionError(
                e.lock_path, kind="rebuild",
            ) from e

    def acquire(self) -> None:
        """ロックを取得する（修正版案I + Ctrl+C 耐性）.

        手順:
        - write の場合: rebuild_lock プリチェック → write_lock 取得
        - rebuild の場合: rebuild_lock 取得 → write_lock 取得

        各ステップでの失敗時、既に取得済みのロックは ``BaseException`` 保護下で
        確実に release される。

        Raises:
            LockAcquisitionError: 競合が発生した場合。``kind`` には相手プロセスの
                実操作種別が設定される
        """
        # write の場合: rebuild_lock プリチェックを先に行う
        if self._operation == "write":
            self._precheck_rebuild_lock()

        # primary（自側ロック）取得
        try:
            self._primary.acquire()
        except LockAcquisitionError as e:
            kind = self._primary_kind
            # write_lock 取得失敗時: rebuild がプリチェック後に開始して
            # write_lock を保持している可能性がある。rebuild_lock を追加
            # チェックして、保持されていれば kind を "rebuild" に切り替える
            if self._operation == "write":
                try:
                    probe = FileLock(self._rebuild_path)
                    probe.acquire()
                    probe.release()
                except LockAcquisitionError:
                    kind = "rebuild"
            raise LockAcquisitionError(
                e.lock_path, kind=kind,
            ) from e

        # secondary が無ければここで完了（write の場合）
        if self._secondary is None:
            return

        # rebuild の secondary（write_lock）取得
        # BaseException で保護し、KeyboardInterrupt 等で中断した場合も
        # primary を確実に release する
        assert self._secondary_kind is not None  # secondary と同時に None/not None
        try:
            self._secondary.acquire()
        except LockAcquisitionError as e:
            # secondary 取得失敗 → primary を release
            self._release_primary_safely()
            raise LockAcquisitionError(
                e.lock_path, kind=self._secondary_kind,
            ) from e
        except BaseException:
            # KeyboardInterrupt 等 → primary を release
            self._release_primary_safely()
            raise

    def _release_primary_safely(self) -> None:
        """primary ロックを解放する（二重エラー防止）."""
        try:
            self._primary.release()
        except Exception:
            # release 失敗時は握りつぶす（元の例外を優先するため）
            pass

    def release(self) -> None:
        """保持しているロックを解放する（取得とは逆順）."""
        try:
            if self._secondary is not None:
                self._secondary.release()
        finally:
            self._primary.release()

    def __enter__(self) -> PipelineLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()


def write_lock(source_store_dir: Path) -> PipelineLock:
    """write 操作用の相互排他ロックを返す.

    rebuild_lock をプリチェックし、write_lock のみ保持する合成ロック。
    ingest / delete 等、source_store への書き込みを伴う操作全般で使用する。
    rebuild が実行中の場合は ``LockAcquisitionError(kind="rebuild")``、
    別の write 操作が実行中の場合は ``LockAcquisitionError(kind="write")`` を送出する。

    Args:
        source_store_dir: source_store のディレクトリパス

    Returns:
        write 操作用の PipelineLock インスタンス
    """
    return PipelineLock(source_store_dir, operation="write")


def rebuild_lock(source_store_dir: Path) -> PipelineLock:
    """rebuild 操作用の相互排他ロックを返す.

    rebuild_lock → write_lock の順で両方取得する合成ロック。
    別の rebuild が実行中の場合は ``LockAcquisitionError(kind="rebuild")``、
    write 操作が実行中の場合は ``LockAcquisitionError(kind="write")`` を送出する。

    Args:
        source_store_dir: source_store のディレクトリパス

    Returns:
        rebuild 操作用の PipelineLock インスタンス
    """
    return PipelineLock(source_store_dir, operation="rebuild")

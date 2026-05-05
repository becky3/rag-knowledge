"""インデックス書き込み戦略 — IndexWriteStrategy Protocol と実装.

仕様: docs/specs/indexer.md「バッチ書き込み戦略（IndexWriteStrategy）」
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol

from rag.bm25_index import BM25Index


class IndexWriteStrategy(Protocol):
    """インデックスのバッチ書き込み戦略.

    各インデックス実装（BM25 / Vector Store 等）が「自身の書き込み戦略」を
    context manager として表現する。`Indexer.batch_writes()` は保有する全
    strategy を nest して enter/exit する。
    """

    def __enter__(self) -> None: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class BM25WriteStrategy:
    """BM25 用のバッチ書き込み戦略.

    enter 時に deferred save モードを有効化し、exit 時に必ず flush + 解除する。
    例外発生時も flush を試行する（部分失敗は呼び出し側のエラー集約で扱う）。
    """

    def __init__(self, bm25_index: BM25Index) -> None:
        self._bm25 = bm25_index

    def __enter__(self) -> None:
        self._bm25.set_deferred_save(True)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # flush() が失敗した場合も deferred モードを必ず解除するため try/finally で囲む。
        # ``BM25Index.flush`` は ``_deferred_save`` フラグを参照しないため、flush と
        # deferred 解除の順序は最終状態に影響しない（旧 ``Indexer.bm25_deferred()`` は
        # 解除→flush の順だったが等価）。
        try:
            self._bm25.flush()
        finally:
            self._bm25.set_deferred_save(False)

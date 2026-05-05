"""IndexWriteStrategy / BM25WriteStrategy / Indexer.batch_writes の単体テスト.

仕様: docs/specs/indexer.md「バッチ書き込み戦略（IndexWriteStrategy）」
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest

from rag.indexer.write_strategy import BM25WriteStrategy, IndexWriteStrategy

if TYPE_CHECKING:
    from rag.indexer.indexer import Indexer


class TestBM25WriteStrategy:
    def test_enter_enables_deferred_save(self) -> None:
        bm25 = MagicMock()
        strategy = BM25WriteStrategy(bm25)

        strategy.__enter__()

        bm25.set_deferred_save.assert_called_once_with(True)
        bm25.flush.assert_not_called()

    def test_exit_flushes_and_disables_deferred(self) -> None:
        bm25 = MagicMock()
        strategy = BM25WriteStrategy(bm25)

        strategy.__enter__()
        bm25.set_deferred_save.reset_mock()
        strategy.__exit__(None, None, None)

        bm25.flush.assert_called_once()
        bm25.set_deferred_save.assert_called_once_with(False)

    def test_with_block_normal_path(self) -> None:
        bm25 = MagicMock()
        strategy = BM25WriteStrategy(bm25)

        with strategy:
            assert bm25.set_deferred_save.call_args_list[-1].args == (True,)

        # exit で flush + set_deferred_save(False) が呼ばれる
        bm25.flush.assert_called_once()
        assert bm25.set_deferred_save.call_args_list[-1].args == (False,)

    def test_exit_on_exception_still_flushes(self) -> None:
        bm25 = MagicMock()
        strategy = BM25WriteStrategy(bm25)

        with pytest.raises(RuntimeError, match="boom"), strategy:
            raise RuntimeError("boom")

        # 例外発生時も flush と deferred 解除は実行される
        bm25.flush.assert_called_once()
        assert bm25.set_deferred_save.call_args_list[-1].args == (False,)

    def test_flush_failure_still_disables_deferred(self) -> None:
        bm25 = MagicMock()
        bm25.flush.side_effect = RuntimeError("flush failed")
        strategy = BM25WriteStrategy(bm25)
        strategy.__enter__()
        bm25.set_deferred_save.reset_mock()

        with pytest.raises(RuntimeError, match="flush failed"):
            strategy.__exit__(None, None, None)

        # flush が失敗しても deferred 解除は確実に実行される
        bm25.set_deferred_save.assert_called_once_with(False)


class _RecordingStrategy:
    """テスト用 strategy: enter/exit 順序を記録する."""

    def __init__(
        self, name: str, log: list[str], *, raise_on_exit: bool = False,
    ) -> None:
        self.name = name
        self.log = log
        self._raise_on_exit = raise_on_exit

    def __enter__(self) -> None:
        self.log.append(f"enter:{self.name}")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.log.append(f"exit:{self.name}")
        if self._raise_on_exit:
            raise RuntimeError(f"{self.name} exit error")


def _build_indexer() -> "Indexer":
    """テスト用に Indexer を最小依存（MagicMock）で構築する."""
    from rag.indexer.indexer import Indexer

    return Indexer(
        vector_store=MagicMock(),
        bm25_index=MagicMock(),
        metadata_db=MagicMock(),
        chunk_size=200,
        chunk_overlap=20,
        embedding_prefix_enabled=True,
        embedding_context_length=512,
        worst_token_char_ratio=0.7,
    )


class TestIndexerBatchWrites:
    def test_protocol_compatibility(self) -> None:
        """BM25WriteStrategy が IndexWriteStrategy Protocol を満たすことを検証."""
        bm25 = MagicMock()
        strategy: IndexWriteStrategy = BM25WriteStrategy(bm25)
        assert hasattr(strategy, "__enter__")
        assert hasattr(strategy, "__exit__")

    def test_default_strategies_includes_bm25(self) -> None:
        """Indexer.__init__ で BM25WriteStrategy が組み立てられることを検証."""
        indexer = _build_indexer()
        # 直接の private 参照は意図的: _write_strategies の組み立てが
        # batch_writes() のリグレッション保護対象であるため
        assert len(indexer._write_strategies) == 1
        assert isinstance(indexer._write_strategies[0], BM25WriteStrategy)

    def test_batch_writes_invokes_bm25_strategy(self) -> None:
        """Indexer.batch_writes() で BM25Index の deferred save / flush が呼ばれる."""
        indexer = _build_indexer()
        bm25 = cast("MagicMock", indexer._bm25)

        with indexer.batch_writes():
            assert bm25.set_deferred_save.call_args_list[-1].args == (True,)

        bm25.flush.assert_called_once()
        assert bm25.set_deferred_save.call_args_list[-1].args == (False,)

    def test_batch_writes_enters_all_strategies_in_order(self) -> None:
        """複数 strategy を持つ Indexer の batch_writes が enter/exit 順序を保つ."""
        indexer = _build_indexer()
        log: list[str] = []
        # _write_strategies を _RecordingStrategy 2 個に差し替え
        indexer._write_strategies = [
            _RecordingStrategy("a", log),
            _RecordingStrategy("b", log),
        ]

        with indexer.batch_writes():
            log.append("inside")

        assert log == [
            "enter:a", "enter:b",
            "inside",
            # exit は LIFO 順（ExitStack 仕様）
            "exit:b", "exit:a",
        ]

    def test_batch_writes_exits_all_on_exception(self) -> None:
        """with ブロック内例外時も全 strategy が exit される."""
        indexer = _build_indexer()
        log: list[str] = []
        indexer._write_strategies = [
            _RecordingStrategy("a", log),
            _RecordingStrategy("b", log),
        ]

        with pytest.raises(RuntimeError, match="inside"), indexer.batch_writes():
            raise RuntimeError("inside")

        assert "exit:a" in log
        assert "exit:b" in log

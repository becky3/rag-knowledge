"""バジェットトラッカー.

仕様: docs/specs/rag-knowledge.md

操作あたりのリクエスト総数を追跡し、ハードリミット上限で停止する。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ハードリミット: 設定・引数・環境変数で引き上げ不可
HARD_LIMIT_MAX_TOTAL_REQUESTS = 500


class BudgetExhaustedError(Exception):
    """バジェット上限到達エラー."""

    def __init__(self, used: int, limit: int) -> None:
        self.used = used
        self.limit = limit
        super().__init__(
            f"リクエストバジェット上限到達: {used}/{limit}"
        )


class BudgetTracker:
    """操作あたりのリクエスト総数を追跡・制限する.

    仕様: docs/specs/rag-knowledge.md
    """

    def __init__(self, max_requests: int = HARD_LIMIT_MAX_TOTAL_REQUESTS) -> None:
        """BudgetTracker を初期化する.

        Args:
            max_requests: 操作あたりのリクエスト上限。
                ハードリミットを超える値はクランプされる。
        """
        if max_requests > HARD_LIMIT_MAX_TOTAL_REQUESTS:
            logger.warning(
                "max_requests=%d がハードリミット %d を超過。クランプします",
                max_requests,
                HARD_LIMIT_MAX_TOTAL_REQUESTS,
            )
            max_requests = HARD_LIMIT_MAX_TOTAL_REQUESTS
        if max_requests < 1:
            logger.warning(
                "max_requests=%d が 1 未満。1 にクランプします",
                max_requests,
            )
            max_requests = 1
        self._max_requests = max_requests
        self._used = 0

    @property
    def used(self) -> int:
        """消費済みリクエスト数."""
        return self._used

    @property
    def remaining(self) -> int:
        """残りリクエスト数."""
        return max(0, self._max_requests - self._used)

    @property
    def limit(self) -> int:
        """設定された上限値."""
        return self._max_requests

    def consume(self) -> None:
        """リクエスト 1 件分のバジェットを消費する.

        Raises:
            BudgetExhaustedError: バジェット上限に達した場合
        """
        if self._used >= self._max_requests:
            raise BudgetExhaustedError(self._used, self._max_requests)
        self._used += 1
        if self._used == self._max_requests:
            logger.warning(
                "バジェット上限到達: %d/%d リクエスト",
                self._used,
                self._max_requests,
            )

    def reset(self) -> None:
        """カウンタをリセットする."""
        self._used = 0

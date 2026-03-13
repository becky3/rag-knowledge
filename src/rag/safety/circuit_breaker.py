"""サーキットブレーカー.

仕様: docs/specs/rag-knowledge.md

連続失敗回数を監視し、閾値超過で操作を中断する。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ハードリミット: 設定・引数・環境変数で引き上げ不可
HARD_LIMIT_CONSECUTIVE_FAILURES = 5


class CircuitBreakerOpenError(Exception):
    """サーキットブレーカー発動エラー."""

    def __init__(self, consecutive_failures: int, threshold: int) -> None:
        self.consecutive_failures = consecutive_failures
        self.threshold = threshold
        super().__init__(
            f"サーキットブレーカー発動: {consecutive_failures} 回連続失敗（閾値: {threshold}）"
        )


class CircuitBreaker:
    """連続失敗回数を監視し、閾値超過で操作を中断する.

    仕様: docs/specs/rag-knowledge.md
    """

    def __init__(self, threshold: int = HARD_LIMIT_CONSECUTIVE_FAILURES) -> None:
        """CircuitBreaker を初期化する.

        Args:
            threshold: 連続失敗の閾値。
                ハードリミットを超える値はクランプされる。
        """
        if threshold > HARD_LIMIT_CONSECUTIVE_FAILURES:
            logger.warning(
                "threshold=%d がハードリミット %d を超過。クランプします",
                threshold,
                HARD_LIMIT_CONSECUTIVE_FAILURES,
            )
            threshold = HARD_LIMIT_CONSECUTIVE_FAILURES
        if threshold < 1:
            logger.warning(
                "threshold=%d が 1 未満。1 にクランプします",
                threshold,
            )
            threshold = 1
        self._threshold = threshold
        self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        """現在の連続失敗回数."""
        return self._consecutive_failures

    @property
    def threshold(self) -> int:
        """設定された閾値."""
        return self._threshold

    @property
    def is_open(self) -> bool:
        """サーキットブレーカーが発動中かどうか."""
        return self._consecutive_failures >= self._threshold

    def record_success(self) -> None:
        """成功を記録し、連続失敗カウンタをリセットする."""
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        """失敗を記録する.

        Raises:
            CircuitBreakerOpenError: 連続失敗が閾値に達した場合
        """
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold:
            logger.warning(
                "サーキットブレーカー発動: %d 回連続失敗（閾値: %d）",
                self._consecutive_failures,
                self._threshold,
            )
            raise CircuitBreakerOpenError(
                self._consecutive_failures, self._threshold
            )

    def reset(self) -> None:
        """カウンタをリセットする."""
        self._consecutive_failures = 0

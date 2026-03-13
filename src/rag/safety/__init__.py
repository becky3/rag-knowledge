"""制約付き中間ライブラリ（Constrained Client）.

仕様: docs/specs/rag-knowledge.md

全ての外部 HTTP リクエストのゲートウェイとして機能し、
ハードリミット・バジェットトラッカー・サーキットブレーカーを統合する。
"""

from rag.safety.budget_tracker import BudgetExhaustedError, BudgetTracker
from rag.safety.circuit_breaker import CircuitBreakerOpenError, CircuitBreaker
from rag.safety.constrained_client import ConstrainedClient

__all__ = [
    "BudgetExhaustedError",
    "BudgetTracker",
    "CircuitBreakerOpenError",
    "CircuitBreaker",
    "ConstrainedClient",
]

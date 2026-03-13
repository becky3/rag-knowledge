"""制約付き中間ライブラリの不変条件 — プロパティベーステスト.

テスト方針:
- Hypothesis による不変条件の網羅的検証
- いかなる引数の組み合わせでも安全制約が破られないことを証明する
- 実際の HTTP 通信は行わず、安全機構のロジックのみをテスト

対象の不変条件:
1. リクエスト総数上限: effective 値が HARD_LIMIT_MAX_TOTAL_REQUESTS を超えない
2. リクエスト間隔下限: effective 値が HARD_LIMIT_MIN_REQUEST_INTERVAL を下回らない
3. 操作タイムアウト・リクエストタイムアウト: effective 値がハードリミットを超えない
4. バジェット消費の単調性: consume() のたびに remaining は単調減少する
5. サーキットブレーカー: effective threshold が [1, HARD_LIMIT] 範囲内
6. ConstrainedClient 統合: いかなるパラメータ組み合わせでも全制約がハードリミット内
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from rag.safety.budget_tracker import (
    BudgetExhaustedError,
    BudgetTracker,
    HARD_LIMIT_MAX_TOTAL_REQUESTS,
)
from rag.safety.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    HARD_LIMIT_CONSECUTIVE_FAILURES,
)
from rag.safety.constrained_client import (
    ConstrainedClient,
    HARD_LIMIT_MIN_REQUEST_INTERVAL,
    HARD_LIMIT_OPERATION_TIMEOUT,
    _clamp_request_interval,
    _clamp_request_timeout,
)


# ---------------------------------------------------------------------------
# 戦略定義
# ---------------------------------------------------------------------------

# 極端値を含む整数（負数・0・極大値）
any_int = st.integers(min_value=-1000, max_value=100_000)

# 極端値を含む浮動小数点数（負数・0 付近・極大値）
any_float = st.floats(min_value=-100.0, max_value=10_000.0, allow_nan=False)


# ---------------------------------------------------------------------------
# 不変条件 1: リクエスト総数上限
# ---------------------------------------------------------------------------


class TestBudgetTrackerInvariants:
    """BudgetTracker の不変条件."""

    @given(max_requests=any_int)
    @settings(max_examples=200)
    def test_effective_limit_never_exceeds_hard_limit(
        self, max_requests: int
    ) -> None:
        """いかなる max_requests でも effective 上限は HARD_LIMIT 以下."""
        tracker = BudgetTracker(max_requests=max_requests)
        assert tracker.limit <= HARD_LIMIT_MAX_TOTAL_REQUESTS

    @given(max_requests=any_int)
    @settings(max_examples=200)
    def test_effective_limit_at_least_one(self, max_requests: int) -> None:
        """いかなる max_requests でも effective 上限は 1 以上."""
        tracker = BudgetTracker(max_requests=max_requests)
        assert tracker.limit >= 1

    @given(max_requests=st.integers(min_value=1, max_value=600))
    @settings(max_examples=200)
    def test_cannot_consume_more_than_limit(self, max_requests: int) -> None:
        """consume 可能な回数は effective limit を超えない."""
        tracker = BudgetTracker(max_requests=max_requests)
        effective_limit = tracker.limit
        consumed = 0
        for _ in range(effective_limit + 1):
            try:
                tracker.consume()
                consumed += 1
            except BudgetExhaustedError:
                break
        assert consumed == effective_limit
        assert consumed <= HARD_LIMIT_MAX_TOTAL_REQUESTS

    @given(
        max_requests=st.integers(min_value=1, max_value=100),
        n_consumes=st.integers(min_value=0, max_value=100),
    )
    @settings(max_examples=200)
    def test_remaining_is_monotonically_decreasing(
        self, max_requests: int, n_consumes: int
    ) -> None:
        """consume のたびに remaining は単調減少する."""
        tracker = BudgetTracker(max_requests=max_requests)
        prev_remaining = tracker.remaining

        for _ in range(n_consumes):
            try:
                tracker.consume()
            except BudgetExhaustedError:
                break
            assert tracker.remaining < prev_remaining
            prev_remaining = tracker.remaining

    @given(
        max_requests=st.integers(min_value=1, max_value=100),
        n_consumes=st.integers(min_value=0, max_value=150),
    )
    @settings(max_examples=200)
    def test_used_plus_remaining_equals_limit(
        self, max_requests: int, n_consumes: int
    ) -> None:
        """used + remaining は常に limit と等しい."""
        tracker = BudgetTracker(max_requests=max_requests)
        # 初期状態でも保存則が成立することを検証
        assert tracker.used + tracker.remaining == tracker.limit
        for _ in range(n_consumes):
            try:
                tracker.consume()
            except BudgetExhaustedError:
                break
            assert tracker.used + tracker.remaining == tracker.limit
        # ループ終了後も検証（exhaust 後を含む）
        assert tracker.used + tracker.remaining == tracker.limit

    @given(max_requests=any_int)
    @settings(max_examples=200)
    def test_remaining_never_negative(self, max_requests: int) -> None:
        """remaining は常に非負."""
        tracker = BudgetTracker(max_requests=max_requests)
        assert tracker.remaining >= 0
        # exhaust してからも確認
        for _ in range(HARD_LIMIT_MAX_TOTAL_REQUESTS + 1):
            try:
                tracker.consume()
            except BudgetExhaustedError:
                break
        assert tracker.remaining >= 0


# ---------------------------------------------------------------------------
# 不変条件 2: リクエスト間隔下限
# ---------------------------------------------------------------------------


class TestRequestIntervalInvariants:
    """リクエスト間隔の不変条件."""

    @given(interval=any_float)
    @settings(max_examples=200)
    def test_effective_interval_never_below_hard_limit(
        self, interval: float
    ) -> None:
        """いかなる request_interval でもクランプ後は HARD_LIMIT 以上."""
        clamped = _clamp_request_interval(interval)
        assert clamped >= HARD_LIMIT_MIN_REQUEST_INTERVAL

    @given(interval=any_float)
    @settings(max_examples=200)
    def test_effective_interval_at_most_sixty(self, interval: float) -> None:
        """いかなる request_interval でもクランプ後は 60 以下."""
        clamped = _clamp_request_interval(interval)
        # 上限 60.0 は _clamp_request_interval 内のハードコーディング値
        assert clamped <= 60.0

    @given(interval=st.floats(min_value=0.5, max_value=60.0))
    @settings(max_examples=200)
    def test_valid_range_preserved(self, interval: float) -> None:
        """許容範囲内の値はそのまま保持される."""
        clamped = _clamp_request_interval(interval)
        assert clamped == interval


# ---------------------------------------------------------------------------
# 不変条件 3a: リクエストタイムアウト
# ---------------------------------------------------------------------------


class TestRequestTimeoutInvariants:
    """リクエストタイムアウトの不変条件."""

    @given(timeout=any_float)
    @settings(max_examples=200)
    def test_request_timeout_within_bounds(self, timeout: float) -> None:
        """いかなる request_timeout でもクランプ後は [1, 120] 範囲内."""
        clamped = _clamp_request_timeout(timeout)
        # 範囲 [1.0, 120.0] は _clamp_request_timeout 内のハードコーディング値
        assert 1.0 <= clamped <= 120.0


# ---------------------------------------------------------------------------
# 不変条件 3b: 操作タイムアウト
# ---------------------------------------------------------------------------


class TestOperationTimeoutInvariants:
    """操作タイムアウトの不変条件."""

    @given(timeout=any_float)
    @settings(max_examples=200)
    def test_effective_timeout_never_exceeds_hard_limit(
        self, timeout: float
    ) -> None:
        """いかなる operation_timeout でもクランプ後は HARD_LIMIT 以下."""
        client = ConstrainedClient(operation_timeout=timeout)
        assert client._operation_timeout <= HARD_LIMIT_OPERATION_TIMEOUT


# ---------------------------------------------------------------------------
# 不変条件 4: サーキットブレーカー
# ---------------------------------------------------------------------------


class TestCircuitBreakerInvariants:
    """CircuitBreaker の不変条件."""

    @given(threshold=any_int)
    @settings(max_examples=200)
    def test_effective_threshold_never_exceeds_hard_limit(
        self, threshold: int
    ) -> None:
        """いかなる threshold でも effective 値は HARD_LIMIT 以下."""
        cb = CircuitBreaker(threshold=threshold)
        assert cb.threshold <= HARD_LIMIT_CONSECUTIVE_FAILURES

    @given(threshold=any_int)
    @settings(max_examples=200)
    def test_effective_threshold_at_least_one(self, threshold: int) -> None:
        """いかなる threshold でも effective 値は 1 以上."""
        cb = CircuitBreaker(threshold=threshold)
        assert cb.threshold >= 1

    @given(threshold=st.integers(min_value=1, max_value=10))
    @settings(max_examples=200)
    def test_opens_exactly_at_threshold(self, threshold: int) -> None:
        """連続失敗が effective threshold に達したときに発動する."""
        cb = CircuitBreaker(threshold=threshold)
        effective = cb.threshold  # クランプ後の値

        for i in range(effective - 1):
            try:
                cb.record_failure()
            except CircuitBreakerOpenError:
                pytest.fail(
                    f"threshold={effective} なのに {i + 1} 回目で発動した"
                )
        assert not cb.is_open

        with pytest.raises(CircuitBreakerOpenError):
            cb.record_failure()
        assert cb.is_open

    @given(
        threshold=st.integers(min_value=1, max_value=5),
        successes_before=st.integers(min_value=0, max_value=10),
    )
    @settings(max_examples=200)
    def test_success_resets_failure_count(
        self, threshold: int, successes_before: int
    ) -> None:
        """success 記録で連続失敗カウンタがリセットされる."""
        cb = CircuitBreaker(threshold=threshold)
        effective = cb.threshold

        # 成功を複数回記録してもカウンタは 0 のまま
        for _ in range(successes_before):
            cb.record_success()
        assert cb.consecutive_failures == 0

        # 閾値の直前まで失敗させる
        for _ in range(effective - 1):
            try:
                cb.record_failure()
            except CircuitBreakerOpenError:
                break

        # success で reset
        cb.record_success()
        assert cb.consecutive_failures == 0
        assert not cb.is_open


# ---------------------------------------------------------------------------
# 不変条件 5: ConstrainedClient の統合的な制約
# ---------------------------------------------------------------------------


class TestConstrainedClientInvariants:
    """ConstrainedClient 構成パラメータの不変条件."""

    @given(
        request_timeout=st.floats(min_value=-10.0, max_value=1000.0, allow_nan=False),
        request_interval=st.floats(min_value=-10.0, max_value=1000.0, allow_nan=False),
        max_requests=st.integers(min_value=-100, max_value=10_000),
        circuit_breaker_threshold=st.integers(min_value=-100, max_value=1_000),
        operation_timeout=st.floats(min_value=-10.0, max_value=10_000.0, allow_nan=False),
    )
    @settings(max_examples=200)
    def test_all_constraints_within_hard_limits(
        self,
        request_timeout: float,
        request_interval: float,
        max_requests: int,
        circuit_breaker_threshold: int,
        operation_timeout: float,
    ) -> None:
        """いかなるパラメータ組み合わせでも全制約がハードリミット内に収まる."""
        client = ConstrainedClient(
            request_timeout=request_timeout,
            request_interval=request_interval,
            max_requests=max_requests,
            circuit_breaker_threshold=circuit_breaker_threshold,
            operation_timeout=operation_timeout,
        )
        # バジェット: [1, 500]（公開プロパティ経由）
        assert 1 <= client.budget.limit <= HARD_LIMIT_MAX_TOTAL_REQUESTS
        # サーキットブレーカー: [1, 5]（公開プロパティ経由）
        assert 1 <= client.circuit_breaker.threshold <= HARD_LIMIT_CONSECUTIVE_FAILURES
        # 操作タイムアウト: ≤ 600（公開プロパティなし、private アクセス）
        assert client._operation_timeout <= HARD_LIMIT_OPERATION_TIMEOUT
        # リクエストタイムアウト: [1, 120]（公開プロパティなし、private アクセス）
        assert 1.0 <= client._request_timeout <= 120.0
        # リクエスト間隔: [HARD_LIMIT, 60]（公開プロパティなし、private アクセス）
        assert HARD_LIMIT_MIN_REQUEST_INTERVAL <= client._request_interval <= 60.0

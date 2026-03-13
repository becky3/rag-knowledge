"""制約付き中間ライブラリのテスト.

テスト方針:
- BudgetTracker: バジェット消費・上限到達・ハードリミットクランプ
- CircuitBreaker: 連続失敗カウント・閾値到達・成功リセット・ハードリミットクランプ
- ConstrainedClient: 統合テスト（バジェット・サーキットブレーカー・レート制限・タイムアウト）
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

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


# --- BudgetTracker ---


class TestBudgetTracker:
    """BudgetTracker のテスト."""

    def test_initial_state(self) -> None:
        bt = BudgetTracker(max_requests=10)
        assert bt.used == 0
        assert bt.remaining == 10
        assert bt.limit == 10

    def test_consume(self) -> None:
        bt = BudgetTracker(max_requests=3)
        bt.consume()
        assert bt.used == 1
        assert bt.remaining == 2

    def test_consume_until_exhausted(self) -> None:
        bt = BudgetTracker(max_requests=2)
        bt.consume()
        bt.consume()
        with pytest.raises(BudgetExhaustedError) as exc_info:
            bt.consume()
        assert exc_info.value.used == 2
        assert exc_info.value.limit == 2

    def test_reset(self) -> None:
        bt = BudgetTracker(max_requests=2)
        bt.consume()
        bt.consume()
        bt.reset()
        assert bt.used == 0
        assert bt.remaining == 2

    def test_clamp_over_hard_limit(self) -> None:
        bt = BudgetTracker(max_requests=HARD_LIMIT_MAX_TOTAL_REQUESTS + 100)
        assert bt.limit == HARD_LIMIT_MAX_TOTAL_REQUESTS

    def test_clamp_below_minimum(self) -> None:
        bt = BudgetTracker(max_requests=0)
        assert bt.limit == 1

    def test_hard_limit_cannot_be_raised(self) -> None:
        """ハードリミットを超える値を設定しても上限でクランプされる."""
        bt = BudgetTracker(max_requests=999)
        assert bt.limit == HARD_LIMIT_MAX_TOTAL_REQUESTS
        # ハードリミット回数分消費できる
        for _ in range(HARD_LIMIT_MAX_TOTAL_REQUESTS):
            bt.consume()
        with pytest.raises(BudgetExhaustedError):
            bt.consume()


# --- CircuitBreaker ---


class TestCircuitBreaker:
    """CircuitBreaker のテスト."""

    def test_initial_state(self) -> None:
        cb = CircuitBreaker(threshold=3)
        assert cb.consecutive_failures == 0
        assert cb.threshold == 3
        assert not cb.is_open

    def test_record_failure_below_threshold(self) -> None:
        cb = CircuitBreaker(threshold=3)
        cb.record_failure()
        assert cb.consecutive_failures == 1
        assert not cb.is_open

    def test_record_failure_at_threshold(self) -> None:
        cb = CircuitBreaker(threshold=3)
        cb.record_failure()
        cb.record_failure()
        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            cb.record_failure()
        assert exc_info.value.consecutive_failures == 3
        assert cb.is_open

    def test_record_success_resets(self) -> None:
        cb = CircuitBreaker(threshold=5)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.consecutive_failures == 0
        assert not cb.is_open

    def test_reset(self) -> None:
        cb = CircuitBreaker(threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.reset()
        assert cb.consecutive_failures == 0

    def test_clamp_over_hard_limit(self) -> None:
        cb = CircuitBreaker(threshold=HARD_LIMIT_CONSECUTIVE_FAILURES + 10)
        assert cb.threshold == HARD_LIMIT_CONSECUTIVE_FAILURES

    def test_clamp_below_minimum(self) -> None:
        cb = CircuitBreaker(threshold=0)
        assert cb.threshold == 1

    def test_hard_limit_cannot_be_raised(self) -> None:
        """ハードリミットを超える閾値を設定してもクランプされる."""
        cb = CircuitBreaker(threshold=100)
        assert cb.threshold == HARD_LIMIT_CONSECUTIVE_FAILURES
        # ハードリミット - 1 回まで記録可能
        for _ in range(HARD_LIMIT_CONSECUTIVE_FAILURES - 1):
            cb.record_failure()
        assert not cb.is_open
        with pytest.raises(CircuitBreakerOpenError):
            cb.record_failure()


# --- ConstrainedClient ---


class TestClampFunctions:
    """クランプ関数のテスト."""

    def test_clamp_request_timeout_normal(self) -> None:
        assert _clamp_request_timeout(30.0) == 30.0

    def test_clamp_request_timeout_too_low(self) -> None:
        assert _clamp_request_timeout(0.5) == 1.0

    def test_clamp_request_timeout_too_high(self) -> None:
        assert _clamp_request_timeout(200.0) == 120.0

    def test_clamp_request_interval_normal(self) -> None:
        assert _clamp_request_interval(1.0) == 1.0

    def test_clamp_request_interval_too_low(self) -> None:
        assert _clamp_request_interval(0.1) == HARD_LIMIT_MIN_REQUEST_INTERVAL

    def test_clamp_request_interval_too_high(self) -> None:
        assert _clamp_request_interval(100.0) == 60.0


class TestConstrainedClient:
    """ConstrainedClient のテスト."""

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        """async context manager として正しく動作する."""
        cc = ConstrainedClient(request_timeout=5.0, request_interval=0.5)
        async with cc:
            assert cc._session is not None
        assert cc._session is None

    @pytest.mark.asyncio
    async def test_get_without_context_manager_raises(self) -> None:
        """context manager 外での呼び出しで RuntimeError."""
        cc = ConstrainedClient()
        with pytest.raises(RuntimeError, match="context manager"):
            await cc.get("http://example.com")

    @pytest.mark.asyncio
    async def test_budget_exhaustion(self) -> None:
        """バジェット上限で BudgetExhaustedError."""
        cc = ConstrainedClient(
            max_requests=2,
            request_interval=0.5,
            request_timeout=5.0,
        )
        mock_resp = MagicMock(spec=aiohttp.ClientResponse)

        async with cc:
            with patch.object(cc._session, "get", new_callable=AsyncMock, return_value=mock_resp):
                await cc.get("http://example.com/1")
                await cc.get("http://example.com/2")
                with pytest.raises(BudgetExhaustedError):
                    await cc.get("http://example.com/3")

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens(self) -> None:
        """連続失敗でサーキットブレーカー発動."""
        cc = ConstrainedClient(
            circuit_breaker_threshold=2,
            request_interval=0.5,
            request_timeout=5.0,
            max_requests=100,
        )

        async with cc:
            with patch.object(
                cc._session,
                "get",
                new_callable=AsyncMock,
                side_effect=aiohttp.ClientError("connection failed"),
            ):
                # 1回目: 失敗 → record_failure (1)
                with pytest.raises(aiohttp.ClientError):
                    await cc.get("http://example.com/1")

                # 2回目: 失敗 → record_failure (2) → CircuitBreakerOpenError
                with pytest.raises(CircuitBreakerOpenError):
                    await cc.get("http://example.com/2")

    @pytest.mark.asyncio
    async def test_circuit_breaker_resets_on_success(self) -> None:
        """成功でサーキットブレーカーがリセットされる."""
        cc = ConstrainedClient(
            circuit_breaker_threshold=3,
            request_interval=0.5,
            request_timeout=5.0,
            max_requests=100,
        )
        mock_resp = MagicMock(spec=aiohttp.ClientResponse)

        async with cc:
            # 2回失敗
            with patch.object(
                cc._session,
                "get",
                new_callable=AsyncMock,
                side_effect=aiohttp.ClientError("fail"),
            ):
                with pytest.raises(aiohttp.ClientError):
                    await cc.get("http://example.com/1")
                with pytest.raises(aiohttp.ClientError):
                    await cc.get("http://example.com/2")

            assert cc.circuit_breaker.consecutive_failures == 2

            # 1回成功 → リセット
            with patch.object(cc._session, "get", new_callable=AsyncMock, return_value=mock_resp):
                await cc.get("http://example.com/3")

            assert cc.circuit_breaker.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_operation_timeout(self) -> None:
        """操作全体タイムアウトで TimeoutError."""
        cc = ConstrainedClient(
            operation_timeout=0.1,
            request_interval=0.5,
            request_timeout=5.0,
            max_requests=100,
        )
        mock_resp = MagicMock(spec=aiohttp.ClientResponse)
        fake_now = [1000.0]

        with patch("rag.safety.constrained_client.time") as mock_time:
            mock_time.monotonic.side_effect = lambda: fake_now[0]
            async with cc:
                with patch.object(cc._session, "get", new_callable=AsyncMock, return_value=mock_resp):
                    await cc.get("http://example.com/1")

                    # 疑似的にタイムアウトを超過させる
                    fake_now[0] += 0.15

                    with pytest.raises(TimeoutError, match="操作全体タイムアウト"):
                        await cc.get("http://example.com/2")

    @pytest.mark.asyncio
    async def test_rate_limiting(self) -> None:
        """レート制限が効いている（リクエスト間隔が守られる）."""
        cc = ConstrainedClient(
            request_interval=0.5,
            request_timeout=5.0,
            max_requests=100,
            operation_timeout=10.0,
        )
        mock_resp = MagicMock(spec=aiohttp.ClientResponse)
        fake_now = [1000.0]

        async def advance_time(seconds: float) -> None:
            """sleep 呼び出し時に疑似時間を進める."""
            fake_now[0] += seconds

        with patch("rag.safety.constrained_client.time") as mock_time, \
             patch.object(asyncio, "sleep", new_callable=AsyncMock, side_effect=advance_time) as mock_sleep:
            mock_time.monotonic.side_effect = lambda: fake_now[0]
            async with cc:
                with patch.object(cc._session, "get", new_callable=AsyncMock, return_value=mock_resp):
                    await cc.get("http://example.com/1")
                    await cc.get("http://example.com/2")

                    # sleep が呼ばれ、要求された待機時間が request_interval 以下であること
                    assert mock_sleep.call_count == 1
                    sleep_duration = mock_sleep.call_args[0][0]
                    assert 0 < sleep_duration <= 0.5

    @pytest.mark.asyncio
    async def test_operation_timeout_clamped(self) -> None:
        """操作タイムアウトがハードリミットにクランプされる."""
        cc = ConstrainedClient(operation_timeout=9999.0)
        assert cc._operation_timeout == HARD_LIMIT_OPERATION_TIMEOUT

    @pytest.mark.asyncio
    async def test_allow_redirects_default_false(self) -> None:
        """デフォルトでリダイレクト追従が無効（SSRF対策）."""
        cc = ConstrainedClient(request_timeout=5.0, request_interval=0.5)
        mock_resp = MagicMock(spec=aiohttp.ClientResponse)

        async with cc:
            with patch.object(cc._session, "get", new_callable=AsyncMock, return_value=mock_resp) as mock_get:
                await cc.get("http://example.com")
                mock_get.assert_called_once_with(
                    "http://example.com", allow_redirects=False
                )

    @pytest.mark.asyncio
    async def test_post_applies_constraints(self) -> None:
        """POST でもバジェット・サーキットブレーカー等の制約が適用される."""
        cc = ConstrainedClient(
            max_requests=2,
            request_interval=0.5,
            request_timeout=5.0,
        )
        mock_resp = MagicMock(spec=aiohttp.ClientResponse)

        async with cc:
            with patch.object(cc._session, "post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
                await cc.post("http://example.com/api", json={"key": "value"}, params={"q": "test"})
                mock_post.assert_called_once_with(
                    "http://example.com/api",
                    json={"key": "value"},
                    params={"q": "test"},
                    allow_redirects=False,
                )
                assert cc.budget.used == 1

                await cc.post("http://example.com/api2")
                with pytest.raises(BudgetExhaustedError):
                    await cc.post("http://example.com/api3")

    @pytest.mark.asyncio
    async def test_post_without_context_manager_raises(self) -> None:
        """context manager 外での POST 呼び出しで RuntimeError."""
        cc = ConstrainedClient()
        with pytest.raises(RuntimeError, match="context manager"):
            await cc.post("http://example.com")

    @pytest.mark.asyncio
    async def test_post_circuit_breaker(self) -> None:
        """POST でもサーキットブレーカーが機能する."""
        cc = ConstrainedClient(
            circuit_breaker_threshold=2,
            request_interval=0.5,
            request_timeout=5.0,
            max_requests=100,
        )

        async with cc:
            with patch.object(
                cc._session,
                "post",
                new_callable=AsyncMock,
                side_effect=aiohttp.ClientError("connection failed"),
            ):
                with pytest.raises(aiohttp.ClientError):
                    await cc.post("http://example.com/1")

                with pytest.raises(CircuitBreakerOpenError):
                    await cc.post("http://example.com/2")

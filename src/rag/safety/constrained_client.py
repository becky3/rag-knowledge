"""制約付き HTTP クライアント.

仕様: docs/specs/rag-knowledge.md

aiohttp.ClientSession をラップし、ハードリミット・バジェット・
サーキットブレーカー・レート制限を統合する。
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import TracebackType

import aiohttp

from rag.safety.budget_tracker import BudgetTracker, HARD_LIMIT_MAX_TOTAL_REQUESTS
from rag.safety.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    HARD_LIMIT_CONSECUTIVE_FAILURES,
)

logger = logging.getLogger(__name__)

# ハードリミット: 設定・引数・環境変数で引き上げ不可
HARD_LIMIT_MIN_REQUEST_INTERVAL = 0.5  # 秒
HARD_LIMIT_OPERATION_TIMEOUT = 600.0  # 秒


def _clamp_request_timeout(value: float) -> float:
    """リクエストタイムアウトを許容範囲にクランプする."""
    clamped = max(1.0, min(value, 120.0))
    if clamped != value:
        logger.warning(
            "request_timeout=%.1f を許容範囲 [1, 120] にクランプ: %.1f",
            value,
            clamped,
        )
    return clamped


def _clamp_request_interval(value: float) -> float:
    """リクエスト間隔を許容範囲にクランプする."""
    clamped = max(HARD_LIMIT_MIN_REQUEST_INTERVAL, min(value, 60.0))
    if clamped != value:
        logger.warning(
            "request_interval=%.1f を許容範囲 [%.1f, 60] にクランプ: %.1f",
            value,
            HARD_LIMIT_MIN_REQUEST_INTERVAL,
            clamped,
        )
    return clamped


def _clamp_operation_timeout(value: float) -> float:
    """操作全体タイムアウトを許容範囲にクランプする."""
    clamped = max(1.0, min(value, HARD_LIMIT_OPERATION_TIMEOUT))
    if clamped != value:
        logger.warning(
            "operation_timeout=%.1f を許容範囲 [1, %.1f] にクランプ: %.1f",
            value,
            HARD_LIMIT_OPERATION_TIMEOUT,
            clamped,
        )
    return clamped


class ConstrainedClient:
    """制約付き HTTP クライアント.

    全ての外部 HTTP リクエストのゲートウェイとして機能する。
    async context manager として使用する。

    仕様: docs/specs/rag-knowledge.md
    """

    def __init__(
        self,
        *,
        request_timeout: float = 30.0,
        request_interval: float = 1.0,
        max_requests: int = HARD_LIMIT_MAX_TOTAL_REQUESTS,
        circuit_breaker_threshold: int = HARD_LIMIT_CONSECUTIVE_FAILURES,
        operation_timeout: float = HARD_LIMIT_OPERATION_TIMEOUT,
        headers: dict[str, str] | None = None,
    ) -> None:
        """ConstrainedClient を初期化する.

        全てのパラメータはハードリミット以下にクランプされる。

        Args:
            request_timeout: 個別リクエストのタイムアウト（秒）。許容範囲: 1〜120
            request_interval: リクエスト間の最低間隔（秒）。許容範囲: 0.5〜60
            max_requests: 操作あたりのリクエスト上限。上限: 500
            circuit_breaker_threshold: サーキットブレーカー閾値。上限: 5
            operation_timeout: 操作全体のタイムアウト（秒）。許容範囲: 1〜600
            headers: HTTP ヘッダー
        """
        self._request_timeout = _clamp_request_timeout(request_timeout)
        self._request_interval = _clamp_request_interval(request_interval)
        self._operation_timeout = _clamp_operation_timeout(operation_timeout)
        self._headers = headers or {}
        self._budget = BudgetTracker(max_requests=max_requests)
        self._circuit_breaker = CircuitBreaker(threshold=circuit_breaker_threshold)
        self._session: aiohttp.ClientSession | None = None
        self._last_request_time: float = 0.0
        self._rate_lock = asyncio.Lock()
        self._operation_start: float | None = None

    @property
    def budget(self) -> BudgetTracker:
        """バジェットトラッカー."""
        return self._budget

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """サーキットブレーカー."""
        return self._circuit_breaker

    async def __aenter__(self) -> ConstrainedClient:
        """操作を開始する.

        操作単位の状態（バジェット・サーキットブレーカー・レート制限）を
        リセットし、新しい操作として開始する。
        """
        self._budget.reset()
        self._circuit_breaker.reset()
        self._last_request_time = 0.0
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._request_timeout),
            headers=self._headers,
        )
        self._operation_start = time.monotonic()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """操作を終了し、セッションをクローズする."""
        if self._session:
            await self._session.close()
            self._session = None

    def _check_operation_timeout(self) -> None:
        """操作全体のタイムアウトをチェックする.

        Raises:
            TimeoutError: 操作全体タイムアウトに達した場合
        """
        if self._operation_start is None:
            return
        elapsed = time.monotonic() - self._operation_start
        if elapsed >= self._operation_timeout:
            raise TimeoutError(
                f"操作全体タイムアウト: {elapsed:.1f}秒経過"
                f"（上限: {self._operation_timeout:.1f}秒）"
            )

    async def _wait_interval(self) -> None:
        """最低リクエスト間隔を待機する."""
        if self._last_request_time > 0:
            elapsed = time.monotonic() - self._last_request_time
            if elapsed < self._request_interval:
                await asyncio.sleep(self._request_interval - elapsed)

    def _ensure_session(self) -> aiohttp.ClientSession:
        """セッションの存在を確認し返す.

        Raises:
            RuntimeError: context manager 外での呼び出し
        """
        if self._session is None:
            raise RuntimeError(
                "ConstrainedClient は async context manager として使用してください"
            )
        return self._session

    async def _apply_constraints(self) -> None:
        """全制約チェック（タイムアウト・サーキットブレーカー・レート制限・バジェット）を適用する.

        Raises:
            BudgetExhaustedError: バジェット上限到達
            CircuitBreakerOpenError: サーキットブレーカー発動中
            TimeoutError: 操作全体タイムアウト
        """
        # 操作全体タイムアウトチェック
        self._check_operation_timeout()

        # サーキットブレーカーチェック（発動中なら例外）
        if self._circuit_breaker.is_open:
            raise CircuitBreakerOpenError(
                self._circuit_breaker.consecutive_failures,
                self._circuit_breaker.threshold,
            )

        # レート制限待機（ロックで直列化）
        async with self._rate_lock:
            await self._wait_interval()
            self._last_request_time = time.monotonic()

        # レート制限待機後に操作タイムアウトを再チェック
        self._check_operation_timeout()

        # バジェット消費（実リクエスト直前で消費）
        self._budget.consume()

    async def get(
        self,
        url: str,
        *,
        allow_redirects: bool = False,
    ) -> aiohttp.ClientResponse:
        """GET リクエストを実行する.

        バジェット消費・サーキットブレーカー・レート制限・
        操作タイムアウトの全制約を適用する。
        複数コルーチンからの同時呼び出しに対してレート制限は
        ロックにより直列化される。

        呼び出し側はレスポンスの読み取り後に resp.release() を
        呼び出して接続を解放する責務を持つ。

        Args:
            url: リクエスト先 URL
            allow_redirects: リダイレクト追従の有無（デフォルト: False、SSRF 対策）

        Returns:
            aiohttp.ClientResponse（使用後に release() で解放すること）

        Raises:
            BudgetExhaustedError: バジェット上限到達
            CircuitBreakerOpenError: サーキットブレーカー発動中
            TimeoutError: 操作全体タイムアウト
            RuntimeError: context manager 外での呼び出し
        """
        session = self._ensure_session()
        await self._apply_constraints()

        try:
            resp = await session.get(url, allow_redirects=allow_redirects)
            # 成功 = HTTP レスポンスを受信できた（ステータスコードによらず）
            self._circuit_breaker.record_success()
            return resp
        except Exception:
            self._circuit_breaker.record_failure()
            raise

    async def post(
        self,
        url: str,
        *,
        json: object = None,
        params: dict[str, str] | None = None,
        allow_redirects: bool = False,
    ) -> aiohttp.ClientResponse:
        """POST リクエストを実行する.

        GET と同じ制約（バジェット・サーキットブレーカー・レート制限・
        操作タイムアウト）を適用する。

        Args:
            url: リクエスト先 URL
            json: JSON ボディ
            params: クエリパラメータ
            allow_redirects: リダイレクト追従の有無（デフォルト: False、SSRF 対策）

        Returns:
            aiohttp.ClientResponse（使用後に release() で解放すること）

        Raises:
            BudgetExhaustedError: バジェット上限到達
            CircuitBreakerOpenError: サーキットブレーカー発動中
            TimeoutError: 操作全体タイムアウト
            RuntimeError: context manager 外での呼び出し
        """
        session = self._ensure_session()
        await self._apply_constraints()

        try:
            resp = await session.post(
                url,
                json=json,
                params=params,
                allow_redirects=allow_redirects,
            )
            self._circuit_breaker.record_success()
            return resp
        except Exception:
            self._circuit_breaker.record_failure()
            raise

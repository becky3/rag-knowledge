"""Google Safe Browsing API クライアント

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from .config import RAGSettings
    from .safety.constrained_client import ConstrainedClient

logger = logging.getLogger(__name__)


class SafetyCheckError(Exception):
    """URL安全性チェック失敗時の例外.

    危険なURLが検出された場合、またはfail_close設定時にAPI障害が発生した場合に送出される。
    """

    def __init__(self, url: str, message: str, threats: list[str] | None = None) -> None:
        """SafetyCheckErrorを初期化する.

        Args:
            url: 検査対象のURL
            message: エラーメッセージ
            threats: 検出された脅威タイプのリスト（オプション）
        """
        super().__init__(message)
        self.url = url
        self.threats = threats or []


class ThreatType(Enum):
    """Google Safe Browsing の脅威タイプ."""

    MALWARE = "MALWARE"
    SOCIAL_ENGINEERING = "SOCIAL_ENGINEERING"
    UNWANTED_SOFTWARE = "UNWANTED_SOFTWARE"
    POTENTIALLY_HARMFUL_APPLICATION = "POTENTIALLY_HARMFUL_APPLICATION"


class PlatformType(Enum):
    """Google Safe Browsing のプラットフォームタイプ."""

    ANY_PLATFORM = "ANY_PLATFORM"


class ThreatEntryType(Enum):
    """Google Safe Browsing の脅威エントリタイプ."""

    URL = "URL"


@dataclass
class ThreatMatch:
    """検出された脅威情報."""

    threat_type: ThreatType
    platform_type: str
    threat_url: str
    cache_duration: str | None = None


@dataclass
class SafeBrowsingResult:
    """URL安全性チェック結果."""

    url: str
    is_safe: bool
    threats: list[ThreatMatch] = field(default_factory=list)
    error: str | None = None
    cached: bool = False


@dataclass
class CacheEntry:
    """キャッシュエントリ."""

    result: SafeBrowsingResult
    expires_at: float


class SafeBrowsingClient:
    """Google Safe Browsing API v4 クライアント.

    仕様: docs/specs/rag-knowledge.md
    Issue: #159

    脅威タイプ:
    - MALWARE: マルウェア配布サイト
    - SOCIAL_ENGINEERING: フィッシングサイト
    - UNWANTED_SOFTWARE: 不要なソフトウェア配布サイト
    - POTENTIALLY_HARMFUL_APPLICATION: 有害な可能性のあるアプリ配布サイト
    """

    API_URL = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
    DEFAULT_CACHE_TTL = 300  # 5分（デフォルトTTL）
    MAX_CACHE_SIZE = 1000  # キャッシュの最大エントリ数

    def __init__(
        self,
        api_key: str,
        timeout: float = 10.0,
        cache_ttl: float | None = None,
        fail_open: bool = True,
        client_id: str = "rag-knowledge",
        client_version: str = "1.0.0",
        max_cache_size: int | None = None,
        constrained_client: ConstrainedClient | None = None,
    ) -> None:
        """SafeBrowsingClient を初期化する.

        Args:
            api_key: Google Safe Browsing API キー
            timeout: APIリクエストのタイムアウト秒数
            cache_ttl: キャッシュのTTL秒数（None の場合はデフォルトTTLを使用）
            fail_open: API障害時の動作（True: URLを許可, False: URLを拒否）
            client_id: クライアント識別子
            client_version: クライアントバージョン
            max_cache_size: キャッシュの最大エントリ数（None の場合はデフォルト値を使用）
            constrained_client: 制約付き HTTP クライアント（指定時は aiohttp 直接利用の代わりに使用）
        """
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._cache_ttl = cache_ttl
        self._fail_open = fail_open
        self._client_id = client_id
        self._client_version = client_version
        self._max_cache_size = max_cache_size if max_cache_size is not None else self.MAX_CACHE_SIZE
        self._cache: dict[str, CacheEntry] = {}
        self._cache_lock = asyncio.Lock()
        self._constrained_client = constrained_client

    def _get_cache_key(self, url: str) -> str:
        """URLからキャッシュキーを生成する."""
        return hashlib.sha256(url.encode()).hexdigest()

    async def _get_from_cache(self, url: str) -> SafeBrowsingResult | None:
        """キャッシュから結果を取得する."""
        cache_key = self._get_cache_key(url)
        async with self._cache_lock:
            entry = self._cache.get(cache_key)
            if entry is None:
                return None
            if time.time() > entry.expires_at:
                del self._cache[cache_key]
                return None
            # キャッシュヒットをマーク
            result = entry.result
            return SafeBrowsingResult(
                url=result.url,
                is_safe=result.is_safe,
                threats=list(result.threats),
                error=result.error,
                cached=True,
            )

    async def _set_cache(
        self, url: str, result: SafeBrowsingResult, ttl: float | None = None
    ) -> None:
        """結果をキャッシュに保存する.

        キャッシュが最大サイズを超えた場合、有効期限が最も近いエントリを削除する。
        """
        cache_key = self._get_cache_key(url)
        # ttl=0 を有効値として扱うため、is not None で分岐
        if ttl is not None:
            effective_ttl = ttl
        elif self._cache_ttl is not None:
            effective_ttl = self._cache_ttl
        else:
            effective_ttl = self.DEFAULT_CACHE_TTL
        expires_at = time.time() + effective_ttl
        async with self._cache_lock:
            # キャッシュ上限チェック: 上限に達している場合は最も古いエントリを削除
            if len(self._cache) >= self._max_cache_size and cache_key not in self._cache:
                # 最も期限が近い（古い）エントリを削除
                oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k].expires_at)
                del self._cache[oldest_key]
                logger.debug("Cache eviction: removed oldest entry (max size: %d)", self._max_cache_size)
            self._cache[cache_key] = CacheEntry(result=result, expires_at=expires_at)

    def _build_request_body(self, urls: list[str]) -> dict[str, Any]:
        """APIリクエストボディを構築する."""
        return {
            "client": {
                "clientId": self._client_id,
                "clientVersion": self._client_version,
            },
            "threatInfo": {
                "threatTypes": [t.value for t in ThreatType],
                "platformTypes": [PlatformType.ANY_PLATFORM.value],
                "threatEntryTypes": [ThreatEntryType.URL.value],
                "threatEntries": [{"url": url} for url in urls],
            },
        }

    def _parse_response(
        self, response_data: dict[str, Any], urls: list[str]
    ) -> dict[str, SafeBrowsingResult]:
        """APIレスポンスをパースする."""
        results: dict[str, SafeBrowsingResult] = {}

        # まず全URLを安全として初期化
        for url in urls:
            results[url] = SafeBrowsingResult(url=url, is_safe=True)

        # マッチした脅威を処理
        matches = response_data.get("matches", [])
        for match in matches:
            threat_url = match.get("threat", {}).get("url", "")
            if not threat_url:
                continue

            threat_type_str = match.get("threatType", "")
            try:
                threat_type = ThreatType(threat_type_str)
            except ValueError:
                logger.warning("Unknown threat type: %s", threat_type_str)
                continue

            threat_match = ThreatMatch(
                threat_type=threat_type,
                platform_type=match.get("platformType", ""),
                threat_url=threat_url,
                cache_duration=match.get("cacheDuration"),
            )

            if threat_url in results:
                results[threat_url].is_safe = False
                results[threat_url].threats.append(threat_match)

        return results

    async def check_url(self, url: str) -> SafeBrowsingResult:
        """単一URLの安全性をチェックする.

        Args:
            url: チェックするURL

        Returns:
            SafeBrowsingResult: チェック結果
        """
        results = await self.check_urls([url])
        return results.get(url, SafeBrowsingResult(url=url, is_safe=True))

    async def check_urls(self, urls: list[str]) -> dict[str, SafeBrowsingResult]:
        """複数URLの安全性を一括チェックする.

        Args:
            urls: チェックするURLのリスト

        Returns:
            URL -> SafeBrowsingResult のマッピング
        """
        if not urls:
            return {}

        results: dict[str, SafeBrowsingResult] = {}
        urls_to_check: list[str] = []

        # キャッシュチェック
        for url in urls:
            cached = await self._get_from_cache(url)
            if cached:
                results[url] = cached
                logger.debug("Cache hit for URL: %s", url)
            else:
                urls_to_check.append(url)

        # キャッシュミスのURLがなければ終了
        if not urls_to_check:
            return results

        # API呼び出し
        try:
            api_results = await self._call_api(urls_to_check)
            for url, result in api_results.items():
                results[url] = result
                # キャッシュに保存
                await self._set_cache(url, result)
        except Exception as e:
            logger.exception("Safe Browsing API error")
            if self._fail_open:
                # fail-open: API障害時はURLを許可
                for url in urls_to_check:
                    results[url] = SafeBrowsingResult(
                        url=url,
                        is_safe=True,
                        error=str(e),
                    )
            else:
                # fail-close: API障害時はURLを拒否（例外を送出）
                raise SafetyCheckError(
                    url=urls_to_check[0] if len(urls_to_check) == 1 else "",
                    message=f"Safe Browsing API障害のためURL安全性を確認できません: {e}",
                ) from e

        return results

    async def _call_api(self, urls: list[str]) -> dict[str, SafeBrowsingResult]:
        """Safe Browsing API を呼び出す."""
        request_body = self._build_request_body(urls)

        if self._constrained_client is not None:
            async with self._constrained_client as client:
                resp = await client.post(
                    self.API_URL,
                    params={"key": self._api_key},
                    json=request_body,
                )
                if resp.status != 200:
                    error_text = await resp.text()
                    raise RuntimeError(
                        f"Safe Browsing API error: {resp.status} - {error_text}"
                    )
                response_data = await resp.json()
        else:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(
                    self.API_URL, params={"key": self._api_key}, json=request_body
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise RuntimeError(
                            f"Safe Browsing API error: {resp.status} - {error_text}"
                        )
                    response_data = await resp.json()

        return self._parse_response(response_data, urls)

    async def is_url_safe(self, url: str) -> bool:
        """URLが安全かどうかを判定する（シンプルなインターフェース）.

        Args:
            url: チェックするURL

        Returns:
            True: 安全（または判定不能）, False: 危険
        """
        result = await self.check_url(url)
        return result.is_safe

    async def clear_cache(self) -> None:
        """キャッシュをクリアする."""
        async with self._cache_lock:
            self._cache.clear()
        logger.debug("Safe Browsing cache cleared")

    async def cleanup_expired_cache(self) -> int:
        """期限切れのキャッシュエントリを削除する.

        Returns:
            削除されたエントリ数
        """
        now = time.time()
        expired_keys: list[str] = []

        async with self._cache_lock:
            for key, entry in self._cache.items():
                if now > entry.expires_at:
                    expired_keys.append(key)

            for key in expired_keys:
                del self._cache[key]

        if expired_keys:
            logger.debug("Cleaned up %d expired cache entries", len(expired_keys))

        return len(expired_keys)


def create_safe_browsing_client(settings: RAGSettings) -> SafeBrowsingClient | None:
    """設定に基づいてSafeBrowsingClientを生成する.

    Args:
        settings: アプリケーション設定

    Returns:
        SafeBrowsingClient または None（無効時）
    """
    from .safety.constrained_client import ConstrainedClient

    if not settings.rag_url_safety_check:
        logger.debug("URL safety check is disabled")
        return None

    if not settings.google_safe_browsing_api_key:
        logger.warning(
            "URL safety check is enabled but GOOGLE_SAFE_BROWSING_API_KEY is not set. "
            "Skipping Safe Browsing integration."
        )
        return None

    cache_ttl: float | None = None
    if settings.rag_url_safety_cache_ttl > 0:
        cache_ttl = float(settings.rag_url_safety_cache_ttl)

    constrained_client = ConstrainedClient(
        request_timeout=settings.rag_url_safety_timeout,
        max_requests=10,
        circuit_breaker_threshold=3,
    )

    return SafeBrowsingClient(
        api_key=settings.google_safe_browsing_api_key,
        timeout=settings.rag_url_safety_timeout,
        cache_ttl=cache_ttl,
        fail_open=settings.rag_url_safety_fail_open,
        constrained_client=constrained_client,
    )

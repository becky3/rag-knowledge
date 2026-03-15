"""Web インジェスター: 既存 WebCrawler のラッパー

仕様: docs/specs/rag-knowledge.md
Issue: #62
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .base_ingester import BaseIngester, IngestedContent

if TYPE_CHECKING:
    from ..safe_browsing import SafeBrowsingClient
    from ..web_crawler import WebCrawler

logger = logging.getLogger(__name__)


class WebIngester(BaseIngester):
    """Web ページ取り込み用インジェスター.

    既存の WebCrawler に委譲し、CrawledPage を IngestedContent に変換する。
    Safe Browsing チェックを統合する。
    """

    def __init__(
        self,
        web_crawler: WebCrawler,
        *,
        safe_browsing_client: SafeBrowsingClient | None = None,
    ) -> None:
        """WebIngester を初期化する.

        Args:
            web_crawler: Web クローラーインスタンス
            safe_browsing_client: Safe Browsing クライアント（オプション）
        """
        self._web_crawler = web_crawler
        self._safe_browsing_client = safe_browsing_client

    def validate_identifier(self, identifier: str) -> str:
        """URL を検証し、正規化済み URL を返す.

        Args:
            identifier: 検証する URL

        Returns:
            正規化済み URL

        Raises:
            ValueError: URL が不正な場合
        """
        return self._web_crawler.validate_url(identifier)

    async def _check_safety(self, url: str) -> None:
        """URL の安全性をチェックする.

        Args:
            url: チェックする URL

        Raises:
            ValueError: URL が危険と判定された場合
        """
        if not self._safe_browsing_client:
            return

        result = await self._safe_browsing_client.check_url(url)
        if not result.is_safe:
            threat_types = [t.threat_type.value for t in result.threats]
            logger.warning(
                "Unsafe URL rejected: %s (threats: %s)", url, threat_types
            )
            raise ValueError(
                f"URLが安全ではありません: {url} "
                f"(検出された脅威: {', '.join(threat_types)})"
            )

    async def fetch_single(
        self, identifier: str, *, skip_safety_check: bool = False
    ) -> IngestedContent | None:
        """単一 URL からコンテンツを取得する.

        1. URL 検証
        2. Safe Browsing チェック（skip_safety_check=True で省略可能）
        3. WebCrawler でクロール
        4. CrawledPage → IngestedContent に変換

        Args:
            identifier: 取得する URL
            skip_safety_check: True の場合、Safe Browsing チェックを省略する
                （バッチ処理で事前にチェック済みの場合に使用）

        Returns:
            IngestedContent、または取得失敗時は None

        Raises:
            ValueError: URL 検証失敗または URL が危険な場合
        """
        validated_url = self.validate_identifier(identifier)
        if not skip_safety_check:
            await self._check_safety(validated_url)

        page = await self._web_crawler.crawl_page(validated_url)
        if page is None:
            logger.warning("Failed to crawl page: %s", validated_url)
            return None

        return IngestedContent(
            source_id=page.url,
            title=page.title,
            text=page.text,
            ingested_at=IngestedContent.now_iso(),
            source_type="web",
            metadata={
                "crawled_at": page.crawled_at,
            },
        )

    async def fetch_batch(self, identifiers: list[str]) -> list[IngestedContent]:
        """複数 URL からコンテンツを一括取得する.

        Safe Browsing チェック後、WebCrawler の並行クロールを利用する。

        Args:
            identifiers: URL のリスト

        Returns:
            取得に成功した IngestedContent のリスト
        """
        if not identifiers:
            return []

        # Safe Browsing 一括チェック
        safe_urls = await self.filter_safe_urls(identifiers)
        if not safe_urls:
            return []

        # 並行クロール（WebCrawler.crawl_page を asyncio.gather で）
        tasks = [
            self._web_crawler.crawl_page(url)
            for url in safe_urls
        ]

        pages = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[IngestedContent] = []
        for page in pages:
            if isinstance(page, BaseException):
                logger.warning("Failed to crawl page in batch: %s", page)
                continue
            if page is not None:
                results.append(
                    IngestedContent(
                        source_id=page.url,
                        title=page.title,
                        text=page.text,
                        ingested_at=IngestedContent.now_iso(),
                        source_type="web",
                        metadata={
                            "crawled_at": page.crawled_at,
                        },
                    )
                )

        return results

    async def filter_safe_urls(self, urls: list[str]) -> list[str]:
        """Safe Browsing で安全な URL のみをフィルタリングする.

        Args:
            urls: チェックする URL のリスト

        Returns:
            安全な URL のリスト
        """
        if not self._safe_browsing_client:
            return list(urls)

        check_results = await self._safe_browsing_client.check_urls(urls)
        safe_urls: list[str] = []
        for url in urls:
            result = check_results.get(url)
            if result and not result.is_safe:
                threat_types = [t.threat_type.value for t in result.threats]
                logger.warning(
                    "Unsafe URL skipped: %s (threats: %s)", url, threat_types
                )
            else:
                safe_urls.append(url)

        if len(safe_urls) < len(urls):
            logger.info(
                "Safe Browsing: %d URLs skipped as unsafe out of %d",
                len(urls) - len(safe_urls),
                len(urls),
            )

        return safe_urls

    async def discover(self, source: str, **kwargs: object) -> list[str]:
        """リンク集ページから取り込み対象 URL を発見する.

        Args:
            source: リンク集ページの URL
            **kwargs: url_pattern (str) — URL フィルタリング用正規表現

        Returns:
            発見された URL のリスト
        """
        url_pattern = str(kwargs.get("url_pattern", ""))
        return await self._web_crawler.crawl_index_page(source, url_pattern)

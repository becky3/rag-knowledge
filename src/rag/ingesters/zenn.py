"""Zenn インジェスター: Zenn API 経由で記事を取得する

仕様: docs/specs/rag-knowledge.md
Issue: #73
"""

from __future__ import annotations

import logging
import re

import aiohttp

from .base import BaseIngester, IngestedContent

logger = logging.getLogger(__name__)

ZENN_API_BASE = "https://zenn.dev/api"
"""Zenn API のベース URL."""

_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
"""slug の形式: 英小文字・数字・ハイフン・アンダースコア."""


class ZennIngester(BaseIngester):
    """Zenn 記事取り込み用インジェスター.

    Zenn API から記事を取得し、IngestedContent に変換する。
    """

    def __init__(
        self,
        *,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> None:
        """ZennIngester を初期化する.

        Args:
            timeout: HTTP リクエストのタイムアウト設定
        """
        self._timeout = timeout or aiohttp.ClientTimeout(total=30)

    def validate_identifier(self, identifier: str) -> str:
        """slug を検証し、正規化済み slug を返す.

        Args:
            identifier: 検証する slug

        Returns:
            正規化済み slug（小文字変換済み）

        Raises:
            ValueError: slug が不正な場合
        """
        slug = identifier.strip().lower()
        if not slug:
            raise ValueError("slugが空です")
        if not _SLUG_PATTERN.match(slug):
            raise ValueError(
                f"不正なslug形式です: {identifier!r}"
                "（英小文字・数字・ハイフン・アンダースコアのみ使用可能）"
            )
        return slug

    async def fetch_single(self, identifier: str) -> IngestedContent | None:
        """Zenn API から単一記事を取得する.

        Args:
            identifier: 記事の slug

        Returns:
            IngestedContent、または取得失敗時は None
        """
        slug = self.validate_identifier(identifier)
        url = f"{ZENN_API_BASE}/articles/{slug}"

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url) as resp:
                    if resp.status == 404:
                        logger.warning("Article not found: %s", slug)
                        return None
                    if resp.status != 200:
                        logger.warning(
                            "Zenn API error: status=%d, slug=%s",
                            resp.status,
                            slug,
                        )
                        return None
                    data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as e:
            logger.warning("Failed to fetch article %s: %s", slug, e)
            return None

        article = data.get("article", {})
        title = article.get("title", "")
        body = article.get("body_markdown", "")
        if not body:
            logger.warning("Article has no body: %s", slug)
            return None

        return IngestedContent(
            source_id=f"https://zenn.dev/articles/{slug}",
            title=title,
            text=body,
            ingested_at=IngestedContent.now_iso(),
            source_type="zenn",
            metadata={
                "slug": slug,
                "emoji": article.get("emoji", ""),
                "article_type": article.get("article_type", ""),
                "published": article.get("published", False),
            },
        )

    async def discover(self, source: str, **kwargs: object) -> list[str]:
        """ユーザー名から記事 slug 一覧を取得する.

        Args:
            source: Zenn ユーザー名
            **kwargs: 追加パラメータ（未使用）

        Returns:
            記事 slug のリスト
        """
        username = source.strip()
        if not username:
            return []

        url = f"{ZENN_API_BASE}/articles"
        slugs: list[str] = []
        next_page: str | None = None

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                while True:
                    params: dict[str, str] = {"username": username}
                    if next_page is not None:
                        params["next_page"] = next_page

                    async with session.get(url, params=params) as resp:
                        if resp.status != 200:
                            logger.warning(
                                "Zenn API error during discover: "
                                "status=%d, username=%s",
                                resp.status,
                                username,
                            )
                            break
                        data = await resp.json()

                    articles = data.get("articles", [])
                    for article in articles:
                        slug = article.get("slug")
                        if slug:
                            slugs.append(slug)

                    next_page = data.get("next_page")
                    if not next_page:
                        break
        except (aiohttp.ClientError, TimeoutError) as e:
            logger.warning(
                "Failed to discover articles for %s: %s", username, e
            )

        return slugs

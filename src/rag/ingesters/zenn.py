"""Zenn インジェスター: Zenn API 経由で記事を取得

仕様: docs/specs/rag-knowledge.md（ZennIngester セクション）
Issue: #94
"""

from __future__ import annotations

import asyncio
import logging
import re

import aiohttp

from .base import BaseIngester, IngestedContent

logger = logging.getLogger(__name__)

# Zenn API ベース URL
_ZENN_API_BASE = "https://zenn.dev/api"

# スラッグのバリデーション: 英小文字・数字・ハイフンのみ
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# ユーザー名のバリデーション: 英小文字・数字・ハイフン・アンダースコアのみ
_USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# User-Agent（Bot 識別を明示）
_USER_AGENT = "RAGKnowledgeBot/1.0 (+https://github.com/becky3/rag-knowledge)"


class ZennIngester(BaseIngester):
    """Zenn API 経由で記事を取得するインジェスター.

    仕様: docs/specs/rag-knowledge.md（ZennIngester セクション）
    """

    def __init__(
        self,
        *,
        request_delay_sec: float = 1.0,
        request_timeout_sec: float = 30.0,
        max_pagination_pages: int = 100,
        max_retries: int = 3,
    ) -> None:
        """ZennIngester を初期化する.

        Args:
            request_delay_sec: リクエスト間ディレイ（秒）
            request_timeout_sec: リクエストタイムアウト（秒）
            max_pagination_pages: ページネーション上限
            max_retries: 429 リトライ上限
        """
        self._request_delay_sec = request_delay_sec
        self._request_timeout_sec = request_timeout_sec
        self._max_pagination_pages = max_pagination_pages
        self._max_retries = max_retries

    def validate_identifier(self, identifier: str) -> str:
        """スラッグを検証し、正規化済みスラッグを返す.

        Args:
            identifier: 検証するスラッグ

        Returns:
            正規化済みスラッグ（小文字）

        Raises:
            ValueError: スラッグが不正な場合
        """
        if not identifier or not identifier.strip():
            raise ValueError("スラッグが空です")

        normalized = identifier.strip().lower()

        if not _SLUG_PATTERN.match(normalized):
            raise ValueError(
                f"不正なスラッグです（英小文字・数字・ハイフンのみ）: {identifier}"
            )

        return normalized

    async def _request_with_retry(
        self,
        url: str,
        *,
        session: aiohttp.ClientSession,
    ) -> dict[str, object] | None:
        """429 指数バックオフリトライ付きで GET リクエストを実行する.

        Args:
            url: リクエスト URL
            session: aiohttp セッション

        Returns:
            JSON レスポンス辞書、またはエラー時は None

        Raises:
            ValueError: 404 レスポンス時
            aiohttp.ClientError: リトライ上限超過時
        """
        backoff = 1.0
        for attempt in range(self._max_retries + 1):
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data: dict[str, object] = await resp.json()
                        return data
                    if resp.status == 404:
                        raise ValueError(
                            f"記事が見つかりません (404): {url}"
                        )
                    if resp.status == 429:
                        if attempt >= self._max_retries:
                            logger.error(
                                "Rate limit exceeded after %d retries: %s",
                                self._max_retries,
                                url,
                            )
                            raise aiohttp.ClientResponseError(
                                request_info=resp.request_info,
                                history=resp.history,
                                status=429,
                                message="Rate limit exceeded",
                            )
                        # Retry-After ヘッダーを尊重
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            try:
                                wait = float(retry_after)
                            except (ValueError, TypeError):
                                wait = backoff
                        else:
                            wait = backoff
                        logger.warning(
                            "Rate limited (429), retrying in %.1fs (attempt %d/%d): %s",
                            wait,
                            attempt + 1,
                            self._max_retries,
                            url,
                        )
                        await asyncio.sleep(wait)
                        backoff *= 2
                        continue
                    # 5xx サーバーエラー
                    if resp.status >= 500:
                        logger.warning(
                            "Server error (%d) from Zenn API: %s",
                            resp.status,
                            url,
                        )
                        return None
                    # その他のエラー
                    logger.warning(
                        "Unexpected status %d from Zenn API: %s",
                        resp.status,
                        url,
                    )
                    return None
            except asyncio.TimeoutError:
                logger.warning("Request timeout: %s", url)
                return None
            except (ValueError, aiohttp.ClientError):
                raise
            except Exception:
                logger.exception("Unexpected error requesting %s", url)
                return None
        return None

    def _create_session(self) -> aiohttp.ClientSession:
        """短命セッションを生成する.

        Returns:
            aiohttp.ClientSession
        """
        timeout = aiohttp.ClientTimeout(total=self._request_timeout_sec)
        return aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
        )

    async def fetch_single(self, identifier: str) -> IngestedContent | None:
        """スラッグを指定して Zenn 記事を取得する.

        Args:
            identifier: 記事スラッグ

        Returns:
            IngestedContent、または取得失敗時は None

        Raises:
            ValueError: スラッグ検証失敗または記事が存在しない場合
        """
        slug = self.validate_identifier(identifier)
        url = f"{_ZENN_API_BASE}/articles/{slug}"

        async with self._create_session() as session:
            data = await self._request_with_retry(url, session=session)

        if data is None:
            return None

        article = data.get("article", data)
        if isinstance(article, dict):
            body_md = str(article.get("body_md", ""))
            title = str(article.get("title", ""))
            published_at = article.get("published_at")
        else:
            return None

        # 本文が空の記事はスキップ
        if not body_md or not body_md.strip():
            logger.info("Skipping article with empty body: %s", slug)
            return None

        metadata: dict[str, object] = {}
        if published_at:
            metadata["published_at"] = published_at

        return IngestedContent(
            source_id=slug,
            title=title,
            text=body_md,
            ingested_at=IngestedContent.now_iso(),
            source_type="zenn",
            metadata=metadata,
        )

    async def discover(self, source: str, **kwargs: object) -> list[str]:
        """ユーザー名を指定して記事スラッグ一覧を取得する.

        Args:
            source: Zenn ユーザー名
            **kwargs: 未使用

        Returns:
            スラッグのリスト

        Raises:
            ValueError: ユーザー名が不正な場合
        """
        username = source.strip().lower()
        if not username:
            return []

        if not _USERNAME_PATTERN.match(username):
            raise ValueError(
                f"不正なユーザー名です（英小文字・数字・ハイフン・アンダースコアのみ）: {source}"
            )

        slugs: list[str] = []
        page = 1

        async with self._create_session() as session:
            while page <= self._max_pagination_pages:
                url = (
                    f"{_ZENN_API_BASE}/articles"
                    f"?username={username}&order=latest&page={page}"
                )

                data = await self._request_with_retry(url, session=session)
                if data is None:
                    break

                articles = data.get("articles", [])
                if not isinstance(articles, list) or not articles:
                    break

                for article in articles:
                    if isinstance(article, dict):
                        slug = article.get("slug")
                        if slug and isinstance(slug, str):
                            slugs.append(slug)

                # ページネーション: next_page の有無で判定
                next_page = data.get("next_page")
                if next_page is None:
                    break

                page += 1

                # ディレイ適用
                if self._request_delay_sec > 0:
                    await asyncio.sleep(self._request_delay_sec)

        logger.info(
            "Discovered %d articles for user '%s' (%d pages)",
            len(slugs),
            username,
            page,
        )
        return slugs

    async def fetch_batch(self, identifiers: list[str]) -> list[IngestedContent]:
        """複数スラッグを順次取得する（レート制限ディレイ適用）.

        Args:
            identifiers: スラッグのリスト

        Returns:
            取得に成功した IngestedContent のリスト
        """
        if not identifiers:
            return []

        results: list[IngestedContent] = []
        for i, identifier in enumerate(identifiers):
            try:
                content = await self.fetch_single(identifier)
                if content is not None:
                    results.append(content)
            except ValueError:
                logger.warning("Skipping invalid slug: %s", identifier)
            except Exception:
                logger.exception("Failed to fetch article: %s", identifier)

            # ディレイ適用（最後のリクエスト後は不要）
            if i < len(identifiers) - 1 and self._request_delay_sec > 0:
                await asyncio.sleep(self._request_delay_sec)

        return results

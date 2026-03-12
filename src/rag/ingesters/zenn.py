"""Zenn インジェスター: Zenn API 経由で記事を取得するインジェスター

仕様: docs/specs/features/zenn-ingester.md
Issue: #114
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from markdownify import MarkdownConverter

from .base import BaseIngester, IngestedContent

logger = logging.getLogger(__name__)

ZENN_API_BASE = "https://zenn.dev/api"
USER_AGENT = "RAG-Knowledge/1.0"
DEFAULT_MAX_PAGES = 10
REQUEST_INTERVAL = 1.0
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF_BASE = 1  # 指数バックオフ: 1s, 2s, 4s

# slug / username の形式: 英数字・ハイフン・アンダースコア
_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass
class DiscoverResult:
    """discover() の戻り値.

    slug のリストと、ページネーション上限到達フラグを保持する。
    """

    slugs: list[str] = field(default_factory=list)
    limit_reached: bool = False


def format_zenn_ingest_result(
    result: dict[str, object],
    username: str,
) -> str:
    """Zenn 一括取り込み結果をフォーマットする.

    server.py（MCP ツール）と cli.py（CLI コマンド）で共用する。

    Args:
        result: ingest_zenn() の戻り値
        username: Zenn ユーザー名

    Returns:
        フォーマット済み文字列
    """
    if result["dry_run"]:
        articles = result.get("articles", [])
        found = result["articles_found"]
        lines: list[str] = [f"[dry-run] Zenn 記事一覧 ({username}): {found}件"]
        if isinstance(articles, list):
            for i, article in enumerate(articles, start=1):
                if isinstance(article, dict):
                    slug = article.get("slug", "")
                    lines.append(f"  {i}. {slug}")
        return "\n".join(lines)

    found = result["articles_found"]
    ingested = result["articles_ingested"]
    chunks = result["chunks_stored"]
    errors = result["errors"]
    return (
        f"Zenn 記事取り込み完了 ({username}): "
        f"発見={found}件, 取り込み={ingested}件, "
        f"チャンク={chunks}, エラー={errors}件"
    )


class _ZennMarkdownConverter(MarkdownConverter):  # type: ignore[misc]
    """Zenn 記事用 Markdown コンバーター.

    リンク URL と画像 URL を除去し、テキスト情報のみを保持する。
    """

    def convert_a(self, el: Any, text: str, convert_as_inline: bool) -> str:
        """リンクはテキストのみ保持."""
        return text or ""

    def convert_img(self, el: Any, text: str, convert_as_inline: bool) -> str:
        """画像は alt テキストのみ保持."""
        return el.get("alt", "") or ""


class ZennIngester(BaseIngester):
    """Zenn API からの記事取得を担うインジェスター.

    仕様: docs/specs/features/zenn-ingester.md

    BaseIngester を継承し、Zenn API を使って記事を取得する。
    ページネーション上限・リクエスト間隔・リトライ・タイムアウトの
    安全制約を実装する。
    """

    def __init__(self, *, max_pages: int = DEFAULT_MAX_PAGES) -> None:
        """ZennIngester を初期化する.

        Args:
            max_pages: ページネーション上限（0 で無制限）
        """
        self._max_pages = max_pages
        self._timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        self._md_converter = _ZennMarkdownConverter(
            heading_style="ATX",
            escape_underscores=False,
            escape_asterisks=False,
        )

    def validate_identifier(self, identifier: str) -> str:
        """slug の形式を検証する.

        Args:
            identifier: Zenn 記事の slug

        Returns:
            検証済み slug

        Raises:
            ValueError: slug の形式が不正な場合
        """
        if not identifier or not _IDENTIFIER_PATTERN.match(identifier):
            raise ValueError(
                f"不正な Zenn 記事 slug です: {identifier!r} "
                "(英数字・ハイフン・アンダースコアのみ使用可能)"
            )
        return identifier

    async def _request_with_retry(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> dict[str, Any]:
        """リトライ付きで GET リクエストを送信する.

        Args:
            session: aiohttp セッション
            url: リクエスト先 URL

        Returns:
            JSON レスポンス

        Raises:
            aiohttp.ClientError: 最大リトライ回数超過
            ValueError: レスポンスが JSON でない場合
        """
        last_error: BaseException | None = None
        for attempt in range(MAX_RETRIES):
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    data: dict[str, Any] = await resp.json()
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    wait = BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "Request failed (attempt %d/%d), retrying in %ds: %s %s",
                        attempt + 1,
                        MAX_RETRIES,
                        wait,
                        url,
                        e,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "Request failed after %d retries: %s %s",
                        MAX_RETRIES,
                        url,
                        e,
                    )

        assert last_error is not None
        raise last_error

    def _html_to_text(self, html: str) -> str:
        """HTML をプレーンテキスト（Markdown）に変換する.

        Args:
            html: HTML 文字列

        Returns:
            Markdown テキスト
        """
        if not html:
            return ""
        text: str = self._md_converter.convert(html)
        # 連続空行を 2 行に正規化、行末空白を除去
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        return text.strip()

    async def fetch_single(self, identifier: str) -> IngestedContent | None:
        """単一の Zenn 記事を slug 指定で取得する.

        Args:
            identifier: Zenn 記事の slug

        Returns:
            IngestedContent、または取得失敗時は None
        """
        slug = self.validate_identifier(identifier)
        url = f"{ZENN_API_BASE}/articles/{slug}"

        try:
            async with aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"User-Agent": USER_AGENT},
            ) as session:
                data = await self._request_with_retry(session, url)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            logger.error("Failed to fetch article: %s", slug)
            return None

        article = data.get("article")
        if article is None or not isinstance(article, dict):
            logger.error(
                "API response missing 'article' field for slug: %s "
                "(API schema may have changed)",
                slug,
            )
            return None

        # 必須フィールドチェック
        body_html = article.get("body_html")
        if body_html is None:
            logger.error(
                "API response missing 'body_html' for slug: %s "
                "(API schema may have changed)",
                slug,
            )
            return None

        article_slug = article.get("slug", slug)
        title = article.get("title", "")
        source_url = f"https://zenn.dev/articles/{article_slug}"
        text = self._html_to_text(body_html)

        return IngestedContent(
            source_id=source_url,
            title=title,
            text=text,
            ingested_at=IngestedContent.now_iso(),
            source_type="zenn",
            metadata={
                "slug": article_slug,
                "username": article.get("user", {}).get("username", ""),
                "published_at": article.get("published_at", ""),
                "raw_html": body_html,
            },
        )

    async def fetch_batch(self, identifiers: list[str]) -> list[IngestedContent]:
        """複数の slug を順次取得する（リクエスト間隔 1 秒以上）.

        Args:
            identifiers: slug のリスト

        Returns:
            取得に成功した IngestedContent のリスト
        """
        if not identifiers:
            return []

        results: list[IngestedContent] = []
        for i, slug in enumerate(identifiers):
            if i > 0:
                await asyncio.sleep(REQUEST_INTERVAL)
            try:
                content = await self.fetch_single(slug)
                if content is not None:
                    results.append(content)
            except Exception:
                logger.exception("Failed to fetch article: %s", slug)

        return results

    async def discover(self, source: str, **kwargs: object) -> DiscoverResult:  # type: ignore[override]
        """ユーザーの記事一覧を取得し、DiscoverResult を返す.

        Args:
            source: Zenn ユーザー名
            **kwargs:
                max_pages (int): ページネーション上限の上書き
                no_limit (bool): ページネーション上限を解除

        Returns:
            DiscoverResult（slugs と limit_reached を含む）
        """
        username = source

        # username のバリデーション
        if not username or not _IDENTIFIER_PATTERN.match(username):
            logger.error("Invalid Zenn username: %r", username)
            return DiscoverResult()

        limit_reached = False

        # max_pages の決定
        no_limit = bool(kwargs.get("no_limit", False))
        if no_limit:
            max_pages = 0  # 無制限
        else:
            max_pages_override = kwargs.get("max_pages")
            if max_pages_override is not None:
                max_pages = int(str(max_pages_override))
            else:
                max_pages = self._max_pages

        slugs: list[str] = []
        page = 1

        async with aiohttp.ClientSession(
            timeout=self._timeout,
            headers={"User-Agent": USER_AGENT},
        ) as session:
            while True:
                # ページネーション上限チェック
                if max_pages > 0 and page > max_pages:
                    logger.warning(
                        "Pagination limit reached (%d pages) for user: %s",
                        max_pages,
                        username,
                    )
                    limit_reached = True
                    break

                url = (
                    f"{ZENN_API_BASE}/articles"
                    f"?username={username}&order=latest&page={page}"
                )

                try:
                    data = await self._request_with_retry(session, url)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    # 記事一覧取得のリトライ失敗時は処理全体を停止
                    logger.error(
                        "Failed to fetch article list for user: %s (page %d). "
                        "Stopping pagination.",
                        username,
                        page,
                    )
                    break

                # articles フィールドの検証
                articles = data.get("articles")
                if articles is None:
                    logger.error(
                        "API response missing 'articles' field for user: %s "
                        "(API schema may have changed). Stopping.",
                        username,
                    )
                    break

                if not isinstance(articles, list):
                    logger.error(
                        "API response 'articles' is not a list for user: %s. Stopping.",
                        username,
                    )
                    break

                if not articles:
                    # 空の articles = これ以上記事がない
                    break

                for article in articles:
                    slug = article.get("slug")
                    if slug is None:
                        logger.error(
                            "Article missing 'slug' field (API schema may have changed). "
                            "Stopping.",
                        )
                        return DiscoverResult(slugs=slugs, limit_reached=limit_reached)
                    slugs.append(slug)

                # next_page の検証
                next_page = data.get("next_page")
                if next_page is None:
                    # 最終ページ
                    break
                if not isinstance(next_page, int) or next_page <= 0:
                    logger.warning(
                        "Unexpected next_page value: %r. Stopping pagination.",
                        next_page,
                    )
                    break

                page = next_page

                # リクエスト間隔
                await asyncio.sleep(REQUEST_INTERVAL)

        return DiscoverResult(slugs=slugs, limit_reached=limit_reached)

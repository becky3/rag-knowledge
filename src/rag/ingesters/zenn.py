"""Zenn インジェスター: Zenn 記事を API 経由で取得しナレッジベースに取り込む

仕様: docs/specs/zenn-ingester.md
Issue: #162
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from .base import BaseIngester, IngestedContent

if TYPE_CHECKING:
    from py_common_lib.httpx import ConstrainedClient

logger = logging.getLogger(__name__)

# --- ハードリミット（コード内定数。設定・引数・環境変数で緩和不可） ---
MAX_PAGINATION_PAGES = 10
"""ページネーション走査上限（ページ数）"""

MAX_ARTICLES_HARD_LIMIT = 100
"""記事取得上限（件数）"""

# --- Zenn API ---
ZENN_API_BASE = "https://zenn.dev/api"
"""Zenn API ベース URL"""

# --- バリデーション ---
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
"""slug の許容パターン（英小文字・数字・ハイフン・アンダースコア）"""


def _validate_max_articles(value: int) -> int:
    """max_articles をバリデーションし、必要に応じてクランプする.

    仕様: docs/specs/zenn-ingester.md「バリデーションとクランプの使い分け」

    - バリデーションエラー（拒否）: 型不正、0、負数
    - クランプ（警告ログ付き）: 正の整数だが許容範囲外

    Args:
        value: 検証する値

    Returns:
        検証済みの値（クランプ適用後）

    Raises:
        ValueError: 0 または負数の場合
        TypeError: 整数でない場合
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(
            f"max_articles must be an integer, got {type(value).__name__}"
        )
    if value <= 0:
        raise ValueError(
            f"max_articles must be a positive integer, got {value}"
        )
    if value > MAX_ARTICLES_HARD_LIMIT:
        logger.warning(
            "max_articles=%d exceeds hard limit %d, clamping to %d",
            value,
            MAX_ARTICLES_HARD_LIMIT,
            MAX_ARTICLES_HARD_LIMIT,
        )
        return MAX_ARTICLES_HARD_LIMIT
    return value


class ZennIngester(BaseIngester):
    """Zenn 記事取り込み用インジェスター.

    仕様: docs/specs/zenn-ingester.md

    Zenn API を使用して記事一覧を走査し、個別記事の本文を取得する。
    全 HTTP リクエストは ConstrainedClient 経由で実行する。
    """

    def __init__(
        self,
        client: ConstrainedClient,
        *,
        max_articles: int = 50,
    ) -> None:
        """ZennIngester を初期化する.

        Args:
            client: ConstrainedClient インスタンス
            max_articles: 取得する最大記事数（デフォルト: 50、許容範囲: 1〜100）

        Raises:
            ValueError: max_articles が 0 または負数の場合
            TypeError: max_articles が整数でない場合
        """
        self._client = client
        self._max_articles = _validate_max_articles(max_articles)

    def validate_identifier(self, identifier: str) -> str:
        """slug を検証し、正規化済みの slug を返す.

        Args:
            identifier: 検証する slug

        Returns:
            正規化済み slug

        Raises:
            ValueError: slug が不正な場合
        """
        if not identifier or not identifier.strip():
            raise ValueError("slug must not be empty")
        slug = identifier.strip()
        if not _SLUG_PATTERN.match(slug):
            raise ValueError(
                f"Invalid slug format: {slug!r}. "
                "slug must contain only lowercase letters, digits, hyphens, and underscores, "
                "and must start with a letter or digit."
            )
        return slug

    async def discover(self, source: str, **kwargs: object) -> list[str]:
        """記事一覧 API をページネーション走査し、slug リストを返す.

        Args:
            source: Zenn ユーザー名
            **kwargs: max_articles (int) — 取得する最大記事数（オプション）

        Returns:
            発見された記事の slug リスト
        """
        username = source.strip()
        if not username:
            raise ValueError("username must not be empty")

        max_articles = self._max_articles
        if "max_articles" in kwargs:
            raw = kwargs["max_articles"]
            if isinstance(raw, int) and not isinstance(raw, bool):
                max_articles = _validate_max_articles(raw)
            else:
                logger.warning(
                    "max_articles kwarg ignored: expected int, got %s",
                    type(raw).__name__,
                )

        slugs: list[str] = []
        page = 1

        while page <= MAX_PAGINATION_PAGES and len(slugs) < max_articles:
            query = urlencode({
                "username": username,
                "order": "latest",
                "page": page,
            })
            url = f"{ZENN_API_BASE}/articles?{query}"
            try:
                resp = await self._client.get(url)
            except Exception:
                logger.exception(
                    "Failed to fetch article list page %d for user %s",
                    page,
                    username,
                )
                break

            if resp.status_code != 200:
                logger.error(
                    "Zenn API returned status %d for article list page %d (user: %s)",
                    resp.status_code,
                    page,
                    username,
                )
                break

            try:
                data: dict[str, Any] = resp.json()
            except Exception:
                logger.exception(
                    "Failed to parse JSON response for article list page %d (user: %s)",
                    page,
                    username,
                )
                break

            articles = data.get("articles")
            if not isinstance(articles, list):
                logger.error(
                    "Unexpected response format: 'articles' is not a list (user: %s, page: %d)",
                    username,
                    page,
                )
                break

            if not articles:
                break

            for article in articles:
                if len(slugs) >= max_articles:
                    break
                slug = article.get("slug")
                if isinstance(slug, str) and slug:
                    slugs.append(slug)

            next_page = data.get("next_page")
            if next_page is None:
                break

            try:
                page = int(next_page)
            except (ValueError, TypeError):
                logger.warning(
                    "Unexpected next_page value: %r (user: %s)",
                    next_page,
                    username,
                )
                break

        if page > MAX_PAGINATION_PAGES:
            logger.warning(
                "Pagination limit reached (%d pages) for user %s",
                MAX_PAGINATION_PAGES,
                username,
            )

        logger.info(
            "Discovered %d articles for user %s (pages scanned: %d)",
            len(slugs),
            username,
            min(page, MAX_PAGINATION_PAGES),
        )
        return slugs

    async def fetch_single(self, identifier: str) -> IngestedContent | None:
        """記事詳細 API から本文を取得し、IngestedContent を返す.

        Args:
            identifier: 記事の slug

        Returns:
            IngestedContent、または取得失敗時は None
        """
        slug = self.validate_identifier(identifier)
        url = f"{ZENN_API_BASE}/articles/{slug}"

        try:
            resp = await self._client.get(url)
        except Exception:
            logger.exception("Failed to fetch article detail: %s", slug)
            return None

        if resp.status_code != 200:
            logger.error(
                "Zenn API returned status %d for article: %s",
                resp.status_code,
                slug,
            )
            return None

        try:
            data: dict[str, Any] = resp.json()
        except Exception:
            logger.exception("Failed to parse JSON for article: %s", slug)
            return None

        # article キーでラップされている場合の対応
        article_data = data.get("article", data)
        if not isinstance(article_data, dict):
            logger.warning(
                "Unexpected article data format for article: %s (type: %s)",
                slug,
                type(article_data).__name__,
            )
            return None

        body_html = article_data.get("body_html", "")
        if not body_html:
            logger.warning("Empty body_html for article: %s", slug)
            return None

        # HTML からテキストを抽出
        text = self._extract_text_from_html(body_html)
        if not text.strip():
            logger.warning("No text extracted from article: %s", slug)
            return None

        # user 情報の取得（source_id 構築とメタデータの両方で使用）
        user = article_data.get("user", {})
        article_username = user.get("username", "") if isinstance(user, dict) else ""

        # source_id の構築
        path = article_data.get("path", "")
        if path:
            source_id = f"https://zenn.dev{path}"
        else:
            # path が取得できない場合のフォールバック
            if article_username:
                source_id = f"https://zenn.dev/{article_username}/articles/{slug}"
            else:
                logger.warning(
                    "Neither path nor username available for article: %s",
                    slug,
                )
                source_id = f"https://zenn.dev/articles/{slug}"

        title = article_data.get("title", "")

        # メタデータの構築
        topics = article_data.get("topics", [])
        topic_names: list[str] = []
        if isinstance(topics, list):
            for topic in topics:
                if isinstance(topic, dict):
                    name = topic.get("display_name") or topic.get("name", "")
                    if name:
                        topic_names.append(str(name))
                elif isinstance(topic, str):
                    topic_names.append(topic)

        metadata: dict[str, object] = {
            "slug": slug,
            "article_type": article_data.get("article_type", ""),
            "published_at": article_data.get("published_at", ""),
            "liked_count": article_data.get("liked_count", 0),
        }
        if topic_names:
            metadata["topics"] = topic_names
        if article_username:
            metadata["username"] = article_username

        return IngestedContent(
            source_id=source_id,
            title=title,
            text=text,
            ingested_at=IngestedContent.now_iso(),
            source_type="zenn",
            metadata=metadata,
        )

    @staticmethod
    def _extract_text_from_html(html: str) -> str:
        """HTML からテキストを抽出する.

        Args:
            html: HTML 文字列

        Returns:
            抽出されたテキスト
        """
        soup = BeautifulSoup(html, "html.parser")

        # 不要なタグを除去
        for tag_name in ("script", "style"):
            for tag in soup.find_all(tag_name):
                tag.decompose()

        text = soup.get_text(separator="\n")

        # 連続する空白行を整理
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

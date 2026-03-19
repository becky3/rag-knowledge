"""Zenn インジェスター.

仕様: docs/specs/ingesters/zenn.md

Zenn API 経由で記事・スクラップを取得し、
source_store にファイルを配置する。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from rag.pipeline.ingesters._common import IngestResult, now_iso

if TYPE_CHECKING:

    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)

# ハードリミット: ページネーション走査上限
MAX_PAGINATION_PAGES = 10

# ハードリミット: 記事取得上限
MAX_ARTICLES_HARD_LIMIT = 100

# Zenn API ベース URL
ZENN_API_BASE = "https://zenn.dev/api"


class ZennIngester:
    """Zenn インジェスター.

    Zenn API から記事・スクラップを取得し、
    source_store にファイルを配置する。
    """

    def __init__(
        self,
        source_store: SourceStore,
        *,
        max_articles: int = 50,
    ) -> None:
        self._store = source_store
        self._max_articles = max_articles

    async def crawl_zenn(
        self,
        username: str,
        *,
        max_articles: int | None = None,
        content_type: str = "all",
        client: Any | None = None,
    ) -> IngestResult:
        """Zenn コンテンツを取得し source_store に配置する.

        Args:
            username: Zenn ユーザー名
            max_articles: 取得する最大コンテンツ数（None の場合はインスタンス設定を使用）
            content_type: 取得対象（``articles``, ``scraps``, ``all``）
            client: ConstrainedClient インスタンス

        Returns:
            配置結果
        """
        result = IngestResult()

        # バリデーション
        if not username or not username.strip():
            raise ValueError("username が空です")
        if content_type not in ("articles", "scraps", "all"):
            raise ValueError(
                f"content_type は 'articles', 'scraps', 'all' のいずれかで指定してください"
                f"（受け取った値: {content_type!r}）"
            )

        effective_max = self._validate_max_articles(
            max_articles if max_articles is not None else self._max_articles
        )

        if client is None:
            raise ValueError("client (ConstrainedClient) が必要です")

        # content_type に応じて処理
        if content_type in ("articles", "all"):
            await self._crawl_articles(
                username, effective_max, client, result
            )
        if content_type in ("scraps", "all"):
            await self._crawl_scraps(
                username, effective_max, client, result
            )

        return result

    async def _crawl_articles(
        self,
        username: str,
        max_articles: int,
        client: Any,
        result: IngestResult,
    ) -> None:
        """記事を取得して配置する."""
        # 一覧走査
        slugs = await self._discover_slugs(
            username, "articles", max_articles, client
        )

        for slug in slugs:
            try:
                # 記事詳細取得
                url = f"{ZENN_API_BASE}/articles/{slug}"
                resp = await client.get(url)
                data = resp.json()
                article = data.get("article", data)

                body_html = article.get("body_html", "")
                if not body_html:
                    logger.info("body_html が空のためスキップ: %s", slug)
                    result.skipped += 1
                    continue

                # ファイル配置
                rel_path = f"zenn/{username}/articles/{slug}.html"
                html_bytes = body_html.encode("utf-8")

                # topics の抽出
                topics = self._extract_topics(article.get("topics", []))

                # .meta 生成
                path = article.get("path", f"/{username}/articles/{slug}")
                metadata = {
                    "source_id": f"https://zenn.dev{path}",
                    "source_type": "zenn",
                    "title": article.get("title", ""),
                    "collected_at": now_iso(),
                    "slug": slug,
                    "content_type": "article",
                    "article_type": article.get("article_type", ""),
                    "published_at": article.get("published_at", ""),
                    "liked_count": article.get("liked_count", 0),
                    "topics": topics,
                    "comments_count": 0,
                    "closed": False,
                    "username": username,
                }

                self._store.place_file(
                    source_type="zenn",
                    data=html_bytes,
                    rel_path=rel_path,
                    metadata=metadata,
                )
                result.placed += 1

            except Exception:
                logger.exception("記事の取得・配置に失敗しました: %s", slug)
                result.errors += 1
                result.error_details.append(f"articles/{slug}")

    async def _crawl_scraps(
        self,
        username: str,
        max_articles: int,
        client: Any,
        result: IngestResult,
    ) -> None:
        """スクラップを取得して配置する."""
        # 一覧走査
        slugs = await self._discover_slugs(
            username, "scraps", max_articles, client
        )

        for slug in slugs:
            try:
                # スクラップ詳細取得
                url = f"{ZENN_API_BASE}/scraps/{slug}"
                resp = await client.get(url)
                data = resp.json()
                scrap = data.get("scrap", data)

                # JSON として保存
                rel_path = f"zenn/{username}/scraps/{slug}.json"
                json_data = json.dumps(scrap, ensure_ascii=False, indent=2)
                json_bytes = json_data.encode("utf-8")

                # topics の抽出
                topics = self._extract_topics(scrap.get("topics", []))

                # .meta 生成
                path = scrap.get("path", f"/{username}/scraps/{slug}")
                metadata = {
                    "source_id": f"https://zenn.dev{path}",
                    "source_type": "zenn",
                    "title": scrap.get("title", ""),
                    "collected_at": now_iso(),
                    "slug": slug,
                    "content_type": "scrap",
                    "article_type": "",
                    "published_at": scrap.get("created_at", ""),
                    "liked_count": scrap.get("liked_count", 0),
                    "topics": topics,
                    "comments_count": scrap.get("comments_count", 0),
                    "closed": scrap.get("closed", False),
                    "username": username,
                }

                self._store.place_file(
                    source_type="zenn",
                    data=json_bytes,
                    rel_path=rel_path,
                    metadata=metadata,
                )
                result.placed += 1

            except Exception:
                logger.exception("スクラップの取得・配置に失敗しました: %s", slug)
                result.errors += 1
                result.error_details.append(f"scraps/{slug}")

    async def _discover_slugs(
        self,
        username: str,
        kind: str,
        max_count: int,
        client: Any,
    ) -> list[str]:
        """コンテンツ一覧 API を走査して slug のリストを収集する.

        Args:
            username: Zenn ユーザー名
            kind: "articles" または "scraps"
            max_count: 最大取得件数
            client: ConstrainedClient

        Returns:
            slug のリスト
        """
        slugs: list[str] = []
        page = 1

        while page <= MAX_PAGINATION_PAGES and len(slugs) < max_count:
            url = f"{ZENN_API_BASE}/{kind}?username={username}&order=latest&page={page}"
            resp = await client.get(url)
            data = resp.json()

            items = data.get(kind, [])
            if not items:
                break

            for item in items:
                if len(slugs) >= max_count:
                    break
                slug = item.get("slug", "")
                if slug:
                    slugs.append(slug)

            next_page = data.get("next_page")
            if next_page is None:
                break
            page = next_page

        return slugs

    @staticmethod
    def _extract_topics(topics_data: list[object]) -> list[str]:
        """topics 配列からトピック名を抽出する."""
        topics: list[str] = []
        for topic in topics_data:
            if isinstance(topic, dict):
                name = topic.get("display_name") or topic.get("name", "")
                if name:
                    topics.append(str(name))
            elif isinstance(topic, str):
                topics.append(topic)
        return topics

    def _validate_max_articles(self, max_articles: object) -> int:
        """max_articles のバリデーション."""
        if isinstance(max_articles, bool) or not isinstance(max_articles, int):
            raise TypeError(
                f"max_articles は整数で指定してください（受け取った値: {max_articles!r}）"
            )
        if max_articles <= 0:
            raise ValueError(
                f"max_articles は 1 以上で指定してください（受け取った値: {max_articles}）"
            )
        if max_articles > MAX_ARTICLES_HARD_LIMIT:
            logger.warning(
                "max_articles が上限 %d を超えています（%d）。%d にクランプします",
                MAX_ARTICLES_HARD_LIMIT,
                max_articles,
                MAX_ARTICLES_HARD_LIMIT,
            )
            return MAX_ARTICLES_HARD_LIMIT
        return max_articles

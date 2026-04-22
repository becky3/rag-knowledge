"""Zenn インジェスター.

仕様: docs/specs/ingesters/zenn.md

Zenn API 経由で記事・スクラップを取得し、
source_store にファイルを配置する。
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

import httpx

from rag.pipeline.ingesters._common import (
    IngestResult,
    ProgressCallback,
    fetch_get,
    now_iso,
)

if TYPE_CHECKING:

    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)

# ハードリミット: ページネーション走査上限
MAX_PAGINATION_PAGES = 10

# ハードリミット: 記事取得上限
MAX_ARTICLES_HARD_LIMIT = 100

# Zenn API ベース URL
ZENN_API_BASE = "https://zenn.dev/api"

_ZENN_URL_RE = re.compile(
    r"^https?://zenn\.dev/([^/]+)/(articles|scraps)/([^/?#]+)",
)


def parse_zenn_url(url: str) -> tuple[str, str, str] | None:
    """Zenn コンテンツ URL から (username, kind, slug) を抽出する.

    kind は "articles" or "scraps"。

    Returns:
        (username, kind, slug) タプル、またはパース失敗時は None
    """
    m = _ZENN_URL_RE.match(url.strip())
    if m is None:
        return None
    return m.group(1), m.group(2), m.group(3)


class ZennIngester:
    """Zenn インジェスター.

    Zenn API から記事・スクラップを取得し、
    source_store にファイルを配置する。
    """

    def __init__(
        self,
        source_store: SourceStore,
        *,
        max_articles: int,
    ) -> None:
        self._store = source_store
        self._max_articles = max_articles

    async def crawl_zenn(
        self,
        username: str,
        *,
        max_articles: int | None = None,
        content_type: str = "all",
        force: bool = False,
        client: Any | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> IngestResult:
        """Zenn コンテンツを取得し source_store に配置する.

        Args:
            username: Zenn ユーザー名
            max_articles: 取得する最大コンテンツ数（None の場合はインスタンス設定を使用）
            content_type: 取得対象（``articles``, ``scraps``, ``all``）
            force: 既存ファイルを上書きするか（デフォルト: False＝スキップモード）
            client: ConstrainedClient インスタンス
            progress_callback: 進捗コールバック (processed, total, current)

        Returns:
            配置結果
        """
        logger.info(
            "Zenn crawl started: username=%s, content_type=%s, max=%s, force=%s",
            username, content_type,
            max_articles if max_articles is not None else self._max_articles,
            force,
        )
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
        # content_type="all" 時は articles → scraps の順に処理するため、
        # 進捗の total がリセットされないようオフセットで合計管理する
        progress_offset = [0]

        def _offset_progress(processed: int, total: int, current: str) -> None:
            assert progress_callback is not None  # noqa: S101
            progress_callback(
                progress_offset[0] + processed,
                progress_offset[0] + total,
                current,
            )

        effective_cb = _offset_progress if progress_callback is not None else None

        if content_type in ("articles", "all"):
            items_before = result.placed + result.overwritten + result.skipped + result.errors
            await self._crawl_articles(
                username, effective_max, client, result, force=force,
                progress_callback=effective_cb,
            )
            progress_offset[0] = (result.placed + result.overwritten + result.skipped + result.errors) - items_before

        if content_type in ("scraps", "all"):
            await self._crawl_scraps(
                username, effective_max, client, result, force=force,
                progress_callback=effective_cb,
            )

        logger.info(
            "Zenn crawl completed: placed=%d, overwritten=%d, skipped=%d, errors=%d",
            result.placed, result.overwritten, result.skipped, result.errors,
        )
        return result

    async def _crawl_articles(
        self,
        username: str,
        max_articles: int,
        client: Any,
        result: IngestResult,
        *,
        force: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        """記事を取得して配置する."""
        # 一覧走査
        slugs = await self._discover_slugs(
            username, "articles", max_articles, client
        )

        for i, slug in enumerate(slugs):
            rel_path = f"zenn/{username}/articles/{slug}.json"
            dest = self._store.root_dir / rel_path

            # 取得フェーズ（category="metadata_fetch"）
            try:
                # スキップ判定: 既存ファイルがあり force でなければスキップ
                if dest.exists() and not force:
                    logger.debug("既存ファイルのためスキップ: %s", rel_path)
                    result.skipped += 1
                    if progress_callback is not None:
                        progress_callback(i + 1, len(slugs), f"articles/{slug}")
                    continue

                # 記事詳細取得
                url = f"{ZENN_API_BASE}/articles/{slug}"
                resp = await fetch_get(client, url)
                data = resp.json()
                article = data.get("article", data)

                if not article:
                    logger.info("article オブジェクトが空のためスキップ: %s", slug)
                    result.skipped += 1
                    if progress_callback is not None:
                        progress_callback(i + 1, len(slugs), f"articles/{slug}")
                    continue

                # JSON として保存
                json_data = json.dumps(article, ensure_ascii=False, indent=2)
                json_bytes = json_data.encode("utf-8")

                # topics の抽出
                topics = self._extract_topics(article.get("topics", []))

                # .meta 生成
                path = article.get("path", f"/{username}/articles/{slug}")
                metadata = {
                    "url": f"https://zenn.dev{path}",
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
            except Exception as exc:
                logger.exception("記事の取得に失敗しました: %s", slug)
                result.errors += 1
                fetch_detail: dict[str, Any] = {
                    "category": "metadata_fetch",
                    "target": f"articles/{slug}",
                    "message": str(exc),
                }
                if isinstance(exc, httpx.HTTPStatusError):
                    fetch_detail["status"] = exc.response.status_code
                    fetch_detail["url"] = str(exc.request.url)
                result.error_details.append(fetch_detail)
                if progress_callback is not None:
                    progress_callback(i + 1, len(slugs), f"articles/{slug}")
                continue

            # 配置フェーズ（category="placement"）
            try:
                is_overwrite = dest.exists()
                self._store.place_file(
                    source_type="zenn",
                    data=json_bytes,
                    rel_path=rel_path,
                    metadata=metadata,
                )
                if is_overwrite:
                    result.overwritten += 1
                else:
                    result.placed += 1
            except Exception as exc:
                logger.exception("記事の配置に失敗しました: %s", rel_path)
                result.errors += 1
                result.error_details.append({
                    "category": "placement",
                    "target": rel_path,
                    "message": str(exc),
                })

            if progress_callback is not None:
                progress_callback(i + 1, len(slugs), f"articles/{slug}")

    async def _crawl_scraps(
        self,
        username: str,
        max_articles: int,
        client: Any,
        result: IngestResult,
        *,
        force: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        """スクラップを取得して配置する."""
        # 一覧走査
        slugs = await self._discover_slugs(
            username, "scraps", max_articles, client
        )

        for i, slug in enumerate(slugs):
            rel_path = f"zenn/{username}/scraps/{slug}.json"
            dest = self._store.root_dir / rel_path

            # 取得フェーズ（category="metadata_fetch"）
            try:
                # スキップ判定: 既存ファイルがあり force でなければスキップ
                if dest.exists() and not force:
                    logger.debug("既存ファイルのためスキップ: %s", rel_path)
                    result.skipped += 1
                    if progress_callback is not None:
                        progress_callback(i + 1, len(slugs), f"scraps/{slug}")
                    continue

                # スクラップ詳細取得
                url = f"{ZENN_API_BASE}/scraps/{slug}"
                resp = await fetch_get(client, url)
                data = resp.json()
                scrap = data.get("scrap", data)
                json_data = json.dumps(scrap, ensure_ascii=False, indent=2)
                json_bytes = json_data.encode("utf-8")

                # topics の抽出
                topics = self._extract_topics(scrap.get("topics", []))

                # .meta 生成
                path = scrap.get("path", f"/{username}/scraps/{slug}")
                metadata = {
                    "url": f"https://zenn.dev{path}",
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
            except Exception as exc:
                logger.exception("スクラップの取得に失敗しました: %s", slug)
                result.errors += 1
                fetch_detail: dict[str, Any] = {
                    "category": "metadata_fetch",
                    "target": f"scraps/{slug}",
                    "message": str(exc),
                }
                if isinstance(exc, httpx.HTTPStatusError):
                    fetch_detail["status"] = exc.response.status_code
                    fetch_detail["url"] = str(exc.request.url)
                result.error_details.append(fetch_detail)
                if progress_callback is not None:
                    progress_callback(i + 1, len(slugs), f"scraps/{slug}")
                continue

            # 配置フェーズ（category="placement"）
            try:
                is_overwrite = dest.exists()
                self._store.place_file(
                    source_type="zenn",
                    data=json_bytes,
                    rel_path=rel_path,
                    metadata=metadata,
                )
                if is_overwrite:
                    result.overwritten += 1
                else:
                    result.placed += 1
            except Exception as exc:
                logger.exception("スクラップの配置に失敗しました: %s", rel_path)
                result.errors += 1
                result.error_details.append({
                    "category": "placement",
                    "target": rel_path,
                    "message": str(exc),
                })

            if progress_callback is not None:
                progress_callback(i + 1, len(slugs), f"scraps/{slug}")

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
        logger.info("Discovering %s slugs (max=%d)", kind, max_count)
        slugs: list[str] = []
        page = 1

        while page <= MAX_PAGINATION_PAGES and len(slugs) < max_count:
            url = f"{ZENN_API_BASE}/{kind}?username={username}&order=latest&page={page}"
            resp = await fetch_get(client, url)
            data = resp.json()

            items = data.get(kind, [])
            if not items:
                break

            for item in items:
                if len(slugs) >= max_count:
                    break
                # ユーザー名検証: Zenn API は ?username= を無視して
                # 全ユーザーの記事を返す場合があるため、
                # レスポンス内の user.username を照合する
                item_user = item.get("user")
                if isinstance(item_user, dict):
                    item_username = item_user.get("username", "")
                    if item_username and item_username != username:
                        logger.info(
                            "Skipping %s from different user: %s (expected: %s)",
                            kind,
                            item_username,
                            username,
                        )
                        continue
                slug = item.get("slug", "")
                if slug:
                    slugs.append(slug)

            next_page = data.get("next_page")
            if next_page is None:
                break
            page = next_page

        logger.info("Discovered %d %s slugs", len(slugs), kind)
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

    async def _fetch_and_place_single(
        self,
        username: str,
        kind: str,
        slug: str,
        client: Any,
        result: IngestResult,
        *,
        force: bool = True,
    ) -> None:
        """単一の記事またはスクラップを取得して配置する."""
        rel_path = f"zenn/{username}/{kind}/{slug}.json"
        dest = self._store.root_dir / rel_path

        if dest.exists() and not force:
            result.skipped += 1
            return

        try:
            api_url = f"{ZENN_API_BASE}/{kind}/{slug}"
            resp = await fetch_get(client, api_url)
            data = resp.json()

            if kind == "articles":
                content_obj = data.get("article", data)
            else:
                content_obj = data.get("scrap", data)

            if not content_obj:
                logger.warning("コンテンツオブジェクトが空です: %s/%s", kind, slug)
                result.errors += 1
                result.error_details.append({
                    "category": "metadata_fetch",
                    "target": f"{kind}/{slug}",
                    "message": "Empty content object",
                })
                return

            json_data = json.dumps(content_obj, ensure_ascii=False, indent=2)
            json_bytes = json_data.encode("utf-8")

            topics = self._extract_topics(content_obj.get("topics", []))

            if kind == "articles":
                path = content_obj.get("path", f"/{username}/articles/{slug}")
                metadata: dict[str, Any] = {
                    "url": f"https://zenn.dev{path}",
                    "source_type": "zenn",
                    "title": content_obj.get("title", ""),
                    "collected_at": now_iso(),
                    "slug": slug,
                    "content_type": "article",
                    "article_type": content_obj.get("article_type", ""),
                    "published_at": content_obj.get("published_at", ""),
                    "liked_count": content_obj.get("liked_count", 0),
                    "topics": topics,
                    "comments_count": 0,
                    "closed": False,
                    "username": username,
                }
            else:
                path = content_obj.get("path", f"/{username}/scraps/{slug}")
                metadata = {
                    "url": f"https://zenn.dev{path}",
                    "source_type": "zenn",
                    "title": content_obj.get("title", ""),
                    "collected_at": now_iso(),
                    "slug": slug,
                    "content_type": "scrap",
                    "article_type": "",
                    "published_at": content_obj.get("created_at", ""),
                    "liked_count": content_obj.get("liked_count", 0),
                    "topics": topics,
                    "comments_count": content_obj.get("comments_count", 0),
                    "closed": content_obj.get("closed", False),
                    "username": username,
                }
        except Exception as exc:
            logger.exception("コンテンツの取得に失敗しました: %s/%s", kind, slug)
            result.errors += 1
            fetch_detail: dict[str, Any] = {
                "category": "metadata_fetch",
                "target": f"{kind}/{slug}",
                "message": str(exc),
            }
            if isinstance(exc, httpx.HTTPStatusError):
                fetch_detail["status"] = exc.response.status_code
                fetch_detail["url"] = str(exc.request.url)
            result.error_details.append(fetch_detail)
            return

        try:
            is_overwrite = dest.exists()
            self._store.place_file(
                source_type="zenn",
                data=json_bytes,
                rel_path=rel_path,
                metadata=metadata,
            )
            if is_overwrite:
                result.overwritten += 1
            else:
                result.placed += 1
        except Exception as exc:
            logger.exception("コンテンツの配置に失敗しました: %s", rel_path)
            result.errors += 1
            result.error_details.append({
                "category": "placement",
                "target": rel_path,
                "message": str(exc),
            })

    async def ingest_contents(
        self,
        urls: list[str],
        *,
        client: Any,
    ) -> IngestResult:
        """指定 URL の Zenn コンテンツを取得して source_store に配置する.

        仕様: docs/specs/ingesters/zenn.md
        """
        result = IngestResult()

        if not urls:
            return result

        for url in urls:
            parsed = parse_zenn_url(url)
            if parsed is None:
                logger.warning("Zenn URL のパースに失敗しました: %s", url)
                result.errors += 1
                result.error_details.append({
                    "category": "metadata_fetch",
                    "target": url,
                    "message": "Invalid Zenn URL format",
                })
                continue

            username, kind, slug = parsed
            await self._fetch_and_place_single(
                username, kind, slug, client, result, force=True,
            )

        logger.info(
            "Zenn ingest_contents completed: placed=%d, overwritten=%d, errors=%d",
            result.placed, result.overwritten, result.errors,
        )
        return result

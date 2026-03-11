"""ZennIngester のテスト

仕様: docs/specs/rag-knowledge.md（ZennIngester セクション）
Issue: #94

テスト方針:
- バリデーションテスト（正常スラッグ、大文字正規化、不正文字）
- fetch_single テスト（正常取得、404、5xx、空本文、タイムアウト）
- discover テスト（ページネーション、上限、ユーザー不存在）
- レート制限テスト（ディレイ適用確認、429 バックオフ）
- 外部 API はモック化
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from rag.ingesters.zenn import ZennIngester
from rag.rag_knowledge import RAGKnowledgeService


# --- validate_identifier テスト ---


class TestZennIngesterValidate:
    """ZennIngester.validate_identifier() のテスト."""

    def test_valid_slug(self) -> None:
        """正常なスラッグが通ること."""
        ingester = ZennIngester()
        assert ingester.validate_identifier("my-article-123") == "my-article-123"

    def test_valid_slug_numbers_only(self) -> None:
        """数字のみのスラッグが通ること."""
        ingester = ZennIngester()
        assert ingester.validate_identifier("123") == "123"

    def test_uppercase_normalized(self) -> None:
        """大文字が小文字に正規化されること."""
        ingester = ZennIngester()
        assert ingester.validate_identifier("My-Article") == "my-article"

    def test_whitespace_trimmed(self) -> None:
        """前後の空白がトリムされること."""
        ingester = ZennIngester()
        assert ingester.validate_identifier("  my-slug  ") == "my-slug"

    def test_empty_slug_raises(self) -> None:
        """空文字列で ValueError が発生すること."""
        ingester = ZennIngester()
        with pytest.raises(ValueError, match="スラッグが空です"):
            ingester.validate_identifier("")

    def test_whitespace_only_raises(self) -> None:
        """空白のみで ValueError が発生すること."""
        ingester = ZennIngester()
        with pytest.raises(ValueError, match="スラッグが空です"):
            ingester.validate_identifier("   ")

    def test_invalid_chars_raises(self) -> None:
        """不正な文字（アンダースコア）で ValueError が発生すること."""
        ingester = ZennIngester()
        with pytest.raises(ValueError, match="不正なスラッグです"):
            ingester.validate_identifier("my_article")

    def test_invalid_chars_slash(self) -> None:
        """スラッシュを含む場合に ValueError が発生すること."""
        ingester = ZennIngester()
        with pytest.raises(ValueError, match="不正なスラッグです"):
            ingester.validate_identifier("user/article")

    def test_invalid_chars_japanese(self) -> None:
        """日本語文字で ValueError が発生すること."""
        ingester = ZennIngester()
        with pytest.raises(ValueError, match="不正なスラッグです"):
            ingester.validate_identifier("記事")

    def test_starts_with_hyphen_raises(self) -> None:
        """ハイフン始まりで ValueError が発生すること."""
        ingester = ZennIngester()
        with pytest.raises(ValueError, match="不正なスラッグです"):
            ingester.validate_identifier("-invalid")


# --- fetch_single テスト ---


def _mock_response(
    status: int = 200,
    json_data: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """aiohttp レスポンスのモックを作成する."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    resp.headers = headers or {}
    resp.request_info = MagicMock()
    resp.history = ()
    # async context manager
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


class TestZennIngesterFetchSingle:
    """ZennIngester.fetch_single() のテスト."""

    async def test_fetch_single_success(self) -> None:
        """正常に記事を取得して IngestedContent を返すこと."""
        ingester = ZennIngester(request_delay_sec=0)
        article_data = {
            "article": {
                "slug": "my-article",
                "title": "My Article",
                "body_md": "# Hello\nThis is content.",
                "published_at": "2024-01-01T00:00:00+00:00",
            }
        }
        mock_resp = _mock_response(200, article_data)

        with patch.object(
            aiohttp.ClientSession, "get", return_value=mock_resp
        ):
            result = await ingester.fetch_single("my-article")

        assert result is not None
        assert result.source_id == "my-article"
        assert result.title == "My Article"
        assert result.text == "# Hello\nThis is content."
        assert result.source_type == "zenn"
        assert result.metadata["published_at"] == "2024-01-01T00:00:00+00:00"

    async def test_fetch_single_404(self) -> None:
        """404 レスポンスで ValueError が発生すること."""
        ingester = ZennIngester(request_delay_sec=0)
        mock_resp = _mock_response(404)

        with patch.object(
            aiohttp.ClientSession, "get", return_value=mock_resp
        ):
            with pytest.raises(ValueError, match="記事が見つかりません"):
                await ingester.fetch_single("nonexistent")

    async def test_fetch_single_5xx(self) -> None:
        """5xx レスポンスで None を返すこと."""
        ingester = ZennIngester(request_delay_sec=0)
        mock_resp = _mock_response(500)

        with patch.object(
            aiohttp.ClientSession, "get", return_value=mock_resp
        ):
            result = await ingester.fetch_single("my-article")
        assert result is None

    async def test_fetch_single_empty_body(self) -> None:
        """本文が空の記事で None を返すこと."""
        ingester = ZennIngester(request_delay_sec=0)
        article_data = {
            "article": {
                "slug": "empty-article",
                "title": "Empty",
                "body_md": "",
            }
        }
        mock_resp = _mock_response(200, article_data)

        with patch.object(
            aiohttp.ClientSession, "get", return_value=mock_resp
        ):
            result = await ingester.fetch_single("empty-article")
        assert result is None

    async def test_fetch_single_whitespace_body(self) -> None:
        """空白のみの本文で None を返すこと."""
        ingester = ZennIngester(request_delay_sec=0)
        article_data = {
            "article": {
                "slug": "ws-article",
                "title": "Whitespace",
                "body_md": "   \n  ",
            }
        }
        mock_resp = _mock_response(200, article_data)

        with patch.object(
            aiohttp.ClientSession, "get", return_value=mock_resp
        ):
            result = await ingester.fetch_single("ws-article")
        assert result is None

    async def test_fetch_single_timeout(self) -> None:
        """タイムアウトで None を返すこと."""
        ingester = ZennIngester(request_delay_sec=0, request_timeout_sec=0.1)

        mock_resp = MagicMock()
        mock_resp.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with patch.object(
            aiohttp.ClientSession, "get", return_value=mock_resp
        ):
            result = await ingester.fetch_single("timeout-article")
        assert result is None


# --- discover テスト ---


class TestZennIngesterDiscover:
    """ZennIngester.discover() のテスト."""

    async def test_discover_single_page(self) -> None:
        """1ページの記事一覧を正しく取得すること."""
        ingester = ZennIngester(request_delay_sec=0)
        response_data = {
            "articles": [
                {"slug": "article-1"},
                {"slug": "article-2"},
            ],
            "next_page": None,
        }
        mock_resp = _mock_response(200, response_data)

        with patch.object(
            aiohttp.ClientSession, "get", return_value=mock_resp
        ):
            slugs = await ingester.discover("testuser")

        assert slugs == ["article-1", "article-2"]

    async def test_discover_pagination(self) -> None:
        """複数ページのページネーションに対応すること."""
        ingester = ZennIngester(request_delay_sec=0)

        page1_data = {
            "articles": [{"slug": "article-1"}],
            "next_page": 2,
        }
        page2_data = {
            "articles": [{"slug": "article-2"}],
            "next_page": None,
        }

        mock_resp1 = _mock_response(200, page1_data)
        mock_resp2 = _mock_response(200, page2_data)

        with patch.object(
            aiohttp.ClientSession,
            "get",
            side_effect=[mock_resp1, mock_resp2],
        ):
            slugs = await ingester.discover("testuser")

        assert slugs == ["article-1", "article-2"]

    async def test_discover_pagination_limit(self) -> None:
        """ページネーション上限で取得を停止すること."""
        ingester = ZennIngester(request_delay_sec=0, max_pagination_pages=1)

        response_data = {
            "articles": [{"slug": "article-1"}],
            "next_page": 2,
        }
        mock_resp = _mock_response(200, response_data)

        with patch.object(
            aiohttp.ClientSession, "get", return_value=mock_resp
        ):
            slugs = await ingester.discover("testuser")

        assert slugs == ["article-1"]

    async def test_discover_empty_user(self) -> None:
        """空のユーザー名で空リストを返すこと."""
        ingester = ZennIngester(request_delay_sec=0)
        slugs = await ingester.discover("")
        assert slugs == []

    async def test_discover_invalid_username_raises(self) -> None:
        """不正なユーザー名で ValueError が発生すること."""
        ingester = ZennIngester(request_delay_sec=0)
        with pytest.raises(ValueError, match="不正なユーザー名です"):
            await ingester.discover("user&admin=true")

    async def test_discover_invalid_username_slash(self) -> None:
        """スラッシュを含むユーザー名で ValueError が発生すること."""
        ingester = ZennIngester(request_delay_sec=0)
        with pytest.raises(ValueError, match="不正なユーザー名です"):
            await ingester.discover("user/evil")

    async def test_discover_nonexistent_user(self) -> None:
        """存在しないユーザーで空リストを返すこと."""
        ingester = ZennIngester(request_delay_sec=0)
        response_data: dict[str, object] = {
            "articles": [],
            "next_page": None,
        }
        mock_resp = _mock_response(200, response_data)

        with patch.object(
            aiohttp.ClientSession, "get", return_value=mock_resp
        ):
            slugs = await ingester.discover("nonexistent-user")

        assert slugs == []


# --- レート制限テスト ---


class TestZennIngesterRateLimit:
    """レート制限関連のテスト."""

    async def test_fetch_batch_applies_delay(self) -> None:
        """fetch_batch() がリクエスト間にディレイを適用すること."""
        ingester = ZennIngester(request_delay_sec=0.1)

        article_data = {
            "article": {
                "slug": "article-1",
                "title": "Article 1",
                "body_md": "Content 1",
            }
        }
        mock_resp = _mock_response(200, article_data)

        with patch.object(
            aiohttp.ClientSession, "get", return_value=mock_resp
        ), patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            results = await ingester.fetch_batch(["article-1", "article-2"])

        assert len(results) == 2
        # 2件の間に1回のディレイが入る
        mock_sleep.assert_called_once_with(0.1)

    async def test_429_backoff_retry(self) -> None:
        """429 レスポンスで指数バックオフリトライすること."""
        ingester = ZennIngester(request_delay_sec=0, max_retries=2)

        article_data = {
            "article": {
                "slug": "article-1",
                "title": "Article 1",
                "body_md": "Content after retry",
            }
        }
        mock_429 = _mock_response(429)
        mock_ok = _mock_response(200, article_data)

        with patch.object(
            aiohttp.ClientSession,
            "get",
            side_effect=[mock_429, mock_ok],
        ), patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock):
            result = await ingester.fetch_single("article-1")

        assert result is not None
        assert result.text == "Content after retry"

    async def test_429_retry_after_header(self) -> None:
        """429 + Retry-After ヘッダーの値を尊重すること."""
        ingester = ZennIngester(request_delay_sec=0, max_retries=2)

        article_data = {
            "article": {
                "slug": "article-1",
                "title": "Article 1",
                "body_md": "Content",
            }
        }
        mock_429 = _mock_response(429, headers={"Retry-After": "5"})
        mock_ok = _mock_response(200, article_data)

        with patch.object(
            aiohttp.ClientSession,
            "get",
            side_effect=[mock_429, mock_ok],
        ), patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await ingester.fetch_single("article-1")

        assert result is not None
        # Retry-After の値（5秒）で待機したことを確認
        mock_sleep.assert_called_once_with(5.0)

    async def test_429_max_retries_exceeded(self) -> None:
        """429 リトライ上限で例外が発生すること."""
        ingester = ZennIngester(request_delay_sec=0, max_retries=1)

        mock_429 = _mock_response(429)

        with patch.object(
            aiohttp.ClientSession,
            "get",
            side_effect=[mock_429, mock_429],
        ), patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(aiohttp.ClientResponseError):
                await ingester.fetch_single("article-1")

    async def test_discover_applies_delay_between_pages(self) -> None:
        """discover() がページネーションリクエスト間にディレイを適用すること."""
        ingester = ZennIngester(request_delay_sec=0.1)

        page1_data = {
            "articles": [{"slug": "a1"}],
            "next_page": 2,
        }
        page2_data = {
            "articles": [{"slug": "a2"}],
            "next_page": None,
        }
        mock_resp1 = _mock_response(200, page1_data)
        mock_resp2 = _mock_response(200, page2_data)

        with patch.object(
            aiohttp.ClientSession,
            "get",
            side_effect=[mock_resp1, mock_resp2],
        ), patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            slugs = await ingester.discover("testuser")

        assert slugs == ["a1", "a2"]
        # ページ間のディレイが1回入る
        mock_sleep.assert_called_once_with(0.1)


# --- fetch_batch テスト ---


class TestZennIngesterFetchBatch:
    """ZennIngester.fetch_batch() のテスト."""

    async def test_fetch_batch_empty(self) -> None:
        """空リストで空の結果を返すこと."""
        ingester = ZennIngester(request_delay_sec=0)
        results = await ingester.fetch_batch([])
        assert results == []

    async def test_fetch_batch_with_failures(self) -> None:
        """一部失敗しても成功分を返すこと."""
        ingester = ZennIngester(request_delay_sec=0)

        ok_data = {
            "article": {
                "slug": "ok-article",
                "title": "OK",
                "body_md": "Content",
            }
        }
        mock_ok = _mock_response(200, ok_data)
        mock_500 = _mock_response(500)

        with patch.object(
            aiohttp.ClientSession,
            "get",
            side_effect=[mock_ok, mock_500],
        ):
            results = await ingester.fetch_batch(["ok-article", "fail-article"])

        assert len(results) == 1
        assert results[0].source_id == "ok-article"

    async def test_fetch_batch_skips_invalid_slugs(self) -> None:
        """不正なスラッグをスキップすること."""
        ingester = ZennIngester(request_delay_sec=0)

        ok_data = {
            "article": {
                "slug": "valid",
                "title": "Valid",
                "body_md": "Content",
            }
        }
        mock_ok = _mock_response(200, ok_data)

        with patch.object(
            aiohttp.ClientSession,
            "get",
            return_value=mock_ok,
        ):
            results = await ingester.fetch_batch(["valid", "invalid_slug"])

        assert len(results) == 1
        assert results[0].source_id == "valid"


# --- RAGKnowledgeService 統合テスト ---


class TestRAGServiceIngesterRegistry:
    """RAGKnowledgeService のインジェスター登録・ルーティングのテスト."""

    def _create_service(self) -> RAGKnowledgeService:
        """テスト用 RAGKnowledgeService を作成する."""
        from rag.vector_store import VectorStore
        from rag.web_crawler import WebCrawler

        mock_vs = MagicMock(spec=VectorStore)
        mock_vs.add_documents = AsyncMock(return_value=1)
        mock_vs.delete_stale_chunks = AsyncMock(return_value=0)
        mock_wc = MagicMock(spec=WebCrawler)
        mock_wc.crawl_page = AsyncMock(return_value=None)
        mock_wc.validate_url = MagicMock(side_effect=lambda url: url)

        service = RAGKnowledgeService(
            vector_store=mock_vs,
            web_crawler=mock_wc,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
        )
        return service

    def test_register_and_get_ingester(self) -> None:
        """インジェスターを登録して取得できること."""
        service = self._create_service()
        ingester = ZennIngester(request_delay_sec=0)
        service.register_ingester("zenn", ingester)

        result = service.get_ingester("zenn")
        assert result is ingester

    def test_get_ingester_unknown_type_raises(self) -> None:
        """未登録のソースタイプで ValueError が発生すること."""
        service = self._create_service()

        with pytest.raises(ValueError, match="未登録のソースタイプです"):
            service.get_ingester("unknown")

    async def test_ingest_single_via_zenn(self) -> None:
        """ingest_single() が ZennIngester 経由で動作すること."""
        service = self._create_service()
        ingester = ZennIngester(request_delay_sec=0)
        service.register_ingester("zenn", ingester)

        article_data = {
            "article": {
                "slug": "test-article",
                "title": "Test",
                "body_md": "Test content.",
            }
        }
        mock_resp = _mock_response(200, article_data)

        with patch.object(
            aiohttp.ClientSession, "get", return_value=mock_resp
        ):
            chunks = await service.ingest_single("zenn", "test-article")

        assert chunks >= 1

    async def test_ingest_batch_via_zenn(self) -> None:
        """ingest_batch() が ZennIngester 経由で動作すること."""
        service = self._create_service()
        ingester = ZennIngester(request_delay_sec=0)
        service.register_ingester("zenn", ingester)

        discover_data = {
            "articles": [
                {"slug": "article-1"},
            ],
            "next_page": None,
        }
        article_data = {
            "article": {
                "slug": "article-1",
                "title": "Article 1",
                "body_md": "Content 1.",
            }
        }
        mock_discover = _mock_response(200, discover_data)
        mock_article = _mock_response(200, article_data)

        with patch.object(
            aiohttp.ClientSession,
            "get",
            side_effect=[mock_discover, mock_article],
        ):
            result = await service.ingest_batch("zenn", "testuser")

        assert result["ingested"] == 1
        assert result["chunks_stored"] >= 1
        assert result["errors"] == 0

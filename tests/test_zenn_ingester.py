"""Zenn インジェスターのテスト

仕様: docs/specs/features/zenn-ingester.md
Issue: #114

テスト対象: ZennIngester クラスの全メソッド
テスト手法: 単体テスト（Zenn API はモックで代替）
重点観点:
  - ページネーション上限（10 ページ）で正しく停止するか
  - リクエスト間隔（1 秒以上）が守られているか
  - リトライ（3 回・指数バックオフ）の動作
  - 存在しないユーザー名 → 0 件で正常終了
  - API スキーマ変更（必須フィールド欠落）→ エラー停止
  - next_page の異常値 → ページネーション停止
  - dry_run 時にデータ書き込みが発生しないこと
  - body_html → テキスト/Markdown 変換の正確性
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from rag.ingesters.zenn import (
    DEFAULT_MAX_PAGES,
    DiscoverResult,
    ZennIngester,
)


# --- validate_identifier テスト ---


class TestZennIngesterValidate:
    """ZennIngester.validate_identifier() のテスト."""

    def test_valid_slug(self) -> None:
        """正常な slug が検証を通ること."""
        ingester = ZennIngester()
        assert ingester.validate_identifier("example-article") == "example-article"

    def test_valid_slug_with_numbers(self) -> None:
        """数字を含む slug が検証を通ること."""
        ingester = ZennIngester()
        assert ingester.validate_identifier("article-123") == "article-123"

    def test_valid_slug_with_underscore(self) -> None:
        """アンダースコアを含む slug が検証を通ること."""
        ingester = ZennIngester()
        assert ingester.validate_identifier("my_article") == "my_article"

    def test_empty_slug_raises(self) -> None:
        """空の slug で ValueError が発生すること."""
        ingester = ZennIngester()
        with pytest.raises(ValueError, match="不正な Zenn 記事 slug"):
            ingester.validate_identifier("")

    def test_invalid_slug_with_spaces(self) -> None:
        """スペースを含む slug で ValueError が発生すること."""
        ingester = ZennIngester()
        with pytest.raises(ValueError, match="不正な Zenn 記事 slug"):
            ingester.validate_identifier("invalid slug")

    def test_invalid_slug_with_special_chars(self) -> None:
        """特殊文字を含む slug で ValueError が発生すること."""
        ingester = ZennIngester()
        with pytest.raises(ValueError, match="不正な Zenn 記事 slug"):
            ingester.validate_identifier("slug/with/slashes")


# --- _html_to_text テスト ---


class TestZennIngesterHtmlToText:
    """ZennIngester._html_to_text() のテスト."""

    def test_basic_html_conversion(self) -> None:
        """基本的な HTML が Markdown に変換されること."""
        ingester = ZennIngester()
        html = "<h1>Title</h1><p>Hello world</p>"
        text = ingester._html_to_text(html)
        assert "Title" in text
        assert "Hello world" in text

    def test_empty_html(self) -> None:
        """空の HTML が空文字列を返すこと."""
        ingester = ZennIngester()
        assert ingester._html_to_text("") == ""

    def test_html_with_code_block(self) -> None:
        """コードブロックを含む HTML が変換されること."""
        ingester = ZennIngester()
        html = "<pre><code>print('hello')</code></pre>"
        text = ingester._html_to_text(html)
        assert "print('hello')" in text

    def test_html_links_stripped(self) -> None:
        """リンク URL が除去されテキストのみ残ること."""
        ingester = ZennIngester()
        html = '<a href="https://example.com">Link Text</a>'
        text = ingester._html_to_text(html)
        assert "Link Text" in text
        assert "https://example.com" not in text

    def test_html_images_stripped(self) -> None:
        """画像 URL が除去され alt テキストのみ残ること."""
        ingester = ZennIngester()
        html = '<img src="https://example.com/img.png" alt="My Image">'
        text = ingester._html_to_text(html)
        assert "My Image" in text
        assert "https://example.com" not in text

    def test_consecutive_blank_lines_normalized(self) -> None:
        """連続空行が 2 行に正規化されること."""
        ingester = ZennIngester()
        html = "<h1>Title</h1>\n\n\n\n\n<p>Content</p>"
        text = ingester._html_to_text(html)
        # 3 行以上の連続空行がないことを確認
        assert "\n\n\n" not in text
        assert "Title" in text
        assert "Content" in text


# --- fetch_single テスト ---


class TestZennIngesterFetchSingle:
    """ZennIngester.fetch_single() のテスト."""

    async def test_fetch_single_success(self) -> None:
        """正常に記事を取得して IngestedContent を返すこと."""
        ingester = ZennIngester()
        mock_response = {
            "article": {
                "slug": "test-article",
                "title": "Test Article",
                "body_html": "<h1>Test</h1><p>Content</p>",
                "published_at": "2024-01-01T00:00:00.000+09:00",
                "user": {"username": "testuser"},
            }
        }

        with patch.object(
            ingester, "_request_with_retry", new_callable=AsyncMock
        ) as mock_request:
            mock_request.return_value = mock_response

            result = await ingester.fetch_single("test-article")

        assert result is not None
        assert result.source_id == "https://zenn.dev/articles/test-article"
        assert result.title == "Test Article"
        assert "Test" in result.text
        assert "Content" in result.text
        assert result.source_type == "zenn"
        assert result.metadata["slug"] == "test-article"
        assert result.metadata["username"] == "testuser"
        assert result.metadata["raw_html"] == "<h1>Test</h1><p>Content</p>"

    async def test_fetch_single_missing_body_html(self) -> None:
        """body_html が欠落している場合に None を返すこと."""
        ingester = ZennIngester()
        mock_response = {
            "article": {
                "slug": "test-article",
                "title": "Test Article",
                # body_html がない
            }
        }

        with patch.object(
            ingester, "_request_with_retry", new_callable=AsyncMock
        ) as mock_request:
            mock_request.return_value = mock_response

            result = await ingester.fetch_single("test-article")

        assert result is None

    async def test_fetch_single_network_error(self) -> None:
        """ネットワークエラー時に None を返すこと."""
        ingester = ZennIngester()

        with patch.object(
            ingester, "_request_with_retry", new_callable=AsyncMock
        ) as mock_request:
            mock_request.side_effect = aiohttp.ClientError("Connection failed")

            result = await ingester.fetch_single("test-article")

        assert result is None

    async def test_fetch_single_invalid_slug(self) -> None:
        """不正な slug で ValueError が発生すること."""
        ingester = ZennIngester()

        with pytest.raises(ValueError, match="不正な Zenn 記事 slug"):
            await ingester.fetch_single("invalid slug")


# --- fetch_batch テスト ---


class TestZennIngesterFetchBatch:
    """ZennIngester.fetch_batch() のテスト."""

    async def test_fetch_batch_respects_interval(self) -> None:
        """リクエスト間隔（1 秒以上）が守られていること."""
        ingester = ZennIngester()
        mock_response = {
            "article": {
                "slug": "article-1",
                "title": "Article 1",
                "body_html": "<p>Content</p>",
                "user": {"username": "user"},
            }
        }

        call_times: list[float] = []
        original_sleep = asyncio.sleep

        async def mock_sleep(seconds: float) -> None:
            call_times.append(seconds)
            # Don't actually sleep in tests
            await original_sleep(0)

        with (
            patch.object(
                ingester, "_request_with_retry", new_callable=AsyncMock
            ) as mock_request,
            patch("rag.ingesters.zenn.asyncio.sleep", side_effect=mock_sleep),
        ):
            mock_request.return_value = mock_response

            await ingester.fetch_batch(["article-1", "article-2", "article-3"])

        # fetch_batch の間隔: 2 回の sleep（2 番目と 3 番目の前）
        assert len(call_times) == 2
        assert all(t >= 1.0 for t in call_times)

    async def test_fetch_batch_empty(self) -> None:
        """空リストで空の結果を返すこと."""
        ingester = ZennIngester()
        results = await ingester.fetch_batch([])
        assert results == []

    async def test_fetch_batch_partial_failure(self) -> None:
        """一部失敗しても成功分を返すこと."""
        ingester = ZennIngester()

        async def mock_fetch(slug: str) -> MagicMock | None:
            if slug == "good-article":
                content = MagicMock()
                content.source_id = "https://zenn.dev/articles/good-article"
                return content
            return None

        with patch.object(
            ingester, "fetch_single", side_effect=mock_fetch,
        ):
            results = await ingester.fetch_batch(
                ["good-article", "bad-article"]
            )

        assert len(results) == 1


# --- _request_with_retry テスト ---


class TestZennIngesterRetry:
    """ZennIngester._request_with_retry() のテスト."""

    async def test_retry_on_failure(self) -> None:
        """失敗時にリトライが行われること."""
        ingester = ZennIngester()
        call_count = 0

        def make_success_cm() -> MagicMock:
            """成功するコンテキストマネージャを作成."""
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = AsyncMock(return_value={"articles": []})
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=resp)
            cm.__aexit__ = AsyncMock(return_value=False)
            return cm

        def make_failing_cm() -> MagicMock:
            """失敗するコンテキストマネージャを作成."""
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(
                side_effect=aiohttp.ClientError("error")
            )
            cm.__aexit__ = AsyncMock(return_value=False)
            return cm

        def mock_get(url: str) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return make_failing_cm()
            return make_success_cm()

        mock_session = MagicMock()
        mock_session.get = mock_get

        with patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock):
            result = await ingester._request_with_retry(
                mock_session, "https://example.com"
            )

        assert result == {"articles": []}
        assert call_count == 3

    async def test_retry_exhausted_raises(self) -> None:
        """最大リトライ回数超過でエラーが発生すること."""
        ingester = ZennIngester()

        def failing_get(url: str) -> MagicMock:
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(
                side_effect=aiohttp.ClientError("persistent error")
            )
            cm.__aexit__ = AsyncMock(return_value=False)
            return cm

        mock_session = MagicMock()
        mock_session.get = failing_get

        with (
            patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(aiohttp.ClientError),
        ):
            await ingester._request_with_retry(
                mock_session, "https://example.com"
            )


# --- discover テスト ---


class TestZennIngesterDiscover:
    """ZennIngester.discover() のテスト."""

    async def test_discover_basic(self) -> None:
        """基本的な記事一覧取得が動作すること."""
        ingester = ZennIngester()

        mock_response = {
            "articles": [
                {"slug": "article-1"},
                {"slug": "article-2"},
            ],
            "next_page": None,
        }

        with (
            patch.object(
                ingester, "_request_with_retry", new_callable=AsyncMock
            ) as mock_request,
            patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_request.return_value = mock_response

            result = await ingester.discover("testuser")

        assert isinstance(result, DiscoverResult)
        assert result.slugs == ["article-1", "article-2"]
        assert result.limit_reached is False

    async def test_discover_pagination(self) -> None:
        """ページネーションが正しく動作すること."""
        ingester = ZennIngester(max_pages=3)

        responses = [
            {"articles": [{"slug": "a1"}], "next_page": 2},
            {"articles": [{"slug": "a2"}], "next_page": 3},
            {"articles": [{"slug": "a3"}], "next_page": None},
        ]

        with (
            patch.object(
                ingester, "_request_with_retry", new_callable=AsyncMock
            ) as mock_request,
            patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_request.side_effect = responses

            result = await ingester.discover("testuser")

        assert result.slugs == ["a1", "a2", "a3"]
        assert result.limit_reached is False
        assert mock_request.call_count == 3

    async def test_discover_pagination_limit(self) -> None:
        """ページネーション上限（デフォルト10ページ）で停止すること."""
        ingester = ZennIngester(max_pages=2)

        responses = [
            {"articles": [{"slug": "a1"}], "next_page": 2},
            {"articles": [{"slug": "a2"}], "next_page": 3},
            # Page 3 should NOT be requested
        ]

        with (
            patch.object(
                ingester, "_request_with_retry", new_callable=AsyncMock
            ) as mock_request,
            patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_request.side_effect = responses

            result = await ingester.discover("testuser")

        # 上限 2 ページで停止: a1, a2 のみ
        assert result.slugs == ["a1", "a2"]
        assert result.limit_reached is True
        assert mock_request.call_count == 2

    async def test_discover_default_pagination_limit(self) -> None:
        """デフォルトのページネーション上限が 10 であること."""
        ingester = ZennIngester()
        assert ingester._max_pages == DEFAULT_MAX_PAGES
        assert DEFAULT_MAX_PAGES == 10

    async def test_discover_no_limit(self) -> None:
        """no_limit=True でページネーション上限が解除されること."""
        ingester = ZennIngester(max_pages=2)

        responses = [
            {"articles": [{"slug": "a1"}], "next_page": 2},
            {"articles": [{"slug": "a2"}], "next_page": 3},
            {"articles": [{"slug": "a3"}], "next_page": None},
        ]

        with (
            patch.object(
                ingester, "_request_with_retry", new_callable=AsyncMock
            ) as mock_request,
            patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_request.side_effect = responses

            result = await ingester.discover("testuser", no_limit=True)

        # no_limit なので 3 ページ全て取得
        assert result.slugs == ["a1", "a2", "a3"]
        assert result.limit_reached is False
        assert mock_request.call_count == 3

    async def test_discover_empty_user(self) -> None:
        """存在しないユーザー（記事 0 件）で空リストを返すこと."""
        ingester = ZennIngester()

        mock_response = {
            "articles": [],
            "next_page": None,
        }

        with (
            patch.object(
                ingester, "_request_with_retry", new_callable=AsyncMock
            ) as mock_request,
            patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_request.return_value = mock_response

            result = await ingester.discover("nonexistent-user")

        assert result.slugs == []

    async def test_discover_missing_articles_field(self) -> None:
        """API レスポンスに articles フィールドがない場合にエラー停止すること."""
        ingester = ZennIngester()

        mock_response = {
            "data": [],  # articles フィールドがない
        }

        with (
            patch.object(
                ingester, "_request_with_retry", new_callable=AsyncMock
            ) as mock_request,
            patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_request.return_value = mock_response

            result = await ingester.discover("testuser")

        assert result.slugs == []

    async def test_discover_missing_slug_in_article(self) -> None:
        """記事に slug フィールドがない場合にエラー停止すること."""
        ingester = ZennIngester()

        mock_response = {
            "articles": [
                {"title": "No Slug Article"},  # slug がない
            ],
            "next_page": None,
        }

        with (
            patch.object(
                ingester, "_request_with_retry", new_callable=AsyncMock
            ) as mock_request,
            patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_request.return_value = mock_response

            result = await ingester.discover("testuser")

        assert result.slugs == []

    async def test_discover_unexpected_next_page(self) -> None:
        """next_page が予期しない値の場合にページネーションが停止すること."""
        ingester = ZennIngester()

        mock_response = {
            "articles": [{"slug": "a1"}],
            "next_page": "invalid",  # 文字列（整数でない）
        }

        with (
            patch.object(
                ingester, "_request_with_retry", new_callable=AsyncMock
            ) as mock_request,
            patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_request.return_value = mock_response

            result = await ingester.discover("testuser")

        assert result.slugs == ["a1"]
        # 1 回のリクエストのみ（next_page が不正で停止）
        assert mock_request.call_count == 1

    async def test_discover_negative_next_page(self) -> None:
        """next_page が負の値の場合にページネーションが停止すること."""
        ingester = ZennIngester()

        mock_response = {
            "articles": [{"slug": "a1"}],
            "next_page": -1,
        }

        with (
            patch.object(
                ingester, "_request_with_retry", new_callable=AsyncMock
            ) as mock_request,
            patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_request.return_value = mock_response

            result = await ingester.discover("testuser")

        assert result.slugs == ["a1"]

    async def test_discover_request_interval(self) -> None:
        """ページ間のリクエスト間隔（1 秒以上）が守られていること."""
        ingester = ZennIngester(max_pages=3)

        responses = [
            {"articles": [{"slug": "a1"}], "next_page": 2},
            {"articles": [{"slug": "a2"}], "next_page": 3},
            {"articles": [{"slug": "a3"}], "next_page": None},
        ]

        sleep_calls: list[float] = []

        async def mock_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        with (
            patch.object(
                ingester, "_request_with_retry", new_callable=AsyncMock
            ) as mock_request,
            patch("rag.ingesters.zenn.asyncio.sleep", side_effect=mock_sleep),
        ):
            mock_request.side_effect = responses

            await ingester.discover("testuser")

        # 2 回のスリープ（2 ページ目と 3 ページ目の前）
        assert len(sleep_calls) == 2
        assert all(s >= 1.0 for s in sleep_calls)

    async def test_discover_network_failure_stops(self) -> None:
        """記事一覧取得のリトライ失敗時は処理全体が停止すること."""
        ingester = ZennIngester()

        with (
            patch.object(
                ingester, "_request_with_retry", new_callable=AsyncMock
            ) as mock_request,
            patch("rag.ingesters.zenn.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_request.side_effect = aiohttp.ClientError("network error")

            result = await ingester.discover("testuser")

        assert result.slugs == []


# --- RAGKnowledgeService 統合テスト ---


class TestRAGServiceZennIntegration:
    """RAGKnowledgeService の Zenn インジェスター統合テスト."""

    async def test_ingest_zenn_dry_run(self) -> None:
        """dry_run 時にデータ書き込みが発生しないこと."""
        from rag.rag_knowledge import RAGKnowledgeService

        mock_vector_store = MagicMock()
        mock_vector_store.add_documents = AsyncMock(return_value=1)
        mock_vector_store.delete_stale_chunks = AsyncMock(return_value=0)

        mock_web_crawler = MagicMock()
        mock_zenn_ingester = MagicMock()
        mock_zenn_ingester.discover = AsyncMock(
            return_value=DiscoverResult(
                slugs=["slug-1", "slug-2", "slug-3"],
            )
        )
        mock_zenn_ingester.fetch_batch = AsyncMock(return_value=[])

        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
            zenn_ingester=mock_zenn_ingester,
        )

        result = await service.ingest_zenn("testuser", dry_run=True)

        assert result["dry_run"] is True
        assert result["articles_found"] == 3
        assert result["articles_ingested"] == 0
        assert result["chunks_stored"] == 0
        # fetch_batch は呼ばれない（dry_run）
        mock_zenn_ingester.fetch_batch.assert_not_called()
        # ベクトルストアへの書き込みがないこと
        mock_vector_store.add_documents.assert_not_called()

    async def test_ingest_zenn_full(self) -> None:
        """通常の一括取り込みが動作すること."""
        from rag.ingesters.base import IngestedContent
        from rag.rag_knowledge import RAGKnowledgeService

        mock_vector_store = MagicMock()
        mock_vector_store.add_documents = AsyncMock(return_value=2)
        mock_vector_store.delete_stale_chunks = AsyncMock(return_value=0)

        mock_web_crawler = MagicMock()
        mock_zenn_ingester = MagicMock()
        mock_zenn_ingester.discover = AsyncMock(
            return_value=DiscoverResult(slugs=["slug-1", "slug-2"])
        )
        mock_zenn_ingester.fetch_batch = AsyncMock(
            return_value=[
                IngestedContent(
                    source_id="https://zenn.dev/articles/slug-1",
                    title="Article 1",
                    text="Content 1",
                    ingested_at="2024-01-01T00:00:00+00:00",
                    source_type="zenn",
                ),
                IngestedContent(
                    source_id="https://zenn.dev/articles/slug-2",
                    title="Article 2",
                    text="Content 2",
                    ingested_at="2024-01-01T00:00:00+00:00",
                    source_type="zenn",
                ),
            ]
        )

        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
            zenn_ingester=mock_zenn_ingester,
        )

        result = await service.ingest_zenn("testuser")

        assert result["dry_run"] is False
        assert result["articles_found"] == 2
        assert result["articles_ingested"] == 2
        assert result["chunks_stored"] == 4  # 2 articles * 2 chunks each
        assert result["errors"] == 0

    async def test_add_zenn_success(self) -> None:
        """単体取り込みが動作すること."""
        from rag.ingesters.base import IngestedContent
        from rag.rag_knowledge import RAGKnowledgeService

        mock_vector_store = MagicMock()
        mock_vector_store.add_documents = AsyncMock(return_value=3)
        mock_vector_store.delete_stale_chunks = AsyncMock(return_value=0)

        mock_web_crawler = MagicMock()
        mock_zenn_ingester = MagicMock()
        mock_zenn_ingester.fetch_single = AsyncMock(
            return_value=IngestedContent(
                source_id="https://zenn.dev/articles/test-slug",
                title="Test Article",
                text="Test content for chunking.",
                ingested_at="2024-01-01T00:00:00+00:00",
                source_type="zenn",
            )
        )

        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
            zenn_ingester=mock_zenn_ingester,
        )

        result = await service.add_zenn("test-slug")

        assert result == 3
        mock_zenn_ingester.fetch_single.assert_called_once_with("test-slug")

    async def test_add_zenn_not_found(self) -> None:
        """記事が見つからない場合に 0 を返すこと."""
        from rag.rag_knowledge import RAGKnowledgeService

        mock_vector_store = MagicMock()
        mock_web_crawler = MagicMock()
        mock_zenn_ingester = MagicMock()
        mock_zenn_ingester.fetch_single = AsyncMock(return_value=None)

        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
            zenn_ingester=mock_zenn_ingester,
        )

        result = await service.add_zenn("nonexistent-slug")

        assert result == 0

    async def test_ingest_zenn_without_ingester_raises(self) -> None:
        """ZennIngester 未設定時に RuntimeError が発生すること."""
        from rag.rag_knowledge import RAGKnowledgeService

        mock_vector_store = MagicMock()
        mock_web_crawler = MagicMock()

        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
            # zenn_ingester を設定しない
        )

        with pytest.raises(RuntimeError, match="ZennIngester が設定されていません"):
            await service.ingest_zenn("testuser")

    async def test_add_zenn_without_ingester_raises(self) -> None:
        """ZennIngester 未設定時に RuntimeError が発生すること."""
        from rag.rag_knowledge import RAGKnowledgeService

        mock_vector_store = MagicMock()
        mock_web_crawler = MagicMock()

        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
        )

        with pytest.raises(RuntimeError, match="ZennIngester が設定されていません"):
            await service.add_zenn("test-slug")

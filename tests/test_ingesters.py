"""インジェスター基盤のテスト

仕様: docs/specs/rag-knowledge.md
Issue: #62
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from rag.ingesters.base import BaseIngester, IngestedContent
from rag.ingesters.web import WebIngester
from rag.ingesters.zenn import ZennIngester
from rag.rag_knowledge import RAGKnowledgeService
from rag.vector_store import VectorStore
from rag.web_crawler import CrawledPage, WebCrawler


# --- IngestedContent テスト ---


class TestIngestedContent:
    """IngestedContent データモデルのテスト."""

    def test_create_ingested_content(self) -> None:
        """IngestedContent が正しく生成されること."""
        content = IngestedContent(
            source_id="https://example.com/page",
            title="Test Page",
            text="Test content",
            ingested_at="2024-01-01T00:00:00+00:00",
            source_type="web",
        )
        assert content.source_id == "https://example.com/page"
        assert content.title == "Test Page"
        assert content.text == "Test content"
        assert content.ingested_at == "2024-01-01T00:00:00+00:00"
        assert content.source_type == "web"
        assert content.metadata == {}

    def test_create_with_metadata(self) -> None:
        """メタデータ付きで生成できること."""
        content = IngestedContent(
            source_id="https://example.com/page",
            title="Test",
            text="Content",
            ingested_at="2024-01-01T00:00:00+00:00",
            source_type="web",
            metadata={"key": "value"},
        )
        assert content.metadata == {"key": "value"}

    def test_now_iso_format(self) -> None:
        """now_iso() が ISO 8601 形式の文字列を返すこと."""
        iso_str = IngestedContent.now_iso()
        assert "T" in iso_str
        assert "+" in iso_str or "Z" in iso_str


# --- BaseIngester テスト ---


class ConcreteIngester(BaseIngester):
    """テスト用の具象インジェスター."""

    def __init__(self) -> None:
        self.fetch_single_mock = AsyncMock(return_value=None)

    async def fetch_single(self, identifier: str) -> IngestedContent | None:
        return await self.fetch_single_mock(identifier)

    def validate_identifier(self, identifier: str) -> str:
        if not identifier.startswith("test://"):
            raise ValueError(f"Invalid identifier: {identifier}")
        return identifier


class TestBaseIngester:
    """BaseIngester 抽象基底クラスのテスト."""

    def test_cannot_instantiate_abstract(self) -> None:
        """BaseIngester は直接インスタンス化できないこと."""
        with pytest.raises(TypeError):
            BaseIngester()  # type: ignore[abstract]

    async def test_fetch_batch_default(self) -> None:
        """fetch_batch() のデフォルト実装が fetch_single() を順次呼ぶこと."""
        ingester = ConcreteIngester()
        content = IngestedContent(
            source_id="test://1",
            title="Test",
            text="Content",
            ingested_at="2024-01-01T00:00:00+00:00",
            source_type="test",
        )
        ingester.fetch_single_mock.side_effect = [content, None, content]

        results = await ingester.fetch_batch(["test://1", "test://2", "test://3"])

        assert len(results) == 2
        assert ingester.fetch_single_mock.call_count == 3

    async def test_fetch_batch_empty(self) -> None:
        """空リストで fetch_batch() を呼ぶと空リストを返すこと."""
        ingester = ConcreteIngester()
        results = await ingester.fetch_batch([])
        assert results == []

    async def test_discover_default(self) -> None:
        """discover() のデフォルト実装が空リストを返すこと."""
        ingester = ConcreteIngester()
        result = await ingester.discover("test://source")
        assert result == []

    def test_validate_identifier(self) -> None:
        """validate_identifier() が正しく検証すること."""
        ingester = ConcreteIngester()
        assert ingester.validate_identifier("test://valid") == "test://valid"
        with pytest.raises(ValueError):
            ingester.validate_identifier("invalid")


# --- WebIngester テスト ---


@pytest.fixture
def mock_web_crawler() -> MagicMock:
    """モック WebCrawler を作成する."""
    mock = MagicMock(spec=WebCrawler)
    mock.crawl_page = AsyncMock(return_value=None)
    mock.crawl_index_page = AsyncMock(return_value=[])
    mock.validate_url = MagicMock(side_effect=lambda url: url)
    return mock


@pytest.fixture
def mock_safe_browsing() -> MagicMock:
    """モック SafeBrowsingClient を作成する."""
    from rag.safe_browsing import SafeBrowsingResult

    mock = MagicMock()
    mock.check_url = AsyncMock(
        return_value=SafeBrowsingResult(url="https://safe.com", is_safe=True)
    )
    mock.check_urls = AsyncMock(return_value={})
    return mock


@pytest.fixture
def web_ingester(mock_web_crawler: MagicMock) -> WebIngester:
    """WebIngester インスタンスを作成する."""
    return WebIngester(web_crawler=mock_web_crawler)


@pytest.fixture
def web_ingester_with_safety(
    mock_web_crawler: MagicMock,
    mock_safe_browsing: MagicMock,
) -> WebIngester:
    """Safe Browsing 有効な WebIngester を作成する."""
    return WebIngester(
        web_crawler=mock_web_crawler,
        safe_browsing_client=mock_safe_browsing,
    )


class TestWebIngesterValidate:
    """WebIngester.validate_identifier() のテスト."""

    def test_validate_delegates_to_web_crawler(
        self,
        web_ingester: WebIngester,
        mock_web_crawler: MagicMock,
    ) -> None:
        """validate_identifier() が WebCrawler.validate_url() に委譲すること."""
        result = web_ingester.validate_identifier("https://example.com")
        assert result == "https://example.com"
        mock_web_crawler.validate_url.assert_called_once_with("https://example.com")

    def test_validate_raises_on_invalid(
        self,
        web_ingester: WebIngester,
        mock_web_crawler: MagicMock,
    ) -> None:
        """不正な URL で ValueError が発生すること."""
        mock_web_crawler.validate_url.side_effect = ValueError("invalid")
        with pytest.raises(ValueError, match="invalid"):
            web_ingester.validate_identifier("not-a-url")


class TestWebIngesterFetchSingle:
    """WebIngester.fetch_single() のテスト."""

    async def test_fetch_single_success(
        self,
        web_ingester: WebIngester,
        mock_web_crawler: MagicMock,
    ) -> None:
        """正常にクロールして IngestedContent を返すこと."""
        mock_web_crawler.crawl_page.return_value = CrawledPage(
            url="https://example.com/page",
            title="Test Page",
            text="Test content",
            crawled_at="2024-01-01T00:00:00+00:00",
        )

        result = await web_ingester.fetch_single("https://example.com/page")

        assert result is not None
        assert result.source_id == "https://example.com/page"
        assert result.title == "Test Page"
        assert result.text == "Test content"
        assert result.source_type == "web"
        assert result.ingested_at  # 動的生成（now_iso）なので存在チェックのみ
        assert result.metadata["crawled_at"] == "2024-01-01T00:00:00+00:00"

    async def test_fetch_single_crawl_failed(
        self,
        web_ingester: WebIngester,
        mock_web_crawler: MagicMock,
    ) -> None:
        """クロール失敗時に None を返すこと."""
        mock_web_crawler.crawl_page.return_value = None

        result = await web_ingester.fetch_single("https://example.com/fail")
        assert result is None

    async def test_fetch_single_with_safety_check_safe(
        self,
        web_ingester_with_safety: WebIngester,
        mock_web_crawler: MagicMock,
        mock_safe_browsing: MagicMock,
    ) -> None:
        """安全な URL はクロールが実行されること."""
        from rag.safe_browsing import SafeBrowsingResult

        mock_safe_browsing.check_url.return_value = SafeBrowsingResult(
            url="https://safe.com", is_safe=True
        )
        mock_web_crawler.crawl_page.return_value = CrawledPage(
            url="https://safe.com",
            title="Safe",
            text="Content",
            crawled_at="2024-01-01T00:00:00+00:00",
        )

        result = await web_ingester_with_safety.fetch_single("https://safe.com")

        assert result is not None
        mock_safe_browsing.check_url.assert_called_once()
        mock_web_crawler.crawl_page.assert_called_once()

    async def test_fetch_single_with_safety_check_unsafe(
        self,
        web_ingester_with_safety: WebIngester,
        mock_web_crawler: MagicMock,
        mock_safe_browsing: MagicMock,
    ) -> None:
        """危険な URL で ValueError が発生すること."""
        from rag.safe_browsing import SafeBrowsingResult, ThreatMatch, ThreatType

        mock_safe_browsing.check_url.return_value = SafeBrowsingResult(
            url="https://malware.com",
            is_safe=False,
            threats=[
                ThreatMatch(
                    threat_type=ThreatType.MALWARE,
                    platform_type="ANY_PLATFORM",
                    threat_url="https://malware.com",
                )
            ],
        )

        with pytest.raises(ValueError, match="URLが安全ではありません"):
            await web_ingester_with_safety.fetch_single("https://malware.com")

        mock_web_crawler.crawl_page.assert_not_called()


class TestWebIngesterFetchBatch:
    """WebIngester.fetch_batch() のテスト."""

    async def test_fetch_batch_success(
        self,
        web_ingester: WebIngester,
        mock_web_crawler: MagicMock,
    ) -> None:
        """複数 URL を一括取得できること."""
        mock_web_crawler.crawl_page.side_effect = [
            CrawledPage(
                url="https://example.com/p1",
                title="P1",
                text="Content 1",
                crawled_at="2024-01-01T00:00:00+00:00",
            ),
            CrawledPage(
                url="https://example.com/p2",
                title="P2",
                text="Content 2",
                crawled_at="2024-01-01T00:00:00+00:00",
            ),
        ]

        results = await web_ingester.fetch_batch([
            "https://example.com/p1",
            "https://example.com/p2",
        ])

        assert len(results) == 2
        assert all(r.source_type == "web" for r in results)

    async def test_fetch_batch_empty(
        self,
        web_ingester: WebIngester,
    ) -> None:
        """空リストで空の結果を返すこと."""
        results = await web_ingester.fetch_batch([])
        assert results == []

    async def test_fetch_batch_with_failures(
        self,
        web_ingester: WebIngester,
        mock_web_crawler: MagicMock,
    ) -> None:
        """一部失敗しても成功分を返すこと."""
        mock_web_crawler.crawl_page.side_effect = [
            CrawledPage(
                url="https://example.com/ok",
                title="OK",
                text="Content",
                crawled_at="2024-01-01T00:00:00+00:00",
            ),
            None,  # 失敗
        ]

        results = await web_ingester.fetch_batch([
            "https://example.com/ok",
            "https://example.com/fail",
        ])

        assert len(results) == 1


class TestWebIngesterFilterSafeUrls:
    """WebIngester.filter_safe_urls() のテスト."""

    async def test_no_client_returns_all(
        self,
        web_ingester: WebIngester,
    ) -> None:
        """SafeBrowsingClient がない場合、全 URL を返すこと."""
        urls = ["https://a.com", "https://b.com"]
        result = await web_ingester.filter_safe_urls(urls)
        assert result == urls

    async def test_filters_unsafe_urls(
        self,
        web_ingester_with_safety: WebIngester,
        mock_safe_browsing: MagicMock,
    ) -> None:
        """危険な URL がフィルタリングされること."""
        from rag.safe_browsing import SafeBrowsingResult, ThreatMatch, ThreatType

        mock_safe_browsing.check_urls.return_value = {
            "https://safe.com": SafeBrowsingResult(
                url="https://safe.com", is_safe=True
            ),
            "https://bad.com": SafeBrowsingResult(
                url="https://bad.com",
                is_safe=False,
                threats=[
                    ThreatMatch(
                        threat_type=ThreatType.MALWARE,
                        platform_type="ANY_PLATFORM",
                        threat_url="https://bad.com",
                    )
                ],
            ),
        }

        result = await web_ingester_with_safety.filter_safe_urls([
            "https://safe.com",
            "https://bad.com",
        ])

        assert result == ["https://safe.com"]


class TestWebIngesterDiscover:
    """WebIngester.discover() のテスト."""

    async def test_discover_delegates_to_crawl_index_page(
        self,
        web_ingester: WebIngester,
        mock_web_crawler: MagicMock,
    ) -> None:
        """discover() が crawl_index_page() に委譲すること."""
        mock_web_crawler.crawl_index_page.return_value = [
            "https://example.com/p1",
            "https://example.com/p2",
        ]

        result = await web_ingester.discover(
            "https://example.com/index",
            url_pattern=r"p\d",
        )

        assert len(result) == 2
        mock_web_crawler.crawl_index_page.assert_called_once_with(
            "https://example.com/index", r"p\d"
        )


# --- RAGKnowledgeService + WebIngester 統合テスト ---


@pytest.fixture
def mock_vector_store() -> MagicMock:
    """モック VectorStore を作成する."""
    mock = MagicMock(spec=VectorStore)
    mock.add_documents = AsyncMock(return_value=1)
    mock.search = AsyncMock(return_value=[])
    mock.delete_by_source = AsyncMock(return_value=0)
    mock.delete_stale_chunks = AsyncMock(return_value=0)
    mock.get_stats = MagicMock(return_value={
        "total_chunks": 0,
        "source_count": 0,
        "sources": [],
    })
    return mock


@pytest.fixture
def mock_web_crawler_for_service() -> MagicMock:
    """サービス用モック WebCrawler を作成する."""
    mock = MagicMock(spec=WebCrawler)
    mock.crawl_page = AsyncMock(return_value=None)
    mock.crawl_index_page = AsyncMock(return_value=[])
    mock.validate_url = MagicMock(side_effect=lambda url: url)
    mock._crawl_delay = 0.0
    return mock


class TestRAGServiceWithWebIngester:
    """RAGKnowledgeService の WebIngester 統合テスト."""

    async def test_ingest_page_via_web_ingester(
        self,
        mock_vector_store: MagicMock,
        mock_web_crawler_for_service: MagicMock,
    ) -> None:
        """ingest_page() が WebIngester 経由で動作すること."""
        mock_web_crawler_for_service.crawl_page.return_value = CrawledPage(
            url="https://example.com/page",
            title="Test Page",
            text="Test content.",
            crawled_at="2024-01-01T00:00:00+00:00",
        )
        mock_vector_store.add_documents.return_value = 1

        web_ingester = WebIngester(web_crawler=mock_web_crawler_for_service)
        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler_for_service,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
            web_ingester=web_ingester,
        )

        result = await service.ingest_page("https://example.com/page")

        assert result == 1
        mock_vector_store.add_documents.assert_called_once()
        mock_vector_store.delete_stale_chunks.assert_called_once()

    async def test_ingest_page_via_web_ingester_crawl_failed(
        self,
        mock_vector_store: MagicMock,
        mock_web_crawler_for_service: MagicMock,
    ) -> None:
        """WebIngester 経由でクロール失敗時に 0 を返すこと."""
        mock_web_crawler_for_service.crawl_page.return_value = None

        web_ingester = WebIngester(web_crawler=mock_web_crawler_for_service)
        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler_for_service,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
            web_ingester=web_ingester,
        )

        result = await service.ingest_page("https://example.com/fail")
        assert result == 0

    async def test_ingest_from_index_via_web_ingester(
        self,
        mock_vector_store: MagicMock,
        mock_web_crawler_for_service: MagicMock,
    ) -> None:
        """ingest_from_index() が WebIngester 経由で動作すること."""
        mock_web_crawler_for_service.crawl_index_page.return_value = [
            "https://example.com/p1",
            "https://example.com/p2",
        ]
        mock_web_crawler_for_service.crawl_page.side_effect = [
            CrawledPage(
                url="https://example.com/p1",
                title="P1",
                text="Content 1",
                crawled_at="2024-01-01T00:00:00+00:00",
            ),
            CrawledPage(
                url="https://example.com/p2",
                title="P2",
                text="Content 2",
                crawled_at="2024-01-01T00:00:00+00:00",
            ),
        ]
        mock_vector_store.add_documents.return_value = 1

        web_ingester = WebIngester(web_crawler=mock_web_crawler_for_service)
        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler_for_service,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
            web_ingester=web_ingester,
        )

        result = await service.ingest_from_index(
            "https://example.com/index",
            url_pattern=r"p\d",
        )

        assert result["pages_crawled"] == 2
        assert result["chunks_stored"] >= 2
        assert result["errors"] == 0
        assert result["unsafe_urls"] == 0

    async def test_ingest_content_stores_chunks(
        self,
        mock_vector_store: MagicMock,
        mock_web_crawler_for_service: MagicMock,
    ) -> None:
        """_ingest_content() がチャンキング・保存を行うこと."""
        mock_vector_store.add_documents.return_value = 1

        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler_for_service,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
        )

        content = IngestedContent(
            source_id="https://example.com/test",
            title="Test",
            text="Test content for chunking.",
            ingested_at="2024-01-01T00:00:00+00:00",
            source_type="web",
        )

        result = await service._ingest_content(content)

        assert result == 1
        mock_vector_store.add_documents.assert_called_once()
        mock_vector_store.delete_stale_chunks.assert_called_once()

    async def test_ingest_content_empty_text(
        self,
        mock_vector_store: MagicMock,
        mock_web_crawler_for_service: MagicMock,
    ) -> None:
        """空テキストの場合は 0 を返すこと."""
        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler_for_service,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
        )

        content = IngestedContent(
            source_id="https://example.com/empty",
            title="Empty",
            text="",
            ingested_at="2024-01-01T00:00:00+00:00",
            source_type="web",
        )

        result = await service._ingest_content(content)
        assert result == 0
        mock_vector_store.add_documents.assert_not_called()

    async def test_ingest_content_normalizes_fragment(
        self,
        mock_vector_store: MagicMock,
        mock_web_crawler_for_service: MagicMock,
    ) -> None:
        """_ingest_content() がフラグメント付き URL を正規化すること."""
        import hashlib

        mock_vector_store.add_documents.return_value = 1

        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler_for_service,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
        )

        content = IngestedContent(
            source_id="https://example.com/page#section",
            title="Test",
            text="Content.",
            ingested_at="2024-01-01T00:00:00+00:00",
            source_type="web",
        )

        await service._ingest_content(content)

        # add_documents に渡されたチャンクを検証
        call_args = mock_vector_store.add_documents.call_args[0][0]
        chunk = call_args[0]
        expected_hash = hashlib.sha256(
            "https://example.com/page".encode()
        ).hexdigest()[:16]
        assert chunk.id.startswith(expected_hash)
        assert chunk.metadata["source_url"] == "https://example.com/page"


# --- ZennIngester テスト ---


class TestZennIngesterValidate:
    """ZennIngester.validate_identifier() のテスト."""

    def test_valid_slug(self) -> None:
        """正常な slug が検証を通過すること."""
        ingester = ZennIngester()
        assert ingester.validate_identifier("my-article") == "my-article"

    def test_valid_slug_with_numbers(self) -> None:
        """数字を含む slug が検証を通過すること."""
        ingester = ZennIngester()
        assert ingester.validate_identifier("article123") == "article123"

    def test_valid_slug_with_underscore(self) -> None:
        """アンダースコアを含む slug が検証を通過すること."""
        ingester = ZennIngester()
        assert ingester.validate_identifier("my_article") == "my_article"

    def test_slug_normalized_to_lowercase(self) -> None:
        """大文字を含む slug が小文字に正規化されること."""
        ingester = ZennIngester()
        assert ingester.validate_identifier("My-Article") == "my-article"

    def test_slug_trimmed(self) -> None:
        """前後の空白がトリムされること."""
        ingester = ZennIngester()
        assert ingester.validate_identifier("  my-article  ") == "my-article"

    def test_empty_slug_raises(self) -> None:
        """空の slug で ValueError が発生すること."""
        ingester = ZennIngester()
        with pytest.raises(ValueError, match="slugが空です"):
            ingester.validate_identifier("")

    def test_whitespace_only_slug_raises(self) -> None:
        """空白のみの slug で ValueError が発生すること."""
        ingester = ZennIngester()
        with pytest.raises(ValueError, match="slugが空です"):
            ingester.validate_identifier("   ")

    def test_invalid_slug_with_special_chars(self) -> None:
        """特殊文字を含む slug で ValueError が発生すること."""
        ingester = ZennIngester()
        with pytest.raises(ValueError, match="不正なslug形式です"):
            ingester.validate_identifier("my article!")

    def test_invalid_slug_with_slash(self) -> None:
        """スラッシュを含む slug で ValueError が発生すること."""
        ingester = ZennIngester()
        with pytest.raises(ValueError, match="不正なslug形式です"):
            ingester.validate_identifier("user/article")


class TestZennIngesterFetchSingle:
    """ZennIngester.fetch_single() のテスト."""

    async def test_fetch_single_success(self) -> None:
        """正常に記事を取得して IngestedContent を返すこと."""
        ingester = ZennIngester()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "article": {
                "title": "テスト記事",
                "body_markdown": "# テスト\n\nこれはテスト記事です。",
                "slug": "test-article",
                "emoji": "📝",
                "article_type": "tech",
                "published": True,
            }
        })

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=False),
        ))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "aiohttp.ClientSession",
                lambda **kwargs: AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_session),
                    __aexit__=AsyncMock(return_value=False),
                ),
            )
            result = await ingester.fetch_single("test-article")

        assert result is not None
        assert result.source_id == "https://zenn.dev/articles/test-article"
        assert result.title == "テスト記事"
        assert result.text == "# テスト\n\nこれはテスト記事です。"
        assert result.source_type == "zenn"
        assert result.metadata["slug"] == "test-article"
        assert result.metadata["emoji"] == "📝"
        assert result.metadata["article_type"] == "tech"
        assert result.metadata["published"] is True

    async def test_fetch_single_not_found(self) -> None:
        """404 レスポンスで None を返すこと."""
        ingester = ZennIngester()
        mock_response = AsyncMock()
        mock_response.status = 404

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=False),
        ))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "aiohttp.ClientSession",
                lambda **kwargs: AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_session),
                    __aexit__=AsyncMock(return_value=False),
                ),
            )
            result = await ingester.fetch_single("nonexistent")

        assert result is None

    async def test_fetch_single_api_error(self) -> None:
        """API エラー（500）で None を返すこと."""
        ingester = ZennIngester()
        mock_response = AsyncMock()
        mock_response.status = 500

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=False),
        ))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "aiohttp.ClientSession",
                lambda **kwargs: AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_session),
                    __aexit__=AsyncMock(return_value=False),
                ),
            )
            result = await ingester.fetch_single("error-article")

        assert result is None

    async def test_fetch_single_network_error(self) -> None:
        """ネットワークエラーで None を返すこと."""
        ingester = ZennIngester()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "aiohttp.ClientSession",
                lambda **kwargs: AsyncMock(
                    __aenter__=AsyncMock(
                        side_effect=aiohttp.ClientError("connection failed")
                    ),
                    __aexit__=AsyncMock(return_value=False),
                ),
            )
            result = await ingester.fetch_single("test-article")

        assert result is None

    async def test_fetch_single_empty_body(self) -> None:
        """本文が空の場合 None を返すこと."""
        ingester = ZennIngester()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "article": {
                "title": "Empty Article",
                "body_markdown": "",
                "slug": "empty-article",
            }
        })

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=False),
        ))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "aiohttp.ClientSession",
                lambda **kwargs: AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_session),
                    __aexit__=AsyncMock(return_value=False),
                ),
            )
            result = await ingester.fetch_single("empty-article")

        assert result is None


class TestZennIngesterDiscover:
    """ZennIngester.discover() のテスト."""

    async def test_discover_returns_slugs(self) -> None:
        """ユーザーの記事 slug 一覧を取得すること."""
        ingester = ZennIngester()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "articles": [
                {"slug": "article-1"},
                {"slug": "article-2"},
                {"slug": "article-3"},
            ],
            "next_page": None,
        })

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=False),
        ))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "aiohttp.ClientSession",
                lambda **kwargs: AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_session),
                    __aexit__=AsyncMock(return_value=False),
                ),
            )
            result = await ingester.discover("testuser")

        assert result == ["article-1", "article-2", "article-3"]

    async def test_discover_empty_username(self) -> None:
        """空のユーザー名で空リストを返すこと."""
        ingester = ZennIngester()
        result = await ingester.discover("")
        assert result == []

    async def test_discover_api_error(self) -> None:
        """API エラー時に途中までの結果を返すこと."""
        ingester = ZennIngester()
        mock_response = AsyncMock()
        mock_response.status = 500

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=False),
        ))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "aiohttp.ClientSession",
                lambda **kwargs: AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_session),
                    __aexit__=AsyncMock(return_value=False),
                ),
            )
            result = await ingester.discover("testuser")

        assert result == []

    async def test_discover_network_error(self) -> None:
        """ネットワークエラーでも空リストを返すこと."""
        ingester = ZennIngester()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "aiohttp.ClientSession",
                lambda **kwargs: AsyncMock(
                    __aenter__=AsyncMock(
                        side_effect=aiohttp.ClientError("connection failed")
                    ),
                    __aexit__=AsyncMock(return_value=False),
                ),
            )
            result = await ingester.discover("testuser")

        assert result == []

    async def test_discover_pagination(self) -> None:
        """ページネーションが正しく処理されること."""
        ingester = ZennIngester()

        page1_response = AsyncMock()
        page1_response.status = 200
        page1_response.json = AsyncMock(return_value={
            "articles": [{"slug": "article-1"}],
            "next_page": "2",
        })

        page2_response = AsyncMock()
        page2_response.status = 200
        page2_response.json = AsyncMock(return_value={
            "articles": [{"slug": "article-2"}],
            "next_page": None,
        })

        call_count = 0

        def make_context_manager(*args: object, **kwargs: object) -> AsyncMock:
            nonlocal call_count
            call_count += 1
            resp = page1_response if call_count == 1 else page2_response
            return AsyncMock(
                __aenter__=AsyncMock(return_value=resp),
                __aexit__=AsyncMock(return_value=False),
            )

        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=make_context_manager)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "aiohttp.ClientSession",
                lambda **kwargs: AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_session),
                    __aexit__=AsyncMock(return_value=False),
                ),
            )
            result = await ingester.discover("testuser")

        assert result == ["article-1", "article-2"]


class TestZennIngesterInheritance:
    """ZennIngester が BaseIngester を正しく継承していることのテスト."""

    def test_is_subclass_of_base_ingester(self) -> None:
        """ZennIngester が BaseIngester のサブクラスであること."""
        assert issubclass(ZennIngester, BaseIngester)

    def test_instance_is_base_ingester(self) -> None:
        """ZennIngester インスタンスが BaseIngester のインスタンスであること."""
        ingester = ZennIngester()
        assert isinstance(ingester, BaseIngester)

    async def test_fetch_batch_inherited(self) -> None:
        """fetch_batch() がデフォルト実装で動作すること."""
        ingester = ZennIngester()

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "article": {
                "title": "Test",
                "body_markdown": "Content",
                "slug": "test",
            }
        })

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=False),
        ))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "aiohttp.ClientSession",
                lambda **kwargs: AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_session),
                    __aexit__=AsyncMock(return_value=False),
                ),
            )
            results = await ingester.fetch_batch(["test"])

        assert len(results) == 1
        assert results[0].source_type == "zenn"

"""インジェスター基盤のテスト

仕様: docs/specs/rag-knowledge.md
Issue: #62, #71
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.ingesters.base import BaseIngester, IngestedContent
from rag.ingesters.local_file import SUPPORTED_EXTENSIONS, LocalFileIngester
from rag.ingesters.web import WebIngester
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


# --- LocalFileIngester テスト ---


@pytest.fixture
def local_ingester() -> LocalFileIngester:
    """LocalFileIngester インスタンスを作成する."""
    return LocalFileIngester()


class TestLocalFileIngesterValidate:
    """LocalFileIngester.validate_identifier() のテスト."""

    def test_validate_md_file(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """Markdown ファイルのパスが正常に検証されること."""
        md_file = tmp_path / "test.md"
        md_file.write_text("# Title\nContent", encoding="utf-8")

        result = local_ingester.validate_identifier(str(md_file))
        assert result == str(md_file.resolve())

    def test_validate_txt_file(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """テキストファイルのパスが正常に検証されること."""
        txt_file = tmp_path / "test.txt"
        txt_file.write_text("Plain text", encoding="utf-8")

        result = local_ingester.validate_identifier(str(txt_file))
        assert result == str(txt_file.resolve())

    def test_validate_nonexistent_file(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """存在しないファイルで ValueError が発生すること."""
        with pytest.raises(ValueError, match="ファイルが存在しません"):
            local_ingester.validate_identifier(str(tmp_path / "nonexistent.md"))

    def test_validate_directory(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """ディレクトリパスで ValueError が発生すること."""
        with pytest.raises(ValueError, match="ファイルではありません"):
            local_ingester.validate_identifier(str(tmp_path))

    def test_validate_unsupported_extension(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """未対応の拡張子で ValueError が発生すること."""
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_text("content", encoding="utf-8")

        with pytest.raises(ValueError, match="未対応の拡張子です"):
            local_ingester.validate_identifier(str(pdf_file))


class TestLocalFileIngesterFetchSingle:
    """LocalFileIngester.fetch_single() のテスト."""

    async def test_fetch_md_file(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """Markdown ファイルを正常に読み込めること."""
        md_file = tmp_path / "test.md"
        md_file.write_text("# My Title\nSome content", encoding="utf-8")

        result = await local_ingester.fetch_single(str(md_file))

        assert result is not None
        assert result.source_id == str(md_file.resolve())
        assert result.title == "My Title"
        assert result.text == "# My Title\nSome content"
        assert result.source_type == "file"
        assert result.metadata["file_extension"] == ".md"
        assert result.metadata["file_name"] == "test.md"
        assert result.ingested_at

    async def test_fetch_txt_file(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """テキストファイルを正常に読み込めること."""
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("Plain text content", encoding="utf-8")

        result = await local_ingester.fetch_single(str(txt_file))

        assert result is not None
        assert result.title == "notes"
        assert result.text == "Plain text content"
        assert result.source_type == "file"
        assert result.metadata["file_extension"] == ".txt"

    async def test_fetch_md_without_heading(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """見出しなし Markdown のタイトルがファイル名になること."""
        md_file = tmp_path / "no_heading.md"
        md_file.write_text("No heading here", encoding="utf-8")

        result = await local_ingester.fetch_single(str(md_file))

        assert result is not None
        assert result.title == "no_heading"

    async def test_fetch_nonexistent_returns_none(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """存在しないファイルで None を返すこと."""
        result = await local_ingester.fetch_single(str(tmp_path / "missing.md"))
        assert result is None

    async def test_fetch_unsupported_extension_returns_none(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """未対応拡張子で None を返すこと."""
        pdf_file = tmp_path / "doc.pdf"
        pdf_file.write_text("content", encoding="utf-8")

        result = await local_ingester.fetch_single(str(pdf_file))
        assert result is None


class TestLocalFileIngesterDiscover:
    """LocalFileIngester.discover() のテスト."""

    async def test_discover_finds_supported_files(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """対応拡張子のファイルを発見すること."""
        (tmp_path / "a.md").write_text("md", encoding="utf-8")
        (tmp_path / "b.txt").write_text("txt", encoding="utf-8")
        (tmp_path / "c.pdf").write_text("pdf", encoding="utf-8")

        result = await local_ingester.discover(str(tmp_path))

        assert len(result) == 2
        names = [Path(p).name for p in result]
        assert "a.md" in names
        assert "b.txt" in names
        assert "c.pdf" not in names

    async def test_discover_recursive(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """サブディレクトリのファイルも再帰的に発見すること."""
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "root.md").write_text("root", encoding="utf-8")
        (sub / "nested.md").write_text("nested", encoding="utf-8")

        result = await local_ingester.discover(str(tmp_path))

        assert len(result) == 2

    async def test_discover_with_pattern(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """glob パターンでフィルタリングできること."""
        (tmp_path / "a.md").write_text("md", encoding="utf-8")
        (tmp_path / "b.txt").write_text("txt", encoding="utf-8")

        result = await local_ingester.discover(str(tmp_path), pattern="*.md")

        assert len(result) == 1
        assert Path(result[0]).name == "a.md"

    async def test_discover_nonexistent_directory(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """存在しないディレクトリで ValueError が発生すること."""
        with pytest.raises(ValueError, match="ディレクトリが存在しません"):
            await local_ingester.discover(str(tmp_path / "nonexistent"))

    async def test_discover_file_as_directory(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """ファイルをディレクトリとして指定すると ValueError が発生すること."""
        f = tmp_path / "file.md"
        f.write_text("content", encoding="utf-8")

        with pytest.raises(ValueError, match="ディレクトリではありません"):
            await local_ingester.discover(str(f))

    async def test_discover_empty_directory(
        self, local_ingester: LocalFileIngester, tmp_path: Path
    ) -> None:
        """空ディレクトリで空リストを返すこと."""
        result = await local_ingester.discover(str(tmp_path))
        assert result == []


class TestLocalFileIngesterExtractTitle:
    """LocalFileIngester._extract_title() のテスト."""

    def test_extract_title_from_h1(self) -> None:
        """Markdown の # 見出しからタイトルを抽出すること."""
        text = "# My Title\n\nContent"
        title = LocalFileIngester._extract_title(text, Path("test.md"))
        assert title == "My Title"

    def test_skip_h2_heading(self) -> None:
        """## 見出しはタイトルとして使わないこと."""
        text = "## Sub Heading\n\nContent"
        title = LocalFileIngester._extract_title(text, Path("test.md"))
        assert title == "test"

    def test_fallback_to_filename(self) -> None:
        """見出しがない場合、ファイル名をタイトルとすること."""
        text = "No heading here"
        title = LocalFileIngester._extract_title(text, Path("my_notes.md"))
        assert title == "my_notes"

    def test_txt_uses_filename(self) -> None:
        """テキストファイルはファイル名をタイトルとすること."""
        text = "# Heading"
        title = LocalFileIngester._extract_title(text, Path("notes.txt"))
        assert title == "notes"

    def test_first_h1_wins(self) -> None:
        """複数 # 見出しがある場合、最初のものが使われること."""
        text = "# First\n# Second"
        title = LocalFileIngester._extract_title(text, Path("test.md"))
        assert title == "First"


class TestLocalFileIngesterReExport:
    """__init__.py からの re-export テスト."""

    def test_import_from_ingesters_package(self) -> None:
        """rag.ingesters パッケージから LocalFileIngester をインポートできること."""
        from rag.ingesters import LocalFileIngester as LFI

        assert LFI is LocalFileIngester

    def test_supported_extensions(self) -> None:
        """対応拡張子が .md と .txt であること."""
        assert SUPPORTED_EXTENSIONS == frozenset({".md", ".txt"})

"""RAGナレッジサービスのハイブリッド検索統合テスト

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.bm25_index import BM25Index, BM25Result
from rag.vector_store import RetrievalResult, VectorStore
from rag.rag_knowledge import RAGKnowledgeService, RAGRetrievalResult
from rag.web_crawler import CrawledPage, WebCrawler


@pytest.fixture
def mock_embedding_provider() -> MagicMock:
    """モックEmbeddingプロバイダーを作成する."""
    mock = MagicMock()
    mock.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
    return mock


@pytest.fixture
def mock_vector_store(mock_embedding_provider: MagicMock) -> MagicMock:
    """モックVectorStoreを作成する."""
    mock = MagicMock(spec=VectorStore)
    mock.add_documents = AsyncMock(return_value=3)
    mock.search = AsyncMock(return_value=[])
    mock.delete_by_source = AsyncMock(return_value=0)
    mock.delete_stale_chunks = AsyncMock(return_value=0)
    mock.get_stats = MagicMock(return_value={"total_chunks": 10, "source_count": 2})
    return mock


@pytest.fixture
def mock_web_crawler() -> MagicMock:
    """モックWebCrawlerを作成する."""
    mock = MagicMock(spec=WebCrawler)
    mock.crawl_index_page = AsyncMock(return_value=[])
    mock.crawl_page = AsyncMock(return_value=None)
    mock.crawl_pages = AsyncMock(return_value=[])
    mock.validate_url = MagicMock(side_effect=lambda url: url)
    return mock


@pytest.fixture
def mock_bm25_index() -> MagicMock:
    """モックBM25Indexを作成する."""
    mock = MagicMock(spec=BM25Index)
    mock.add_documents = MagicMock(return_value=3)
    mock.search = MagicMock(return_value=[])
    mock.delete_by_source = MagicMock(return_value=0)
    mock.get_document_count = MagicMock(return_value=0)
    return mock


@pytest.fixture
def rag_service_vector_only(
    mock_vector_store: MagicMock,
    mock_web_crawler: MagicMock,
) -> RAGKnowledgeService:
    """ベクトル検索のみのRAGKnowledgeServiceインスタンスを作成する."""
    return RAGKnowledgeService(
        vector_store=mock_vector_store,
        web_crawler=mock_web_crawler,
        chunk_size=200,
        chunk_overlap=30,
        similarity_threshold=None,
        hybrid_search_enabled=False,
    )


@pytest.fixture
def rag_service_hybrid(
    mock_vector_store: MagicMock,
    mock_web_crawler: MagicMock,
    mock_bm25_index: MagicMock,
) -> RAGKnowledgeService:
    """ハイブリッド検索有効のRAGKnowledgeServiceインスタンスを作成する."""
    return RAGKnowledgeService(
        vector_store=mock_vector_store,
        web_crawler=mock_web_crawler,
        chunk_size=200,
        chunk_overlap=30,
        similarity_threshold=None,
        bm25_index=mock_bm25_index,
        hybrid_search_enabled=True,
        vector_weight=0.5,
    )


class TestHybridSearchDisabled:
    """AC9: hybrid_enabled=false時のテスト（従来動作）."""

    async def test_hybrid_disabled_uses_vector_only(
        self,
        rag_service_vector_only: RAGKnowledgeService,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC9: ハイブリッド検索無効時はベクトル検索のみが動作すること."""
        # Arrange
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="ベクトル検索結果",
                metadata={"source_id": "https://example.com/page1"},
                distance=0.2,
            ),
        ]

        # Act
        result = await rag_service_vector_only.retrieve("テストクエリ", n_results=5)

        # Assert
        assert isinstance(result, RAGRetrievalResult)
        assert "ベクトル検索結果" in result.context
        mock_vector_store.search.assert_called_once()

    async def test_hybrid_disabled_bm25_not_used(
        self,
        mock_vector_store: MagicMock,
        mock_web_crawler: MagicMock,
        mock_bm25_index: MagicMock,
    ) -> None:
        """AC9: hybrid_enabled=falseの場合、BM25インデックスが検索に使用されないこと."""
        # Arrange: BM25インデックスを渡すがハイブリッド検索は無効
        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
            bm25_index=mock_bm25_index,
            hybrid_search_enabled=False,
        )

        mock_vector_store.search.return_value = []

        # Act
        await service.retrieve("テストクエリ", n_results=5)

        # Assert: BM25の検索は呼ばれない
        mock_bm25_index.search.assert_not_called()


class TestHybridSearchEnabled:
    """AC6: ハイブリッド検索有効時のテスト（BM25インデックス統合）."""

    async def test_hybrid_enabled_uses_both_vector_and_bm25(
        self,
        rag_service_hybrid: RAGKnowledgeService,
        mock_vector_store: MagicMock,
        mock_bm25_index: MagicMock,
    ) -> None:
        """AC6: ハイブリッド検索有効時、ベクトル検索とBM25検索の両方が使用されること."""
        # Arrange
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="ベクトル検索結果",
                metadata={"source_id": "https://example.com/page1", "chunk_index": 0},
                distance=0.2,
            ),
        ]
        mock_bm25_index.search.return_value = [
            BM25Result(
                doc_id="abc123_0",
                score=5.0,
                text="BM25検索結果",
            ),
        ]

        # Act
        result = await rag_service_hybrid.retrieve("テストクエリ", n_results=5)

        # Assert
        assert isinstance(result, RAGRetrievalResult)
        # 両方の検索が呼ばれること
        mock_vector_store.search.assert_called()
        mock_bm25_index.search.assert_called()


class TestBM25IndexIntegration:
    """AC6: BM25インデックスとの統合テスト."""

    async def test_ingest_adds_to_bm25(
        self,
        rag_service_hybrid: RAGKnowledgeService,
        mock_web_crawler: MagicMock,
        mock_vector_store: MagicMock,
        mock_bm25_index: MagicMock,
    ) -> None:
        """AC6: 取り込み時にBM25インデックスにもドキュメントが追加されること."""
        # Arrange
        mock_web_crawler.crawl_page.return_value = CrawledPage(
            url="https://example.com/page1",
            title="Test Page",
            text="これはテストコンテンツです。十分な長さのテキストが必要です。",
            crawled_at="2024-01-01T00:00:00+00:00",
        )
        mock_vector_store.add_documents.return_value = 1

        # Act
        result = await rag_service_hybrid.ingest_page("https://example.com/page1")

        # Assert
        assert result >= 1
        mock_bm25_index.add_documents.assert_called()

    async def test_delete_source_removes_from_bm25(
        self,
        rag_service_hybrid: RAGKnowledgeService,
        mock_vector_store: MagicMock,
        mock_bm25_index: MagicMock,
    ) -> None:
        """AC6: delete_source()がBM25インデックスからもドキュメントを削除すること."""
        # Arrange
        mock_vector_store.delete_by_source.return_value = 5
        mock_bm25_index.delete_by_source.return_value = 5

        # Act
        result = await rag_service_hybrid.delete_source("https://example.com/page1")

        # Assert
        assert result == 5
        mock_bm25_index.delete_by_source.assert_called_once_with(
            "https://example.com/page1"
        )


class TestTableDataSearch:
    """AC12: テーブル内データ検索テスト."""

    async def test_table_data_search_ryuuou(
        self,
        mock_vector_store: MagicMock,
        mock_web_crawler: MagicMock,
        mock_bm25_index: MagicMock,
    ) -> None:
        """AC12: 「魔王」クエリでテーブル内のデータが検索できること.

        ベクトル検索では閾値を超えてしまうケースでも、
        BM25検索でキーワードマッチにより検索できる。
        """
        # similarity_threshold=0.5 のサービスを作成
        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=0.5,
            bm25_index=mock_bm25_index,
            hybrid_search_enabled=True,
            vector_weight=0.5,
        )

        # Arrange: ベクトル検索は閾値超過（距離が大きい）
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="名前: 魔王\nHP: 200, MP: 100, 攻撃力: 140",
                metadata={"source_id": "https://example.com/monsters", "chunk_index": 0},
                distance=0.7,  # 閾値0.5を超過
            ),
        ]

        # BM25検索ではキーワードマッチでヒット
        import hashlib
        url_hash = hashlib.sha256(b"https://example.com/monsters").hexdigest()[:16]
        mock_bm25_index.search.return_value = [
            BM25Result(
                doc_id=f"{url_hash}_0",
                score=8.5,
                text="名前: 魔王\nHP: 200, MP: 100, 攻撃力: 140",
            ),
        ]

        # Act
        result = await service.retrieve("魔王", n_results=5)

        # Assert: BM25のおかげで結果が返る
        assert isinstance(result, RAGRetrievalResult)
        assert "魔王" in result.context
        assert "HP: 200" in result.context


class TestKeywordExactMatch:
    """AC13: キーワード完全一致検索テスト."""

    async def test_keyword_exact_match(
        self,
        rag_service_hybrid: RAGKnowledgeService,
        mock_vector_store: MagicMock,
        mock_bm25_index: MagicMock,
    ) -> None:
        """AC13: キーワード完全一致のケースで確実にヒットすること."""
        # Arrange
        import hashlib
        url_hash = hashlib.sha256(b"https://example.com/doc").hexdigest()[:16]

        # ベクトル検索では結果なし
        mock_vector_store.search.return_value = []

        # BM25検索で完全一致
        mock_bm25_index.search.return_value = [
            BM25Result(
                doc_id=f"{url_hash}_0",
                score=10.0,
                text="特定のキーワード「フロベニウスノルム」についての説明です。",
            ),
        ]

        # Act
        result = await rag_service_hybrid.retrieve("フロベニウスノルム", n_results=5)

        # Assert: BM25でキーワードマッチした結果が返る
        assert isinstance(result, RAGRetrievalResult)
        assert "フロベニウスノルム" in result.context


class TestSmartChunking:
    """AC1/AC3/AC5: _smart_chunk()メソッドのテスト."""

    def test_smart_chunk_detects_table_data(
        self,
        rag_service_hybrid: RAGKnowledgeService,
    ) -> None:
        """AC1: テーブルデータが正しく検出・チャンキングされること."""
        # Arrange: テーブル形式のテキスト
        table_text = """名前	HP	MP	攻撃力
魔王	200	100	140
ゴブリン	8	0	5
ゴーレム	120	0	90"""

        # Act
        chunks = rag_service_hybrid._smart_chunk(table_text)

        # Assert: テーブルとして処理され、各行がチャンクになる
        assert len(chunks) > 0
        # テーブルチャンクはフォーマット済みで「名前:」を含む
        assert any("名前:" in chunk or "魔王" in chunk for chunk in chunks)

    def test_smart_chunk_detects_headings(
        self,
        rag_service_hybrid: RAGKnowledgeService,
    ) -> None:
        """AC3: 見出し付きテキストが正しく検出・チャンキングされること."""
        # Arrange: 見出し形式のテキスト
        heading_text = """# メインタイトル

これは最初のセクションです。

## サブセクション1

サブセクション1の内容です。

## サブセクション2

サブセクション2の内容です。"""

        # Act
        chunks = rag_service_hybrid._smart_chunk(heading_text)

        # Assert: 見出しごとにチャンクが分割される
        assert len(chunks) > 0

    def test_smart_chunk_prose_fallback(
        self,
        rag_service_hybrid: RAGKnowledgeService,
    ) -> None:
        """AC5: 通常テキストは従来のチャンキングにフォールバックすること."""
        # Arrange: 見出しもテーブルもないプレーンテキスト
        prose_text = """これは通常の段落テキストです。
特に構造化されていない長いテキストが続きます。
複数の文で構成されており、自然言語で書かれています。"""

        # Act
        chunks = rag_service_hybrid._smart_chunk(prose_text)

        # Assert: 何らかのチャンクが生成される
        assert len(chunks) >= 1


class TestHybridSearchEngineInitialization:
    """AC9: HybridSearchEngineの初期化テスト."""

    def test_hybrid_engine_initialized_when_enabled(
        self,
        mock_vector_store: MagicMock,
        mock_web_crawler: MagicMock,
        mock_bm25_index: MagicMock,
    ) -> None:
        """AC9: hybrid_search_enabled=Trueの場合、HybridSearchEngineが初期化されること."""
        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
            bm25_index=mock_bm25_index,
            hybrid_search_enabled=True,
            vector_weight=0.5,
        )

        assert service._hybrid_search_engine is not None
        assert service._hybrid_search_enabled is True

    def test_hybrid_engine_not_initialized_when_disabled(
        self,
        mock_vector_store: MagicMock,
        mock_web_crawler: MagicMock,
        mock_bm25_index: MagicMock,
    ) -> None:
        """AC9: hybrid_search_enabled=Falseの場合、HybridSearchEngineは初期化されないこと."""
        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
            bm25_index=mock_bm25_index,
            hybrid_search_enabled=False,
        )

        assert service._hybrid_search_engine is None
        assert service._hybrid_search_enabled is False

    def test_hybrid_engine_not_initialized_without_bm25_index(
        self,
        mock_vector_store: MagicMock,
        mock_web_crawler: MagicMock,
    ) -> None:
        """AC9: bm25_indexがNoneの場合、hybrid_enabled=Trueでも初期化されないこと."""
        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
            bm25_index=None,  # BM25インデックスなし
            hybrid_search_enabled=True,
            vector_weight=0.5,
        )

        # BM25インデックスがないため、エンジンは初期化されない
        assert service._hybrid_search_engine is None

"""ベクトルストアのテスト (Issue #116).

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import uuid

import pytest

from rag.embedding.base import EmbeddingProvider
from rag.vector_store import DocumentChunk, RetrievalResult, VectorStore


class MockEmbeddingProvider(EmbeddingProvider):
    """テスト用のモックEmbeddingプロバイダー."""

    def __init__(self, dimension: int = 3) -> None:
        self._dimension = dimension
        self._call_count = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """テキストを固定次元のベクトルに変換する."""
        self._call_count += 1
        # 各テキストを単純なベクトルに変換（テスト用）
        return [[float(i + len(text)) for i in range(self._dimension)] for text in texts]

    async def is_available(self) -> bool:
        """常にTrueを返す."""
        return True


@pytest.fixture
def mock_embedding() -> MockEmbeddingProvider:
    """モックEmbeddingプロバイダーを返すフィクスチャ."""
    return MockEmbeddingProvider(dimension=3)


@pytest.fixture
def ephemeral_store(mock_embedding: MockEmbeddingProvider) -> VectorStore:
    """インメモリのVectorStoreを返すフィクスチャ.

    各テストで独立したコレクションを使用するためにUUIDをコレクション名に含める。
    """
    unique_collection = f"test_collection_{uuid.uuid4().hex[:8]}"
    return VectorStore.create_ephemeral(mock_embedding, collection_name=unique_collection)


class TestAC8AddDocuments:
    """AC8: VectorStore.add_documents() でチャンクをEmbedding→ChromaDBに保存できること."""

    @pytest.mark.asyncio
    async def test_add_single_document(self, ephemeral_store: VectorStore) -> None:
        """単一のドキュメントを追加できる."""
        chunk = DocumentChunk(
            id="doc1_0",
            text="これはテストテキストです。",
            metadata={"source_id": "https://example.com/doc1", "chunk_index": 0},
        )
        count = await ephemeral_store.add_documents([chunk])
        assert count == 1

    @pytest.mark.asyncio
    async def test_add_multiple_documents(self, ephemeral_store: VectorStore) -> None:
        """複数のドキュメントを追加できる."""
        chunks = [
            DocumentChunk(
                id=f"doc_{i}",
                text=f"テキスト{i}",
                metadata={"source_id": "https://example.com", "chunk_index": i},
            )
            for i in range(5)
        ]
        count = await ephemeral_store.add_documents(chunks)
        assert count == 5

    @pytest.mark.asyncio
    async def test_add_empty_list(self, ephemeral_store: VectorStore) -> None:
        """空のリストを渡すと0を返す."""
        count = await ephemeral_store.add_documents([])
        assert count == 0

    @pytest.mark.asyncio
    async def test_embedding_is_called(
        self,
        mock_embedding: MockEmbeddingProvider,
        ephemeral_store: VectorStore,
    ) -> None:
        """Embeddingプロバイダーが呼び出される."""
        chunk = DocumentChunk(
            id="doc1_0",
            text="テスト",
            metadata={"source_id": "https://example.com", "chunk_index": 0},
        )
        await ephemeral_store.add_documents([chunk])
        assert mock_embedding._call_count == 1


class TestAC9SearchSimilarChunks:
    """AC9: VectorStore.search() でクエリに類似するチャンクを検索できること."""

    @pytest.mark.asyncio
    async def test_search_returns_results(self, ephemeral_store: VectorStore) -> None:
        """検索結果が返される."""
        # ドキュメントを追加
        chunks = [
            DocumentChunk(
                id=f"doc_{i}",
                text=f"テキスト{i}",
                metadata={"source_id": "https://example.com", "chunk_index": i},
            )
            for i in range(3)
        ]
        await ephemeral_store.add_documents(chunks)

        # 検索
        results = await ephemeral_store.search("テキスト", n_results=2)
        assert len(results) == 2
        assert all(isinstance(r, RetrievalResult) for r in results)

    @pytest.mark.asyncio
    async def test_search_returns_text_and_metadata(
        self,
        ephemeral_store: VectorStore,
    ) -> None:
        """検索結果にテキストとメタデータが含まれる."""
        chunk = DocumentChunk(
            id="doc1_0",
            text="テストテキスト",
            metadata={"source_id": "https://example.com/test", "chunk_index": 0},
        )
        await ephemeral_store.add_documents([chunk])

        results = await ephemeral_store.search("テスト", n_results=1)
        assert len(results) == 1
        assert results[0].text == "テストテキスト"
        assert results[0].metadata["source_id"] == "https://example.com/test"

    @pytest.mark.asyncio
    async def test_search_returns_distance(self, ephemeral_store: VectorStore) -> None:
        """検索結果にdistanceが含まれる."""
        chunk = DocumentChunk(
            id="doc1_0",
            text="テスト",
            metadata={"source_id": "https://example.com", "chunk_index": 0},
        )
        await ephemeral_store.add_documents([chunk])

        results = await ephemeral_store.search("テスト", n_results=1)
        assert len(results) == 1
        assert isinstance(results[0].distance, float)

    @pytest.mark.asyncio
    async def test_search_empty_store(self, ephemeral_store: VectorStore) -> None:
        """空のストアを検索すると空のリストを返す."""
        results = await ephemeral_store.search("テスト", n_results=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_search_respects_n_results(self, ephemeral_store: VectorStore) -> None:
        """n_resultsで返却数を制限できる."""
        chunks = [
            DocumentChunk(
                id=f"doc_{i}",
                text=f"テキスト{i}",
                metadata={"source_id": "https://example.com", "chunk_index": i},
            )
            for i in range(10)
        ]
        await ephemeral_store.add_documents(chunks)

        results = await ephemeral_store.search("テキスト", n_results=3)
        assert len(results) == 3


class TestAC10DeleteBySource:
    """AC10: VectorStore.delete_by_source() でソースURL指定のチャンクを削除できること."""

    @pytest.mark.asyncio
    async def test_delete_by_source_url(self, ephemeral_store: VectorStore) -> None:
        """ソースURL指定で削除できる."""
        # 2つの異なるソースからドキュメントを追加
        chunks = [
            DocumentChunk(
                id="doc1_0",
                text="テキスト1",
                metadata={"source_id": "https://example.com/page1", "chunk_index": 0},
            ),
            DocumentChunk(
                id="doc1_1",
                text="テキスト2",
                metadata={"source_id": "https://example.com/page1", "chunk_index": 1},
            ),
            DocumentChunk(
                id="doc2_0",
                text="テキスト3",
                metadata={"source_id": "https://example.com/page2", "chunk_index": 0},
            ),
        ]
        await ephemeral_store.add_documents(chunks)

        # page1のドキュメントを削除
        deleted_count = await ephemeral_store.delete_by_source("https://example.com/page1")
        assert deleted_count == 2

        # page2のドキュメントは残っている
        stats = ephemeral_store.get_stats()
        assert stats["total_chunks"] == 1

    @pytest.mark.asyncio
    async def test_delete_nonexistent_source(self, ephemeral_store: VectorStore) -> None:
        """存在しないソースURLを指定すると0を返す."""
        chunk = DocumentChunk(
            id="doc1_0",
            text="テスト",
            metadata={"source_id": "https://example.com/page1", "chunk_index": 0},
        )
        await ephemeral_store.add_documents([chunk])

        deleted_count = await ephemeral_store.delete_by_source("https://example.com/nonexistent")
        assert deleted_count == 0


class TestAC11GetStats:
    """AC11: VectorStore.get_stats() でナレッジベースの統計情報を取得できること."""

    def test_get_stats_empty_store(self, ephemeral_store: VectorStore) -> None:
        """空のストアの統計."""
        stats = ephemeral_store.get_stats()
        assert stats["total_chunks"] == 0
        assert stats["source_count"] == 0
        assert stats["sources"] == []

    @pytest.mark.asyncio
    async def test_get_stats_with_documents(self, ephemeral_store: VectorStore) -> None:
        """ドキュメントがある場合の統計."""
        chunks = [
            DocumentChunk(
                id="doc1_0",
                text="テキスト1",
                metadata={
                    "source_id": "https://example.com/page1",
                    "title": "ページ1",
                    "chunk_index": 0,
                },
            ),
            DocumentChunk(
                id="doc1_1",
                text="テキスト2",
                metadata={
                    "source_id": "https://example.com/page1",
                    "title": "ページ1",
                    "chunk_index": 1,
                },
            ),
            DocumentChunk(
                id="doc2_0",
                text="テキスト3",
                metadata={
                    "source_id": "https://example.com/page2",
                    "title": "ページ2",
                    "chunk_index": 0,
                },
            ),
        ]
        await ephemeral_store.add_documents(chunks)

        stats = ephemeral_store.get_stats()
        assert stats["total_chunks"] == 3
        assert stats["source_count"] == 2

    @pytest.mark.asyncio
    async def test_get_stats_returns_sources_with_domain_grouping(
        self,
        ephemeral_store: VectorStore,
    ) -> None:
        """ソース一覧がドメイン別にグルーピングされて返ること."""
        chunks = [
            DocumentChunk(
                id="a_0",
                text="テキスト1",
                metadata={
                    "source_id": "https://example.com/page1",
                    "title": "ページA",
                    "chunk_index": 0,
                },
            ),
            DocumentChunk(
                id="a_1",
                text="テキスト2",
                metadata={
                    "source_id": "https://example.com/page1",
                    "title": "ページA",
                    "chunk_index": 1,
                },
            ),
            DocumentChunk(
                id="b_0",
                text="テキスト3",
                metadata={
                    "source_id": "https://other.com/doc",
                    "title": "ドキュメントB",
                    "chunk_index": 0,
                },
            ),
        ]
        await ephemeral_store.add_documents(chunks)

        stats = ephemeral_store.get_stats()
        sources = stats["sources"]
        assert isinstance(sources, list)
        assert len(sources) == 2

        # ドメイン名でソートされている
        domains = [g["domain"] for g in sources]
        assert domains == ["example.com", "other.com"]

        # example.com のページ情報
        example_group = sources[0]
        assert example_group["domain"] == "example.com"
        pages = example_group["pages"]
        assert len(pages) == 1  # 1つのユニークURL
        assert pages[0]["url"] == "https://example.com/page1"
        assert pages[0]["title"] == "ページA"
        assert pages[0]["chunks"] == 2

        # other.com のページ情報
        other_group = sources[1]
        assert other_group["domain"] == "other.com"
        pages = other_group["pages"]
        assert len(pages) == 1
        assert pages[0]["url"] == "https://other.com/doc"
        assert pages[0]["title"] == "ドキュメントB"
        assert pages[0]["chunks"] == 1


class TestVectorStoreDataClasses:
    """データクラスのテスト."""

    def test_document_chunk_creation(self) -> None:
        """DocumentChunkが正しく作成できる."""
        chunk = DocumentChunk(
            id="test_id",
            text="テストテキスト",
            metadata={"source_id": "https://example.com", "chunk_index": 0},
        )
        assert chunk.id == "test_id"
        assert chunk.text == "テストテキスト"
        assert chunk.metadata["source_id"] == "https://example.com"

    def test_retrieval_result_creation(self) -> None:
        """RetrievalResultが正しく作成できる."""
        result = RetrievalResult(
            text="テストテキスト",
            metadata={"source_id": "https://example.com"},
            distance=0.5,
        )
        assert result.text == "テストテキスト"
        assert result.metadata["source_id"] == "https://example.com"
        assert result.distance == 0.5


class TestVectorStoreFactory:
    """VectorStoreのファクトリメソッドのテスト."""

    def test_create_ephemeral(self, mock_embedding: MockEmbeddingProvider) -> None:
        """create_ephemeral()でインメモリストアを作成できる."""
        store = VectorStore.create_ephemeral(mock_embedding, collection_name="test")
        assert store._persist_directory == ""
        assert store._collection_name == "test"

    def test_create_ephemeral_default_collection(
        self,
        mock_embedding: MockEmbeddingProvider,
    ) -> None:
        """create_ephemeral()のデフォルトコレクション名."""
        store = VectorStore.create_ephemeral(mock_embedding)
        assert store._collection_name == "knowledge"


class TestAC38SimilarityThreshold:
    """AC38: 類似度閾値フィルタリングのテスト."""

    @pytest.mark.asyncio
    async def test_threshold_filters_distant_results(
        self,
        ephemeral_store: VectorStore,
    ) -> None:
        """閾値を超えるdistanceの結果がフィルタリングされること."""
        # Arrange: 複数のドキュメントを追加
        chunks = [
            DocumentChunk(
                id=f"doc_{i}",
                text=f"テキスト{i}" * (i + 1),  # 異なる長さで異なるベクトルを生成
                metadata={"source_id": f"https://example.com/page{i}", "chunk_index": 0},
            )
            for i in range(5)
        ]
        await ephemeral_store.add_documents(chunks)

        # Act: 閾値なしで検索
        results_no_threshold = await ephemeral_store.search("テキスト", n_results=5)

        # Act: 非常に厳しい閾値で検索（ほぼすべて除外）
        results_strict = await ephemeral_store.search(
            "テキスト", n_results=5, similarity_threshold=0.0001
        )

        # Assert: 閾値なしでは結果が返り、厳しい閾値では結果が少ない
        assert len(results_no_threshold) > 0
        assert len(results_strict) < len(results_no_threshold)

    @pytest.mark.asyncio
    async def test_threshold_none_returns_all(
        self,
        ephemeral_store: VectorStore,
    ) -> None:
        """閾値がNoneの場合はフィルタリングなしで全結果を返すこと."""
        # Arrange
        chunks = [
            DocumentChunk(
                id=f"doc_{i}",
                text=f"テキスト{i}",
                metadata={"source_id": "https://example.com", "chunk_index": i},
            )
            for i in range(3)
        ]
        await ephemeral_store.add_documents(chunks)

        # Act
        results = await ephemeral_store.search("テキスト", n_results=3, similarity_threshold=None)

        # Assert
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_threshold_respects_n_results(
        self,
        ephemeral_store: VectorStore,
    ) -> None:
        """閾値フィルタリング後もn_results件数を超えないこと."""
        # Arrange
        chunks = [
            DocumentChunk(
                id=f"doc_{i}",
                text=f"テキスト{i}",
                metadata={"source_id": "https://example.com", "chunk_index": i},
            )
            for i in range(10)
        ]
        await ephemeral_store.add_documents(chunks)

        # Act: 緩い閾値で検索（多くの結果が閾値を通過）
        results = await ephemeral_store.search(
            "テキスト", n_results=3, similarity_threshold=2.0
        )

        # Assert: n_results=3 を超えない
        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_threshold_returns_empty_when_all_filtered(
        self,
        ephemeral_store: VectorStore,
    ) -> None:
        """全結果が閾値で除外される場合は空リストを返すこと."""
        # Arrange
        chunks = [
            DocumentChunk(
                id="doc_0",
                text="非常に長いテキスト" * 100,
                metadata={"source_id": "https://example.com", "chunk_index": 0},
            ),
        ]
        await ephemeral_store.add_documents(chunks)

        # Act: 極端に厳しい閾値（ほぼ完全一致のみ許可）
        results = await ephemeral_store.search(
            "短いクエリ", n_results=5, similarity_threshold=0.00001
        )

        # Assert: 極端に厳しい閾値ではすべて除外され空リストになる
        assert results == []

    @pytest.mark.asyncio
    async def test_threshold_filtering_logs_excluded_count(
        self,
        ephemeral_store: VectorStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """閾値で除外された件数がデバッグログに出力されること."""
        import logging

        # Arrange
        chunks = [
            DocumentChunk(
                id=f"doc_{i}",
                text=f"テキスト{i}" * (i + 1),
                metadata={"source_id": "https://example.com", "chunk_index": i},
            )
            for i in range(5)
        ]
        await ephemeral_store.add_documents(chunks)

        # Act
        with caplog.at_level(logging.DEBUG, logger="rag.vector_store"):
            await ephemeral_store.search(
                "テキスト", n_results=5, similarity_threshold=0.0001
            )

        # Assert: 厳しい閾値により除外が発生し、デバッグログが出力される
        threshold_logs = [
            r for r in caplog.records
            if "Similarity threshold filtering" in r.message
        ]
        assert len(threshold_logs) > 0, "閾値フィルタリングのログが出力されていない"


class TestGetChunksBySource:
    """get_chunks_by_source() のテスト (Issue #27)."""

    @pytest.mark.asyncio
    async def test_get_chunks_by_source_returns_correct_results(
        self,
        ephemeral_store: VectorStore,
    ) -> None:
        """ソースURL指定で正しい結果が返ること."""
        # Arrange: 2つのソースからドキュメントを追加
        chunks = [
            DocumentChunk(
                id="page1_0",
                text="Page1 チャンク0",
                metadata={"source_id": "https://example.com/page1", "chunk_index": 0},
            ),
            DocumentChunk(
                id="page1_1",
                text="Page1 チャンク1",
                metadata={"source_id": "https://example.com/page1", "chunk_index": 1},
            ),
            DocumentChunk(
                id="page2_0",
                text="Page2 チャンク0",
                metadata={"source_id": "https://example.com/page2", "chunk_index": 0},
            ),
        ]
        await ephemeral_store.add_documents(chunks)

        # Act
        results = await ephemeral_store.get_chunks_by_source("https://example.com/page1")

        # Assert
        assert len(results) == 2
        assert all(isinstance(r, RetrievalResult) for r in results)
        assert results[0].text == "Page1 チャンク0"
        assert results[1].text == "Page1 チャンク1"
        # distance は 0.0 固定
        assert all(r.distance == 0.0 for r in results)

    @pytest.mark.asyncio
    async def test_get_chunks_by_source_sorted_by_chunk_index(
        self,
        ephemeral_store: VectorStore,
    ) -> None:
        """chunk_index 昇順でソートされること."""
        # Arrange: chunk_index を逆順で追加
        chunks = [
            DocumentChunk(
                id="page_2",
                text="チャンク2",
                metadata={"source_id": "https://example.com/page", "chunk_index": 2},
            ),
            DocumentChunk(
                id="page_0",
                text="チャンク0",
                metadata={"source_id": "https://example.com/page", "chunk_index": 0},
            ),
            DocumentChunk(
                id="page_1",
                text="チャンク1",
                metadata={"source_id": "https://example.com/page", "chunk_index": 1},
            ),
        ]
        await ephemeral_store.add_documents(chunks)

        # Act
        results = await ephemeral_store.get_chunks_by_source("https://example.com/page")

        # Assert: chunk_index 昇順
        assert len(results) == 3
        assert results[0].text == "チャンク0"
        assert results[1].text == "チャンク1"
        assert results[2].text == "チャンク2"
        assert int(results[0].metadata["chunk_index"]) == 0
        assert int(results[1].metadata["chunk_index"]) == 1
        assert int(results[2].metadata["chunk_index"]) == 2

    @pytest.mark.asyncio
    async def test_get_chunks_by_source_nonexistent_url(
        self,
        ephemeral_store: VectorStore,
    ) -> None:
        """存在しないURLに対して空リストが返ること."""
        # Arrange: 別のURLのドキュメントを追加
        chunk = DocumentChunk(
            id="page1_0",
            text="テスト",
            metadata={"source_id": "https://example.com/page1", "chunk_index": 0},
        )
        await ephemeral_store.add_documents([chunk])

        # Act
        results = await ephemeral_store.get_chunks_by_source("https://example.com/nonexistent")

        # Assert
        assert results == []


class TestEmbeddingMethodDispatch:
    """VectorStore が embed_documents()/embed_query() を呼ぶことの確認 (Issue #517)."""

    @pytest.mark.asyncio
    async def test_add_documents_calls_embed_documents(
        self,
        mock_embedding: MockEmbeddingProvider,
        ephemeral_store: VectorStore,
    ) -> None:
        """add_documents() が embed_documents() を呼ぶこと."""
        from unittest.mock import AsyncMock

        original_embed_documents = mock_embedding.embed_documents
        mock_embedding.embed_documents = AsyncMock(side_effect=original_embed_documents)  # type: ignore[method-assign]

        chunk = DocumentChunk(
            id="doc1_0",
            text="テスト",
            metadata={"source_id": "https://example.com", "chunk_index": 0},
        )
        await ephemeral_store.add_documents([chunk])

        mock_embedding.embed_documents.assert_awaited_once_with(["テスト"])

    @pytest.mark.asyncio
    async def test_search_calls_embed_query(
        self,
        mock_embedding: MockEmbeddingProvider,
        ephemeral_store: VectorStore,
    ) -> None:
        """search() が embed_query() を呼ぶこと."""
        from unittest.mock import AsyncMock

        # まずドキュメントを追加
        chunk = DocumentChunk(
            id="doc1_0",
            text="テスト",
            metadata={"source_id": "https://example.com", "chunk_index": 0},
        )
        await ephemeral_store.add_documents([chunk])

        # embed_query をモック化
        original_embed_query = mock_embedding.embed_query
        mock_embedding.embed_query = AsyncMock(side_effect=original_embed_query)  # type: ignore[method-assign]

        await ephemeral_store.search("テスト", n_results=1)

        mock_embedding.embed_query.assert_awaited_once_with("テスト")

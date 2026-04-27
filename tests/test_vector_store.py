"""ベクトルストアのテスト (Issue #116).

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import uuid

import pytest

from rag.embedding.base import EmbeddingProvider
from rag.vector_store import DocumentChunk, RetrievalResult, VectorStore

from factories import make_vector_store, make_vector_store_ephemeral, make_vector_store_http


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
    return make_vector_store_ephemeral(mock_embedding, collection_name=unique_collection)


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


class TestDeleteBySourceType:
    """VectorStore.delete_by_source_type() で source_type 指定の一括削除ができること (#544)."""

    @pytest.mark.asyncio
    async def test_delete_by_source_type(self, ephemeral_store: VectorStore) -> None:
        """source_type 指定で該当チャンクを一括削除できる."""
        chunks = [
            DocumentChunk(
                id="web_0",
                text="Web text 1",
                metadata={"source_id": "src_web", "source_type": "web", "chunk_index": 0},
            ),
            DocumentChunk(
                id="web_1",
                text="Web text 2",
                metadata={"source_id": "src_web", "source_type": "web", "chunk_index": 1},
            ),
            DocumentChunk(
                id="zenn_0",
                text="Zenn text 1",
                metadata={"source_id": "src_zenn", "source_type": "zenn", "chunk_index": 0},
            ),
        ]
        await ephemeral_store.add_documents(chunks)

        deleted_count = await ephemeral_store.delete_by_source_type("web")
        assert deleted_count == 2

        stats = ephemeral_store.get_stats()
        assert stats["total_chunks"] == 1

    @pytest.mark.asyncio
    async def test_delete_by_source_type_nonexistent(self, ephemeral_store: VectorStore) -> None:
        """存在しない source_type を指定すると 0 を返す."""
        chunk = DocumentChunk(
            id="web_0",
            text="Web text",
            metadata={"source_id": "src_web", "source_type": "web", "chunk_index": 0},
        )
        await ephemeral_store.add_documents([chunk])

        deleted_count = await ephemeral_store.delete_by_source_type("bluesky")
        assert deleted_count == 0

        stats = ephemeral_store.get_stats()
        assert stats["total_chunks"] == 1


class TestDeleteBySourceIdPrefix:
    """VectorStore.delete_by_source_id_prefix() の path prefix 一括削除 (#678)."""

    @pytest.mark.asyncio
    async def test_delete_by_source_id_prefix(self, ephemeral_store: VectorStore) -> None:
        """指定パス配下（再帰的）のチャンクを一括削除できる."""
        chunks = [
            DocumentChunk(
                id="c1",
                text="intro",
                metadata={
                    "source_id": "local/unity-docs/intro.md",
                    "source_type": "local",
                    "chunk_index": 0,
                },
            ),
            DocumentChunk(
                id="c2",
                text="api",
                metadata={
                    "source_id": "local/unity-docs/sub/api.md",
                    "source_type": "local",
                    "chunk_index": 0,
                },
            ),
            DocumentChunk(
                id="c3",
                text="other",
                metadata={
                    "source_id": "local/other/x.md",
                    "source_type": "local",
                    "chunk_index": 0,
                },
            ),
        ]
        await ephemeral_store.add_documents(chunks)

        deleted = await ephemeral_store.delete_by_source_id_prefix(
            "local/unity-docs",
        )
        assert deleted == 2
        assert ephemeral_store.get_stats()["total_chunks"] == 1

    @pytest.mark.asyncio
    async def test_delete_by_source_id_prefix_paginated(
        self, ephemeral_store: VectorStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ChromaDB の limit/offset で分割取得しながら削除できる (#694 review)."""
        # 小さい page size で複数ページに分かれるデータを構築
        monkeypatch.setattr(type(ephemeral_store), "_BATCH_SIZE", 2)

        chunks = [
            DocumentChunk(
                id=f"c{i}",
                text=f"chunk {i}",
                metadata={
                    "source_id": f"local/target/{i}.md",
                    "source_type": "local",
                    "chunk_index": 0,
                },
            )
            for i in range(5)
        ]
        await ephemeral_store.add_documents(chunks)

        deleted = await ephemeral_store.delete_by_source_id_prefix(
            "local/target",
        )
        assert deleted == 5
        assert ephemeral_store.get_stats()["total_chunks"] == 0

    @pytest.mark.asyncio
    async def test_delete_by_source_id_prefix_excludes_sibling(
        self, ephemeral_store: VectorStore,
    ) -> None:
        """`local/foo` 指定は `local/foobar/...` にはマッチしない (lex range の境界)."""
        chunks = [
            DocumentChunk(
                id="c1",
                text="in foo",
                metadata={
                    "source_id": "local/foo/a.md",
                    "source_type": "local",
                    "chunk_index": 0,
                },
            ),
            DocumentChunk(
                id="c2",
                text="in foobar",
                metadata={
                    "source_id": "local/foobar/a.md",
                    "source_type": "local",
                    "chunk_index": 0,
                },
            ),
        ]
        await ephemeral_store.add_documents(chunks)

        deleted = await ephemeral_store.delete_by_source_id_prefix("local/foo")
        assert deleted == 1
        assert ephemeral_store.get_stats()["total_chunks"] == 1


class TestAC11GetStats:
    """AC11: VectorStore.get_stats() で総チャンク数を取得できること."""

    def test_get_stats_empty_store(self, ephemeral_store: VectorStore) -> None:
        """空のストアの統計."""
        stats = ephemeral_store.get_stats()
        assert stats["total_chunks"] == 0
        assert "source_count" not in stats
        assert "sources" not in stats

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
        store = make_vector_store_ephemeral(mock_embedding, collection_name="test")
        assert store._persist_directory == ""
        assert store._collection_name == "test"

    def test_create_ephemeral_with_collection_name(
        self,
        mock_embedding: MockEmbeddingProvider,
    ) -> None:
        """create_ephemeral() で指定したコレクション名が設定されること."""
        store = make_vector_store_ephemeral(mock_embedding, collection_name="knowledge")
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

        # Act — server.py が rag ロガーの propagate を False にするため明示的に復元
        rag_logger = logging.getLogger("rag")
        original_propagate = rag_logger.propagate
        rag_logger.propagate = True
        try:
            with caplog.at_level(logging.DEBUG, logger="rag.vector_store"):
                await ephemeral_store.search(
                    "テキスト", n_results=5, similarity_threshold=0.0001
                )
        finally:
            rag_logger.propagate = original_propagate

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


class TestClose:
    """VectorStore.close() の回帰テスト (#335)."""

    def test_close_clears_shared_system_cache(
        self,
        mock_embedding: MockEmbeddingProvider,
        tmp_path: object,
    ) -> None:
        """close() で SharedSystemClient キャッシュがクリアされること."""
        from chromadb.api.shared_system_client import SharedSystemClient

        persist_dir = str(tmp_path)
        store = make_vector_store(
            embedding_provider=mock_embedding,
            persist_directory=persist_dir,
        )

        # PersistentClient 作成後、キャッシュにエントリが存在する
        systems = getattr(SharedSystemClient, "_identifier_to_system", {})
        assert persist_dir in systems

        # close() でキャッシュがクリアされる
        store.close()
        systems_after = getattr(SharedSystemClient, "_identifier_to_system", {})
        assert persist_dir not in systems_after

    def test_close_ephemeral_is_noop(
        self,
        ephemeral_store: VectorStore,
    ) -> None:
        """EphemeralClient の close() はエラーにならないこと."""
        # ephemeral は persist_directory が空文字なので何もしない
        ephemeral_store.close()  # 例外が出なければOK


class TestCreateHttp:
    """VectorStore.create_http() のテスト (#406)."""

    def test_create_http_sets_attributes(
        self,
        mock_embedding: MockEmbeddingProvider,
    ) -> None:
        """create_http() で属性が正しく設定されること."""
        from unittest.mock import patch, MagicMock

        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_client.get_or_create_collection.return_value = mock_collection

        with patch("chromadb.HttpClient", return_value=mock_client):
            store = make_vector_store_http(
                embedding_provider=mock_embedding,
                host="example.com",
                port=9000,
                collection_name="test_col",
            )

        assert store._persist_directory == ""
        assert store._collection_name == "test_col"
        assert store._client is mock_client
        assert store._collection is mock_collection

    def test_create_http_factory_defaults(
        self,
        mock_embedding: MockEmbeddingProvider,
    ) -> None:
        """ファクトリのデフォルト引数が仕様通りであること."""
        from unittest.mock import patch, MagicMock

        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = MagicMock()

        with patch("chromadb.HttpClient", return_value=mock_client) as mock_http:
            make_vector_store_http(embedding_provider=mock_embedding)

        # host=localhost, port=8000
        assert mock_http.call_count == 1
        _, kwargs = mock_http.call_args
        assert kwargs["host"] == "localhost"
        assert kwargs["port"] == 8000

    def test_create_http_passes_anonymized_telemetry_false(
        self,
        mock_embedding: MockEmbeddingProvider,
    ) -> None:
        """create_http() がテレメトリ無効の Settings を渡すこと."""
        from unittest.mock import patch, MagicMock

        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = MagicMock()

        with patch("chromadb.HttpClient", return_value=mock_client) as mock_http:
            make_vector_store_http(embedding_provider=mock_embedding)

        _, kwargs = mock_http.call_args
        settings = kwargs["settings"]
        assert settings.anonymized_telemetry is False

    def test_create_http_collection_uses_cosine(
        self,
        mock_embedding: MockEmbeddingProvider,
    ) -> None:
        """create_http() が cosine 距離のコレクションを作成すること."""
        from unittest.mock import patch, MagicMock

        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = MagicMock()

        with patch("chromadb.HttpClient", return_value=mock_client):
            make_vector_store_http(embedding_provider=mock_embedding)

        mock_client.get_or_create_collection.assert_called_once_with(
            name="knowledge",
            metadata={
                "hnsw:space": "cosine",
                "hnsw:M": 48,
                "hnsw:construction_ef": 400,
                "hnsw:search_ef": 300,
            },
        )

    def test_create_http_custom_hnsw_params(
        self,
        mock_embedding: MockEmbeddingProvider,
    ) -> None:
        """HNSW パラメータを明示指定した場合に metadata に反映されること."""
        from unittest.mock import patch, MagicMock

        mock_client = MagicMock()
        mock_client.heartbeat.return_value = True
        mock_client.get_or_create_collection.return_value = MagicMock()

        with patch("chromadb.HttpClient", return_value=mock_client):
            make_vector_store_http(
                embedding_provider=mock_embedding,
                hnsw_m=32,
                hnsw_construction_ef=200,
                hnsw_search_ef=150,
            )

        mock_client.get_or_create_collection.assert_called_once_with(
            name="knowledge",
            metadata={
                "hnsw:space": "cosine",
                "hnsw:M": 32,
                "hnsw:construction_ef": 200,
                "hnsw:search_ef": 150,
            },
        )

    def test_create_http_close_is_noop(
        self,
        mock_embedding: MockEmbeddingProvider,
    ) -> None:
        """HttpClient で作成した VectorStore の close() はエラーにならないこと."""
        from unittest.mock import patch, MagicMock

        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = MagicMock()

        with patch("chromadb.HttpClient", return_value=mock_client):
            store = make_vector_store_http(embedding_provider=mock_embedding)

        # persist_directory が空文字なので close() は noop
        store.close()  # 例外が出なければOK

    def test_create_http_connection_error(
        self,
        mock_embedding: MockEmbeddingProvider,
    ) -> None:
        """サーバー未起動時に ConnectionError がガイダンス付きで送出されること."""
        from unittest.mock import patch, MagicMock

        mock_client = MagicMock()
        mock_client.heartbeat.side_effect = Exception("Connection refused")

        with patch("chromadb.HttpClient", return_value=mock_client):
            with pytest.raises(ConnectionError, match="ChromaDB サーバー.*失敗しました"):
                make_vector_store_http(
                    embedding_provider=mock_embedding,
                    host="localhost",
                    port=8000,
                )


class TestBatchProcessing:
    """バッチ分割処理のテスト (#644 Phase 1)."""

    @pytest.mark.asyncio
    async def test_get_metadata_by_ids_batches(
        self, ephemeral_store: VectorStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_BATCH_SIZE を超える ID リストでもメタデータを取得できること."""
        monkeypatch.setattr(VectorStore, "_BATCH_SIZE", 2)

        chunks = [
            DocumentChunk(
                id=f"batch_{i}",
                text=f"Text {i}",
                metadata={"source_id": "src", "chunk_index": i},
            )
            for i in range(5)
        ]
        await ephemeral_store.add_documents(chunks)

        ids = [f"batch_{i}" for i in range(5)]
        result = await ephemeral_store.get_metadata_by_ids(ids)

        assert len(result) == 5
        for i in range(5):
            assert f"batch_{i}" in result
            assert result[f"batch_{i}"]["source_id"] == "src"

    @pytest.mark.asyncio
    async def test_get_metadata_by_ids_empty(
        self, ephemeral_store: VectorStore
    ) -> None:
        """空の ID リストで空辞書を返すこと."""
        result = await ephemeral_store.get_metadata_by_ids([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_update_metadata_batches(
        self, ephemeral_store: VectorStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_BATCH_SIZE を超えるメタデータ更新が成功すること."""
        monkeypatch.setattr(VectorStore, "_BATCH_SIZE", 2)

        chunks = [
            DocumentChunk(
                id=f"upd_{i}",
                text=f"Text {i}",
                metadata={"source_id": "src", "chunk_index": i, "title": "old"},
            )
            for i in range(5)
        ]
        await ephemeral_store.add_documents(chunks)

        ids = [f"upd_{i}" for i in range(5)]
        new_meta = [
            {"source_id": "src", "chunk_index": i, "title": "new"}
            for i in range(5)
        ]
        await ephemeral_store.update_metadata(ids, new_meta)

        result = await ephemeral_store.get_metadata_by_ids(ids)
        for i in range(5):
            assert result[f"upd_{i}"]["title"] == "new"

    @pytest.mark.asyncio
    async def test_delete_stale_chunks_without_metadatas(
        self, ephemeral_store: VectorStore
    ) -> None:
        """delete_stale_chunks が include=[] で正しく動作すること."""
        chunks = [
            DocumentChunk(
                id=f"stale_{i}",
                text=f"Text {i}",
                metadata={"source_id": "src_a", "chunk_index": i},
            )
            for i in range(3)
        ]
        await ephemeral_store.add_documents(chunks)

        deleted = await ephemeral_store.delete_stale_chunks(
            "src_a", {"stale_0", "stale_2"}
        )
        assert deleted == 1

        stats = ephemeral_store.get_stats()
        assert stats["total_chunks"] == 2

    @pytest.mark.asyncio
    async def test_delete_by_source_without_metadatas(
        self, ephemeral_store: VectorStore
    ) -> None:
        """delete_by_source が include=[] で正しく動作すること."""
        chunks = [
            DocumentChunk(
                id=f"del_{i}",
                text=f"Text {i}",
                metadata={"source_id": "src_del", "chunk_index": i},
            )
            for i in range(3)
        ]
        await ephemeral_store.add_documents(chunks)

        deleted = await ephemeral_store.delete_by_source("src_del")
        assert deleted == 3

        stats = ephemeral_store.get_stats()
        assert stats["total_chunks"] == 0

"""SearchPort (RealSearchAdapter) のハイブリッド検索統合テスト

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.bm25_index import BM25Index, BM25Result
from rag.search.models import RAGRetrievalResult
from rag.search.search_port import RealSearchAdapter
from rag.vector_store import RetrievalResult, VectorStore

from factories import make_search_adapter


@pytest.fixture
def mock_embedding_provider() -> MagicMock:
    mock = MagicMock()
    mock.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
    return mock


@pytest.fixture
def mock_vector_store(mock_embedding_provider: MagicMock) -> MagicMock:
    mock = MagicMock(spec=VectorStore)
    mock.add_documents = AsyncMock(return_value=3)
    mock.search = AsyncMock(return_value=[])
    mock.delete_by_source = AsyncMock(return_value=0)
    mock.delete_stale_chunks = AsyncMock(return_value=0)
    mock.get_stats = MagicMock(return_value={"total_chunks": 10})
    return mock


@pytest.fixture
def mock_bm25_index() -> MagicMock:
    mock = MagicMock(spec=BM25Index)
    mock.add_documents = MagicMock(return_value=3)
    mock.search = MagicMock(return_value=[])
    mock.delete_by_source = MagicMock(return_value=0)
    mock.get_document_count = MagicMock(return_value=0)
    return mock


@pytest.fixture
def search_vector_only(
    mock_vector_store: MagicMock,
) -> RealSearchAdapter:
    """ベクトル検索のみの RealSearchAdapter."""
    return make_search_adapter(
        vector_store=mock_vector_store,
    )


@pytest.fixture
def search_hybrid(
    mock_vector_store: MagicMock,
    mock_bm25_index: MagicMock,
) -> RealSearchAdapter:
    """ハイブリッド検索有効の RealSearchAdapter."""
    return make_search_adapter(
        vector_store=mock_vector_store,
        bm25_index=mock_bm25_index,
        hybrid_search_enabled=True,
        vector_weight=0.5,
    )


class TestHybridSearchDisabled:
    """AC9: hybrid_enabled=false 時のテスト（従来動作）."""

    async def test_hybrid_disabled_uses_vector_only(
        self,
        search_vector_only: RealSearchAdapter,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC9: ハイブリッド検索無効時はベクトル検索のみが動作すること."""
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="ベクトル検索結果",
                metadata={"source_id": "https://example.com/page1"},
                distance=0.2,
            ),
        ]

        result = await search_vector_only.retrieve("テストクエリ", n_results=5)

        assert isinstance(result, RAGRetrievalResult)
        assert "ベクトル検索結果" in result.context
        mock_vector_store.search.assert_called_once()

    async def test_hybrid_disabled_bm25_not_used(
        self,
        mock_vector_store: MagicMock,
        mock_bm25_index: MagicMock,
    ) -> None:
        """AC9: hybrid_enabled=false の場合、BM25 インデックスが検索に使用されないこと."""
        adapter = make_search_adapter(
            vector_store=mock_vector_store,
            bm25_index=mock_bm25_index,
        )

        mock_vector_store.search.return_value = []

        await adapter.retrieve("テストクエリ", n_results=5)

        mock_bm25_index.search.assert_not_called()


class TestHybridSearchEnabled:
    """AC6: ハイブリッド検索有効時のテスト（BM25 インデックス統合）."""

    async def test_hybrid_enabled_uses_both_vector_and_bm25(
        self,
        search_hybrid: RealSearchAdapter,
        mock_vector_store: MagicMock,
        mock_bm25_index: MagicMock,
    ) -> None:
        """AC6: ハイブリッド検索有効時、ベクトル検索と BM25 検索の両方が使用されること."""
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

        result = await search_hybrid.retrieve("テストクエリ", n_results=5)

        assert isinstance(result, RAGRetrievalResult)
        mock_vector_store.search.assert_called()
        mock_bm25_index.search.assert_called()


class TestTableDataSearch:
    """AC12: テーブル内データ検索テスト."""

    async def test_table_data_search_strong_keyword(
        self,
        mock_vector_store: MagicMock,
        mock_bm25_index: MagicMock,
    ) -> None:
        """AC12: 「魔王」クエリでテーブル内のデータが検索できること.

        ベクトル検索では閾値を超えてしまうケースでも、
        BM25 検索でキーワードマッチにより検索できる。
        """
        adapter = make_search_adapter(
            vector_store=mock_vector_store,
            similarity_threshold=0.5,
            bm25_index=mock_bm25_index,
            hybrid_search_enabled=True,
            vector_weight=0.5,
        )

        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="名前: 魔王\nHP: 200, MP: 100, 攻撃力: 140",
                metadata={"source_id": "https://example.com/monsters", "chunk_index": 0},
                distance=0.7,
            ),
        ]

        import hashlib
        url_hash = hashlib.sha256(b"https://example.com/monsters").hexdigest()[:16]
        mock_bm25_index.search.return_value = [
            BM25Result(
                doc_id=f"{url_hash}_0",
                score=8.5,
                text="名前: 魔王\nHP: 200, MP: 100, 攻撃力: 140",
            ),
        ]

        result = await adapter.retrieve("魔王", n_results=5)

        assert isinstance(result, RAGRetrievalResult)
        assert "魔王" in result.context
        assert "HP: 200" in result.context


class TestKeywordExactMatch:
    """AC13: キーワード完全一致検索テスト."""

    async def test_keyword_exact_match(
        self,
        search_hybrid: RealSearchAdapter,
        mock_vector_store: MagicMock,
        mock_bm25_index: MagicMock,
    ) -> None:
        """AC13: キーワード完全一致のケースで確実にヒットすること."""
        import hashlib
        url_hash = hashlib.sha256(b"https://example.com/doc").hexdigest()[:16]

        mock_vector_store.search.return_value = []

        mock_bm25_index.search.return_value = [
            BM25Result(
                doc_id=f"{url_hash}_0",
                score=10.0,
                text="特定のキーワード「フロベニウスノルム」についての説明です。",
            ),
        ]

        result = await search_hybrid.retrieve("フロベニウスノルム", n_results=5)

        assert isinstance(result, RAGRetrievalResult)
        assert "フロベニウスノルム" in result.context


class TestHybridSearchEngineInitialization:
    """AC9: HybridSearchEngine の初期化テスト."""

    def test_hybrid_engine_initialized_when_enabled(
        self,
        mock_vector_store: MagicMock,
        mock_bm25_index: MagicMock,
    ) -> None:
        """AC9: hybrid_search_enabled=True の場合、HybridSearchEngine が初期化されること."""
        adapter = make_search_adapter(
            vector_store=mock_vector_store,
            bm25_index=mock_bm25_index,
            hybrid_search_enabled=True,
            vector_weight=0.5,
        )

        assert adapter._hybrid_search_engine is not None  # noqa: SLF001
        assert adapter._hybrid_search_enabled is True  # noqa: SLF001

    def test_hybrid_engine_not_initialized_when_disabled(
        self,
        mock_vector_store: MagicMock,
        mock_bm25_index: MagicMock,
    ) -> None:
        """AC9: hybrid_search_enabled=False の場合、HybridSearchEngine は初期化されないこと."""
        adapter = make_search_adapter(
            vector_store=mock_vector_store,
            bm25_index=mock_bm25_index,
        )

        assert adapter._hybrid_search_engine is None  # noqa: SLF001
        assert adapter._hybrid_search_enabled is False  # noqa: SLF001

    def test_hybrid_engine_not_initialized_without_bm25_index(
        self,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC9: bm25_index が None の場合、hybrid_enabled=True でも初期化されないこと."""
        adapter = make_search_adapter(
            vector_store=mock_vector_store,
            hybrid_search_enabled=True,
            vector_weight=0.5,
        )

        assert adapter._hybrid_search_engine is None  # noqa: SLF001

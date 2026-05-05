"""SearchPort (RealSearchAdapter) のテスト

仕様: docs/specs/rag-knowledge.md / docs/specs/search-response.md
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from settings_defaults import TEST_SETTINGS_DEFAULTS
from rag.bm25_index import BM25Index, BM25Result
from rag.config import RAGSettings
from rag.search.models import (
    BM25SearchItem,
    RAGRetrievalResult,
    RawSearchResults,
    VectorSearchItem,
)
from rag.search.search_port import RealSearchAdapter
from rag.vector_store import RetrievalResult, VectorStore

from factories import make_search_adapter


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
    mock.get_metadata_by_ids = AsyncMock(return_value={})
    return mock


@pytest.fixture
def search_adapter(
    mock_vector_store: MagicMock,
) -> RealSearchAdapter:
    """RealSearchAdapter インスタンスを作成する."""
    return make_search_adapter(
        vector_store=mock_vector_store,
    )


class TestRetrieve:
    """retrieve() のテスト (AC18, AC19)."""

    async def test_retrieve_returns_formatted_text(
        self,
        search_adapter: RealSearchAdapter,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC18: クエリに関連するチャンクを検索し、フォーマット済みテキストを返すこと."""
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="This is relevant content 1.",
                metadata={"source_id": "https://example.com/page1"},
                distance=0.1,
            ),
            RetrievalResult(
                text="This is relevant content 2.",
                metadata={"source_id": "https://example.com/page2"},
                distance=0.2,
            ),
        ]

        result = await search_adapter.retrieve("test query", n_results=5)

        assert isinstance(result, RAGRetrievalResult)
        assert "--- 参考情報 1 ---" in result.context
        assert "出典: https://example.com/page1" in result.context
        assert "This is relevant content 1." in result.context
        assert "--- 参考情報 2 ---" in result.context
        assert "出典: https://example.com/page2" in result.context
        call_args = mock_vector_store.search.call_args
        assert call_args[0][0] == "test query"
        assert call_args[1]["n_results"] == 5

    async def test_retrieve_returns_empty_when_no_results(
        self,
        search_adapter: RealSearchAdapter,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC19: 結果がない場合は空のRAGRetrievalResultを返すこと."""
        mock_vector_store.search.return_value = []

        result = await search_adapter.retrieve("unrelated query")

        assert isinstance(result, RAGRetrievalResult)
        assert result.context == ""
        assert result.sources == []


class TestConfiguration:
    """設定のテスト (AC28, AC29) — RAGSettings (src/rag/config.py)."""

    def test_embedding_provider_switch(self) -> None:
        """AC28: embedding_provider で local / online を切り替えられること."""
        settings = RAGSettings(**{**TEST_SETTINGS_DEFAULTS, "embedding_provider": "local"})
        assert settings.embedding_provider == "local"

        settings = RAGSettings(**{**TEST_SETTINGS_DEFAULTS, "embedding_provider": "online"})
        assert settings.embedding_provider == "online"

    def test_configurable_parameters(self) -> None:
        """AC29: チャンクサイズ・オーバーラップ・検索件数が設定可能であること."""
        settings = RAGSettings(**{
            **TEST_SETTINGS_DEFAULTS,
            "rag_chunk_size": 1000,
            "rag_chunk_overlap": 100,
            "rag_retrieval_count": 10,
        })
        assert settings.rag_chunk_size == 1000
        assert settings.rag_chunk_overlap == 100
        assert settings.rag_retrieval_count == 10

    def test_similarity_threshold_configurable(self) -> None:
        """類似度閾値が設定可能であること (Issue #190)."""
        settings = RAGSettings(**{**TEST_SETTINGS_DEFAULTS, "rag_similarity_threshold": 0.5})
        assert settings.rag_similarity_threshold == 0.5

        settings = RAGSettings(**TEST_SETTINGS_DEFAULTS)
        assert settings.rag_similarity_threshold is None

    def test_similarity_threshold_validation(self) -> None:
        """類似度閾値のバリデーション (Issue #190)."""
        with pytest.raises(ValidationError):
            RAGSettings(**{**TEST_SETTINGS_DEFAULTS, "rag_similarity_threshold": -0.1})

        with pytest.raises(ValidationError):
            RAGSettings(**{**TEST_SETTINGS_DEFAULTS, "rag_similarity_threshold": 2.5})


class TestRAGDebugLog:
    """RAG検索結果のログ出力テスト."""

    @pytest.fixture
    def search_adapter_log_enabled(
        self,
        mock_vector_store: MagicMock,
    ) -> RealSearchAdapter:
        """デバッグログ有効な RealSearchAdapter を作成する."""
        return make_search_adapter(
            vector_store=mock_vector_store,
            debug_log_enabled=True,
        )

    @pytest.fixture
    def search_adapter_log_disabled(
        self,
        mock_vector_store: MagicMock,
    ) -> RealSearchAdapter:
        """デバッグログ無効な RealSearchAdapter を作成する."""
        return make_search_adapter(
            vector_store=mock_vector_store,
        )

    async def test_retrieve_logs_query(
        self,
        search_adapter_log_enabled: RealSearchAdapter,
        mock_vector_store: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC1: RAG_DEBUG_LOG_ENABLED=true の場合、検索クエリがINFOログに出力されること."""
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="Test content",
                metadata={"source_id": "https://example.com/page1"},
                distance=0.234,
            ),
        ]

        with caplog.at_level(logging.INFO, logger="rag.search.search_port"):
            await search_adapter_log_enabled.retrieve("しれんのしろ アイテム", n_results=5)

        assert "RAG retrieve (vector only): query='しれんのしろ アイテム'" in caplog.text

    async def test_retrieve_logs_results(
        self,
        search_adapter_log_enabled: RealSearchAdapter,
        mock_vector_store: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC2: 各検索結果の distance、source_url がINFOログに出力されること."""
        long_text = "A" * 150
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text=long_text,
                metadata={"source_id": "https://example.com/page1"},
                distance=0.234,
            ),
            RetrievalResult(
                text="Short text",
                metadata={"source_id": "https://example.com/page2"},
                distance=0.312,
            ),
        ]

        with caplog.at_level(logging.INFO, logger="rag.search.search_port"):
            await search_adapter_log_enabled.retrieve("test query", n_results=5)

        assert "RAG result 1: distance=0.234" in caplog.text
        assert "source='https://example.com/page1'" in caplog.text
        assert "RAG result 2: distance=0.312" in caplog.text
        assert "source='https://example.com/page2'" in caplog.text

    async def test_retrieve_logs_full_text_debug(
        self,
        search_adapter_log_enabled: RealSearchAdapter,
        mock_vector_store: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC3: 各検索結果の全文がDEBUGログに出力されること."""
        full_text = "This is the full text content that should appear in DEBUG log."
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text=full_text,
                metadata={"source_id": "https://example.com/page1"},
                distance=0.1,
            ),
        ]

        with caplog.at_level(logging.DEBUG, logger="rag.search.search_port"):
            await search_adapter_log_enabled.retrieve("test query", n_results=5)

        assert "RAG result 1 full text:" in caplog.text
        assert full_text in caplog.text

    async def test_retrieve_no_log_when_disabled(
        self,
        search_adapter_log_disabled: RealSearchAdapter,
        mock_vector_store: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC4: RAG_DEBUG_LOG_ENABLED=false の場合、ログが出力されないこと."""
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="Test content",
                metadata={"source_id": "https://example.com/page1"},
                distance=0.1,
            ),
        ]

        with caplog.at_level(logging.DEBUG, logger="rag.search.search_port"):
            await search_adapter_log_disabled.retrieve("test query", n_results=5)

        assert "RAG retrieve:" not in caplog.text
        assert "RAG result" not in caplog.text


class TestRAGRetrievalResultSources:
    """RAG検索結果のソース情報テスト."""

    async def test_retrieve_returns_sources(
        self,
        search_adapter: RealSearchAdapter,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC5: retrieve() がソースURLリストを返すこと."""
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="Content 1",
                metadata={"source_id": "https://example.com/page1"},
                distance=0.1,
            ),
            RetrievalResult(
                text="Content 2",
                metadata={"source_id": "https://example.com/page2"},
                distance=0.2,
            ),
        ]

        result = await search_adapter.retrieve("test query", n_results=5)

        assert isinstance(result, RAGRetrievalResult)
        assert len(result.sources) == 2
        assert "https://example.com/page1" in result.sources
        assert "https://example.com/page2" in result.sources

    async def test_sources_are_unique(
        self,
        search_adapter: RealSearchAdapter,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC6: ソースURLは重複なく表示されること."""
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="Content 1 from page1",
                metadata={"source_id": "https://example.com/page1"},
                distance=0.1,
            ),
            RetrievalResult(
                text="Content 2 from page1",
                metadata={"source_id": "https://example.com/page1"},
                distance=0.2,
            ),
            RetrievalResult(
                text="Content from page2",
                metadata={"source_id": "https://example.com/page2"},
                distance=0.3,
            ),
        ]

        result = await search_adapter.retrieve("test query", n_results=5)

        assert len(result.sources) == 2
        assert result.sources.count("https://example.com/page1") == 1
        assert result.sources.count("https://example.com/page2") == 1

    async def test_sources_exclude_unknown(
        self,
        search_adapter: RealSearchAdapter,
        mock_vector_store: MagicMock,
    ) -> None:
        """ソースURLが不明の場合はソースリストに含まれないこと."""
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="Content with source",
                metadata={"source_id": "https://example.com/page1"},
                distance=0.1,
            ),
            RetrievalResult(
                text="Content without source",
                metadata={},
                distance=0.2,
            ),
        ]

        result = await search_adapter.retrieve("test query", n_results=5)

        assert len(result.sources) == 1
        assert "https://example.com/page1" in result.sources
        assert "不明" not in result.sources


class TestSimilarityThreshold:
    """類似度閾値フィルタリングのテスト (Issue #190)."""

    async def test_retrieve_passes_threshold_to_search(
        self,
        mock_vector_store: MagicMock,
    ) -> None:
        """retrieve() がコンストラクタで受け取った閾値を search() に渡すこと."""
        adapter = make_search_adapter(
            vector_store=mock_vector_store,
            similarity_threshold=0.5,
        )
        mock_vector_store.search.return_value = []

        await adapter.retrieve("test query", n_results=5)

        mock_vector_store.search.assert_called_once_with(
            "test query",
            n_results=5,
            similarity_threshold=0.5,
        )

    async def test_retrieve_passes_none_threshold_when_not_set(
        self,
        search_adapter: RealSearchAdapter,
        mock_vector_store: MagicMock,
    ) -> None:
        """閾値が設定されていない場合は None を渡すこと."""
        mock_vector_store.search.return_value = []

        await search_adapter.retrieve("test query", n_results=5)

        mock_vector_store.search.assert_called_once_with(
            "test query",
            n_results=5,
            similarity_threshold=None,
        )


class TestRetrieveRawResults:
    """retrieve_raw_results() のテスト（準Agentic Search, Issue #548）."""

    async def test_vector_and_bm25_raw_results_returned(
        self,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC1: ベクトル検索とBM25の生結果が個別に返ること."""
        mock_bm25 = MagicMock(spec=BM25Index)
        mock_bm25.search.return_value = [
            BM25Result(doc_id="doc1", score=4.521, text="BM25 result text"),
        ]
        mock_bm25.get_source_url.return_value = "https://example.com/bm25"

        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="Vector result text",
                metadata={"source_id": "https://example.com/vector", "chunk_index": 0},
                distance=0.234,
            ),
        ]

        adapter = make_search_adapter(
            vector_store=mock_vector_store,
            bm25_index=mock_bm25,
        )

        result = await adapter.retrieve_raw_results("test query", n_results=3)

        assert isinstance(result, RawSearchResults)
        assert len(result.vector_results) == 1
        assert len(result.bm25_results) == 1

        vec = result.vector_results[0]
        assert isinstance(vec, VectorSearchItem)
        assert vec.text == "Vector result text"
        assert vec.source_url == "https://example.com/vector"
        assert vec.distance == 0.234
        assert vec.chunk_index == 0

        bm25 = result.bm25_results[0]
        assert isinstance(bm25, BM25SearchItem)
        assert bm25.text == "BM25 result text"
        assert bm25.source_url == "https://example.com/bm25"
        assert bm25.score == 4.521
        assert bm25.doc_id == "doc1"

    async def test_bm25_none_returns_vector_only(
        self,
        mock_vector_store: MagicMock,
    ) -> None:
        """BM25なしの場合はベクトル検索結果のみ返ること."""
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="Vector only",
                metadata={"source_id": "https://example.com/vec", "chunk_index": 2},
                distance=0.5,
            ),
        ]

        adapter = make_search_adapter(
            vector_store=mock_vector_store,
        )

        result = await adapter.retrieve_raw_results("test query")

        assert len(result.vector_results) == 1
        assert len(result.bm25_results) == 0

    async def test_both_empty_returns_empty_raw_results(
        self,
        mock_vector_store: MagicMock,
    ) -> None:
        """両方空なら空の RawSearchResults."""
        mock_bm25 = MagicMock(spec=BM25Index)
        mock_bm25.search.return_value = []
        mock_vector_store.search.return_value = []

        adapter = make_search_adapter(
            vector_store=mock_vector_store,
            bm25_index=mock_bm25,
        )

        result = await adapter.retrieve_raw_results("empty query")

        assert len(result.vector_results) == 0
        assert len(result.bm25_results) == 0

    async def test_raw_scores_preserved(
        self,
        mock_vector_store: MagicMock,
    ) -> None:
        """生スコアが変換されず保持されること."""
        mock_bm25 = MagicMock(spec=BM25Index)
        mock_bm25.search.return_value = [
            BM25Result(doc_id="d1", score=7.89, text="high score"),
            BM25Result(doc_id="d2", score=0.12, text="low score"),
        ]
        mock_bm25.get_source_url.return_value = "https://example.com"

        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="close match",
                metadata={"source_id": "https://example.com", "chunk_index": 0},
                distance=0.05,
            ),
            RetrievalResult(
                text="far match",
                metadata={"source_id": "https://example.com", "chunk_index": 1},
                distance=1.234,
            ),
        ]

        adapter = make_search_adapter(
            vector_store=mock_vector_store,
            bm25_index=mock_bm25,
        )

        result = await adapter.retrieve_raw_results("test", n_results=5)

        assert result.vector_results[0].distance == 0.05
        assert result.vector_results[1].distance == 1.234
        assert result.bm25_results[0].score == 7.89
        assert result.bm25_results[1].score == 0.12

    async def test_similarity_threshold_not_applied(
        self,
        mock_vector_store: MagicMock,
    ) -> None:
        """retrieve_raw_results は similarity_threshold=None で呼ぶこと."""
        mock_vector_store.search.return_value = []

        adapter = make_search_adapter(
            vector_store=mock_vector_store,
            similarity_threshold=0.5,
        )

        await adapter.retrieve_raw_results("test", n_results=3)

        mock_vector_store.search.assert_called_once_with(
            "test",
            n_results=3,
            similarity_threshold=None,
            where=None,
        )

    async def test_source_type_filter_passed_to_stores(
        self,
        mock_vector_store: MagicMock,
    ) -> None:
        """source_type 指定時にベクトルストアと BM25 に正しく伝播すること."""
        mock_vector_store.search.return_value = []
        mock_bm25 = MagicMock()
        mock_bm25.search.return_value = []

        adapter = make_search_adapter(
            vector_store=mock_vector_store,
            similarity_threshold=0.5,
            bm25_index=mock_bm25,
        )

        await adapter.retrieve_raw_results("test", n_results=3, source_type="bluesky")

        mock_vector_store.search.assert_called_once_with(
            "test",
            n_results=3,
            similarity_threshold=None,
            where={"source_type": "bluesky"},
        )
        mock_bm25.search.assert_called_once_with(
            "test", n_results=3, source_type="bluesky", filters=None,
        )

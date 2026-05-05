"""RAGナレッジサービスのテスト

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from settings_defaults import TEST_SETTINGS_DEFAULTS
from rag.bm25_index import BM25Index, BM25Result
from rag.config import RAGSettings
from rag.vector_store import RetrievalResult, VectorStore

from rag.rag_knowledge import (
    BM25SearchItem,
    RAGKnowledgeService,
    RAGRetrievalResult,
    RawSearchResults,
    VectorSearchItem,
)

from factories import make_rag_knowledge_service


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
    mock.get_metadata_by_ids = AsyncMock(return_value={})
    mock.get_stats = MagicMock(return_value={
        "total_chunks": 10,
    })
    return mock


@pytest.fixture
def rag_service(
    mock_vector_store: MagicMock,
) -> RAGKnowledgeService:
    """RAGKnowledgeServiceインスタンスを作成する."""
    return make_rag_knowledge_service(
        vector_store=mock_vector_store,
    )


class TestRetrieve:
    """retrieve() のテスト (AC18, AC19)."""

    async def test_retrieve_returns_formatted_text(
        self,
        rag_service: RAGKnowledgeService,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC18: クエリに関連するチャンクを検索し、フォーマット済みテキストを返すこと."""
        # Arrange
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

        # Act
        result = await rag_service.retrieve("test query", n_results=5)

        # Assert
        assert isinstance(result, RAGRetrievalResult)
        assert "--- 参考情報 1 ---" in result.context
        assert "出典: https://example.com/page1" in result.context
        assert "This is relevant content 1." in result.context
        assert "--- 参考情報 2 ---" in result.context
        assert "出典: https://example.com/page2" in result.context
        # similarity_threshold=None がデフォルトで渡される（モックでは設定されていない）
        call_args = mock_vector_store.search.call_args
        assert call_args[0][0] == "test query"
        assert call_args[1]["n_results"] == 5

    async def test_retrieve_returns_empty_when_no_results(
        self,
        rag_service: RAGKnowledgeService,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC19: 結果がない場合は空のRAGRetrievalResultを返すこと."""
        # Arrange
        mock_vector_store.search.return_value = []

        # Act
        result = await rag_service.retrieve("unrelated query")

        # Assert
        assert isinstance(result, RAGRetrievalResult)
        assert result.context == ""
        assert result.sources == []


class TestDeleteSource:
    """delete_source() のテスト."""

    async def test_delete_source(
        self,
        rag_service: RAGKnowledgeService,
        mock_vector_store: MagicMock,
    ) -> None:
        """ソースURL指定で削除できること."""
        # Arrange
        mock_vector_store.delete_by_source.return_value = 8

        # Act
        result = await rag_service.delete_source("https://example.com/page1")

        # Assert
        assert result == 8
        mock_vector_store.delete_by_source.assert_called_once_with(
            "https://example.com/page1"
        )


class TestGetStats:
    """get_stats() のテスト."""

    async def test_get_stats(
        self,
        rag_service: RAGKnowledgeService,
        mock_vector_store: MagicMock,
    ) -> None:
        """統計情報を取得できること."""
        # Arrange
        mock_vector_store.get_stats.return_value = {
            "total_chunks": 100,
        }

        # Act
        result = await rag_service.get_stats()

        # Assert
        assert result["total_chunks"] == 100
        assert "source_count" not in result
        assert "sources" not in result


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
        # 設定あり
        settings = RAGSettings(**{**TEST_SETTINGS_DEFAULTS, "rag_similarity_threshold": 0.5})
        assert settings.rag_similarity_threshold == 0.5

        # 設定なし（デフォルト: None）
        settings = RAGSettings(**TEST_SETTINGS_DEFAULTS)
        assert settings.rag_similarity_threshold is None

    def test_similarity_threshold_validation(self) -> None:
        """類似度閾値のバリデーション (Issue #190)."""
        # 負の値は拒否
        with pytest.raises(ValidationError):
            RAGSettings(**{**TEST_SETTINGS_DEFAULTS, "rag_similarity_threshold": -0.1})

        # 2.0を超える値は拒否（cosine距離の最大値は2.0）
        with pytest.raises(ValidationError):
            RAGSettings(**{**TEST_SETTINGS_DEFAULTS, "rag_similarity_threshold": 2.5})


class TestRAGDebugLog:
    """RAG検索結果のログ出力テスト."""

    @pytest.fixture
    def rag_service_log_enabled(
        self,
        mock_vector_store: MagicMock,
    ) -> RAGKnowledgeService:
        """デバッグログ有効なRAGKnowledgeServiceを作成する."""
        return make_rag_knowledge_service(
            vector_store=mock_vector_store,
            debug_log_enabled=True,
        )

    @pytest.fixture
    def rag_service_log_disabled(
        self,
        mock_vector_store: MagicMock,
    ) -> RAGKnowledgeService:
        """デバッグログ無効なRAGKnowledgeServiceを作成する."""
        return make_rag_knowledge_service(
            vector_store=mock_vector_store,
        )

    async def test_retrieve_logs_query(
        self,
        rag_service_log_enabled: RAGKnowledgeService,
        mock_vector_store: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC1: RAG_DEBUG_LOG_ENABLED=true の場合、検索クエリがINFOログに出力されること."""
        # Arrange
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="Test content",
                metadata={"source_id": "https://example.com/page1"},
                distance=0.234,
            ),
        ]

        # Act
        with caplog.at_level(logging.INFO, logger="rag.search.search_port"):
            await rag_service_log_enabled.retrieve("しれんのしろ アイテム", n_results=5)

        # Assert
        # ハイブリッド検索統合後、ログメッセージに "(vector only)" が追加された
        assert "RAG retrieve (vector only): query='しれんのしろ アイテム'" in caplog.text

    async def test_retrieve_logs_results(
        self,
        rag_service_log_enabled: RAGKnowledgeService,
        mock_vector_store: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC2: 各検索結果の distance、source_url がINFOログに出力されること.

        Note: テキストプレビューはPII漏洩リスク軽減のためDEBUGレベルに移動された。
        """
        # Arrange
        long_text = "A" * 150  # 100文字を超えるテキスト
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

        # Act
        with caplog.at_level(logging.INFO, logger="rag.search.search_port"):
            await rag_service_log_enabled.retrieve("test query", n_results=5)

        # Assert - INFOレベルではdistanceとsourceのみ（テキストは含まない）
        assert "RAG result 1: distance=0.234" in caplog.text
        assert "source='https://example.com/page1'" in caplog.text
        assert "RAG result 2: distance=0.312" in caplog.text
        assert "source='https://example.com/page2'" in caplog.text
        # テキストプレビューはINFOレベルには含まれない（DEBUGレベルで出力）
        # assert "A" * 100 + "..." in caplog.text  # -> DEBUGレベルに移動

    async def test_retrieve_logs_full_text_debug(
        self,
        rag_service_log_enabled: RAGKnowledgeService,
        mock_vector_store: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC3: 各検索結果の全文がDEBUGログに出力されること."""
        # Arrange
        full_text = "This is the full text content that should appear in DEBUG log."
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text=full_text,
                metadata={"source_id": "https://example.com/page1"},
                distance=0.1,
            ),
        ]

        # Act
        with caplog.at_level(logging.DEBUG, logger="rag.search.search_port"):
            await rag_service_log_enabled.retrieve("test query", n_results=5)

        # Assert
        assert "RAG result 1 full text:" in caplog.text
        assert full_text in caplog.text

    async def test_retrieve_no_log_when_disabled(
        self,
        rag_service_log_disabled: RAGKnowledgeService,
        mock_vector_store: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC4: RAG_DEBUG_LOG_ENABLED=false の場合、ログが出力されないこと."""
        # Arrange
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="Test content",
                metadata={"source_id": "https://example.com/page1"},
                distance=0.1,
            ),
        ]

        # Act
        with caplog.at_level(logging.DEBUG, logger="rag.search.search_port"):
            await rag_service_log_disabled.retrieve("test query", n_results=5)

        # Assert
        assert "RAG retrieve:" not in caplog.text
        assert "RAG result" not in caplog.text


class TestRAGRetrievalResultSources:
    """RAG検索結果のソース情報テスト."""

    async def test_retrieve_returns_sources(
        self,
        rag_service: RAGKnowledgeService,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC5: retrieve() がソースURLリストを返すこと."""
        # Arrange
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

        # Act
        result = await rag_service.retrieve("test query", n_results=5)

        # Assert
        assert isinstance(result, RAGRetrievalResult)
        assert len(result.sources) == 2
        assert "https://example.com/page1" in result.sources
        assert "https://example.com/page2" in result.sources

    async def test_sources_are_unique(
        self,
        rag_service: RAGKnowledgeService,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC6: ソースURLは重複なく表示されること."""
        # Arrange - 同じソースURLを持つ複数のチャンク
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

        # Act
        result = await rag_service.retrieve("test query", n_results=5)

        # Assert
        assert len(result.sources) == 2  # 3件のチャンクだが、ソースは2件
        assert result.sources.count("https://example.com/page1") == 1
        assert result.sources.count("https://example.com/page2") == 1

    async def test_sources_exclude_unknown(
        self,
        rag_service: RAGKnowledgeService,
        mock_vector_store: MagicMock,
    ) -> None:
        """ソースURLが不明の場合はソースリストに含まれないこと."""
        # Arrange
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="Content with source",
                metadata={"source_id": "https://example.com/page1"},
                distance=0.1,
            ),
            RetrievalResult(
                text="Content without source",
                metadata={},  # source_url がない
                distance=0.2,
            ),
        ]

        # Act
        result = await rag_service.retrieve("test query", n_results=5)

        # Assert
        assert len(result.sources) == 1
        assert "https://example.com/page1" in result.sources
        assert "不明" not in result.sources


class TestFragmentNormalization:
    """URL フラグメント正規化のテスト (AC36, AC37)."""

    async def test_ingest_crawled_page_uses_normalized_url_for_hash(
        self,
        rag_service: RAGKnowledgeService,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC37: _ingest_crawled_page() がフラグメント除去済みURLでハッシュ計算すること."""
        import hashlib

        # Arrange: フラグメント付きURL
        mock_vector_store.add_documents.return_value = 1

        # Act
        await rag_service._ingest_crawled_page(
            url="https://example.com/page#fragment",
            title="Test Page",
            text="Test content.",
            crawled_at="2024-01-01T00:00:00+00:00",
        )

        # Assert: add_documents に渡されたチャンクのIDとメタデータを検証
        call_args = mock_vector_store.add_documents.call_args[0][0]
        chunk = call_args[0]
        # フラグメント除去済みURLでハッシュ計算されていること
        expected_hash = hashlib.sha256("https://example.com/page".encode()).hexdigest()[:16]
        assert chunk.id.startswith(expected_hash)
        # メタデータもフラグメント除去済みURL
        assert chunk.metadata["source_id"] == "https://example.com/page"

        # delete_stale_chunks もフラグメント除去済みURLで呼ばれる
        mock_vector_store.delete_stale_chunks.assert_called_once()
        stale_call_args = mock_vector_store.delete_stale_chunks.call_args[0]
        assert stale_call_args[0] == "https://example.com/page"

    async def test_delete_source_normalizes_fragment_url(
        self,
        rag_service: RAGKnowledgeService,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC37: delete_source() がフラグメント付きURLを正規化して削除すること.

        後方互換対応として、正規化済みURLとフラグメント付き元URLの両方で削除を試みる。
        """
        # Arrange: 正規化済みURLで3件、フラグメント付きURLで2件削除される
        mock_vector_store.delete_by_source.side_effect = [3, 2]

        # Act
        result = await rag_service.delete_source("https://example.com/page#section")

        # Assert: 合計5件削除
        assert result == 5
        # 正規化済みURL → フラグメント付き元URL の順で呼ばれる
        assert mock_vector_store.delete_by_source.call_count == 2
        calls = mock_vector_store.delete_by_source.call_args_list
        assert calls[0][0][0] == "https://example.com/page"
        assert calls[1][0][0] == "https://example.com/page#section"

    async def test_delete_source_without_fragment(
        self,
        rag_service: RAGKnowledgeService,
        mock_vector_store: MagicMock,
    ) -> None:
        """フラグメントなしURLの場合は1回だけdelete_by_sourceが呼ばれること."""
        # Arrange
        mock_vector_store.delete_by_source.return_value = 5

        # Act
        result = await rag_service.delete_source("https://example.com/page")

        # Assert
        assert result == 5
        mock_vector_store.delete_by_source.assert_called_once_with("https://example.com/page")


class TestSimilarityThreshold:
    """類似度閾値フィルタリングのテスト (Issue #190)."""

    async def test_retrieve_passes_threshold_to_search(
        self,
        mock_vector_store: MagicMock,
    ) -> None:
        """retrieve() がコンストラクタで受け取った閾値を search() に渡すこと."""
        # Arrange
        service = make_rag_knowledge_service(
            vector_store=mock_vector_store,
            similarity_threshold=0.5,
        )
        mock_vector_store.search.return_value = []

        # Act
        await service.retrieve("test query", n_results=5)

        # Assert: search() に similarity_threshold が渡される
        mock_vector_store.search.assert_called_once_with(
            "test query",
            n_results=5,
            similarity_threshold=0.5,
        )

    async def test_retrieve_passes_none_threshold_when_not_set(
        self,
        rag_service: RAGKnowledgeService,
        mock_vector_store: MagicMock,
    ) -> None:
        """閾値が設定されていない場合は None を渡すこと."""
        # Arrange
        mock_vector_store.search.return_value = []

        # Act
        await rag_service.retrieve("test query", n_results=5)

        # Assert: search() に None が渡される（rag_service の similarity_threshold=None）
        mock_vector_store.search.assert_called_once_with(
            "test query",
            n_results=5,
            similarity_threshold=None,
        )


class TestGetFullPageText:
    """get_full_page_text() のテスト (Issue #27)."""

    async def test_get_full_page_text_joins_chunks(
        self,
        rag_service: RAGKnowledgeService,
        mock_vector_store: MagicMock,
    ) -> None:
        """チャンクが '\\n' で結合されること."""
        # Arrange
        mock_vector_store.get_chunks_by_source = AsyncMock(
            return_value=[
                RetrievalResult(
                    text="チャンク1のテキスト",
                    metadata={"source_id": "https://example.com/page", "chunk_index": 0},
                    distance=0.0,
                ),
                RetrievalResult(
                    text="チャンク2のテキスト",
                    metadata={"source_id": "https://example.com/page", "chunk_index": 1},
                    distance=0.0,
                ),
                RetrievalResult(
                    text="チャンク3のテキスト",
                    metadata={"source_id": "https://example.com/page", "chunk_index": 2},
                    distance=0.0,
                ),
            ]
        )

        # Act
        result = await rag_service.get_full_page_text("https://example.com/page")

        # Assert
        assert result == "チャンク1のテキスト\nチャンク2のテキスト\nチャンク3のテキスト"
        mock_vector_store.get_chunks_by_source.assert_called_once_with(
            "https://example.com/page"
        )

    async def test_get_full_page_text_empty_chunks(
        self,
        rag_service: RAGKnowledgeService,
        mock_vector_store: MagicMock,
    ) -> None:
        """チャンクが存在しない場合に空文字列が返ること."""
        # Arrange
        mock_vector_store.get_chunks_by_source = AsyncMock(return_value=[])

        # Act
        result = await rag_service.get_full_page_text("https://example.com/nonexistent")

        # Assert
        assert result == ""
        mock_vector_store.get_chunks_by_source.assert_called_once_with(
            "https://example.com/nonexistent"
        )


class TestRetrieveRawResults:
    """retrieve_raw_results() のテスト（準Agentic Search, Issue #548）."""

    async def test_vector_and_bm25_raw_results_returned(
        self,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC1: ベクトル検索とBM25の生結果が個別に返ること."""
        # Arrange
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

        service = make_rag_knowledge_service(
            vector_store=mock_vector_store,
            bm25_index=mock_bm25,
        )

        # Act
        result = await service.retrieve_raw_results("test query", n_results=3)

        # Assert
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
        # Arrange
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="Vector only",
                metadata={"source_id": "https://example.com/vec", "chunk_index": 2},
                distance=0.5,
            ),
        ]

        service = make_rag_knowledge_service(
            vector_store=mock_vector_store,
        )

        # Act
        result = await service.retrieve_raw_results("test query")

        # Assert
        assert len(result.vector_results) == 1
        assert len(result.bm25_results) == 0

    async def test_both_empty_returns_empty_raw_results(
        self,
        mock_vector_store: MagicMock,
    ) -> None:
        """両方空なら空の RawSearchResults."""
        # Arrange
        mock_bm25 = MagicMock(spec=BM25Index)
        mock_bm25.search.return_value = []
        mock_vector_store.search.return_value = []

        service = make_rag_knowledge_service(
            vector_store=mock_vector_store,
            bm25_index=mock_bm25,
        )

        # Act
        result = await service.retrieve_raw_results("empty query")

        # Assert
        assert len(result.vector_results) == 0
        assert len(result.bm25_results) == 0

    async def test_raw_scores_preserved(
        self,
        mock_vector_store: MagicMock,
    ) -> None:
        """生スコアが変換されず保持されること."""
        # Arrange
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

        service = make_rag_knowledge_service(
            vector_store=mock_vector_store,
            bm25_index=mock_bm25,
        )

        # Act
        result = await service.retrieve_raw_results("test", n_results=5)

        # Assert: スコアが正規化・変換されず元のまま保持
        assert result.vector_results[0].distance == 0.05
        assert result.vector_results[1].distance == 1.234
        assert result.bm25_results[0].score == 7.89
        assert result.bm25_results[1].score == 0.12

    async def test_similarity_threshold_not_applied(
        self,
        mock_vector_store: MagicMock,
    ) -> None:
        """retrieve_raw_results は similarity_threshold=None で呼ぶこと."""
        # Arrange
        mock_vector_store.search.return_value = []

        service = make_rag_knowledge_service(
            vector_store=mock_vector_store,
            similarity_threshold=0.5,  # サービスには閾値を設定
        )

        # Act
        await service.retrieve_raw_results("test", n_results=3)

        # Assert: similarity_threshold=None で呼ばれること（LLMが判断する）
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

        service = make_rag_knowledge_service(
            vector_store=mock_vector_store,
            similarity_threshold=0.5,
            bm25_index=mock_bm25,
        )

        await service.retrieve_raw_results("test", n_results=3, source_type="bluesky")

        mock_vector_store.search.assert_called_once_with(
            "test",
            n_results=3,
            similarity_threshold=None,
            where={"source_type": "bluesky"},
        )
        mock_bm25.search.assert_called_once_with(
            "test", n_results=3, source_type="bluesky", filters=None,
        )

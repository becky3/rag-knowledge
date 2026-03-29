"""ハイブリッド検索のテスト

仕様: docs/specs/rag-knowledge.md
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.bm25_index import BM25Result
from rag.hybrid_search import (
    HybridSearchEngine,
    HybridSearchResult,
    convex_combination,
    min_max_normalize,
)
from rag.vector_store import RetrievalResult

from factories import make_hybrid_search_engine


class TestMinMaxNormalize:
    """min_max_normalize関数のテスト."""

    def test_normal_values(self) -> None:
        """通常の値が[0, 1]に正規化される."""
        scores = [1.0, 3.0, 5.0]
        result = min_max_normalize(scores)

        assert result == [0.0, 0.5, 1.0]

    def test_all_same_values(self) -> None:
        """全て同じ値の場合、全て1.0になる."""
        scores = [3.0, 3.0, 3.0]
        result = min_max_normalize(scores)

        assert result == [1.0, 1.0, 1.0]

    def test_empty_list(self) -> None:
        """空リストの場合、空リストを返す."""
        result = min_max_normalize([])

        assert result == []

    def test_single_element(self) -> None:
        """単一要素の場合、1.0を返す."""
        result = min_max_normalize([5.0])

        assert result == [1.0]

    def test_negative_values(self) -> None:
        """負の値を含む場合も正しく正規化される."""
        scores = [-2.0, 0.0, 2.0]
        result = min_max_normalize(scores)

        assert result == [0.0, 0.5, 1.0]

    def test_two_values(self) -> None:
        """2要素の場合."""
        scores = [10.0, 20.0]
        result = min_max_normalize(scores)

        assert result == [0.0, 1.0]


class TestConvexCombination:
    """convex_combination関数のテスト."""

    def test_both_scores(self) -> None:
        """両方のスコアがある場合のCC計算."""
        norm_vector = {"doc1": 1.0, "doc2": 0.5}
        norm_bm25 = {"doc1": 0.5, "doc2": 1.0}

        scores = convex_combination(norm_vector, norm_bm25, vector_weight=0.5)

        # doc1: 0.5*1.0 + 0.5*0.5 = 0.75
        # doc2: 0.5*0.5 + 0.5*1.0 = 0.75
        assert scores["doc1"] == pytest.approx(0.75)
        assert scores["doc2"] == pytest.approx(0.75)

    def test_vector_only_doc(self) -> None:
        """片方のみにあるドキュメントは他方を0として計算."""
        norm_vector = {"doc1": 1.0}
        norm_bm25 = {"doc2": 1.0}

        scores = convex_combination(norm_vector, norm_bm25, vector_weight=0.5)

        # doc1: 0.5*1.0 + 0.5*0.0 = 0.5
        # doc2: 0.5*0.0 + 0.5*1.0 = 0.5
        assert scores["doc1"] == pytest.approx(0.5)
        assert scores["doc2"] == pytest.approx(0.5)

    def test_weight_affects_scores(self) -> None:
        """重みが結果に影響する."""
        norm_vector = {"doc1": 1.0}
        norm_bm25: dict[str, float] = {}

        # vector_weight=0.8 → doc1: 0.8*1.0 + 0.2*0.0 = 0.8
        scores_high = convex_combination(norm_vector, norm_bm25, vector_weight=0.8)
        # vector_weight=0.2 → doc1: 0.2*1.0 + 0.8*0.0 = 0.2
        scores_low = convex_combination(norm_vector, norm_bm25, vector_weight=0.2)

        assert scores_high["doc1"] > scores_low["doc1"]

    def test_empty_inputs(self) -> None:
        """両方空の場合、空の結果を返す."""
        scores = convex_combination({}, {}, vector_weight=0.5)

        assert scores == {}


class TestHybridSearchResult:
    """HybridSearchResultクラスのテスト."""

    def test_result_with_both_scores(self) -> None:
        """AC14: 両方のスコアを持つ結果."""
        result = HybridSearchResult(
            doc_id="doc1",
            text="テスト",
            metadata={"source_id": "http://example.com"},
            vector_distance=0.3,
            bm25_score=5.0,
            combined_score=0.75,
        )

        assert result.vector_distance == 0.3
        assert result.bm25_score == 5.0
        assert result.combined_score == 0.75

    def test_result_with_vector_only(self) -> None:
        """AC14: ベクトル検索のみの結果."""
        result = HybridSearchResult(
            doc_id="doc1",
            text="テスト",
            metadata={},
            vector_distance=0.3,
            bm25_score=None,
            combined_score=0.5,
        )

        assert result.vector_distance == 0.3
        assert result.bm25_score is None

    def test_result_with_bm25_only(self) -> None:
        """AC14: BM25検索のみの結果."""
        result = HybridSearchResult(
            doc_id="doc1",
            text="テスト",
            metadata={},
            vector_distance=None,
            bm25_score=5.0,
            combined_score=0.5,
        )

        assert result.vector_distance is None
        assert result.bm25_score == 5.0


class TestHybridSearchEngine:
    """HybridSearchEngineクラスのテスト（モックを使用）."""

    @pytest.fixture
    def mock_vector_store(self) -> MagicMock:
        """VectorStoreのモック."""
        store = MagicMock()
        store.search = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def mock_bm25_index(self) -> MagicMock:
        """BM25Indexのモック."""
        index = MagicMock()
        index.search = MagicMock(return_value=[])
        return index

    @pytest.fixture
    def engine(
        self, mock_vector_store: MagicMock, mock_bm25_index: MagicMock
    ) -> HybridSearchEngine:
        """HybridSearchEngineインスタンス."""
        return make_hybrid_search_engine(
            vector_store=mock_vector_store,
            bm25_index=mock_bm25_index,
            vector_weight=0.5,
        )

    @pytest.mark.asyncio
    async def test_search_merges_vector_and_bm25_results(
        self, engine: HybridSearchEngine, mock_vector_store: MagicMock, mock_bm25_index: MagicMock
    ) -> None:
        """ベクトル検索とBM25検索の結果がCCでマージされる."""
        # VectorStoreの結果を設定
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="ドキュメント1の内容",
                metadata={"source_id": "http://example.com/1", "chunk_index": 0},
                distance=0.2,
            ),
            RetrievalResult(
                text="ドキュメント2の内容",
                metadata={"source_id": "http://example.com/2", "chunk_index": 0},
                distance=0.3,
            ),
        ]

        # BM25Indexの結果を設定（doc_idはVectorStoreと同じ形式で生成）
        import hashlib

        url1_hash = hashlib.sha256(b"http://example.com/1").hexdigest()[:16]
        url2_hash = hashlib.sha256(b"http://example.com/2").hexdigest()[:16]
        mock_bm25_index.search.return_value = [
            BM25Result(doc_id=f"{url2_hash}_0", score=5.0, text="ドキュメント2の内容"),
            BM25Result(doc_id=f"{url1_hash}_0", score=3.0, text="ドキュメント1の内容"),
        ]

        results = await engine.search("テストクエリ", n_results=5)

        # 両方の検索が呼ばれたことを確認
        mock_vector_store.search.assert_called_once()
        mock_bm25_index.search.assert_called_once()

        # 結果が返されることを確認
        assert len(results) == 2

        # CCスコアが計算されていることを確認
        for result in results:
            assert result.combined_score > 0

    @pytest.mark.asyncio
    async def test_search_returns_empty_when_no_results(
        self, engine: HybridSearchEngine, mock_vector_store: MagicMock, mock_bm25_index: MagicMock
    ) -> None:
        """AC8: 両方の検索結果が空の場合、空リストを返す."""
        mock_vector_store.search.return_value = []
        mock_bm25_index.search.return_value = []

        results = await engine.search("存在しないクエリ", n_results=5)

        assert results == []

    @pytest.mark.asyncio
    async def test_search_with_vector_only_results(
        self, engine: HybridSearchEngine, mock_vector_store: MagicMock, mock_bm25_index: MagicMock
    ) -> None:
        """AC14: ベクトル検索のみ結果がある場合."""
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="ベクトル検索でのみヒット",
                metadata={"source_id": "http://example.com/vec", "chunk_index": 0},
                distance=0.25,
            ),
        ]
        mock_bm25_index.search.return_value = []

        results = await engine.search("ベクトルクエリ", n_results=5)

        assert len(results) == 1
        assert results[0].vector_distance == 0.25
        assert results[0].bm25_score is None

    @pytest.mark.asyncio
    async def test_search_with_bm25_only_results(
        self, engine: HybridSearchEngine, mock_vector_store: MagicMock, mock_bm25_index: MagicMock
    ) -> None:
        """AC14: BM25検索のみ結果がある場合."""
        mock_vector_store.search.return_value = []
        mock_bm25_index.search.return_value = [
            BM25Result(doc_id="bm25_doc_1", score=8.5, text="BM25でのみヒット"),
        ]

        results = await engine.search("キーワードクエリ", n_results=5)

        assert len(results) == 1
        assert results[0].vector_distance is None
        assert results[0].bm25_score == 8.5

    @pytest.mark.asyncio
    async def test_vector_weight_affects_cc_scores(
        self, mock_vector_store: MagicMock, mock_bm25_index: MagicMock
    ) -> None:
        """vector_weightパラメータがCCスコアに影響する."""
        # VectorStoreの結果
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="テスト",
                metadata={"source_id": "http://example.com/test", "chunk_index": 0},
                distance=0.2,
            ),
        ]
        mock_bm25_index.search.return_value = []

        # vector_weight=0.8 の場合
        engine_high = make_hybrid_search_engine(
            vector_store=mock_vector_store,
            bm25_index=mock_bm25_index,
            vector_weight=0.8,
        )
        results_high = await engine_high.search("テスト", n_results=5)

        # vector_weight=0.2 の場合
        engine_low = make_hybrid_search_engine(
            vector_store=mock_vector_store,
            bm25_index=mock_bm25_index,
            vector_weight=0.2,
        )
        results_low = await engine_low.search("テスト", n_results=5)

        # 高い重みの方がCCスコアが高い
        assert results_high[0].combined_score > results_low[0].combined_score

    @pytest.mark.asyncio
    async def test_search_respects_n_results_limit(
        self, engine: HybridSearchEngine, mock_vector_store: MagicMock, mock_bm25_index: MagicMock
    ) -> None:
        """AC8: n_resultsパラメータで結果数が制限される."""
        # 多くの結果を返すように設定
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text=f"ドキュメント{i}",
                metadata={"source_id": f"http://example.com/{i}", "chunk_index": 0},
                distance=0.1 + i * 0.05,
            )
            for i in range(10)
        ]
        mock_bm25_index.search.return_value = []

        results = await engine.search("テスト", n_results=3)

        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_similarity_threshold_filters_vector_scores(
        self, engine: HybridSearchEngine, mock_vector_store: MagicMock, mock_bm25_index: MagicMock
    ) -> None:
        """similarity_threshold超過のベクトル結果はスコアが0になる."""
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="閾値内",
                metadata={"source_id": "http://example.com/good", "chunk_index": 0},
                distance=0.3,
            ),
            RetrievalResult(
                text="閾値超過",
                metadata={"source_id": "http://example.com/bad", "chunk_index": 0},
                distance=0.8,
            ),
        ]
        mock_bm25_index.search.return_value = []

        results = await engine.search("テスト", n_results=5, similarity_threshold=0.5)

        # 閾値超過のドキュメントは除外される（BM25ヒットもないため）
        assert len(results) == 1
        assert results[0].text == "閾値内"

    @pytest.mark.asyncio
    async def test_fetch_count_minimum_30(
        self, engine: HybridSearchEngine, mock_vector_store: MagicMock, mock_bm25_index: MagicMock
    ) -> None:
        """fetch_countの下限が30であること."""
        mock_vector_store.search.return_value = []
        mock_bm25_index.search.return_value = []

        await engine.search("テスト", n_results=5)

        # n_results=5 → fetch_count = max(5*3, 30) = 30
        call_args = mock_vector_store.search.call_args
        assert call_args[1]["n_results"] == 30

    @pytest.mark.asyncio
    async def test_min_combined_score_filters_low_results(
        self, engine: HybridSearchEngine, mock_vector_store: MagicMock, mock_bm25_index: MagicMock
    ) -> None:
        """min_combined_score指定時に低スコア結果が除外される."""
        # 高スコアと低スコアの結果を返すように設定
        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="高スコアドキュメント",
                metadata={"source_id": "http://example.com/high", "chunk_index": 0},
                distance=0.1,
            ),
            RetrievalResult(
                text="低スコアドキュメント",
                metadata={"source_id": "http://example.com/low", "chunk_index": 0},
                distance=0.9,
            ),
        ]
        mock_bm25_index.search.return_value = []

        # min_combined_score=0.5 でフィルタリング
        results = await engine.search(
            "テスト", n_results=5, min_combined_score=0.5,
        )

        # 低スコアの結果はフィルタリングされる
        assert len(results) == 1
        assert results[0].text == "高スコアドキュメント"

    @pytest.mark.asyncio
    async def test_min_combined_score_none_returns_all(
        self, engine: HybridSearchEngine, mock_vector_store: MagicMock, mock_bm25_index: MagicMock
    ) -> None:
        """min_combined_score=None時は従来通り全結果を返す."""
        import hashlib

        url_a_hash = hashlib.sha256(b"http://example.com/a").hexdigest()[:16]
        url_b_hash = hashlib.sha256(b"http://example.com/b").hexdigest()[:16]

        mock_vector_store.search.return_value = [
            RetrievalResult(
                text="ドキュメントA",
                metadata={"source_id": "http://example.com/a", "chunk_index": 0},
                distance=0.1,
            ),
            RetrievalResult(
                text="ドキュメントB",
                metadata={"source_id": "http://example.com/b", "chunk_index": 0},
                distance=0.3,
            ),
        ]
        # BM25からも結果を返して両方のドキュメントが正のスコアを持つようにする
        mock_bm25_index.search.return_value = [
            BM25Result(doc_id=f"{url_a_hash}_0", score=3.0, text="ドキュメントA"),
            BM25Result(doc_id=f"{url_b_hash}_0", score=5.0, text="ドキュメントB"),
        ]

        # min_combined_score=None（デフォルト）
        results = await engine.search("テスト", n_results=5, min_combined_score=None)

        # min_combined_scoreフィルタが適用されず全結果が返される
        assert len(results) == 2

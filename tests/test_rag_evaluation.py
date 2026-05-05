"""RAG評価メトリクスのテスト

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.evaluation import (
    EvaluationDatasetQuery,
    EvaluationReport,
    FailureTag,
    PrecisionRecallResult,
    QueryEvaluationResult,
    calculate_ndcg,
    calculate_mrr,
    calculate_precision_recall,
    check_negative_sources,
    classify_failure_tags,
    evaluate_retrieval,
    load_evaluation_dataset,
)
from rag.search.models import RAGRetrievalResult


class TestCalculatePrecisionRecall:
    """calculate_precision_recall() のテスト."""

    def test_perfect_match(self) -> None:
        """完全一致の場合、Precision=1.0, Recall=1.0, F1=1.0."""
        retrieved = ["https://a.com", "https://b.com"]
        expected = ["https://a.com", "https://b.com"]

        result = calculate_precision_recall(retrieved, expected)

        assert result.precision == 1.0
        assert result.recall == 1.0
        assert result.f1 == 1.0
        assert result.true_positives == 2
        assert result.false_positives == 0
        assert result.false_negatives == 0

    def test_partial_match(self) -> None:
        """部分一致の場合のPrecision/Recall計算."""
        # 3件取得、2件が正解
        retrieved = ["https://a.com", "https://b.com", "https://c.com"]
        # 正解は2件、うち1件のみ取得
        expected = ["https://a.com", "https://d.com"]

        result = calculate_precision_recall(retrieved, expected)

        # Precision = 1/3 (取得3件中、正解1件)
        assert result.precision == pytest.approx(1 / 3)
        # Recall = 1/2 (正解2件中、取得1件)
        assert result.recall == pytest.approx(1 / 2)
        # F1 = 2 * (1/3 * 1/2) / (1/3 + 1/2) = 2 * (1/6) / (5/6) = 2/5 = 0.4
        assert result.f1 == pytest.approx(0.4)
        assert result.true_positives == 1
        assert result.false_positives == 2
        assert result.false_negatives == 1

    def test_no_match(self) -> None:
        """一致なしの場合、Precision=0, Recall=0, F1=0."""
        retrieved = ["https://a.com", "https://b.com"]
        expected = ["https://c.com", "https://d.com"]

        result = calculate_precision_recall(retrieved, expected)

        assert result.precision == 0.0
        assert result.recall == 0.0
        assert result.f1 == 0.0
        assert result.true_positives == 0
        assert result.false_positives == 2
        assert result.false_negatives == 2

    def test_empty_retrieved(self) -> None:
        """取得結果が空の場合."""
        retrieved: list[str] = []
        expected = ["https://a.com"]

        result = calculate_precision_recall(retrieved, expected)

        # 何も取得しなかった場合、Precision=0（期待があるのに取得なし）
        assert result.precision == 0.0
        # Recall=0（正解1件中、取得0件）
        assert result.recall == 0.0
        assert result.f1 == 0.0
        assert result.true_positives == 0
        assert result.false_positives == 0
        assert result.false_negatives == 1

    def test_empty_expected(self) -> None:
        """期待結果が空の場合（何も期待しない）."""
        retrieved = ["https://a.com"]
        expected: list[str] = []

        result = calculate_precision_recall(retrieved, expected)

        # 期待がないのに取得してしまった場合、Precision=0
        assert result.precision == 0.0
        # 期待がないのに取得した場合、Recall=0（本来不要なものを取得）
        assert result.recall == 0.0
        assert result.f1 == 0.0
        assert result.true_positives == 0
        assert result.false_positives == 1
        assert result.false_negatives == 0

    def test_both_empty(self) -> None:
        """両方空の場合（完璧）."""
        retrieved: list[str] = []
        expected: list[str] = []

        result = calculate_precision_recall(retrieved, expected)

        # 何も期待せず、何も取得しなかった = 完璧
        assert result.precision == 1.0
        assert result.recall == 1.0
        assert result.f1 == 1.0
        assert result.true_positives == 0
        assert result.false_positives == 0
        assert result.false_negatives == 0

    def test_duplicates_are_ignored(self) -> None:
        """重複URLは1件としてカウントされること."""
        # 重複を含むリスト
        retrieved = ["https://a.com", "https://a.com", "https://b.com"]
        expected = ["https://a.com", "https://b.com", "https://b.com"]

        result = calculate_precision_recall(retrieved, expected)

        # 重複を除いた実質: retrieved={a, b}, expected={a, b}
        assert result.precision == 1.0
        assert result.recall == 1.0
        assert result.f1 == 1.0
        assert result.true_positives == 2

    def test_high_precision_low_recall(self) -> None:
        """高Precision・低Recallのケース（慎重な検索）."""
        # 1件だけ取得し、それは正解
        retrieved = ["https://a.com"]
        # 正解は3件
        expected = ["https://a.com", "https://b.com", "https://c.com"]

        result = calculate_precision_recall(retrieved, expected)

        # Precision = 1/1 = 1.0
        assert result.precision == 1.0
        # Recall = 1/3
        assert result.recall == pytest.approx(1 / 3)
        # F1 = 2 * (1 * 1/3) / (1 + 1/3) = 2/3 / (4/3) = 0.5
        assert result.f1 == pytest.approx(0.5)

    def test_low_precision_high_recall(self) -> None:
        """低Precision・高Recallのケース（積極的な検索）."""
        # 5件取得し、期待の2件はすべて含む
        retrieved = [
            "https://a.com",
            "https://b.com",
            "https://c.com",
            "https://d.com",
            "https://e.com",
        ]
        expected = ["https://a.com", "https://b.com"]

        result = calculate_precision_recall(retrieved, expected)

        # Precision = 2/5 = 0.4
        assert result.precision == pytest.approx(0.4)
        # Recall = 2/2 = 1.0
        assert result.recall == 1.0
        # F1 = 2 * (0.4 * 1.0) / (0.4 + 1.0) = 0.8 / 1.4
        assert result.f1 == pytest.approx(0.8 / 1.4)


class TestCalculateNdcg:
    """calculate_ndcg() のテスト."""

    def test_perfect_ranking(self) -> None:
        """全正解が上位に来た場合、NDCG=1.0."""
        retrieved = ["https://a.com", "https://b.com", "https://c.com"]
        expected = ["https://a.com", "https://b.com"]

        result = calculate_ndcg(retrieved, expected)

        assert result == pytest.approx(1.0)

    def test_worst_ranking(self) -> None:
        """正解が下位に来た場合、NDCG < 1.0."""
        retrieved = ["https://x.com", "https://y.com", "https://a.com"]
        expected = ["https://a.com"]

        result = calculate_ndcg(retrieved, expected)

        # 正解が3位 → DCG = 1/log2(4), IDCG = 1/log2(2)
        assert result < 1.0
        assert result > 0.0

    def test_partial_match(self) -> None:
        """一部のみ正解の場合."""
        retrieved = ["https://a.com", "https://x.com", "https://b.com"]
        expected = ["https://a.com", "https://b.com"]

        result = calculate_ndcg(retrieved, expected)

        # a.comは1位、b.comは3位 → 理想は1位と2位
        assert 0.0 < result < 1.0

    def test_no_relevant_results(self) -> None:
        """正解が一つもない場合、NDCG=0.0."""
        retrieved = ["https://x.com", "https://y.com"]
        expected = ["https://a.com"]

        result = calculate_ndcg(retrieved, expected)

        assert result == 0.0

    def test_empty_retrieved(self) -> None:
        """取得結果が空の場合."""
        retrieved: list[str] = []
        expected = ["https://a.com"]

        result = calculate_ndcg(retrieved, expected)

        assert result == 0.0

    def test_empty_expected(self) -> None:
        """期待結果が空の場合."""
        retrieved = ["https://a.com"]
        expected: list[str] = []

        result = calculate_ndcg(retrieved, expected)

        assert result == 0.0

    def test_both_empty(self) -> None:
        """両方空の場合、完璧."""
        retrieved: list[str] = []
        expected: list[str] = []

        result = calculate_ndcg(retrieved, expected)

        assert result == 1.0

    def test_k_parameter(self) -> None:
        """kパラメータで上位k件に制限される."""
        retrieved = ["https://x.com", "https://a.com", "https://b.com"]
        expected = ["https://a.com", "https://b.com"]

        # k=1: 上位1件のみ → xは不正解なのでNDCG低い
        result_k1 = calculate_ndcg(retrieved, expected, k=1)
        # k=3: 上位3件 → aとbが含まれる
        result_k3 = calculate_ndcg(retrieved, expected, k=3)

        assert result_k1 == 0.0
        assert result_k3 > result_k1


class TestCalculateMrr:
    """calculate_mrr() のテスト."""

    def test_first_result_is_relevant(self) -> None:
        """最初の結果が正解の場合、MRR=1.0."""
        retrieved = ["https://a.com", "https://b.com"]
        expected = ["https://a.com"]

        result = calculate_mrr(retrieved, expected)

        assert result == 1.0

    def test_second_result_is_relevant(self) -> None:
        """2番目の結果が正解の場合、MRR=0.5."""
        retrieved = ["https://x.com", "https://a.com"]
        expected = ["https://a.com"]

        result = calculate_mrr(retrieved, expected)

        assert result == pytest.approx(0.5)

    def test_no_relevant_results(self) -> None:
        """正解がない場合、MRR=0.0."""
        retrieved = ["https://x.com", "https://y.com"]
        expected = ["https://a.com"]

        result = calculate_mrr(retrieved, expected)

        assert result == 0.0

    def test_empty_retrieved(self) -> None:
        """取得結果が空の場合、MRR=0.0."""
        retrieved: list[str] = []
        expected = ["https://a.com"]

        result = calculate_mrr(retrieved, expected)

        assert result == 0.0

    def test_empty_expected(self) -> None:
        """期待結果が空で取得ありの場合、MRR=0.0."""
        retrieved = ["https://a.com"]
        expected: list[str] = []

        result = calculate_mrr(retrieved, expected)

        assert result == 0.0

    def test_both_empty(self) -> None:
        """両方空の場合、完璧."""
        retrieved: list[str] = []
        expected: list[str] = []

        result = calculate_mrr(retrieved, expected)

        assert result == 1.0


class TestCheckNegativeSources:
    """check_negative_sources() のテスト."""

    def test_no_negative_found(self) -> None:
        """禁止ソースが含まれていない場合、空リストを返す."""
        retrieved = ["https://safe1.com", "https://safe2.com"]
        negative = ["https://bad.com"]

        result = check_negative_sources(retrieved, negative)

        assert result == []

    def test_negative_found(self) -> None:
        """禁止ソースが含まれている場合、そのリストを返す."""
        retrieved = ["https://safe.com", "https://bad1.com", "https://bad2.com"]
        negative = ["https://bad1.com", "https://bad2.com", "https://bad3.com"]

        result = check_negative_sources(retrieved, negative)

        assert set(result) == {"https://bad1.com", "https://bad2.com"}

    def test_empty_retrieved(self) -> None:
        """取得結果が空の場合、空リストを返す."""
        retrieved: list[str] = []
        negative = ["https://bad.com"]

        result = check_negative_sources(retrieved, negative)

        assert result == []

    def test_empty_negative(self) -> None:
        """禁止リストが空の場合、空リストを返す."""
        retrieved = ["https://any.com"]
        negative: list[str] = []

        result = check_negative_sources(retrieved, negative)

        assert result == []

    def test_issue_176_scenario(self) -> None:
        """Issue #176のシナリオ: しれんのしろ検索で別ゲームのデータが混入."""
        # 「しれんのしろ アイテム」で検索した結果
        retrieved = [
            "https://example.com/dq3/dungeon/shirennoshiro.html",  # 正解
            "https://example.com/ff1/dungeon/trial.html",  # FF1のデータ（混入）
            "https://example.com/dq6/dungeon/trial.html",  # DQ6のデータ（混入）
        ]
        # 含まれてはいけないソース
        negative = [
            "https://example.com/ff1/dungeon/trial.html",
            "https://example.com/dq6/dungeon/trial.html",
        ]

        result = check_negative_sources(retrieved, negative)

        # 2件の禁止ソースが検出される
        assert len(result) == 2
        assert "https://example.com/ff1/dungeon/trial.html" in result
        assert "https://example.com/dq6/dungeon/trial.html" in result


class TestPrecisionRecallResult:
    """PrecisionRecallResult データクラスのテスト."""

    def test_dataclass_fields(self) -> None:
        """データクラスのフィールドが正しく設定されること."""
        result = PrecisionRecallResult(
            precision=0.8,
            recall=0.6,
            f1=0.685,
            true_positives=3,
            false_positives=1,
            false_negatives=2,
        )

        assert result.precision == 0.8
        assert result.recall == 0.6
        assert result.f1 == 0.685
        assert result.true_positives == 3
        assert result.false_positives == 1
        assert result.false_negatives == 2


class TestLoadEvaluationDataset:
    """load_evaluation_dataset() のテスト."""

    def test_load_valid_dataset(self, tmp_path: Path) -> None:
        """有効なデータセットを正しく読み込めること."""
        dataset = {
            "queries": [
                {
                    "id": "q1",
                    "query": "テストクエリ1",
                    "expected_sources": ["https://a.com"],
                    "negative_sources": ["https://bad.com"],
                    "expected_keywords": ["キーワード1"],
                    "description": "テスト説明",
                    "notes": "テストノート",
                },
                {
                    "id": "q2",
                    "query": "テストクエリ2",
                    "expected_sources": ["https://b.com", "https://c.com"],
                },
            ]
        }
        dataset_path = tmp_path / "test_dataset.json"
        dataset_path.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")

        result = load_evaluation_dataset(str(dataset_path))

        assert len(result) == 2
        assert result[0].id == "q1"
        assert result[0].query == "テストクエリ1"
        assert result[0].expected_sources == ["https://a.com"]
        assert result[0].negative_sources == ["https://bad.com"]
        assert result[0].expected_keywords == ["キーワード1"]
        assert result[1].id == "q2"
        assert result[1].negative_sources == []  # デフォルト値

    def test_load_empty_dataset(self, tmp_path: Path) -> None:
        """空のデータセットを読み込んだ場合、空リストを返す."""
        dataset = {"queries": []}
        dataset_path = tmp_path / "empty_dataset.json"
        dataset_path.write_text(json.dumps(dataset), encoding="utf-8")

        result = load_evaluation_dataset(str(dataset_path))

        assert result == []

    def test_load_missing_file(self) -> None:
        """存在しないファイルを読み込もうとした場合、FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_evaluation_dataset("/nonexistent/path.json")


class TestEvaluateRetrieval:
    """evaluate_retrieval() のテスト."""

    @pytest.fixture
    def mock_rag_service(self) -> MagicMock:
        """モック SearchPort を作成する."""
        mock = MagicMock()
        mock.retrieve = AsyncMock()
        return mock

    @pytest.fixture
    def sample_dataset_path(self, tmp_path: Path) -> str:
        """サンプルデータセットファイルを作成する."""
        dataset = {
            "queries": [
                {
                    "id": "q1",
                    "query": "まもりのマント 入手場所",
                    "expected_sources": ["https://example.com/dq3/item/mamori.html"],
                    "negative_sources": ["https://example.com/dq3/dungeon/shiren.html"],
                },
                {
                    "id": "q2",
                    "query": "闇の王 攻略",
                    "expected_sources": [
                        "https://example.com/dq3/boss/zoma.html",
                        "https://example.com/dq3/strategy/final.html",
                    ],
                    "negative_sources": [],
                },
            ]
        }
        dataset_path = tmp_path / "eval_dataset.json"
        dataset_path.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
        return str(dataset_path)

    async def test_evaluate_perfect_retrieval(
        self,
        mock_rag_service: MagicMock,
        sample_dataset_path: str,
    ) -> None:
        """完璧な検索結果の場合のレポート生成."""
        # q1: 期待どおりの結果を返す
        # q2: 期待どおりの結果を返す
        mock_rag_service.retrieve.side_effect = [
            RAGRetrievalResult(
                context="参考情報",
                sources=["https://example.com/dq3/item/mamori.html"],
            ),
            RAGRetrievalResult(
                context="参考情報",
                sources=[
                    "https://example.com/dq3/boss/zoma.html",
                    "https://example.com/dq3/strategy/final.html",
                ],
            ),
        ]

        report = await evaluate_retrieval(mock_rag_service, sample_dataset_path)

        assert report.queries_evaluated == 2
        assert report.average_precision == 1.0
        assert report.average_recall == 1.0
        assert report.average_f1 == 1.0
        assert report.negative_source_violations == []
        assert len(report.query_results) == 2

    async def test_evaluate_with_negative_violations(
        self,
        mock_rag_service: MagicMock,
        sample_dataset_path: str,
    ) -> None:
        """禁止ソースが含まれている場合の検出."""
        # q1: 禁止ソースが含まれている
        mock_rag_service.retrieve.side_effect = [
            RAGRetrievalResult(
                context="参考情報",
                sources=[
                    "https://example.com/dq3/item/mamori.html",
                    "https://example.com/dq3/dungeon/shiren.html",  # 禁止ソース
                ],
            ),
            RAGRetrievalResult(
                context="参考情報",
                sources=["https://example.com/dq3/boss/zoma.html"],
            ),
        ]

        report = await evaluate_retrieval(mock_rag_service, sample_dataset_path)

        assert report.queries_evaluated == 2
        assert report.negative_source_violations == ["q1"]
        assert report.query_results[0].negative_violations == [
            "https://example.com/dq3/dungeon/shiren.html"
        ]

    async def test_evaluate_partial_match(
        self,
        mock_rag_service: MagicMock,
        sample_dataset_path: str,
    ) -> None:
        """部分一致の場合のPrecision/Recall計算."""
        # q1: 正解を1件取得、不要なものも1件取得
        # q2: 2件中1件のみ取得
        mock_rag_service.retrieve.side_effect = [
            RAGRetrievalResult(
                context="参考情報",
                sources=[
                    "https://example.com/dq3/item/mamori.html",  # 正解
                    "https://example.com/other.html",  # 不要
                ],
            ),
            RAGRetrievalResult(
                context="参考情報",
                sources=["https://example.com/dq3/boss/zoma.html"],  # 2件中1件のみ
            ),
        ]

        report = await evaluate_retrieval(mock_rag_service, sample_dataset_path)

        assert report.queries_evaluated == 2
        # q1: precision=0.5, recall=1.0
        # q2: precision=1.0, recall=0.5
        # 平均: precision=0.75, recall=0.75
        assert report.average_precision == pytest.approx(0.75)
        assert report.average_recall == pytest.approx(0.75)

    async def test_evaluate_empty_dataset(
        self,
        mock_rag_service: MagicMock,
        tmp_path: Path,
    ) -> None:
        """空のデータセットの場合、ゼロ値のレポートを返す."""
        empty_dataset = {"queries": []}
        dataset_path = tmp_path / "empty.json"
        dataset_path.write_text(json.dumps(empty_dataset), encoding="utf-8")

        report = await evaluate_retrieval(mock_rag_service, str(dataset_path))

        assert report.queries_evaluated == 0
        assert report.average_precision == 0.0
        assert report.average_recall == 0.0
        assert report.average_f1 == 0.0
        assert report.average_ndcg == 0.0
        assert report.average_mrr == 0.0
        assert report.negative_source_violations == []
        mock_rag_service.retrieve.assert_not_called()


class TestEvaluationReportDataclass:
    """EvaluationReport データクラスのテスト."""

    def test_dataclass_fields(self) -> None:
        """データクラスのフィールドが正しく設定されること."""
        query_result = QueryEvaluationResult(
            query_id="q1",
            query="テスト",
            precision=0.8,
            recall=0.6,
            f1=0.685,
            ndcg=0.9,
            mrr=1.0,
            retrieved_sources=["https://a.com"],
            expected_sources=["https://a.com", "https://b.com"],
            negative_violations=[],
        )
        report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.8,
            average_recall=0.6,
            average_f1=0.685,
            average_ndcg=0.9,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[query_result],
        )

        assert report.queries_evaluated == 1
        assert report.average_precision == 0.8
        assert report.average_recall == 0.6
        assert report.average_f1 == 0.685
        assert report.average_ndcg == 0.9
        assert report.average_mrr == 1.0
        assert len(report.query_results) == 1


class TestEvaluationDatasetQueryDataclass:
    """EvaluationDatasetQuery データクラスのテスト."""

    def test_default_values(self) -> None:
        """デフォルト値が正しく設定されること."""
        query = EvaluationDatasetQuery(
            id="q1",
            query="テスト",
            expected_sources=["https://a.com"],
        )

        assert query.id == "q1"
        assert query.query == "テスト"
        assert query.expected_sources == ["https://a.com"]
        assert query.negative_sources == []
        assert query.expected_keywords == []
        assert query.description == ""
        assert query.notes == ""


class TestClassifyFailureTags:
    """classify_failure_tags() のテスト."""

    def test_perfect_retrieval_no_tags(self) -> None:
        """完璧な検索結果の場合、失敗タグなし."""
        pr_result = PrecisionRecallResult(
            precision=1.0, recall=1.0, f1=1.0,
            true_positives=2, false_positives=0, false_negatives=0,
        )
        tags = classify_failure_tags(
            pr_result=pr_result,
            retrieved_sources=["https://a.com", "https://b.com"],
            expected_sources=["https://a.com", "https://b.com"],
            ndcg=1.0,
        )
        assert tags == []

    def test_retrieval_miss(self) -> None:
        """関連文書が検索されなかった場合、retrieval_missタグ."""
        pr_result = PrecisionRecallResult(
            precision=1.0, recall=0.5, f1=0.667,
            true_positives=1, false_positives=0, false_negatives=1,
        )
        tags = classify_failure_tags(
            pr_result=pr_result,
            retrieved_sources=["https://a.com"],
            expected_sources=["https://a.com", "https://b.com"],
            ndcg=1.0,
        )
        assert FailureTag.RETRIEVAL_MISS in tags

    def test_retrieval_noise(self) -> None:
        """無関係な文書が上位に来た場合、retrieval_noiseタグ."""
        pr_result = PrecisionRecallResult(
            precision=0.5, recall=1.0, f1=0.667,
            true_positives=1, false_positives=1, false_negatives=0,
        )
        tags = classify_failure_tags(
            pr_result=pr_result,
            retrieved_sources=["https://a.com", "https://x.com"],
            expected_sources=["https://a.com"],
            ndcg=1.0,
        )
        assert FailureTag.RETRIEVAL_NOISE in tags

    def test_chunk_fragmentation(self) -> None:
        """情報が分断された場合、chunk_fragmentationタグ."""
        pr_result = PrecisionRecallResult(
            precision=0.5, recall=0.5, f1=0.5,
            true_positives=1, false_positives=1, false_negatives=1,
        )
        tags = classify_failure_tags(
            pr_result=pr_result,
            retrieved_sources=["https://a.com", "https://x.com"],
            expected_sources=["https://a.com", "https://b.com"],
            ndcg=0.3,
        )
        assert FailureTag.CHUNK_FRAGMENTATION in tags

    def test_no_chunk_fragmentation_when_ndcg_high(self) -> None:
        """NDCGが高い場合、chunk_fragmentationタグなし."""
        pr_result = PrecisionRecallResult(
            precision=0.5, recall=0.5, f1=0.5,
            true_positives=1, false_positives=1, false_negatives=1,
        )
        tags = classify_failure_tags(
            pr_result=pr_result,
            retrieved_sources=["https://a.com", "https://x.com"],
            expected_sources=["https://a.com", "https://b.com"],
            ndcg=0.8,
        )
        assert FailureTag.CHUNK_FRAGMENTATION not in tags

    def test_query_mismatch_no_tp(self) -> None:
        """取得したが正解が1つも含まれない場合、query_mismatchタグ."""
        pr_result = PrecisionRecallResult(
            precision=0.0, recall=0.0, f1=0.0,
            true_positives=0, false_positives=2, false_negatives=1,
        )
        tags = classify_failure_tags(
            pr_result=pr_result,
            retrieved_sources=["https://x.com", "https://y.com"],
            expected_sources=["https://a.com"],
            ndcg=0.0,
        )
        assert FailureTag.QUERY_MISMATCH in tags

    def test_query_mismatch_empty_retrieval(self) -> None:
        """取得結果がゼロの場合もquery_mismatchタグ."""
        pr_result = PrecisionRecallResult(
            precision=0.0, recall=0.0, f1=0.0,
            true_positives=0, false_positives=0, false_negatives=1,
        )
        tags = classify_failure_tags(
            pr_result=pr_result,
            retrieved_sources=[],
            expected_sources=["https://a.com"],
            ndcg=0.0,
        )
        assert FailureTag.QUERY_MISMATCH in tags
        assert FailureTag.RETRIEVAL_MISS in tags

    def test_multiple_tags(self) -> None:
        """複数の失敗タグが同時に付与されるケース."""
        pr_result = PrecisionRecallResult(
            precision=0.0, recall=0.0, f1=0.0,
            true_positives=0, false_positives=3, false_negatives=2,
        )
        tags = classify_failure_tags(
            pr_result=pr_result,
            retrieved_sources=["https://x.com", "https://y.com", "https://z.com"],
            expected_sources=["https://a.com", "https://b.com"],
            ndcg=0.0,
        )
        assert FailureTag.RETRIEVAL_MISS in tags
        assert FailureTag.RETRIEVAL_NOISE in tags
        assert FailureTag.QUERY_MISMATCH in tags

    def test_both_empty_no_tags(self) -> None:
        """両方空の場合（完璧）、失敗タグなし."""
        pr_result = PrecisionRecallResult(
            precision=1.0, recall=1.0, f1=1.0,
            true_positives=0, false_positives=0, false_negatives=0,
        )
        tags = classify_failure_tags(
            pr_result=pr_result,
            retrieved_sources=[],
            expected_sources=[],
            ndcg=1.0,
        )
        assert tags == []


class TestFailureTagEnum:
    """FailureTag Enumのテスト."""

    def test_values(self) -> None:
        """各タグの値が正しいこと."""
        assert FailureTag.RETRIEVAL_MISS.value == "retrieval_miss"
        assert FailureTag.RETRIEVAL_NOISE.value == "retrieval_noise"
        assert FailureTag.CHUNK_FRAGMENTATION.value == "chunk_fragmentation"
        assert FailureTag.QUERY_MISMATCH.value == "query_mismatch"

    def test_all_members(self) -> None:
        """4つのタグが定義されていること."""
        assert len(FailureTag) == 4


class TestEvaluateRetrievalWithFailureTags:
    """evaluate_retrieval() の失敗タグ統合テスト."""

    @pytest.fixture
    def mock_rag_service(self) -> MagicMock:
        """モック SearchPort を作成する."""
        mock = MagicMock()
        mock.retrieve = AsyncMock()
        return mock

    @pytest.fixture
    def sample_dataset_path(self, tmp_path: Path) -> str:
        """サンプルデータセットファイルを作成する."""
        dataset = {
            "queries": [
                {
                    "id": "q1",
                    "query": "完璧な検索",
                    "expected_sources": ["https://example.com/a.html"],
                    "negative_sources": [],
                },
                {
                    "id": "q2",
                    "query": "失敗する検索",
                    "expected_sources": [
                        "https://example.com/b.html",
                        "https://example.com/c.html",
                    ],
                    "negative_sources": [],
                },
            ]
        }
        dataset_path = tmp_path / "eval_dataset.json"
        dataset_path.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
        return str(dataset_path)

    async def test_failure_tags_in_query_results(
        self,
        mock_rag_service: MagicMock,
        sample_dataset_path: str,
    ) -> None:
        """クエリ結果に失敗タグが含まれること."""
        mock_rag_service.retrieve.side_effect = [
            RAGRetrievalResult(
                context="参考情報",
                sources=["https://example.com/a.html"],
            ),
            RAGRetrievalResult(
                context="参考情報",
                sources=["https://example.com/x.html"],  # 不正解のみ
            ),
        ]

        report = await evaluate_retrieval(mock_rag_service, sample_dataset_path)

        # q1: 完璧な検索 → タグなし
        assert report.query_results[0].failure_tags == []
        # q2: 不正解のみ → retrieval_miss + retrieval_noise + query_mismatch
        q2_tags = report.query_results[1].failure_tags
        assert FailureTag.RETRIEVAL_MISS in q2_tags
        assert FailureTag.RETRIEVAL_NOISE in q2_tags
        assert FailureTag.QUERY_MISMATCH in q2_tags

    async def test_failure_tag_summary_in_report(
        self,
        mock_rag_service: MagicMock,
        sample_dataset_path: str,
    ) -> None:
        """レポートに失敗タグ集計が含まれること."""
        mock_rag_service.retrieve.side_effect = [
            RAGRetrievalResult(
                context="参考情報",
                sources=["https://example.com/a.html"],
            ),
            RAGRetrievalResult(
                context="参考情報",
                sources=["https://example.com/x.html"],  # 不正解のみ
            ),
        ]

        report = await evaluate_retrieval(mock_rag_service, sample_dataset_path)

        assert report.failure_tag_summary is not None
        assert report.failure_tag_summary.get("retrieval_miss", 0) >= 1
        assert report.failure_tag_summary.get("retrieval_noise", 0) >= 1
        assert report.failure_tag_summary.get("query_mismatch", 0) >= 1

    async def test_perfect_retrieval_no_failure_tags(
        self,
        mock_rag_service: MagicMock,
        sample_dataset_path: str,
    ) -> None:
        """完璧な検索結果の場合、失敗タグ集計が空."""
        mock_rag_service.retrieve.side_effect = [
            RAGRetrievalResult(
                context="参考情報",
                sources=["https://example.com/a.html"],
            ),
            RAGRetrievalResult(
                context="参考情報",
                sources=[
                    "https://example.com/b.html",
                    "https://example.com/c.html",
                ],
            ),
        ]

        report = await evaluate_retrieval(mock_rag_service, sample_dataset_path)

        assert report.failure_tag_summary == {}
        for qr in report.query_results:
            assert qr.failure_tags == []


class TestQueryEvaluationResultFailureTags:
    """QueryEvaluationResult の failure_tags フィールドテスト."""

    def test_default_failure_tags(self) -> None:
        """failure_tagsのデフォルト値は空リスト."""
        result = QueryEvaluationResult(
            query_id="q1",
            query="テスト",
            precision=0.8,
            recall=0.6,
            f1=0.685,
            ndcg=0.9,
            mrr=1.0,
            retrieved_sources=["https://a.com"],
            expected_sources=["https://a.com", "https://b.com"],
            negative_violations=[],
        )
        assert result.failure_tags == []

    def test_failure_tags_with_values(self) -> None:
        """failure_tagsに値を設定できること."""
        result = QueryEvaluationResult(
            query_id="q1",
            query="テスト",
            precision=0.0,
            recall=0.0,
            f1=0.0,
            ndcg=0.0,
            mrr=0.0,
            retrieved_sources=[],
            expected_sources=["https://a.com"],
            negative_violations=[],
            failure_tags=[FailureTag.RETRIEVAL_MISS, FailureTag.QUERY_MISMATCH],
        )
        assert len(result.failure_tags) == 2
        assert FailureTag.RETRIEVAL_MISS in result.failure_tags
        assert FailureTag.QUERY_MISMATCH in result.failure_tags


class TestEvaluationReportFailureTagSummary:
    """EvaluationReport の failure_tag_summary フィールドテスト."""

    def test_default_failure_tag_summary(self) -> None:
        """failure_tag_summaryのデフォルト値は空辞書."""
        report = EvaluationReport(
            queries_evaluated=0,
            average_precision=0.0,
            average_recall=0.0,
            average_f1=0.0,
            average_ndcg=0.0,
            average_mrr=0.0,
            negative_source_violations=[],
        )
        assert report.failure_tag_summary == {}

    def test_failure_tag_summary_with_values(self) -> None:
        """failure_tag_summaryに値を設定できること."""
        report = EvaluationReport(
            queries_evaluated=5,
            average_precision=0.5,
            average_recall=0.5,
            average_f1=0.5,
            average_ndcg=0.5,
            average_mrr=0.5,
            negative_source_violations=[],
            failure_tag_summary={
                "retrieval_miss": 3,
                "retrieval_noise": 2,
                "query_mismatch": 1,
            },
        )
        assert report.failure_tag_summary["retrieval_miss"] == 3
        assert report.failure_tag_summary["retrieval_noise"] == 2
        assert report.failure_tag_summary["query_mismatch"] == 1

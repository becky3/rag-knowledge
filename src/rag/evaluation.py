"""RAG評価メトリクス計算モジュール

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .rag_knowledge import RAGKnowledgeService

logger = logging.getLogger(__name__)


@dataclass
class PrecisionRecallResult:
    """Precision/Recall計算結果."""

    precision: float
    recall: float
    f1: float
    true_positives: int
    false_positives: int
    false_negatives: int


def calculate_precision_recall(
    retrieved_sources: list[str],
    expected_sources: list[str],
) -> PrecisionRecallResult:
    """Precision/Recall を計算する.

    Args:
        retrieved_sources: RAG検索で取得されたソースURLリスト
        expected_sources: 期待されるソースURLリスト（正解データ）

    Returns:
        PrecisionRecallResult: 計算結果（precision, recall, f1, 各カウント）

    Note:
        - Precision = TP / (TP + FP) = 取得結果のうち正解の割合
        - Recall = TP / (TP + FN) = 正解のうち取得できた割合
        - F1 = 2 * (Precision * Recall) / (Precision + Recall)
        - 空リストの場合は適切にハンドリング（ゼロ除算回避）
    """
    retrieved_set = set(retrieved_sources)
    expected_set = set(expected_sources)

    # True Positives: 期待され、かつ取得された
    true_positives = len(retrieved_set & expected_set)
    # False Positives: 取得されたが、期待されていない
    false_positives = len(retrieved_set - expected_set)
    # False Negatives: 期待されたが、取得されなかった
    false_negatives = len(expected_set - retrieved_set)

    # Precision: 取得結果のうち正解の割合
    if len(retrieved_set) > 0:
        precision = true_positives / len(retrieved_set)
    else:
        # 何も取得されなかった場合
        # 期待するものがなければ完璧、あれば0
        precision = 1.0 if len(expected_set) == 0 else 0.0

    # Recall: 正解のうち取得できた割合
    if len(expected_set) > 0:
        recall = true_positives / len(expected_set)
    else:
        # 期待するものがない場合
        # 何も取得しなければ完璧、取得してしまったら0
        recall = 1.0 if len(retrieved_set) == 0 else 0.0

    # F1スコア: PrecisionとRecallの調和平均
    if precision + recall > 0:
        f1 = 2 * (precision * recall) / (precision + recall)
    else:
        f1 = 0.0

    return PrecisionRecallResult(
        precision=precision,
        recall=recall,
        f1=f1,
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
    )


def calculate_ndcg(
    retrieved_sources: list[str],
    expected_sources: list[str],
    k: int | None = None,
) -> float:
    """NDCG（Normalized Discounted Cumulative Gain）を計算する.

    Binary relevance（正解なら1、不正解なら0）でDCGを計算し、
    理想的なランキングのIDCGで正規化する。

    Args:
        retrieved_sources: RAG検索で取得されたソースURLリスト（順序あり）
        expected_sources: 期待されるソースURLリスト（正解データ）
        k: 上位k件までで計算（Noneの場合は全件）

    Returns:
        NDCGスコア（0.0〜1.0）
    """
    if not expected_sources:
        return 1.0 if not retrieved_sources else 0.0

    expected_set = set(expected_sources)
    items = retrieved_sources[:k] if k is not None else retrieved_sources

    # DCG計算: Σ rel(i) / log2(i+1)  (i は 1-indexed)
    dcg = 0.0
    for i, source in enumerate(items):
        if source in expected_set:
            dcg += 1.0 / math.log2(i + 2)  # i+2 because i is 0-indexed, log2(rank+1)

    # IDCG計算: 理想ランキング（全正解が上位に来た場合）
    ideal_count = min(len(expected_set), len(items)) if items else 0
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_count))

    if idcg == 0.0:
        return 0.0

    return dcg / idcg


def calculate_mrr(
    retrieved_sources: list[str],
    expected_sources: list[str],
) -> float:
    """MRR（Mean Reciprocal Rank）を計算する.

    最初に出現する正解の逆数を返す。正解がなければ0.0。

    Args:
        retrieved_sources: RAG検索で取得されたソースURLリスト（順序あり）
        expected_sources: 期待されるソースURLリスト（正解データ）

    Returns:
        Reciprocal Rank（0.0〜1.0）
    """
    if not expected_sources:
        return 1.0 if not retrieved_sources else 0.0

    expected_set = set(expected_sources)

    for i, source in enumerate(retrieved_sources):
        if source in expected_set:
            return 1.0 / (i + 1)

    return 0.0


def check_negative_sources(
    retrieved_sources: list[str],
    negative_sources: list[str],
) -> list[str]:
    """取得結果に含まれてはいけないソースが含まれていないかチェックする.

    Args:
        retrieved_sources: RAG検索で取得されたソースURLリスト
        negative_sources: 含まれてはいけないソースURLリスト

    Returns:
        含まれていた禁止ソースのリスト（空なら問題なし）
    """
    retrieved_set = set(retrieved_sources)
    negative_set = set(negative_sources)
    return list(retrieved_set & negative_set)


class FailureTag(Enum):
    """失敗タグ: 検索失敗の原因分類."""

    RETRIEVAL_MISS = "retrieval_miss"
    RETRIEVAL_NOISE = "retrieval_noise"
    CHUNK_FRAGMENTATION = "chunk_fragmentation"
    QUERY_MISMATCH = "query_mismatch"


def classify_failure_tags(
    pr_result: PrecisionRecallResult,
    retrieved_sources: list[str],
    expected_sources: list[str],
    ndcg: float,
) -> list[FailureTag]:
    """検索結果のメトリクスから失敗タグを分類する.

    Args:
        pr_result: Precision/Recall計算結果
        retrieved_sources: RAG検索で取得されたソースURLリスト
        expected_sources: 期待されるソースURLリスト（正解データ）
        ndcg: NDCGスコア

    Returns:
        該当する失敗タグのリスト（完璧な検索の場合は空リスト）
    """
    tags: list[FailureTag] = []

    # 完璧な検索結果の場合はタグなし
    if pr_result.precision == 1.0 and pr_result.recall == 1.0:
        return tags

    # retrieval_miss: 関連文書が検索されなかった（Recall < 1.0 で FN > 0）
    if pr_result.false_negatives > 0:
        tags.append(FailureTag.RETRIEVAL_MISS)

    # retrieval_noise: 無関係な文書が上位に来た（Precision < 1.0 で FP > 0）
    if pr_result.false_positives > 0:
        tags.append(FailureTag.RETRIEVAL_NOISE)

    # chunk_fragmentation: 回答に必要な情報が分断された
    # 正解ソースが部分的に取得されている（Recall > 0 かつ Recall < 1.0）のに
    # NDCGが低い場合、関連情報が分断されている可能性が高い
    if (
        pr_result.recall > 0.0
        and pr_result.recall < 1.0
        and ndcg < 0.5
    ):
        tags.append(FailureTag.CHUNK_FRAGMENTATION)

    # query_mismatch: クエリと文書の表現が異なる
    # 何も取得できない、または取得したが正解が1つも含まれない場合
    if len(retrieved_sources) > 0 and pr_result.true_positives == 0 and len(expected_sources) > 0:
        tags.append(FailureTag.QUERY_MISMATCH)
    # 取得結果がゼロ（検索自体がヒットしなかった）場合もquery_mismatchの可能性
    elif len(retrieved_sources) == 0 and len(expected_sources) > 0:
        tags.append(FailureTag.QUERY_MISMATCH)

    return tags


@dataclass
class QueryEvaluationResult:
    """個別クエリの評価結果."""

    query_id: str
    query: str
    precision: float
    recall: float
    f1: float
    ndcg: float
    mrr: float
    retrieved_sources: list[str]
    expected_sources: list[str]
    negative_violations: list[str]  # 検出された禁止ソース
    failure_tags: list[FailureTag] = field(default_factory=list)


@dataclass
class EvaluationReport:
    """RAG評価レポート."""

    queries_evaluated: int
    average_precision: float
    average_recall: float
    average_f1: float
    average_ndcg: float
    average_mrr: float
    negative_source_violations: list[str]  # 違反があったクエリIDリスト
    query_results: list[QueryEvaluationResult] = field(default_factory=list)
    failure_tag_summary: dict[str, int] = field(default_factory=dict)


@dataclass
class EvaluationDatasetQuery:
    """評価データセットのクエリ."""

    id: str
    query: str
    expected_sources: list[str]
    negative_sources: list[str] = field(default_factory=list)
    expected_keywords: list[str] = field(default_factory=list)
    category: str = ""
    description: str = ""
    notes: str = ""


def load_evaluation_dataset(dataset_path: str) -> list[EvaluationDatasetQuery]:
    """評価用データセットをJSONファイルから読み込む.

    Args:
        dataset_path: データセットファイルのパス

    Returns:
        クエリリスト

    Raises:
        FileNotFoundError: ファイルが見つからない場合
        json.JSONDecodeError: JSONパースに失敗した場合
    """
    with open(dataset_path, encoding="utf-8") as f:
        data = json.load(f)

    queries: list[EvaluationDatasetQuery] = []
    for q in data.get("queries", []):
        queries.append(
            EvaluationDatasetQuery(
                id=q.get("id", ""),
                query=q.get("query", ""),
                expected_sources=q.get("expected_sources", []),
                negative_sources=q.get("negative_sources", []),
                expected_keywords=q.get("expected_keywords", []),
                category=q.get("category", ""),
                description=q.get("description", ""),
                notes=q.get("notes", ""),
            )
        )
    return queries


async def evaluate_retrieval(
    rag_service: RAGKnowledgeService,
    dataset_path: str,
    n_results: int = 5,
) -> EvaluationReport:
    """データセットを使ってRAG検索の精度を評価する.

    Args:
        rag_service: RAGKnowledgeServiceインスタンス
        dataset_path: 評価データセットファイルのパス
        n_results: 各クエリで取得する結果数

    Returns:
        EvaluationReport: 評価レポート
    """
    # データセット読み込み
    queries = load_evaluation_dataset(dataset_path)

    if not queries:
        logger.warning("No queries found in dataset: %s", dataset_path)
        return EvaluationReport(
            queries_evaluated=0,
            average_precision=0.0,
            average_recall=0.0,
            average_f1=0.0,
            average_ndcg=0.0,
            average_mrr=0.0,
            negative_source_violations=[],
            query_results=[],
        )

    query_results: list[QueryEvaluationResult] = []
    total_precision = 0.0
    total_recall = 0.0
    total_f1 = 0.0
    total_ndcg = 0.0
    total_mrr = 0.0
    negative_violations: list[str] = []

    for dataset_query in queries:
        # RAG検索実行
        try:
            result = await rag_service.retrieve(dataset_query.query, n_results=n_results)
            retrieved_sources = result.sources
        except Exception:
            logger.exception("Failed to evaluate query: %s", dataset_query.id)
            # エラー時は空の結果として処理を継続
            retrieved_sources = []

        # Precision/Recall計算
        pr_result = calculate_precision_recall(
            retrieved_sources=retrieved_sources,
            expected_sources=dataset_query.expected_sources,
        )

        # NDCG/MRR計算
        ndcg = calculate_ndcg(
            retrieved_sources=retrieved_sources,
            expected_sources=dataset_query.expected_sources,
        )
        mrr = calculate_mrr(
            retrieved_sources=retrieved_sources,
            expected_sources=dataset_query.expected_sources,
        )

        # 禁止ソースチェック
        violations = check_negative_sources(
            retrieved_sources=retrieved_sources,
            negative_sources=dataset_query.negative_sources,
        )

        if violations:
            negative_violations.append(dataset_query.id)
            logger.warning(
                "Query %s has negative source violations: %s",
                dataset_query.id,
                violations,
            )

        # 失敗タグ分類
        failure_tags = classify_failure_tags(
            pr_result=pr_result,
            retrieved_sources=retrieved_sources,
            expected_sources=dataset_query.expected_sources,
            ndcg=ndcg,
        )

        # 結果を記録
        query_result = QueryEvaluationResult(
            query_id=dataset_query.id,
            query=dataset_query.query,
            precision=pr_result.precision,
            recall=pr_result.recall,
            f1=pr_result.f1,
            ndcg=ndcg,
            mrr=mrr,
            retrieved_sources=retrieved_sources,
            expected_sources=dataset_query.expected_sources,
            negative_violations=violations,
            failure_tags=failure_tags,
        )
        query_results.append(query_result)

        total_precision += pr_result.precision
        total_recall += pr_result.recall
        total_f1 += pr_result.f1
        total_ndcg += ndcg
        total_mrr += mrr

        logger.debug(
            "Query %s: precision=%.3f, recall=%.3f, f1=%.3f, ndcg=%.3f, mrr=%.3f",
            dataset_query.id,
            pr_result.precision,
            pr_result.recall,
            pr_result.f1,
            ndcg,
            mrr,
        )

    # 平均計算
    num_queries = len(queries)
    avg_precision = total_precision / num_queries
    avg_recall = total_recall / num_queries
    avg_f1 = total_f1 / num_queries
    avg_ndcg = total_ndcg / num_queries
    avg_mrr = total_mrr / num_queries

    # 失敗タグ集計
    tag_counter: Counter[str] = Counter()
    for qr in query_results:
        for tag in qr.failure_tags:
            tag_counter[tag.value] += 1
    failure_tag_summary = dict(tag_counter)

    logger.info(
        "Evaluation complete: %d queries, avg_precision=%.3f, avg_recall=%.3f, "
        "avg_f1=%.3f, avg_ndcg=%.3f, avg_mrr=%.3f",
        num_queries,
        avg_precision,
        avg_recall,
        avg_f1,
        avg_ndcg,
        avg_mrr,
    )
    if failure_tag_summary:
        logger.info("Failure tag summary: %s", failure_tag_summary)

    return EvaluationReport(
        queries_evaluated=num_queries,
        average_precision=avg_precision,
        average_recall=avg_recall,
        average_f1=avg_f1,
        average_ndcg=avg_ndcg,
        average_mrr=avg_mrr,
        negative_source_violations=negative_violations,
        query_results=query_results,
        failure_tag_summary=failure_tag_summary,
    )

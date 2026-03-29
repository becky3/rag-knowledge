"""ハイブリッド検索モジュール

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bm25_index import BM25Index, BM25Result
    from .vector_store import RetrievalResult, VectorStore

logger = logging.getLogger(__name__)


def _generate_doc_id(source_url: str, chunk_index: int) -> str:
    """ドキュメントIDを生成する.

    VectorStoreと同じ形式（url_hash + "_" + chunk_index）で生成する。
    """
    url_hash = hashlib.sha256(source_url.encode()).hexdigest()[:16]
    return f"{url_hash}_{chunk_index}"


def min_max_normalize(scores: list[float]) -> list[float]:
    """スコアリストを[0, 1]の範囲にmin-max正規化する.

    Args:
        scores: 正規化するスコアのリスト

    Returns:
        正規化されたスコアのリスト

    Note:
        - 空リスト → 空リストを返す
        - 全要素が同じ値 → 全て1.0を返す（最大スコアとして扱う）
        - 通常 → [0, 1]に正規化
    """
    if not scores:
        return []

    min_score = min(scores)
    max_score = max(scores)

    if max_score == min_score:
        return [1.0] * len(scores)

    score_range = max_score - min_score
    return [(s - min_score) / score_range for s in scores]


def convex_combination(
    norm_vector_scores: dict[str, float],
    norm_bm25_scores: dict[str, float],
    vector_weight: float,
) -> dict[str, float]:
    """Convex Combination（凸結合）でスコアを統合する.

    score(d) = α * norm_vector(d) + (1-α) * norm_bm25(d)

    片方のみにヒットしたドキュメントは、もう片方を0として計算する。

    Args:
        norm_vector_scores: ドキュメントID → 正規化済みベクトルスコア
        norm_bm25_scores: ドキュメントID → 正規化済みBM25スコア
        vector_weight: ベクトル検索の重み α（0.0〜1.0）

    Returns:
        ドキュメントID → 統合スコアのマッピング
    """
    vector_weight = max(0.0, min(1.0, vector_weight))
    bm25_weight = 1.0 - vector_weight
    all_doc_ids = set(norm_vector_scores) | set(norm_bm25_scores)

    scores: dict[str, float] = {}
    for doc_id in all_doc_ids:
        v_score = norm_vector_scores.get(doc_id, 0.0)
        b_score = norm_bm25_scores.get(doc_id, 0.0)
        scores[doc_id] = vector_weight * v_score + bm25_weight * b_score

    return scores


@dataclass
class HybridSearchResult:
    """ハイブリッド検索結果.

    ベクトル検索とBM25検索の結果を統合したもの。
    """

    doc_id: str
    text: str
    metadata: dict[str, str | int | float | bool]
    vector_distance: float | None  # ベクトル検索での距離（Noneの場合はBM25のみでヒット）
    bm25_score: float | None  # BM25スコア（Noneの場合はベクトル検索のみでヒット）
    combined_score: float  # CCで統合されたスコア


class HybridSearchEngine:
    """ベクトル検索とBM25を組み合わせたハイブリッド検索.

    仕様: docs/specs/rag-knowledge.md
    """

    def __init__(
        self,
        vector_store: VectorStore,
        bm25_index: BM25Index,
        vector_weight: float,
    ) -> None:
        """HybridSearchEngineを初期化する.

        Args:
            vector_store: ベクトルストア
            bm25_index: BM25インデックス
            vector_weight: ベクトル検索の重み（0.0〜1.0）
        """
        self._vector_store = vector_store
        self._bm25_index = bm25_index
        self._vector_weight = max(0.0, min(1.0, vector_weight))

    async def search(
        self,
        query: str,
        n_results: int = 5,
        similarity_threshold: float | None = None,
        min_combined_score: float | None = None,
    ) -> list[HybridSearchResult]:
        """ハイブリッド検索を実行する.

        仕様: docs/specs/rag-knowledge.md

        1. ベクトル検索で候補を取得
        2. BM25検索で候補を取得
        3. 各スコアをmin-max正規化
        4. CC（Convex Combination）でスコアを統合
        5. 統合スコアでソート
        6. min_combined_score 未満の結果を除外

        Args:
            query: 検索クエリ
            n_results: 返却する結果の最大数
            similarity_threshold: ベクトル検索の類似度閾値
            min_combined_score: combined_scoreの下限閾値（None=フィルタなし）

        Returns:
            HybridSearchResultのリスト（統合スコア降順）
        """
        # 多めに取得してからCCで統合
        fetch_count = max(n_results * 3, 30)

        # ベクトル検索
        # 閾値フィルタリングは最終段階で行うため、ここでは適用しない
        vector_results = await self._vector_store.search(
            query,
            n_results=fetch_count,
            similarity_threshold=None,
        )

        # BM25検索
        bm25_results = self._bm25_index.search(query, n_results=fetch_count)

        # 検索結果が両方とも空の場合
        if not vector_results and not bm25_results:
            return []

        # CCで統合（片方が空でも正しく処理される）
        results = self._merge_with_cc(
            vector_results, bm25_results, n_results, similarity_threshold
        )

        # combined_score 下限フィルタ
        if min_combined_score is not None:
            results = [r for r in results if r.combined_score >= min_combined_score]

        return results

    def _merge_with_cc(
        self,
        vector_results: list[RetrievalResult],
        bm25_results: list[BM25Result],
        n_results: int,
        similarity_threshold: float | None,
    ) -> list[HybridSearchResult]:
        """CCで検索結果を統合する.

        1. vector distance → similarity 変換（1.0 - distance）
        2. similarity_threshold 超過分を除外
        3. vector similarity に min-max 正規化
        4. BM25 スコアに min-max 正規化
        5. CC で統合
        6. score > 0 でフィルタ → 降順ソート → 上位 n_results 返却
        """
        # --- ベクトル検索結果の処理 ---
        # doc_id → (similarity, text, metadata, distance) のマッピング
        vector_doc_data: dict[str, tuple[float, str, dict[str, str | int | float | bool], float]] = {}

        for i, vr in enumerate(vector_results):
            source_url = str(vr.metadata.get("source_id", ""))
            chunk_index = int(vr.metadata.get("chunk_index", i))
            doc_id = _generate_doc_id(source_url, chunk_index)

            similarity = 1.0 - vr.distance

            # 閾値チェック: 超過したものはベクトルスコアを0として扱う
            if similarity_threshold is not None and vr.distance > similarity_threshold:
                # 閾値超過: similarity を 0 にしてスコア計算から除外
                vector_doc_data[doc_id] = (0.0, vr.text, vr.metadata, vr.distance)
            else:
                vector_doc_data[doc_id] = (similarity, vr.text, vr.metadata, vr.distance)

        # 閾値を通過した similarity 値だけで min-max 正規化
        valid_similarities = {
            doc_id: data[0]
            for doc_id, data in vector_doc_data.items()
            if data[0] > 0.0
        }
        norm_vector_scores: dict[str, float] = {}
        if valid_similarities:
            raw_values = list(valid_similarities.values())
            normalized = min_max_normalize(raw_values)
            doc_ids = list(valid_similarities.keys())
            norm_vector_scores = dict(zip(doc_ids, normalized))

        # --- BM25結果の処理 ---
        bm25_doc_data: dict[str, tuple[float, str, dict[str, str | int | float | bool]]] = {}

        for br in bm25_results:
            bm25_source_url = self._bm25_index.get_source_url(br.doc_id)
            bm25_metadata: dict[str, str | int | float | bool] = {}
            if bm25_source_url:
                bm25_metadata["source_id"] = bm25_source_url
            bm25_doc_data[br.doc_id] = (br.score, br.text, bm25_metadata)

        # BM25 スコアの min-max 正規化
        norm_bm25_scores: dict[str, float] = {}
        if bm25_doc_data:
            bm25_raw_values = [data[0] for data in bm25_doc_data.values()]
            bm25_normalized = min_max_normalize(bm25_raw_values)
            bm25_doc_ids = list(bm25_doc_data.keys())
            norm_bm25_scores = dict(zip(bm25_doc_ids, bm25_normalized))

        # --- CC 統合 ---
        cc_scores = convex_combination(
            norm_vector_scores, norm_bm25_scores, self._vector_weight
        )

        # --- 結果構築 ---
        # 全ドキュメントのデータを統合
        all_doc_ids = set(vector_doc_data) | set(bm25_doc_data)

        results: list[HybridSearchResult] = []
        for doc_id in all_doc_ids:
            score = cc_scores.get(doc_id, 0.0)

            # score が 0 のドキュメントは除外
            if score <= 0.0:
                continue

            # テキストとメタデータの取得（ベクトル検索結果を優先）
            if doc_id in vector_doc_data:
                _, text, metadata, distance = vector_doc_data[doc_id]
                vector_distance: float | None = distance
            else:
                text = bm25_doc_data[doc_id][1]
                metadata = bm25_doc_data[doc_id][2]
                vector_distance = None

            bm25_score: float | None = (
                bm25_doc_data[doc_id][0] if doc_id in bm25_doc_data else None
            )

            results.append(
                HybridSearchResult(
                    doc_id=doc_id,
                    text=text,
                    metadata=metadata,
                    vector_distance=vector_distance,
                    bm25_score=bm25_score,
                    combined_score=score,
                )
            )

        # 統合スコアでソート
        results.sort(key=lambda x: x.combined_score, reverse=True)

        return results[:n_results]

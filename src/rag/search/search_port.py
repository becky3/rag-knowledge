"""検索 Port + Real Adapter.

仕様: docs/specs/rag-knowledge.md / docs/specs/search-response.md

ベクトル検索・ハイブリッド検索 (vector + BM25) の振る舞いを Protocol として
固定し、RealSearchAdapter が VectorStore / BM25Index を依存として実装する。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from rag.search.filters import build_bm25_filters, build_where_clause
from rag.search.models import (
    BM25SearchItem,
    RAGRetrievalResult,
    RawSearchResults,
    VectorSearchItem,
)

if TYPE_CHECKING:
    from rag.bm25_index import BM25Index
    from rag.hybrid_search import HybridSearchEngine
    from rag.vector_store import VectorStore

logger = logging.getLogger(__name__)


class SearchPort(Protocol):
    """検索 Port.

    MCP / CLI から呼ばれる検索操作を抽象化する。
    """

    async def retrieve(
        self,
        query: str,
        n_results: int = 5,
    ) -> RAGRetrievalResult:
        """関連知識を検索し、フォーマット済み結果を返す."""
        ...

    async def retrieve_raw_results(
        self,
        query: str,
        n_results: int = 5,
        source_type: str | None = None,
        filters: dict[str, str] | None = None,
    ) -> RawSearchResults:
        """ベクトル検索・BM25 検索の生結果を個別に返す."""
        ...


class RealSearchAdapter:
    """SearchPort の本番実装.

    VectorStore / BM25Index / HybridSearchEngine を依存として保持する。
    """

    def __init__(
        self,
        vector_store: VectorStore,
        *,
        bm25_index: BM25Index | None,
        similarity_threshold: float | None,
        hybrid_search_enabled: bool,
        vector_weight: float,
        min_combined_score: float | None,
        debug_log_enabled: bool,
    ) -> None:
        """RealSearchAdapter を初期化する.

        Args:
            vector_store: ベクトルストア
            bm25_index: BM25 インデックス（オプション、ハイブリッド検索用）
            similarity_threshold: 類似度閾値（None で無制限）
            hybrid_search_enabled: ハイブリッド検索の有効/無効
            vector_weight: ベクトル検索の重み（ハイブリッド検索用）
            min_combined_score: combined_score の下限閾値（None=フィルタなし）
            debug_log_enabled: RAG デバッグログの有効/無効
        """
        self._vector_store = vector_store
        self._bm25_index = bm25_index
        self._similarity_threshold = similarity_threshold
        self._hybrid_search_enabled = hybrid_search_enabled
        self._min_combined_score = min_combined_score
        self._debug_log_enabled = debug_log_enabled
        self._hybrid_search_engine: HybridSearchEngine | None = None

        if hybrid_search_enabled and bm25_index is not None:
            from rag.hybrid_search import HybridSearchEngine

            self._hybrid_search_engine = HybridSearchEngine(
                vector_store=vector_store,
                bm25_index=bm25_index,
                vector_weight=vector_weight,
            )
            logger.info("Hybrid search engine initialized")

    async def retrieve(
        self,
        query: str,
        n_results: int = 5,
    ) -> RAGRetrievalResult:
        """関連知識を検索し、結果を返す.

        ハイブリッド検索が有効な場合はベクトル + BM25、無効な場合はベクトル単独。
        """
        if self._hybrid_search_enabled and self._hybrid_search_engine is not None:
            return await self._retrieve_hybrid(query, n_results)
        return await self._retrieve_vector_only(query, n_results)

    async def _retrieve_vector_only(
        self,
        query: str,
        n_results: int,
    ) -> RAGRetrievalResult:
        """ベクトル検索のみで検索を実行する."""
        results = await self._vector_store.search(
            query,
            n_results=n_results,
            similarity_threshold=self._similarity_threshold,
        )

        if not results:
            return RAGRetrievalResult(context="", sources=[])

        if self._debug_log_enabled:
            logger.info("RAG retrieve (vector only): query=%r", query)
            for i, result in enumerate(results, start=1):
                source_url = result.metadata.get("source_id", "不明")
                logger.info(
                    "RAG result %d: distance=%.3f source=%r",
                    i, result.distance, source_url,
                )
                text_preview = (
                    result.text[:100] + "..."
                    if len(result.text) > 100
                    else result.text
                )
                logger.debug("RAG result %d text: %r", i, text_preview)
                logger.debug("RAG result %d full text: %r", i, result.text)

        formatted_parts: list[str] = []
        sources: list[str] = []
        for i, result in enumerate(results, start=1):
            source_url = str(result.metadata.get("source_id", "不明"))
            formatted_parts.append(
                f"--- 参考情報 {i} ---\n出典: {source_url}\n{result.text}",
            )
            if source_url != "不明" and source_url not in sources:
                sources.append(source_url)

        return RAGRetrievalResult(
            context="\n\n".join(formatted_parts),
            sources=sources,
        )

    async def _retrieve_hybrid(
        self,
        query: str,
        n_results: int,
    ) -> RAGRetrievalResult:
        """ハイブリッド検索（ベクトル＋BM25）で検索を実行する."""
        assert self._hybrid_search_engine is not None

        results = await self._hybrid_search_engine.search(
            query,
            n_results=n_results,
            similarity_threshold=self._similarity_threshold,
            min_combined_score=self._min_combined_score,
        )

        if not results:
            return RAGRetrievalResult(context="", sources=[])

        if self._debug_log_enabled:
            logger.info("RAG retrieve (hybrid): query=%r", query)
            for i, result in enumerate(results, start=1):
                source_url = result.metadata.get("source_id", "不明")
                logger.info(
                    "RAG result %d: combined_score=%.4f vector_dist=%s "
                    "bm25_score=%s source=%r",
                    i,
                    result.combined_score,
                    f"{result.vector_distance:.3f}"
                    if result.vector_distance is not None
                    else "N/A",
                    f"{result.bm25_score:.3f}"
                    if result.bm25_score is not None
                    else "N/A",
                    source_url,
                )
                text_preview = (
                    result.text[:100] + "..."
                    if len(result.text) > 100
                    else result.text
                )
                logger.debug("RAG result %d text: %r", i, text_preview)
                logger.debug("RAG result %d full text: %r", i, result.text)

        formatted_parts: list[str] = []
        sources: list[str] = []
        for i, result in enumerate(results, start=1):
            source_url = str(result.metadata.get("source_id", "不明"))
            formatted_parts.append(
                f"--- 参考情報 {i} ---\n出典: {source_url}\n{result.text}",
            )
            if source_url != "不明" and source_url not in sources:
                sources.append(source_url)

        return RAGRetrievalResult(
            context="\n\n".join(formatted_parts),
            sources=sources,
        )

    async def retrieve_raw_results(
        self,
        query: str,
        n_results: int = 5,
        source_type: str | None = None,
        filters: dict[str, str] | None = None,
    ) -> RawSearchResults:
        """ベクトル検索・BM25 検索の生結果を個別に返す（準 Agentic Search 用）."""
        # ベクトル検索（閾値フィルタなし: LLM が判断する）
        where = build_where_clause(source_type, filters)
        vector_results_raw = await self._vector_store.search(
            query,
            n_results=n_results,
            similarity_threshold=None,
            where=where,
        )
        vector_items: list[VectorSearchItem] = []
        for result in vector_results_raw:
            source_url = str(result.metadata.get("source_id", ""))
            chunk_index = int(result.metadata.get("chunk_index", 0))
            title = str(result.metadata.get("title", ""))
            source_type_val = str(result.metadata.get("source_type", ""))
            total_chunks = int(result.metadata.get("total_chunks", 0))
            collected_at = str(
                result.metadata.get("collected_at")
                or result.metadata.get("crawled_at", ""),
            )
            section_path = str(result.metadata.get("section_path", ""))
            url = str(result.metadata.get("custom:url", ""))
            vector_items.append(
                VectorSearchItem(
                    text=result.text,
                    source_url=source_url,
                    distance=result.distance,
                    chunk_index=chunk_index,
                    title=title,
                    source_type=source_type_val,
                    total_chunks=total_chunks,
                    collected_at=collected_at,
                    section_path=section_path,
                    url=url,
                ),
            )

        # BM25 検索
        bm25_items: list[BM25SearchItem] = []
        bm25_filters = build_bm25_filters(filters) if filters else None
        if self._bm25_index is not None:
            bm25_results_raw = self._bm25_index.search(
                query,
                n_results=n_results,
                source_type=source_type,
                filters=bm25_filters,
            )

            bm25_doc_ids = [r.doc_id for r in bm25_results_raw]
            bm25_meta_map = await self._vector_store.get_metadata_by_ids(
                bm25_doc_ids,
            )

            for bm25_result in bm25_results_raw:
                source_url = (
                    self._bm25_index.get_source_url(bm25_result.doc_id) or ""
                )
                meta = bm25_meta_map.get(bm25_result.doc_id, {})
                title = str(meta.get("title", ""))
                bm25_source_type = (
                    self._bm25_index.get_source_type(bm25_result.doc_id)
                    or str(meta.get("source_type", ""))
                )
                total_chunks = int(meta.get("total_chunks", 0))
                chunk_index = int(meta.get("chunk_index", 0))
                collected_at = str(
                    meta.get("collected_at") or meta.get("crawled_at", ""),
                )
                section_path = str(meta.get("section_path", ""))
                url = str(meta.get("custom:url", ""))
                bm25_items.append(
                    BM25SearchItem(
                        text=bm25_result.text,
                        source_url=source_url,
                        score=bm25_result.score,
                        doc_id=bm25_result.doc_id,
                        chunk_index=chunk_index,
                        title=title,
                        source_type=bm25_source_type,
                        total_chunks=total_chunks,
                        collected_at=collected_at,
                        section_path=section_path,
                        url=url,
                    ),
                )

        if self._debug_log_enabled:
            logger.info("RAG retrieve_raw: query=%r", query)
            for i, vec_item in enumerate(vector_items, start=1):
                logger.info(
                    "RAG vector result %d: distance=%.3f source=%r",
                    i, vec_item.distance, vec_item.source_url,
                )
            for i, bm25_log_item in enumerate(bm25_items, start=1):
                logger.info(
                    "RAG bm25 result %d: score=%.3f source=%r",
                    i, bm25_log_item.score, bm25_log_item.source_url,
                )

        return RawSearchResults(
            vector_results=vector_items,
            bm25_results=bm25_items,
        )

    def close(self) -> None:
        """リソースを解放する."""
        self._vector_store.close()

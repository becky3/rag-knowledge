"""RAGナレッジ管理サービス（U2 移行用 facade）.

仕様: docs/specs/rag-knowledge.md

Issue #730 の Port + Adapter 分離移行中の互換 facade。
内部処理は `search/` と `admin/` の各 Adapter に委譲する。
U2 完了時に本ファイルは削除予定（Issue #730）。
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING
from urllib.parse import urldefrag

# 新配置からの re-export (互換性維持。U2 完了で本 facade ごと削除予定)
from .admin.formatting import format_document_response, format_file_size
from .admin.models import DocumentResult
from .admin.source_management_port import (
    RealSourceManagementAdapter,
    get_document,
)
from .admin.stats_port import RealStatsAdapter, list_recent_sources
from .indexer.smart_chunking import smart_chunk
from .search.formatting import format_raw_search_results
from .search.models import (
    BM25SearchItem,
    RAGRetrievalResult,
    RawSearchResults,
    VectorSearchItem,
)
from .search.search_port import RealSearchAdapter
from .vector_store import DocumentChunk, VectorStore

if TYPE_CHECKING:
    from .bm25_index import BM25Index

__all__ = [
    "BM25SearchItem",
    "DocumentResult",
    "RAGKnowledgeService",
    "RAGRetrievalResult",
    "RawSearchResults",
    "VectorSearchItem",
    "format_document_response",
    "format_file_size",
    "format_raw_search_results",
    "get_document",
    "list_recent_sources",
    "smart_chunk",
]

logger = logging.getLogger(__name__)


class RAGKnowledgeService:
    """RAGナレッジ管理サービス（U2 移行用 facade）.

    新 Port (SearchPort / SourceManagementPort / StatsPort) への委譲 facade。
    U2 完了時に本クラスは削除予定。新規利用は各 Port を直接使用すること。

    仕様: docs/specs/rag-knowledge.md
    """

    def __init__(
        self,
        vector_store: VectorStore,
        *,
        chunk_size: int,
        chunk_overlap: int,
        similarity_threshold: float | None,
        bm25_index: BM25Index | None,
        hybrid_search_enabled: bool,
        vector_weight: float,
        min_combined_score: float | None,
        debug_log_enabled: bool,
    ) -> None:
        """RAGKnowledgeService を初期化する.

        各 Port Adapter を内部生成して委譲する。
        """
        self._vector_store = vector_store
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._bm25_index = bm25_index
        self._hybrid_search_enabled = hybrid_search_enabled

        self._search = RealSearchAdapter(
            vector_store=vector_store,
            bm25_index=bm25_index,
            similarity_threshold=similarity_threshold,
            hybrid_search_enabled=hybrid_search_enabled,
            vector_weight=vector_weight,
            min_combined_score=min_combined_score,
            debug_log_enabled=debug_log_enabled,
        )
        self._source_management = RealSourceManagementAdapter(
            vector_store=vector_store,
            bm25_index=bm25_index,
        )
        self._stats = RealStatsAdapter(vector_store=vector_store)

    def close(self) -> None:
        """リソースを解放する."""
        self._vector_store.close()

    @property
    def _hybrid_search_engine(self) -> object | None:
        """互換用 proxy（U2 移行中、テスト互換のため SearchAdapter の内部を露出）."""
        return self._search._hybrid_search_engine  # noqa: SLF001

    def _smart_chunk(self, text: str) -> list[tuple[str, str]]:
        """コンテンツタイプに応じた適切なチャンキング手法を選択する."""
        return smart_chunk(text, self._chunk_size, self._chunk_overlap)

    async def _ingest_crawled_page(
        self,
        *,
        url: str,
        title: str,
        text: str,
        crawled_at: str,
    ) -> int:
        """クロール済みページをチャンキングして保存する（評価用、init-test-db 専用）.

        Issue #739 で運用判断中の評価用ロジック。U2 完了時に cli.py の
        `init_test_db` に取り込む予定。本 facade では暫定的に維持する。
        """
        chunks = self._smart_chunk(text)

        if not chunks:
            logger.info("No chunks generated for page: %s", url)
            return 0

        normalized_url, _ = urldefrag(url)
        url_hash = hashlib.sha256(normalized_url.encode()).hexdigest()[:16]
        document_chunks = [
            DocumentChunk(
                id=f"{url_hash}_{i}",
                text=content,
                metadata={
                    "source_id": normalized_url,
                    "title": title,
                    "chunk_index": i,
                    "crawled_at": crawled_at,
                    "source_type": "web",
                    "section_path": section_path,
                },
            )
            for i, (content, section_path) in enumerate(chunks)
        ]
        new_ids = {chunk.id for chunk in document_chunks}

        count = await self._vector_store.add_documents(document_chunks)
        await self._vector_store.delete_stale_chunks(normalized_url, new_ids)

        if self._bm25_index is not None:
            bm25_docs = []
            bm25_metadata_list = []
            for chunk in document_chunks:
                bm25_docs.append((chunk.id, chunk.text, normalized_url, "web"))
                bm25_metadata_list.append(chunk.metadata)
            try:
                self._bm25_index.add_documents(
                    bm25_docs, metadata_list=bm25_metadata_list,
                )
                logger.debug("Added %d documents to BM25 index", len(bm25_docs))
            except Exception:
                logger.warning(
                    "Failed to add documents to BM25 index for %s",
                    normalized_url,
                    exc_info=True,
                )

        logger.info("Ingested page %s: %d chunks", normalized_url, count)
        return count

    # --- SearchPort 委譲 ---

    async def retrieve(
        self,
        query: str,
        n_results: int = 5,
    ) -> RAGRetrievalResult:
        """関連知識を検索し、結果を返す."""
        return await self._search.retrieve(query, n_results)

    async def retrieve_raw_results(
        self,
        query: str,
        n_results: int = 5,
        source_type: str | None = None,
        filters: dict[str, str] | None = None,
    ) -> RawSearchResults:
        """ベクトル検索・BM25 検索の生結果を個別に返す."""
        return await self._search.retrieve_raw_results(
            query=query,
            n_results=n_results,
            source_type=source_type,
            filters=filters,
        )

    # --- SourceManagementPort 委譲 ---

    async def source_exists(self, source_url: str) -> bool:
        """ソース URL に対応するチャンクが存在するか確認する."""
        return await self._source_management.source_exists(source_url)

    async def get_full_page_text(self, source_url: str) -> str:
        """ソース URL のページ全文を返す."""
        return await self._source_management.get_full_page_text(source_url)

    async def delete_source(self, source_url: str) -> int:
        """ソース URL 指定で知識を削除する."""
        return await self._source_management.delete_source(source_url)

    # --- StatsPort 委譲 ---

    async def get_stats(self) -> dict[str, object]:
        """ナレッジベース統計（総チャンク数）."""
        return await self._stats.get_stats()

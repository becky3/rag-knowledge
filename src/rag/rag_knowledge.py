"""RAGナレッジ管理サービス

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urldefrag

from .chunker import chunk_text
from .content_detector import ContentType, detect_content_type
from .converter.converter import get_converted_rel_path
from .heading_chunker import chunk_by_headings
from .table_chunker import chunk_table_data
from .vector_store import DocumentChunk, VectorStore

if TYPE_CHECKING:
    from .bm25_index import BM25Index
    from .hybrid_search import HybridSearchEngine

logger = logging.getLogger(__name__)


def smart_chunk(
    text: str, chunk_size: int, chunk_overlap: int,
) -> list[tuple[str, str]]:
    """コンテンツタイプに応じた適切なチャンキング手法を選択する.

    仕様: docs/specs/rag-knowledge.md

    - TABLE: テーブルデータとして行単位でチャンキング
    - HEADING/MIXED: 見出し単位でチャンキング
    - PROSE: 従来の段落ベースチャンキング

    Args:
        text: チャンキング対象のテキスト
        chunk_size: チャンクの最大文字数
        chunk_overlap: チャンク間のオーバーラップ文字数

    Returns:
        (content, section_path) のタプルリスト
    """
    if not text or not text.strip():
        return []

    content_type = detect_content_type(text)
    logger.debug("Detected content type: %s", content_type.value)

    if content_type == ContentType.TABLE:
        table_chunks = chunk_table_data(text, row_context_size=1, max_chunk_size=chunk_size)
        if table_chunks:
            return [(c.content, c.section_path) for c in table_chunks]
        logger.debug("Table chunking returned no results, falling back to prose")

    if content_type in (ContentType.HEADING, ContentType.MIXED):
        heading_chunks = chunk_by_headings(text, max_chunk_size=chunk_size, min_chunk_size=max(1, chunk_size // 4))
        if heading_chunks:
            return [(c.content, c.section_path) for c in heading_chunks]
        logger.debug("Heading chunking returned no results, falling back to prose")

    return [
        (c, "") for c in chunk_text(
            text, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
        )
    ]


@dataclass
class RAGRetrievalResult:
    """RAG検索結果.

    仕様: docs/specs/rag-knowledge.md

    Attributes:
        context: フォーマット済みテキスト（システムプロンプト注入用）
        sources: ユニークなソースURLリスト（表示用）
    """

    context: str
    sources: list[str]


@dataclass
class VectorSearchItem:
    """ベクトル検索の生結果アイテム.

    仕様: docs/specs/search-response.md

    Attributes:
        text: チャンクテキスト
        source_url: ソースURL（source_id）
        distance: cosine距離（0に近いほど類似）
        chunk_index: チャンクインデックス（0始まり）
        title: コンテンツのタイトル
        source_type: ソース種別
        total_chunks: 当該ソースのチャンク総数（0はレガシーデータ）
        section_path: 見出し階層（> 区切り）
    """

    text: str
    source_url: str
    distance: float
    chunk_index: int
    title: str = ""
    source_type: str = ""
    total_chunks: int = 0
    collected_at: str = ""
    section_path: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, object]:
        """JSON 出力用 dict に変換する."""
        d: dict[str, object] = {
            "text": self.text,
            "source_url": self.source_url,
            "distance": self.distance,
            "chunk_index": self.chunk_index,
            "title": self.title,
            "source_type": self.source_type,
            "total_chunks": self.total_chunks,
            "collected_at": self.collected_at,
            "section_path": self.section_path,
        }
        if self.url:
            d["url"] = self.url
        return d


@dataclass
class BM25SearchItem:
    """BM25検索の生結果アイテム.

    仕様: docs/specs/search-response.md

    Attributes:
        text: チャンクテキスト
        source_url: ソースURL（source_id）
        score: BM25スコア（高いほどキーワード一致）
        doc_id: ドキュメントID
        chunk_index: チャンクインデックス（0始まり）
        title: コンテンツのタイトル
        source_type: ソース種別
        total_chunks: 当該ソースのチャンク総数（0はレガシーデータ）
        section_path: 見出し階層（> 区切り）
        url: 元 URL（取得元の外部 URL。存在する場合のみ）
    """

    text: str
    source_url: str
    score: float
    doc_id: str
    chunk_index: int = 0
    title: str = ""
    source_type: str = ""
    total_chunks: int = 0
    collected_at: str = ""
    section_path: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, object]:
        """JSON 出力用 dict に変換する."""
        d: dict[str, object] = {
            "text": self.text,
            "source_url": self.source_url,
            "score": self.score,
            "doc_id": self.doc_id,
            "chunk_index": self.chunk_index,
            "title": self.title,
            "source_type": self.source_type,
            "total_chunks": self.total_chunks,
            "collected_at": self.collected_at,
            "section_path": self.section_path,
        }
        if self.url:
            d["url"] = self.url
        return d


@dataclass
class RawSearchResults:
    """ベクトル検索・BM25検索の生結果（準Agentic Search用）.

    仕様: docs/specs/rag-knowledge.md

    統合パイプライン（min-max正規化 + CC結合）を迂回し、
    各エンジンの生結果を個別にLLMに渡して判断を委譲する。

    Attributes:
        vector_results: ベクトル検索の生結果リスト
        bm25_results: BM25検索の生結果リスト
    """

    vector_results: list[VectorSearchItem]
    bm25_results: list[BM25SearchItem]


def format_raw_search_results(raw: RawSearchResults) -> str:
    """RawSearchResults をテキスト形式にフォーマットする.

    server.py（MCP）と cli.py（CLI）で共通使用する。
    仕様: docs/specs/search-response.md

    Returns:
        フォーマット済みテキスト。結果なしの場合は結果なしメッセージ。
    """
    if not raw.vector_results and not raw.bm25_results:
        return "該当する情報が見つかりませんでした"

    sections: list[tuple[str, Sequence[VectorSearchItem | BM25SearchItem]]] = []
    if raw.vector_results:
        sections.append(("## ベクトル検索結果 (意味的類似度)\n", raw.vector_results))
    if raw.bm25_results:
        sections.append(("## BM25 検索結果 (キーワード一致)\n", raw.bm25_results))

    parts: list[str] = []
    for header, items in sections:
        parts.append(header)
        for i, item in enumerate(items, start=1):
            # スコア行: ベクトルは distance、BM25 は score
            if isinstance(item, VectorSearchItem):
                parts.append(f"### Result {i} [distance={item.distance:.3f}]")
            else:
                parts.append(f"### Result {i} [score={item.score:.3f}]")

            chunk_pos = _format_chunk_position(item.chunk_index, item.total_chunks)
            parts.append(f"Source: {item.source_url}")
            if item.url:
                parts.append(f"URL: {item.url}")
            parts.append(f"Title: {item.title}")
            parts.append(f"Chunk: {chunk_pos}")
            parts.append(f"Type: {item.source_type}")
            if item.section_path:
                parts.append(f"Section: {item.section_path}")
            if item.collected_at:
                parts.append(f"Collected: {item.collected_at}")
            parts.append("<<content>>")
            parts.append(item.text)
            parts.append("<</content>>")
            parts.append("")

    return "\n".join(parts).rstrip()


def _format_chunk_position(chunk_index: int, total_chunks: int) -> str:
    """チャンク位置を表示用文字列にフォーマットする."""
    pos = chunk_index + 1
    if total_chunks > 0:
        return f"{pos}/{total_chunks}"
    return f"{pos}/?"


class RAGKnowledgeService:
    """RAGナレッジ管理サービス.

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
        """RAGKnowledgeServiceを初期化する.

        Args:
            vector_store: ベクトルストア
            chunk_size: チャンクの最大文字数
            chunk_overlap: チャンク間のオーバーラップ文字数
            similarity_threshold: 類似度閾値（Noneで無制限）
            bm25_index: BM25インデックス（オプション、ハイブリッド検索用）
            hybrid_search_enabled: ハイブリッド検索の有効/無効
            vector_weight: ベクトル検索の重み（ハイブリッド検索用）
            min_combined_score: combined_scoreの下限閾値（None=フィルタなし）
            debug_log_enabled: RAGデバッグログの有効/無効
        """
        self._vector_store = vector_store
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._similarity_threshold = similarity_threshold
        self._bm25_index = bm25_index
        self._hybrid_search_enabled = hybrid_search_enabled
        self._min_combined_score = min_combined_score
        self._debug_log_enabled = debug_log_enabled
        self._hybrid_search_engine: HybridSearchEngine | None = None

        # ハイブリッド検索エンジンの初期化
        if hybrid_search_enabled and bm25_index is not None:
            from .hybrid_search import HybridSearchEngine

            self._hybrid_search_engine = HybridSearchEngine(
                vector_store=vector_store,
                bm25_index=bm25_index,
                vector_weight=vector_weight,
            )
            logger.info("Hybrid search engine initialized")

    def close(self) -> None:
        """リソースを解放する."""
        self._vector_store.close()

    def _smart_chunk(self, text: str) -> list[tuple[str, str]]:
        """コンテンツタイプに応じた適切なチャンキング手法を選択する.

        仕様: docs/specs/rag-knowledge.md

        Returns:
            (content, section_path) のタプルリスト
        """
        return smart_chunk(text, self._chunk_size, self._chunk_overlap)

    async def _ingest_crawled_page(
        self,
        *,
        url: str,
        title: str,
        text: str,
        crawled_at: str,
    ) -> int:
        """クロール済みページをチャンキングして保存する.

        Args:
            url: ページURL
            title: ページタイトル
            text: ページ本文テキスト
            crawled_at: 取得日時（ISO 8601形式）

        Returns:
            保存されたチャンク数
        """
        # テキストをスマートチャンキング（コンテンツタイプに応じた手法を選択）
        chunks = self._smart_chunk(text)

        if not chunks:
            # チャンク生成失敗時は既存ナレッジを削除しない（データ喪失防止）
            logger.info("No chunks generated for page: %s", url)
            return 0

        # URLからフラグメントを除去して正規化（上流で除去済みだが防御的に再適用）
        normalized_url, _ = urldefrag(url)

        # DocumentChunkに変換
        # SHA256の先頭16文字を使用（衝突確率が十分に低い）
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

        # ベクトルストアにupsert（失敗時もデータロスを防ぐため、先に追加）
        count = await self._vector_store.add_documents(document_chunks)

        # upsert成功後、古いチャンクを削除（チャンク数が減った場合）
        await self._vector_store.delete_stale_chunks(normalized_url, new_ids)

        # BM25インデックスにも追加（ハイブリッド検索用）
        # 注: BM25は補助的機能のため、失敗してもVectorStoreの結果は維持する
        if self._bm25_index is not None:
            bm25_docs = []
            bm25_metadata_list = []
            for chunk in document_chunks:
                bm25_docs.append((chunk.id, chunk.text, normalized_url, "web"))
                bm25_metadata_list.append(chunk.metadata)
            try:
                self._bm25_index.add_documents(bm25_docs, metadata_list=bm25_metadata_list)
                logger.debug("Added %d documents to BM25 index", len(bm25_docs))
            except Exception:
                logger.warning(
                    "Failed to add documents to BM25 index for %s", normalized_url,
                    exc_info=True,
                )

        logger.info("Ingested page %s: %d chunks", normalized_url, count)
        return count

    async def retrieve(self, query: str, n_results: int = 5) -> RAGRetrievalResult:
        """関連知識を検索し、結果を返す.

        MCP クライアントから呼ばれる。結果なしの場合は空のRAGRetrievalResult。

        仕様: docs/specs/rag-knowledge.md

        Args:
            query: 検索クエリ
            n_results: 返却する結果の最大数

        Returns:
            RAGRetrievalResult: コンテキストとソース情報
        """
        # ハイブリッド検索が有効な場合
        if self._hybrid_search_enabled and self._hybrid_search_engine is not None:
            return await self._retrieve_hybrid(query, n_results)

        # 従来のベクトル検索のみ
        return await self._retrieve_vector_only(query, n_results)

    async def _retrieve_vector_only(
        self,
        query: str,
        n_results: int,
    ) -> RAGRetrievalResult:
        """ベクトル検索のみで検索を実行する（従来の動作）.

        Args:
            query: 検索クエリ
            n_results: 返却する結果の最大数

        Returns:
            RAGRetrievalResult: コンテキストとソース情報
        """
        results = await self._vector_store.search(
            query,
            n_results=n_results,
            similarity_threshold=self._similarity_threshold,
        )

        if not results:
            return RAGRetrievalResult(context="", sources=[])

        # デバッグログ出力
        if self._debug_log_enabled:
            logger.info("RAG retrieve (vector only): query=%r", query)
            for i, result in enumerate(results, start=1):
                source_url = result.metadata.get("source_id", "不明")
                logger.info(
                    "RAG result %d: distance=%.3f source=%r",
                    i,
                    result.distance,
                    source_url,
                )
                text_preview = (
                    result.text[:100] + "..." if len(result.text) > 100 else result.text
                )
                logger.debug("RAG result %d text: %r", i, text_preview)
                logger.debug("RAG result %d full text: %r", i, result.text)

        # フォーマット済みテキストを構築
        formatted_parts: list[str] = []
        sources: list[str] = []
        for i, result in enumerate(results, start=1):
            source_url = str(result.metadata.get("source_id", "不明"))
            formatted_parts.append(
                f"--- 参考情報 {i} ---\n出典: {source_url}\n{result.text}"
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
        """ハイブリッド検索（ベクトル＋BM25）で検索を実行する.

        仕様: docs/specs/rag-knowledge.md

        Args:
            query: 検索クエリ
            n_results: 返却する結果の最大数

        Returns:
            RAGRetrievalResult: コンテキストとソース情報
        """
        assert self._hybrid_search_engine is not None

        results = await self._hybrid_search_engine.search(
            query,
            n_results=n_results,
            similarity_threshold=self._similarity_threshold,
            min_combined_score=self._min_combined_score,
        )

        if not results:
            return RAGRetrievalResult(context="", sources=[])

        # デバッグログ出力
        if self._debug_log_enabled:
            logger.info("RAG retrieve (hybrid): query=%r", query)
            for i, result in enumerate(results, start=1):
                source_url = result.metadata.get("source_id", "不明")
                logger.info(
                    "RAG result %d: combined_score=%.4f vector_dist=%s bm25_score=%s source=%r",
                    i,
                    result.combined_score,
                    f"{result.vector_distance:.3f}" if result.vector_distance is not None else "N/A",
                    f"{result.bm25_score:.3f}" if result.bm25_score is not None else "N/A",
                    source_url,
                )
                text_preview = (
                    result.text[:100] + "..." if len(result.text) > 100 else result.text
                )
                logger.debug("RAG result %d text: %r", i, text_preview)
                logger.debug("RAG result %d full text: %r", i, result.text)

        # フォーマット済みテキストを構築
        formatted_parts: list[str] = []
        sources: list[str] = []
        for i, result in enumerate(results, start=1):
            source_url = str(result.metadata.get("source_id", "不明"))
            formatted_parts.append(
                f"--- 参考情報 {i} ---\n出典: {source_url}\n{result.text}"
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
        """ベクトル検索・BM25検索の生結果を個別に返す（準Agentic Search用）.

        統合パイプラインを迂回し、各エンジンの生スコアをそのままLLMに渡す。

        仕様: docs/specs/rag-knowledge.md

        Args:
            query: 検索クエリ
            n_results: 各エンジンから返却する結果の最大数
            source_type: ソース種別フィルタ（指定時はそのソース種別のみ検索対象）
            filters: カスタムメタデータフィルタ（完全一致。キーは .meta の extra フィールドに対応）

        Returns:
            RawSearchResults: ベクトル検索とBM25検索の生結果
        """
        # ベクトル検索（閾値フィルタなし: LLMが判断する）
        where = self._build_where_clause(source_type, filters)
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
            # 新形式 "collected_at"、レガシー "crawled_at" の順でフォールバック
            collected_at = str(
                result.metadata.get("collected_at")
                or result.metadata.get("crawled_at", "")
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
                )
            )

        # BM25検索
        bm25_items: list[BM25SearchItem] = []
        bm25_filters = self._build_bm25_filters(filters) if filters else None
        if self._bm25_index is not None:
            bm25_results_raw = self._bm25_index.search(
                query, n_results=n_results, source_type=source_type,
                filters=bm25_filters,
            )

            # BM25結果のメタデータをChromaDBから一括取得
            bm25_doc_ids = [r.doc_id for r in bm25_results_raw]
            bm25_meta_map = await self._vector_store.get_metadata_by_ids(
                bm25_doc_ids
            )

            for bm25_result in bm25_results_raw:
                source_url = self._bm25_index.get_source_url(bm25_result.doc_id) or ""
                meta = bm25_meta_map.get(bm25_result.doc_id, {})
                title = str(meta.get("title", ""))
                bm25_source_type = (
                    self._bm25_index.get_source_type(bm25_result.doc_id)
                    or str(meta.get("source_type", ""))
                )
                total_chunks = int(meta.get("total_chunks", 0))
                chunk_index = int(meta.get("chunk_index", 0))
                collected_at = str(
                    meta.get("collected_at") or meta.get("crawled_at", "")
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
                    )
                )

        # デバッグログ出力
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

    @staticmethod
    def _build_where_clause(
        source_type: str | None,
        filters: dict[str, str] | None,
    ) -> dict[str, Any] | None:
        """ChromaDB の where 句を構築する.

        source_type と filters を統合する。filters のキーには custom: プレフィックスを
        自動付与する。複数条件の場合は ChromaDB の $and 演算子で結合する。

        Returns:
            単一条件: {"key": value}
            複数条件: {"$and": [{"key1": value1}, {"key2": value2}]}
            条件なし: None
        """
        conditions: dict[str, str | int | float | bool] = {}
        if source_type is not None:
            conditions["source_type"] = source_type
        if filters:
            for key, value in filters.items():
                conditions[f"custom:{key}"] = value

        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions
        return {"$and": [{k: v} for k, v in conditions.items()]}

    @staticmethod
    def _build_bm25_filters(
        filters: dict[str, str],
    ) -> dict[str, str]:
        """BM25 用のフィルタ辞書を構築する.

        キーに custom: プレフィックスを付与する（ChromaDB メタデータのキーと一致させる）。
        """
        return {f"custom:{key}": value for key, value in filters.items()}

    async def source_exists(self, source_url: str) -> bool:
        """ソースURLに対応するチャンクが存在するか確認する（軽量版）.

        本文を読み出さず IDs の有無のみで判定するため、
        get_full_page_text よりも低コストで存在確認できる。

        Args:
            source_url: 確認するソースURL

        Returns:
            チャンクが 1 件以上存在すれば True
        """
        return await self._vector_store.source_exists(source_url)

    async def get_full_page_text(self, source_url: str) -> str:
        """ソースURLのページ全文を返す.

        VectorStore から全チャンクを取得し、chunk_index 昇順で結合する。

        Args:
            source_url: ソースURL

        Returns:
            ページ全文テキスト
        """
        chunks = await self._vector_store.get_chunks_by_source(source_url)
        return "\n".join(chunk.text for chunk in chunks)

    async def delete_source(self, source_url: str) -> int:
        """ソースURL指定で知識を削除.

        Args:
            source_url: 削除するソースURL

        Returns:
            削除チャンク数
        """
        # フラグメントを除去して正規化
        normalized_url, fragment = urldefrag(source_url)

        # 正規化後のURLに紐づくチャンクを削除（ベクトルストア）
        total_deleted = await self._vector_store.delete_by_source(normalized_url)

        # 後方互換: 以前はフラグメント付きURLで保存していた可能性があるため、
        # 元のURL（フラグメント付き）でも削除を試みる
        if fragment:
            legacy_deleted = await self._vector_store.delete_by_source(source_url)
            total_deleted += legacy_deleted
            logger.info(
                "Deleted %d chunks from sources: %s (normalized), %s (with fragment)",
                total_deleted,
                normalized_url,
                source_url,
            )
        else:
            logger.info("Deleted %d chunks from source: %s", total_deleted, normalized_url)

        # BM25インデックスからも削除（ハイブリッド検索用）
        # 注: BM25は補助的機能のため、失敗してもVectorStoreの結果は維持する
        if self._bm25_index is not None:
            try:
                bm25_deleted = self._bm25_index.delete_by_source(normalized_url)
                if fragment:
                    bm25_deleted += self._bm25_index.delete_by_source(source_url)
                logger.debug("Deleted %d documents from BM25 index", bm25_deleted)
            except Exception:
                logger.warning(
                    "Failed to delete from BM25 index for %s", normalized_url,
                    exc_info=True,
                )

        return total_deleted

    async def get_stats(self) -> dict[str, object]:
        """ナレッジベース統計（総チャンク数）.

        Returns:
            統計情報の辞書（total_chunks）
        """
        return await asyncio.to_thread(self._vector_store.get_stats)


# --- ドキュメント全文取得（rag_get_document 共通ロジック） ---

# テキストファイルとして扱う拡張子
_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {".md", ".txt", ".adoc", ".html", ".htm", ".json"}
)


@dataclass
class DocumentResult:
    """ドキュメント全文取得の結果.

    仕様: docs/specs/search-response.md

    Attributes:
        source_id: ソース識別子
        title: コンテンツのタイトル
        source_type: ソース種別
        format: 取得形式（"text" または "original"）
        content: ドキュメントテキスト（バイナリの場合は情報文字列）
        is_binary: バイナリファイルか否か
        error: エラーメッセージ（エラー時のみ）
    """

    source_id: str
    title: str
    source_type: str
    format: str
    content: str
    is_binary: bool = False
    error: str | None = None
    collected_at: str = ""
    extra: dict[str, object] = field(default_factory=dict)


_VALID_FORMATS: frozenset[str] = frozenset({"text", "original"})


def get_document(
    source_id: str,
    format: str,
    source_store_dir: str,
    converted_store_dir: str,
) -> DocumentResult:
    """ドキュメント全文を取得する（MCP/CLI 共通ロジック）.

    仕様: docs/specs/search-response.md

    Args:
        source_id: ソース識別子
        format: 取得形式（"text" または "original"）
        source_store_dir: source_store のルートディレクトリパス
        converted_store_dir: converted_store のルートディレクトリパス

    Returns:
        DocumentResult
    """
    # format バリデーション
    if format not in _VALID_FORMATS:
        valid = ", ".join(sorted(_VALID_FORMATS))
        return DocumentResult(
            source_id=source_id,
            title="",
            source_type="",
            format=format,
            content="",
            error=f"無効な format: {format!r}（有効値: {valid}）",
        )

    from .store.source_store import SourceStore

    with SourceStore(root_dir=Path(source_store_dir)) as store:
        # まずメタデータのみ取得（ファイル読み込みなし）
        record = store.db.get_source(source_id)

        if record is None:
            return DocumentResult(
                source_id=source_id,
                title="",
                source_type="",
                format=format,
                content="",
                error=f"ソースが見つかりません: {source_id}",
            )

        title = record.title
        source_type = record.source_type
        file_path = record.source_id

        # メタデータ取得（collected_at, extra）— record を渡して DB 再問い合わせを回避
        source_meta = store.get_metadata(source_id, record=record)
        collected_at = source_meta.collected_at if source_meta else ""
        extra = source_meta.extra if source_meta else {}

        if format == "original":
            # バイナリ判定: テキスト拡張子以外はバイナリとして扱う
            ext = PurePosixPath(file_path).suffix.lower()
            if ext not in _TEXT_EXTENSIONS:
                # バイナリの場合はファイルを読み込まず、DB の file_size と stat で対応
                mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
                file_size = record.file_size
                info = (
                    f"バイナリファイルです（MIME: {mime_type}, サイズ: {file_size:,} bytes）。\n"
                    "テキスト形式で取得するには format=text を指定してください。"
                )
                return DocumentResult(
                    source_id=source_id,
                    title=title,
                    source_type=source_type,
                    format=format,
                    content=info,
                    is_binary=True,
                    collected_at=collected_at,
                    extra=extra,
                )

            # テキストファイル: source_store から読み取り
            file_data = store.get_file(source_id)
            if file_data is None:
                return DocumentResult(
                    source_id=source_id,
                    title=title,
                    source_type=source_type,
                    format=format,
                    content="",
                    error=f"ファイルが見つかりません: {file_path}",
                )

            try:
                content = file_data.content.decode("utf-8")
            except UnicodeDecodeError:
                content = file_data.content.decode("utf-8", errors="replace")

            return DocumentResult(
                source_id=source_id,
                title=title,
                source_type=source_type,
                format=format,
                content=content,
                collected_at=collected_at,
                extra=extra,
            )

    # format == "text": converted_store から読み取り
    converted_rel = get_converted_rel_path(file_path)
    converted_path = Path(converted_store_dir) / converted_rel

    if not converted_path.exists():
        return DocumentResult(
            source_id=source_id,
            title=title,
            source_type=source_type,
            format=format,
            content="",
            error=(
                f"変換済みファイルが見つかりません: {converted_rel}\n"
                "source_store にオリジナルが存在します。"
                "format=original で取得できます。"
            ),
        )

    try:
        content = converted_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = converted_path.read_bytes().decode("utf-8", errors="replace")

    return DocumentResult(
        source_id=source_id,
        title=title,
        source_type=source_type,
        format=format,
        content=content,
        collected_at=collected_at,
        extra=extra,
    )


def format_document_response(result: DocumentResult) -> str:
    """DocumentResult をプレーンテキストレスポンスにフォーマットする.

    Args:
        result: ドキュメント取得結果

    Returns:
        フォーマット済みレスポンステキスト
    """
    if result.error:
        return f"エラー: {result.error}"

    lines = [
        f"Source: {result.source_id}",
        f"Title: {result.title}",
        f"Type: {result.source_type}",
        f"Format: {result.format}",
    ]
    if result.collected_at:
        lines.append(f"Collected: {result.collected_at}")
    for key, value in result.extra.items():
        if (
            value is None
            or (isinstance(value, str) and value == "")
            or (isinstance(value, (list, dict)) and len(value) == 0)
        ):
            continue
        lines.append(f"{key}: {value}")
    lines.append("")
    lines.append(result.content)
    return "\n".join(lines)


def list_recent_sources(
    source_store_dir: str,
    source_type: str,
    limit: int,
    ascending: bool = False,
    filters: dict[str, str] | None = None,
) -> str:
    """指定 source_type のソースを published_at でソートして一覧取得する（MCP/CLI 共通ロジック）.

    仕様: docs/specs/infrastructure/content-listing.md

    Args:
        source_store_dir: source_store のルートディレクトリパス
        source_type: ソース種別
        limit: 取得件数
        ascending: True で昇順（古い順）、False で降順（新しい順、デフォルト）
        filters: メタデータフィルタ。パース済み辞書を受け取り meta JSON カラムで絞り込む

    Returns:
        フォーマット済みテキスト
    """
    from .store.metadata_db import MetadataDB

    db_path = Path(source_store_dir) / "metadata.db"
    if not db_path.exists():
        return f"source_type: {source_type}（0件 / 全0件）"

    db = MetadataDB(db_path)
    try:
        db.initialize()
        from .store.models import SourceType

        st = cast(SourceType, source_type)
        try:
            sources = db.list_sources(source_type=st, limit=limit, ascending=ascending, filters=filters)
            total = db.count_sources_by_type(source_type=st, filters=filters)
        except ValueError as e:
            return f"エラー: {e}"
    finally:
        db.close()

    logger.info(
        "list-recent: source_type=%s, total=%d, returned=%d",
        source_type,
        total,
        len(sources),
    )
    for i, src in enumerate(sources, 1):
        logger.info(
            "list-recent result %d: source_id=%s, title=%r",
            i,
            src.source_id,
            src.title,
        )

    if not sources:
        return f"source_type: {source_type}（0件 / 全0件）"

    order_label = "古い順" if ascending else "新しい順"
    lines: list[str] = [
        f"source_type: {source_type}（{len(sources)}件 / 全{total}件, {order_label}）",
    ]

    for i, src in enumerate(sources, 1):
        lines.append("")
        lines.append(f"{i}. {src.title}")
        lines.append(f"   Source: {src.source_id}")
        lines.append(f"   Published: {src.published_at}")
        lines.append(f"   Size: {format_file_size(src.file_size)}")

    return "\n".join(lines)


def format_file_size(size_bytes: int) -> str:
    """バイト数を人間が読みやすい単位に変換する."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"

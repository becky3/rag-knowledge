"""RAGナレッジ管理サービス

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urldefrag

from .chunker import chunk_text
from .content_detector import ContentType, detect_content_type
from .converter.converter import get_converted_rel_path
from .heading_chunker import chunk_by_headings
from .ingesters.base_ingester import IngestedContent
from .table_chunker import chunk_table_data
from .vector_store import DocumentChunk, VectorStore

if TYPE_CHECKING:
    from .bm25_index import BM25Index
    from .hybrid_search import HybridSearchEngine
    from .ingesters.web_ingester import WebIngester
    from .safe_browsing import SafeBrowsingClient
    from py_common_lib.httpx import ConstrainedClient
    from .web_crawler import CrawlPreviewPage, CrawledPage, WebCrawler

logger = logging.getLogger(__name__)


def smart_chunk(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
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
        チャンクのリスト
    """
    if not text or not text.strip():
        return []

    content_type = detect_content_type(text)
    logger.debug("Detected content type: %s", content_type.value)

    if content_type == ContentType.TABLE:
        table_chunks = chunk_table_data(text)
        if table_chunks:
            return [chunk.formatted_text for chunk in table_chunks]
        logger.debug("Table chunking returned no results, falling back to prose")

    if content_type in (ContentType.HEADING, ContentType.MIXED):
        heading_chunks = chunk_by_headings(text, max_chunk_size=chunk_size)
        if heading_chunks:
            return [chunk.formatted_text for chunk in heading_chunks]
        logger.debug("Heading chunking returned no results, falling back to prose")

    return chunk_text(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)


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
    """

    text: str
    source_url: str
    distance: float
    chunk_index: int
    title: str = ""
    source_type: str = ""
    total_chunks: int = 0


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
    """

    text: str
    source_url: str
    score: float
    doc_id: str
    chunk_index: int = 0
    title: str = ""
    source_type: str = ""
    total_chunks: int = 0


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


class RAGKnowledgeService:
    """RAGナレッジ管理サービス.

    仕様: docs/specs/rag-knowledge.md
    """

    def __init__(
        self,
        vector_store: VectorStore,
        web_crawler: WebCrawler,
        *,
        chunk_size: int,
        chunk_overlap: int,
        similarity_threshold: float | None,
        safe_browsing_client: SafeBrowsingClient | None = None,
        bm25_index: BM25Index | None = None,
        hybrid_search_enabled: bool = False,
        vector_weight: float = 1.0,
        min_combined_score: float | None = None,
        debug_log_enabled: bool = False,
        web_ingester: WebIngester | None = None,
    ) -> None:
        """RAGKnowledgeServiceを初期化する.

        Args:
            vector_store: ベクトルストア
            web_crawler: Webクローラー
            chunk_size: チャンクの最大文字数
            chunk_overlap: チャンク間のオーバーラップ文字数
            similarity_threshold: 類似度閾値（Noneで無制限）
            safe_browsing_client: Safe Browsing クライアント（オプション）
            bm25_index: BM25インデックス（オプション、ハイブリッド検索用）
            hybrid_search_enabled: ハイブリッド検索の有効/無効
            vector_weight: ベクトル検索の重み（ハイブリッド検索用）
            min_combined_score: combined_scoreの下限閾値（None=フィルタなし）
            debug_log_enabled: RAGデバッグログの有効/無効
            web_ingester: WebIngester（オプション、指定時はingest_page/ingest_from_indexで使用）
        """
        self._vector_store = vector_store
        self._web_crawler = web_crawler
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._similarity_threshold = similarity_threshold
        self._safe_browsing_client = safe_browsing_client
        self._bm25_index = bm25_index
        self._hybrid_search_enabled = hybrid_search_enabled
        self._min_combined_score = min_combined_score
        self._debug_log_enabled = debug_log_enabled
        self._web_ingester = web_ingester
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

    async def crawl_preview(
        self,
        index_url: str,
        url_pattern: str = "",
    ) -> list[CrawlPreviewPage]:
        """クロール対象ページのタイトルとURLを一覧取得する（プレビュー）.

        実際の取り込み（チャンキング・ベクトル化）は行わない。

        Args:
            index_url: リンク集ページのURL
            url_pattern: 正規表現パターンでリンクをフィルタリング（任意）

        Returns:
            CrawlPreviewPage のリスト（タイトルとURL）

        Raises:
            ValueError: URL検証に失敗した場合
        """
        return await self._web_crawler.crawl_preview(index_url, url_pattern)

    async def ingest_from_index(
        self,
        index_url: str,
        url_pattern: str = "",
        progress_callback: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> dict[str, int]:
        """リンク集ページから一括取り込み.

        WebIngester が設定されている場合はそちらを経由する。

        Args:
            index_url: リンク集ページのURL
            url_pattern: 正規表現パターンでリンクをフィルタリング（任意）
            progress_callback: 進捗コールバック関数（オプション）
                引数: (crawled: int, total: int)
                crawled: クロール完了ページ数
                total: 総ページ数

        Returns:
            {"pages_crawled": N, "chunks_stored": M, "errors": E, "unsafe_urls": U}

        Raises:
            SafetyCheckError: Safe Browsing APIでfail_open=False設定時、
                API障害が発生した場合に送出される
        """
        # WebIngester 経由
        if self._web_ingester is not None:
            return await self._ingest_from_index_via_ingester(
                index_url, url_pattern, progress_callback
            )

        # レガシーパス（WebIngester 未設定時）
        return await self._ingest_from_index_legacy(
            index_url, url_pattern, progress_callback
        )

    async def _ingest_from_index_via_ingester(
        self,
        index_url: str,
        url_pattern: str,
        progress_callback: Callable[[int, int], Awaitable[None]] | None,
    ) -> dict[str, int]:
        """WebIngester 経由でリンク集ページから一括取り込みする.

        Args:
            index_url: リンク集ページのURL
            url_pattern: 正規表現パターンでリンクをフィルタリング
            progress_callback: 進捗コールバック関数

        Returns:
            {"pages_crawled": N, "chunks_stored": M, "errors": E, "unsafe_urls": U}
        """
        assert self._web_ingester is not None

        # リンク集ページから URL を発見
        urls = await self._web_ingester.discover(
            index_url, url_pattern=url_pattern
        )
        if not urls:
            logger.warning("No URLs found in index page: %s", index_url)
            return {"pages_crawled": 0, "chunks_stored": 0, "errors": 0, "unsafe_urls": 0}

        # Safe Browsing フィルタリング
        safe_urls = await self._web_ingester.filter_safe_urls(urls)
        unsafe_count = len(urls) - len(safe_urls)

        if not safe_urls:
            logger.warning("No safe URLs to crawl after Safe Browsing check")
            return {"pages_crawled": 0, "chunks_stored": 0, "errors": 0, "unsafe_urls": unsafe_count}

        # 並行クロール（進捗報告付き、Safe Browsing チェック済みなのでスキップ）
        total_urls = len(safe_urls)
        tasks = [
            asyncio.create_task(
                self._web_ingester.fetch_single(url, skip_safety_check=True)
            )
            for url in safe_urls
        ]

        contents: list[IngestedContent] = []
        completed_count = 0
        for coro in asyncio.as_completed(tasks):
            try:
                content = await coro
            except Exception:
                logger.exception("Failed to fetch content in batch")
                content = None
            completed_count += 1
            if content is not None:
                contents.append(content)

            if progress_callback:
                try:
                    await progress_callback(completed_count, total_urls)
                except Exception:
                    logger.debug("Progress callback failed", exc_info=True)

        # 各コンテンツをチャンキングして保存
        total_chunks = 0
        errors = len(safe_urls) - len(contents)

        for content in contents:
            try:
                chunks_stored = await self.ingest_content(content)
                total_chunks += chunks_stored
            except Exception:
                logger.exception("Failed to ingest content: %s", content.source_id)
                errors += 1

        logger.info(
            "Ingested from index: pages=%d, chunks=%d, errors=%d, unsafe=%d",
            len(contents),
            total_chunks,
            errors,
            unsafe_count,
        )

        return {
            "pages_crawled": len(contents),
            "chunks_stored": total_chunks,
            "errors": errors,
            "unsafe_urls": unsafe_count,
        }

    async def _ingest_from_index_legacy(
        self,
        index_url: str,
        url_pattern: str,
        progress_callback: Callable[[int, int], Awaitable[None]] | None,
    ) -> dict[str, int]:
        """レガシーパス: WebCrawler 直接使用でリンク集ページから一括取り込みする.

        Args:
            index_url: リンク集ページのURL
            url_pattern: 正規表現パターンでリンクをフィルタリング
            progress_callback: 進捗コールバック関数

        Returns:
            {"pages_crawled": N, "chunks_stored": M, "errors": E, "unsafe_urls": U}
        """
        # 共有 ConstrainedClient で操作全体のバジェットを管理
        async with self._web_crawler.create_client() as client:
            return await self._ingest_from_index_legacy_with_client(
                index_url, url_pattern, progress_callback, client
            )

    async def _ingest_from_index_legacy_with_client(
        self,
        index_url: str,
        url_pattern: str,
        progress_callback: Callable[[int, int], Awaitable[None]] | None,
        client: ConstrainedClient,
    ) -> dict[str, int]:
        """レガシーパス実装（ConstrainedClient 共有）."""
        # リンク集ページからURLリストを抽出
        urls = await self._web_crawler.crawl_index_page(
            index_url, url_pattern, client=client
        )
        if not urls:
            logger.warning("No URLs found in index page: %s", index_url)
            return {"pages_crawled": 0, "chunks_stored": 0, "errors": 0, "unsafe_urls": 0}

        # Safe Browsing チェック（有効な場合のみ）
        safe_urls = urls
        unsafe_count = 0
        if self._safe_browsing_client:
            check_results = await self._safe_browsing_client.check_urls(urls)
            safe_urls = []
            for url in urls:
                result = check_results.get(url)
                if result and not result.is_safe:
                    threat_types = [t.threat_type.value for t in result.threats]
                    logger.warning(
                        "Unsafe URL skipped: %s (threats: %s)", url, threat_types
                    )
                    unsafe_count += 1
                else:
                    safe_urls.append(url)
            if unsafe_count > 0:
                logger.info(
                    "Safe Browsing: %d URLs skipped as unsafe out of %d",
                    unsafe_count,
                    len(urls),
                )

        if not safe_urls:
            logger.warning("No safe URLs to crawl after Safe Browsing check")
            return {"pages_crawled": 0, "chunks_stored": 0, "errors": 0, "unsafe_urls": unsafe_count}

        # 複数ページを並行クロール（進捗報告付き）
        # WebCrawler.crawl_page() は内部でセマフォ制御と遅延を行う
        total_urls = len(safe_urls)
        tasks = [
            asyncio.create_task(
                self._web_crawler.crawl_page(url, client=client)
            )
            for url in safe_urls
        ]

        pages: list[CrawledPage] = []
        completed_count = 0
        for coro in asyncio.as_completed(tasks):
            page = await coro
            completed_count += 1
            if page is not None:
                pages.append(page)

            # 進捗コールバック呼び出し（エラーを隔離）
            if progress_callback:
                try:
                    await progress_callback(completed_count, total_urls)
                except Exception:
                    logger.debug("Progress callback failed", exc_info=True)

        # 各ページをチャンキングして保存
        total_chunks = 0
        # errorsはクロール失敗数（safe_urlsの数からpagesの数を引く）
        errors = len(safe_urls) - len(pages)

        for page in pages:
            try:
                chunks_stored = await self._ingest_crawled_page(page)
                total_chunks += chunks_stored
            except Exception:
                logger.exception("Failed to ingest page: %s", page.url)
                errors += 1

        logger.info(
            "Ingested from index: pages=%d, chunks=%d, errors=%d, unsafe=%d",
            len(pages),
            total_chunks,
            errors,
            unsafe_count,
        )

        return {
            "pages_crawled": len(pages),
            "chunks_stored": total_chunks,
            "errors": errors,
            "unsafe_urls": unsafe_count,
        }

    async def ingest_page(self, url: str) -> int:
        """単一ページ取り込み.

        同一URLの再取り込み時は、まず add_documents() による upsert を行い、
        その後 delete_stale_chunks() で不要になったチャンクを削除する。

        WebIngester が設定されている場合はそちらを経由する。

        Args:
            url: 取り込むページのURL

        Returns:
            チャンク数

        Raises:
            ValueError: URL検証に失敗した場合、またはURLが危険と判定された場合
        """
        # WebIngester 経由
        if self._web_ingester is not None:
            content = await self._web_ingester.fetch_single(url)
            if content is None:
                return 0
            return await self.ingest_content(content)

        # レガシーパス（WebIngester 未設定時）
        # URL検証を先に行い、失敗時は例外を投げる（ユーザーにエラー理由を伝えるため）
        # 戻り値（正規化済みURL）を以降の処理で使用
        validated_url = self._web_crawler.validate_url(url)

        # Safe Browsing チェック（有効な場合のみ）
        if self._safe_browsing_client:
            result = await self._safe_browsing_client.check_url(validated_url)
            if not result.is_safe:
                threat_types = [t.threat_type.value for t in result.threats]
                logger.warning(
                    "Unsafe URL rejected: %s (threats: %s)", validated_url, threat_types
                )
                raise ValueError(
                    f"URLが安全ではありません: {validated_url} "
                    f"(検出された脅威: {', '.join(threat_types)})"
                )

        page = await self._web_crawler.crawl_page(validated_url)
        if page is None:
            logger.warning("Failed to crawl page: %s", validated_url)
            return 0

        return await self._ingest_crawled_page(page)

    def _smart_chunk(self, text: str) -> list[str]:
        """コンテンツタイプに応じた適切なチャンキング手法を選択する.

        仕様: docs/specs/rag-knowledge.md
        """
        return smart_chunk(text, self._chunk_size, self._chunk_overlap)

    async def _ingest_crawled_page(self, page: CrawledPage) -> int:
        """クロール済みページをチャンキングして保存する.

        Args:
            page: クロール済みページ

        Returns:
            保存されたチャンク数
        """
        # テキストをスマートチャンキング（コンテンツタイプに応じた手法を選択）
        chunks = self._smart_chunk(page.text)

        if not chunks:
            # チャンク生成失敗時は既存ナレッジを削除しない（データ喪失防止）
            logger.info("No chunks generated for page: %s", page.url)
            return 0

        # URLからフラグメントを除去して正規化（上流で除去済みだが防御的に再適用）
        normalized_url, _ = urldefrag(page.url)

        # DocumentChunkに変換
        # SHA256の先頭16文字を使用（衝突確率が十分に低い）
        url_hash = hashlib.sha256(normalized_url.encode()).hexdigest()[:16]
        document_chunks = [
            DocumentChunk(
                id=f"{url_hash}_{i}",
                text=chunk,
                metadata={
                    "source_url": normalized_url,
                    "title": page.title,
                    "chunk_index": i,
                    "crawled_at": page.crawled_at,
                    "source_type": "web",
                },
            )
            for i, chunk in enumerate(chunks)
        ]
        new_ids = {chunk.id for chunk in document_chunks}

        # ベクトルストアにupsert（失敗時もデータロスを防ぐため、先に追加）
        count = await self._vector_store.add_documents(document_chunks)

        # upsert成功後、古いチャンクを削除（チャンク数が減った場合）
        await self._vector_store.delete_stale_chunks(normalized_url, new_ids)

        # BM25インデックスにも追加（ハイブリッド検索用）
        # 注: BM25は補助的機能のため、失敗してもVectorStoreの結果は維持する
        if self._bm25_index is not None:
            bm25_docs = [
                (chunk.id, chunk.text, normalized_url, "web")
                for chunk in document_chunks
            ]
            try:
                self._bm25_index.add_documents(bm25_docs)
                logger.debug("Added %d documents to BM25 index", len(bm25_docs))
            except Exception:
                logger.warning(
                    "Failed to add documents to BM25 index for %s", normalized_url,
                    exc_info=True,
                )

        logger.info("Ingested page %s: %d chunks", normalized_url, count)
        return count

    async def ingest_content(self, content: IngestedContent) -> int:
        """IngestedContent をチャンキングして保存する.

        content.skip_chunking が True の場合、チャンキングをスキップし
        テキスト全体を 1 チャンクとして保存する。

        Args:
            content: 取り込み済みコンテンツ

        Returns:
            保存されたチャンク数
        """
        # テキストをスマートチャンキング（skip_chunking 時は 1 チャンクで格納）
        if content.skip_chunking:
            chunks = [content.text] if content.text.strip() else []
        else:
            chunks = self._smart_chunk(content.text)

        if not chunks:
            logger.info("No chunks generated for content: %s", content.source_id)
            return 0

        # source_id からフラグメントを除去して正規化
        normalized_url, _ = urldefrag(content.source_id)

        # DocumentChunk に変換
        url_hash = hashlib.sha256(normalized_url.encode()).hexdigest()[:16]

        # 共通メタデータ + source_type + カスタムメタデータ（custom: プレフィックス）
        base_metadata: dict[str, str | int | float | bool] = {
            "source_url": normalized_url,
            "title": content.title,
            "crawled_at": content.ingested_at,
            "source_type": content.source_type,
        }
        for key, value in content.metadata.items():
            if isinstance(value, (str, int, float, bool)):
                base_metadata[f"custom:{key}"] = value
            else:
                base_metadata[f"custom:{key}"] = str(value)

        document_chunks = [
            DocumentChunk(
                id=f"{url_hash}_{i}",
                text=chunk,
                metadata={**base_metadata, "chunk_index": i},
            )
            for i, chunk in enumerate(chunks)
        ]
        new_ids = {chunk.id for chunk in document_chunks}

        # ベクトルストアに upsert
        count = await self._vector_store.add_documents(document_chunks)

        # upsert 成功後、古いチャンクを削除
        await self._vector_store.delete_stale_chunks(normalized_url, new_ids)

        # BM25 インデックスにも追加
        if self._bm25_index is not None:
            bm25_docs = [
                (chunk.id, chunk.text, normalized_url, content.source_type)
                for chunk in document_chunks
            ]
            try:
                self._bm25_index.add_documents(bm25_docs)
                logger.debug("Added %d documents to BM25 index", len(bm25_docs))
            except Exception:
                logger.warning(
                    "Failed to add documents to BM25 index for %s", normalized_url,
                    exc_info=True,
                )

        logger.info("Ingested content %s: %d chunks", normalized_url, count)
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
                source_url = result.metadata.get("source_url", "不明")
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
            source_url = str(result.metadata.get("source_url", "不明"))
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
                source_url = result.metadata.get("source_url", "不明")
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
            source_url = str(result.metadata.get("source_url", "不明"))
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
    ) -> RawSearchResults:
        """ベクトル検索・BM25検索の生結果を個別に返す（準Agentic Search用）.

        統合パイプラインを迂回し、各エンジンの生スコアをそのままLLMに渡す。

        仕様: docs/specs/rag-knowledge.md

        Args:
            query: 検索クエリ
            n_results: 各エンジンから返却する結果の最大数
            source_type: ソース種別フィルタ（指定時はそのソース種別のみ検索対象）

        Returns:
            RawSearchResults: ベクトル検索とBM25検索の生結果
        """
        # ベクトル検索（閾値フィルタなし: LLMが判断する）
        where: dict[str, str | int | float | bool] | None = (
            {"source_type": source_type} if source_type is not None else None
        )
        vector_results_raw = await self._vector_store.search(
            query,
            n_results=n_results,
            similarity_threshold=None,
            where=where,
        )
        vector_items: list[VectorSearchItem] = []
        for result in vector_results_raw:
            source_url = str(result.metadata.get("source_url", ""))
            chunk_index = int(result.metadata.get("chunk_index", 0))
            title = str(result.metadata.get("title", ""))
            source_type_val = str(result.metadata.get("source_type", ""))
            total_chunks = int(result.metadata.get("total_chunks", 0))
            vector_items.append(
                VectorSearchItem(
                    text=result.text,
                    source_url=source_url,
                    distance=result.distance,
                    chunk_index=chunk_index,
                    title=title,
                    source_type=source_type_val,
                    total_chunks=total_chunks,
                )
            )

        # BM25検索
        bm25_items: list[BM25SearchItem] = []
        if self._bm25_index is not None:
            bm25_results_raw = self._bm25_index.search(
                query, n_results=n_results, source_type=source_type,
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
        """ナレッジベース統計とソース一覧.

        Returns:
            統計情報の辞書（total_chunks, source_count, sources）
        """
        # VectorStore.get_stats()は同期APIを呼ぶため、to_threadでラップ
        return await asyncio.to_thread(self._vector_store.get_stats)


# --- ドキュメント全文取得（rag_get_document 共通ロジック） ---

# テキストファイルとして扱う拡張子
_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {".md", ".txt", ".adoc", ".html", ".json"}
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
        file_path = record.file_path

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
        "",
        result.content,
    ]
    return "\n".join(lines)

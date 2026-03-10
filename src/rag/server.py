"""RAG MCP サーバー

仕様: docs/specs/rag-knowledge.md
独立リポジトリとして動作する。

FastMCP を使用して 5 つの RAG ツールを公開する:
- rag_search: ナレッジベースから関連情報を検索
- rag_add: 単一ページをナレッジベースに取り込み
- rag_crawl: リンク集ページからクロール＆一括取り込み
- rag_delete: ソースURL指定でナレッジから削除
- rag_stats: ナレッジベースの統計情報を表示
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os

# ChromaDB テレメトリを無効化（import 前に設定する必要がある）
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

# RAG モジュールをモジュールレベルで import する。
# 重要: asyncio.to_thread のワーカースレッド内で import すると、
# anyio イベントループの import lock とデッドロックするため、
# サーバー起動時（メインスレッド）に全て import しておく。
# bm25s が "resource module not available on Windows" を stdout に print する
# 問題への対策として、import 時に stdout を抑制する。
from .config import ensure_utf8_streams

with contextlib.redirect_stdout(io.StringIO()):
    from .bm25_index import BM25Index
    from .config import get_settings
    from .embedding.factory import get_embedding_provider
    from .rag_knowledge import RAGKnowledgeService
    from .safe_browsing import create_safe_browsing_client
    from .vector_store import VectorStore
    from .web_crawler import WebCrawler

from mcp.server.fastmcp import FastMCP

# Windows 環境で stderr が cp932 等の場合に UTF-8 へ再構成する
# stdout は MCP stdio プロトコルが使うため変更しない
ensure_utf8_streams()

logger = logging.getLogger(__name__)

mcp = FastMCP("rag")

# --- 遅延初期化 ---

_rag_service = None
_init_lock = asyncio.Lock()


def _reset_rag_service() -> None:
    """グローバルな RAGKnowledgeService をリセットする（テスト用）."""
    global _rag_service
    _rag_service = None


async def _get_rag_service() -> RAGKnowledgeService:
    """RAGKnowledgeService を遅延初期化して返す."""
    global _rag_service
    if _rag_service is not None:
        return _rag_service

    async with _init_lock:
        # ダブルチェック: Lock 待ちの間に別タスクが初期化済みの場合
        if _rag_service is not None:
            return _rag_service

        # ChromaDB / BM25 のオブジェクト構築は同期的でブロッキング。
        # イベントループをブロックすると MCP stdio 通信が途絶えるため、
        # ワーカースレッドで実行する。
        # 注意: import は全てモジュールレベルで完了済み。ワーカースレッド内で
        # import するとデッドロックする（anyio イベントループとの import lock 競合）。
        _rag_service = await asyncio.to_thread(_build_rag_service)
        logger.info("RAG service initialized")
        return _rag_service


def _build_rag_service() -> RAGKnowledgeService:
    """RAGKnowledgeService を構築する（ワーカースレッド用、import なし）."""
    settings = get_settings()

    embedding_provider = get_embedding_provider(settings, settings.embedding_provider)

    # bm25s が stdout に直接 print する問題への対策（MCP stdio プロトコル保護）
    with contextlib.redirect_stdout(io.StringIO()):
        vector_store = VectorStore(
            embedding_provider=embedding_provider,
            persist_directory=settings.chromadb_persist_dir,
        )
        web_crawler = WebCrawler(
            max_pages=settings.rag_max_crawl_pages,
            crawl_delay=settings.rag_crawl_delay_sec,
            respect_robots_txt=settings.rag_respect_robots_txt,
            robots_txt_cache_ttl=settings.rag_robots_txt_cache_ttl,
        )
        safe_browsing_client = create_safe_browsing_client(settings)

        # BM25は準Agentic Search（生結果個別返却）で常時使用するため、
        # rag_hybrid_search_enabled に関係なく常時初期化する。
        # 既存の hybrid_search_enabled フラグと HybridSearchEngine は従来のまま保持。
        bm25_index = BM25Index(
            k1=settings.rag_bm25_k1,
            b=settings.rag_bm25_b,
            persist_dir=settings.bm25_persist_dir,
        )

    return RAGKnowledgeService(
        vector_store=vector_store,
        web_crawler=web_crawler,
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
        similarity_threshold=settings.rag_similarity_threshold,
        safe_browsing_client=safe_browsing_client,
        bm25_index=bm25_index,
        hybrid_search_enabled=settings.rag_hybrid_search_enabled,
        vector_weight=settings.rag_vector_weight,
        min_combined_score=settings.rag_min_combined_score,
        debug_log_enabled=settings.rag_debug_log_enabled,
    )


# --- MCP ツール定義 ---


@mcp.tool()
async def rag_search(query: str, n_results: int | None = None) -> str:
    """ユーザーの質問に答えるためにナレッジベースを検索する。挨拶・雑談以外の質問では必ずこのツールを最初に呼び出すこと。

    ナレッジベースにはゲーム攻略情報・技術文書等が格納されている。
    知らない用語や固有名詞を含む質問でも必ず検索すること。

    Args:
        query: 検索クエリ（ユーザーの質問からキーワードを抽出して構成する）
        n_results: 各エンジンから取得する結果数（未指定時は設定値を使用）

    Returns:
        検索結果テキスト。ベクトル検索結果とBM25検索結果をセクション分けして返す。
        ヒットしたチャンクのページ全文を返却し、同一URLの重複は参照テキストで省略する。
        結果が0件の場合は「該当する情報が見つかりませんでした」を返す。
    """
    service = await _get_rag_service()
    if n_results is None:
        n_results = get_settings().rag_retrieval_count

    raw = await service.retrieve_raw_results(query, n_results=n_results)

    if not raw.vector_results and not raw.bm25_results:
        return "該当する情報が見つかりませんでした"

    # ページ全文キャッシュ（同一URLの多重DB問い合わせ防止）
    page_cache: dict[str, str] = {}
    # URL初出記録: url -> (セクション名, Result番号)
    url_first_seen: dict[str, tuple[str, int]] = {}

    async def _get_page_text(url: str) -> str:
        if url not in page_cache:
            page_cache[url] = await service.get_full_page_text(url)
        return page_cache[url]

    parts: list[str] = []

    # ベクトル検索結果
    if raw.vector_results:
        parts.append("## ベクトル検索結果 (意味的類似度)\n")
        for i, vec_item in enumerate(raw.vector_results, start=1):
            parts.append(f"### Result {i} [distance={vec_item.distance:.3f}]")
            parts.append(f"Source: {vec_item.source_url}")

            if vec_item.source_url not in url_first_seen:
                url_first_seen[vec_item.source_url] = ("ベクトル検索結果", i)
                full_text = await _get_page_text(vec_item.source_url)
                parts.append(full_text)
            else:
                section, num = url_first_seen[vec_item.source_url]
                parts.append(
                    f"（この URL のページ全文は{section} Result {num} に掲載済み）"
                )
            parts.append("")

    # BM25検索結果
    if raw.bm25_results:
        parts.append("## BM25検索結果 (キーワード一致)\n")
        for i, bm25_item in enumerate(raw.bm25_results, start=1):
            parts.append(f"### Result {i} [score={bm25_item.score:.3f}]")
            parts.append(f"Source: {bm25_item.source_url}")

            if bm25_item.source_url not in url_first_seen:
                url_first_seen[bm25_item.source_url] = ("BM25検索結果", i)
                full_text = await _get_page_text(bm25_item.source_url)
                parts.append(full_text)
            else:
                section, num = url_first_seen[bm25_item.source_url]
                parts.append(
                    f"（この URL のページ全文は{section} Result {num} に掲載済み）"
                )
            parts.append("")

    return "\n".join(parts).rstrip()


@mcp.tool()
async def rag_add(url: str) -> str:
    """単一ページをナレッジベースに取り込む.

    Args:
        url: 取り込むページのURL

    Returns:
        取り込み結果のメッセージ
    """
    service = await _get_rag_service()
    try:
        chunks = await service.ingest_page(url)
        if chunks <= 0:
            return f"エラー: ページの取り込みに失敗しました。URL: {url}"
        return f"ページを取り込みました: {url} ({chunks}チャンク)"
    except ValueError as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to add page: %s", url)
        return f"エラー: ページの取り込みに失敗しました。URL: {url}"


@mcp.tool()
async def rag_crawl(url: str, pattern: str = "") -> str:
    """リンク集ページからクロール＆一括取り込み.

    Args:
        url: リンク集ページのURL
        pattern: URLフィルタリング用の正規表現パターン（任意）

    Returns:
        クロール結果のサマリー
    """
    service = await _get_rag_service()
    try:
        result = await service.ingest_from_index(url, url_pattern=pattern)
        pages = result["pages_crawled"]
        chunks = result["chunks_stored"]
        errors = result["errors"]
        return f"完了: {pages}ページ / {chunks}チャンク / エラー: {errors}件"
    except ValueError as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to crawl: %s", url)
        return f"エラー: クロールに失敗しました。URL: {url}"


@mcp.tool()
async def rag_delete(url: str) -> str:
    """ソースURL指定でナレッジから削除.

    Args:
        url: 削除するソースURL

    Returns:
        削除結果のメッセージ
    """
    service = await _get_rag_service()
    try:
        count = await service.delete_source(url)
        if count == 0:
            return f"該当するソースが見つかりませんでした: {url}"
        return f"削除しました: {url} ({count}チャンク)"
    except Exception:
        logger.exception("Failed to delete: %s", url)
        return f"エラー: 削除に失敗しました。URL: {url}"


@mcp.tool()
async def rag_stats() -> str:
    """ナレッジベースの統計情報を表示.

    Returns:
        統計情報のテキスト
    """
    service = await _get_rag_service()
    try:
        stats = await service.get_stats()
        total_chunks = stats.get("total_chunks", 0)
        source_count = stats.get("source_count", 0)
        return f"ナレッジベース統計:\n  総チャンク数: {total_chunks}\n  ソースURL数: {source_count}"
    except Exception:
        logger.exception("Failed to get stats")
        return "エラー: 統計情報の取得に失敗しました。"


def _configure_and_run() -> None:
    """トランスポート設定に基づいて MCP サーバーを起動する."""
    settings = get_settings()
    transport = settings.rag_transport

    if transport == "http":
        mcp.settings.host = settings.rag_http_host
        mcp.settings.port = settings.rag_http_port
        if not settings.rag_dns_rebinding_protection:
            if mcp.settings.transport_security is not None:
                mcp.settings.transport_security.enable_dns_rebinding_protection = (
                    False
                )
            else:
                logger.warning(
                    "transport_security is None; "
                    "cannot disable DNS rebinding protection"
                )
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    _configure_and_run()

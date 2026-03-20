"""RAG MCP サーバー

仕様: docs/specs/rag-knowledge.md, docs/specs/search-response.md
独立リポジトリとして動作する。

FastMCP を使用して 12 個の RAG ツールを公開する:
- rag_search: ナレッジベース検索（チャンク単位返却）
- rag_get_document: ソース全文取得
- rag_add: 単一ページをナレッジベースに取り込み
- rag_crawl: リンク集ページからクロール＆一括取り込み
- rag_crawl_preview: クロール対象ページのプレビュー（タイトル・URL一覧）
- rag_crawl_zenn: Zenn 記事の一括取り込み
- rag_crawl_bluesky: BlueSky 投稿の一括取り込み
- rag_add_document: ドキュメントファイルをナレッジベースに取り込み
- rag_crawl_documents: ディレクトリ内ドキュメントを一括取り込み
- rag_delete: ソースURL指定でナレッジから論理削除
- rag_rebuild: ナレッジベースの再構築
- rag_stats: ナレッジベースの統計情報を表示
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

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
    from .ingesters.document_ingester import PdfBackendConfig
    from .rag_knowledge import (
        RAGKnowledgeService,
        format_document_response,
        get_document,
    )
    from .vector_store import VectorStore
    from .web_crawler import WebCrawler

    # パイプライン関連
    from .converter import Converter
    from .indexer import Indexer
    from .pipeline.controller import PipelineController
    from .pipeline.ingesters._common import IngestResult
    from .pipeline.ingesters.bluesky import BlueskyIngester as PipelineBlueskyIngester
    from .pipeline.ingesters.local import LocalIngester as PipelineLocalIngester
    from .pipeline.ingesters.web import WebIngester as PipelineWebIngester
    from .pipeline.ingesters.zenn import ZennIngester as PipelineZennIngester
    from .pipeline.models import PipelineMode, PipelineSummary, detect_source_type
    from .store.metadata_db import MetadataDB
    from .store.models import NULL_COMMIT_HASH, SourceType
    from .store.source_store import SourceStore

from py_common_lib.httpx import ConstrainedClient  # safety:allowed
from py_common_lib.secrets import SecretNotFoundError, SecretStoreError, get_secret

from mcp.server.fastmcp import FastMCP

# Windows 環境で stderr が cp932 等の場合に UTF-8 へ再構成する
# stdout は MCP stdio プロトコルが使うため変更しない
ensure_utf8_streams()

logger = logging.getLogger(__name__)

mcp = FastMCP("rag")

# シークレットサービス名
_SECRET_SERVICE_NAME = "rag-knowledge"

# --- 遅延初期化: RAGKnowledgeService（検索用） ---

_rag_service: RAGKnowledgeService | None = None
_init_lock = asyncio.Lock()


def _reset_rag_service() -> None:
    """グローバルな RAGKnowledgeService をリセットする."""
    global _rag_service
    _rag_service = None


async def _get_rag_service() -> RAGKnowledgeService:
    """RAGKnowledgeService を遅延初期化して返す."""
    global _rag_service
    if _rag_service is not None:
        return _rag_service

    async with _init_lock:
        if _rag_service is not None:
            return _rag_service

        _rag_service = await asyncio.to_thread(_build_rag_service)
        logger.info("RAG service initialized")
        return _rag_service


def _build_rag_service() -> RAGKnowledgeService:
    """RAGKnowledgeService を構築する（ワーカースレッド用、import なし）."""
    settings = get_settings()

    embedding_provider = get_embedding_provider(settings, settings.embedding_provider)

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
        safe_browsing_client=None,
        bm25_index=bm25_index,
        hybrid_search_enabled=settings.rag_hybrid_search_enabled,
        vector_weight=settings.rag_vector_weight,
        min_combined_score=settings.rag_min_combined_score,
        debug_log_enabled=settings.rag_debug_log_enabled,
        web_ingester=None,
    )


# --- 遅延初期化: PipelineController（取り込み・再構築用） ---

_pipeline_controller: PipelineController | None = None
_pipeline_lock = asyncio.Lock()


def _reset_pipeline_controller() -> None:
    """グローバルな PipelineController をリセットする."""
    global _pipeline_controller
    _pipeline_controller = None


async def _get_pipeline_controller() -> PipelineController:
    """PipelineController を遅延初期化して返す."""
    global _pipeline_controller
    if _pipeline_controller is not None:
        return _pipeline_controller

    async with _pipeline_lock:
        if _pipeline_controller is not None:
            return _pipeline_controller

        _pipeline_controller = await asyncio.to_thread(_build_pipeline_controller)
        logger.info("Pipeline controller initialized")
        return _pipeline_controller


def _build_pipeline_controller() -> PipelineController:
    """パイプライン制御コントローラを構築する（ワーカースレッド用）."""
    settings = get_settings()
    source_store_dir = Path(settings.source_store_dir)
    converted_store_dir = Path(settings.converted_store_dir)
    converted_store_dir.mkdir(parents=True, exist_ok=True)

    source_store = SourceStore(source_store_dir)
    source_store.initialize()

    pdf_config = PdfBackendConfig(
        backend=settings.rag_pdf_backend,
        mineru_mfd_conf_thres=settings.rag_pdf_mineru_mfd_conf_thres,
        quality_ufffd_threshold=settings.rag_pdf_quality_ufffd_threshold,
        quality_greek_threshold=settings.rag_pdf_quality_greek_threshold,
        quality_cjk_min_threshold=settings.rag_pdf_quality_cjk_min_threshold,
        quality_min_chars_per_page=settings.rag_pdf_quality_min_chars_per_page,
        quality_sample_pages=settings.rag_pdf_quality_sample_pages,
    )
    converter = Converter(regen_option="force", pdf_config=pdf_config)

    embedding_provider = get_embedding_provider(settings, settings.embedding_provider)

    with contextlib.redirect_stdout(io.StringIO()):
        vector_store = VectorStore(
            embedding_provider=embedding_provider,
            persist_directory=settings.chromadb_persist_dir,
        )
        bm25_index = BM25Index(
            k1=settings.rag_bm25_k1,
            b=settings.rag_bm25_b,
            persist_dir=settings.bm25_persist_dir,
        )

    indexer = Indexer(
        vector_store=vector_store,
        bm25_index=bm25_index,
        metadata_db=source_store.db,
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
    )

    return PipelineController(
        source_store=source_store,
        converted_store_dir=converted_store_dir,
        converter=converter,
        indexer=indexer,
    )


_safe_browsing_api_key_cache: str | None = None


def _get_safe_browsing_api_key() -> str:
    """Safe Browsing API キーを取得する（プロセス内キャッシュ）.

    Returns:
        API キー。取得できない場合は空文字列。
    """
    global _safe_browsing_api_key_cache
    if _safe_browsing_api_key_cache is not None:
        return _safe_browsing_api_key_cache

    settings = get_settings()
    if not settings.rag_url_safety_check:
        _safe_browsing_api_key_cache = ""
        return ""
    try:
        key = get_secret(
            "GOOGLE_SAFE_BROWSING_API_KEY", service=_SECRET_SERVICE_NAME,
        )
        _safe_browsing_api_key_cache = key or ""
    except (SecretNotFoundError, SecretStoreError):
        logger.warning("Safe Browsing API key not available")
        _safe_browsing_api_key_cache = ""
    return _safe_browsing_api_key_cache


# --- レスポンスフォーマッタ ---


def _format_ingest_response(
    ingest_result: IngestResult,
    pipeline_summary: PipelineSummary | None,
    *,
    context: str = "",
) -> str:
    """IngestResult + PipelineSummary を統合レスポンスに変換する."""
    parts = [ingest_result.summary(context=context)]
    if pipeline_summary is not None:
        parts.append(f"パイプライン: {pipeline_summary.processed}件処理")
        if pipeline_summary.errors:
            parts.append(f"パイプラインエラー: {len(pipeline_summary.errors)}件")
    return " / ".join(parts)


# --- ファクトリヘルパー ---


def _create_web_ingester(source_store: SourceStore) -> PipelineWebIngester:
    """設定に基づいて PipelineWebIngester を生成する."""
    settings = get_settings()
    return PipelineWebIngester(
        source_store,
        max_crawl_pages=settings.rag_max_crawl_pages,
        crawl_request_timeout=settings.rag_crawl_request_timeout,
        respect_robots_txt=settings.rag_respect_robots_txt,
        robots_txt_cache_ttl=settings.rag_robots_txt_cache_ttl,
        url_safety_check=settings.rag_url_safety_check,
        url_safety_cache_ttl=settings.rag_url_safety_cache_ttl,
        url_safety_fail_open=settings.rag_url_safety_fail_open,
        url_safety_timeout=settings.rag_url_safety_timeout,
    )


def _get_supported_extensions() -> list[str]:
    """設定からサポート拡張子リストを取得する."""
    settings = get_settings()
    return [
        ext.strip() if ext.strip().startswith(".") else f".{ext.strip()}"
        for ext in settings.rag_document_supported_extensions.split(",")
        if ext.strip()
    ]


# --- MCP ツール定義 ---

_VALID_SOURCE_TYPES: frozenset[str] = frozenset({"web", "zenn", "bluesky", "local"})


def _format_chunk_position(chunk_index: int, total_chunks: int) -> str:
    """チャンク位置を表示用文字列にフォーマットする.

    Args:
        chunk_index: 0始まりチャンクインデックス
        total_chunks: チャンク総数（0はレガシーデータ＝不明）

    Returns:
        "3/15" 形式、total_chunks 不明時は "3/?"
    """
    pos = chunk_index + 1
    if total_chunks > 0:
        return f"{pos}/{total_chunks}"
    return f"{pos}/?"


@mcp.tool()
async def rag_search(
    query: str,
    n_results: int | None = None,
    source_type: str | None = None,
) -> str:
    """[rag-knowledge] RAG search - ナレッジベース検索。挨拶・雑談以外の質問では必ずこのツールを最初に呼び出すこと。

    knowledge base, vector search, BM25, retrieval-augmented generation.
    ナレッジベースにはゲーム攻略情報・技術文書等が格納されている。
    蓄積データの詳細は rag_stats ツールで確認できる。
    知らない用語や固有名詞を含む質問でも必ず検索すること。

    Args:
        query: 検索クエリ（ユーザーの質問からキーワードを抽出して構成する）
        n_results: 各エンジンから取得する結果数（未指定時は設定値を使用）
        source_type: ソース種別フィルタ（"web", "zenn", "bluesky", "local"）。
            指定時はそのソース種別のチャンクのみを検索対象とする。未指定時は全種別を検索。

    Returns:
        検索結果テキスト。ベクトル検索結果とBM25検索結果をセクション分けして返す。
        各結果はチャンク単位で返却される。
        詳細が必要な場合は rag_get_document でソースの全文を取得できる。
        結果が0件の場合は「該当する情報が見つかりませんでした」を返す。
    """
    if source_type is not None and source_type not in _VALID_SOURCE_TYPES:
        valid = ", ".join(sorted(_VALID_SOURCE_TYPES))
        return f"無効な source_type: {source_type!r}（有効値: {valid}）"

    service = await _get_rag_service()
    if n_results is None:
        n_results = get_settings().rag_retrieval_count

    raw = await service.retrieve_raw_results(
        query, n_results=n_results, source_type=source_type,
    )

    if not raw.vector_results and not raw.bm25_results:
        return "該当する情報が見つかりませんでした"

    parts: list[str] = []

    # ベクトル検索結果
    if raw.vector_results:
        parts.append("## ベクトル検索結果 (意味的類似度)\n")
        for i, item in enumerate(raw.vector_results, start=1):
            chunk_pos = _format_chunk_position(item.chunk_index, item.total_chunks)
            parts.append(f"### Result {i} [distance={item.distance:.3f}]")
            parts.append(f"Source: {item.source_url}")
            parts.append(f"Title: {item.title}")
            parts.append(f"Chunk: {chunk_pos}")
            parts.append(f"Type: {item.source_type}")
            parts.append("")
            parts.append(item.text)
            parts.append("")

    # BM25検索結果
    if raw.bm25_results:
        parts.append("## BM25 検索結果 (キーワード一致)\n")
        for i, bm25_item in enumerate(raw.bm25_results, start=1):
            chunk_pos = _format_chunk_position(bm25_item.chunk_index, bm25_item.total_chunks)
            parts.append(f"### Result {i} [score={bm25_item.score:.3f}]")
            parts.append(f"Source: {bm25_item.source_url}")
            parts.append(f"Title: {bm25_item.title}")
            parts.append(f"Chunk: {chunk_pos}")
            parts.append(f"Type: {bm25_item.source_type}")
            parts.append("")
            parts.append(bm25_item.text)
            parts.append("")

    return "\n".join(parts).rstrip()


_VALID_DOCUMENT_FORMATS: frozenset[str] = frozenset({"text", "original"})


@mcp.tool()
async def rag_get_document(
    source_id: str,
    format: str = "text",
) -> str:
    """[rag-knowledge] RAG get document - ソース全文取得。rag_search で見つけたソースの全文を取得する。

    knowledge base, full text, document retrieval, get source.
    rag_search の結果に含まれる Source 値をそのまま source_id に指定する。
    format=text で変換済みテキスト、format=original でオリジナルデータを取得できる。

    Args:
        source_id: ソース識別子（rag_search の Source 値）
        format: 取得形式。"text"（変換済みテキスト、デフォルト）または "original"（オリジナル）

    Returns:
        メタデータヘッダー + ドキュメント全文。
        大規模ドキュメントはトランケーションされる場合がある。
        その場合は CLI の --output オプションで全文取得可能。
    """
    if format not in _VALID_DOCUMENT_FORMATS:
        valid = ", ".join(sorted(_VALID_DOCUMENT_FORMATS))
        return f"無効な format: {format!r}（有効値: {valid}）"

    settings = get_settings()
    result = await asyncio.to_thread(
        get_document,
        source_id=source_id,
        format=format,
        source_store_dir=settings.source_store_dir,
        converted_store_dir=settings.converted_store_dir,
    )

    response = format_document_response(result)

    # MCP 経由の場合、rag_max_response_chars でトランケーション（通知文込みで上限内に収める）
    max_chars = settings.rag_max_response_chars
    if max_chars is not None and not result.error and len(response) > max_chars:
        truncation_notice = (
            "\n\n…（レスポンスが上限の{:,}文字を超えたためトランケートされました。"
            "CLI の --output オプションで全文取得できます）"
        ).format(max_chars)
        truncate_at = max(0, max_chars - len(truncation_notice))
        response = response[:truncate_at] + truncation_notice

    return response


@mcp.tool()
async def rag_add(url: str) -> str:
    """[rag-knowledge] RAG add - 単一ページをナレッジベースに取り込む.

    knowledge base, ingest, web page, crawl single URL.

    Args:
        url: 取り込むページのURL

    Returns:
        取り込み結果のメッセージ
    """
    controller = await _get_pipeline_controller()
    web_ingester = _create_web_ingester(controller.source_store)
    settings = get_settings()

    try:
        api_key = _get_safe_browsing_api_key()
        async with ConstrainedClient(
            request_timeout=settings.rag_crawl_request_timeout,
            request_interval=settings.rag_crawl_delay_sec,
        ) as client:
            ingest_result = await web_ingester.add(
                url, client=client, safe_browsing_api_key=api_key,
            )

        if ingest_result.placed == 0 and ingest_result.errors == 0:
            return f"取り込み対象がありませんでした: {url}"

        pipeline_summary = await asyncio.to_thread(
            controller.ingest_and_index, f"ingest(web): add {url}",
        )
        _reset_rag_service()

        return _format_ingest_response(ingest_result, pipeline_summary, context=url)
    except ValueError as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to add page: %s", url)
        return f"エラー: ページの取り込みに失敗しました。URL: {url}"


@mcp.tool()
async def rag_crawl(url: str, pattern: str = "") -> str:
    """[rag-knowledge] RAG crawl - リンク集ページからクロール＆一括取り込み.

    knowledge base, bulk ingest, web crawl, link index.

    Args:
        url: リンク集ページのURL
        pattern: URLフィルタリング用の正規表現パターン（任意）

    Returns:
        クロール結果のサマリー
    """
    controller = await _get_pipeline_controller()
    web_ingester = _create_web_ingester(controller.source_store)
    settings = get_settings()

    try:
        api_key = _get_safe_browsing_api_key()
        async with ConstrainedClient(
            request_timeout=settings.rag_crawl_request_timeout,
            request_interval=settings.rag_crawl_delay_sec,
        ) as client:
            ingest_result = await web_ingester.crawl(
                url, pattern=pattern, client=client,
                safe_browsing_api_key=api_key,
            )

        if ingest_result.placed == 0 and ingest_result.errors == 0:
            return f"対象ページが見つかりませんでした: {url}"

        pipeline_summary = await asyncio.to_thread(
            controller.ingest_and_index, f"ingest(web): crawl {url}",
        )
        _reset_rag_service()

        return _format_ingest_response(ingest_result, pipeline_summary, context=url)
    except ValueError as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to crawl: %s", url)
        return f"エラー: クロールに失敗しました。URL: {url}"


@mcp.tool()
async def rag_crawl_preview(url: str, pattern: str = "") -> str:
    """[rag-knowledge] RAG crawl preview - クロール対象ページのプレビュー.

    knowledge base, crawl preview, dry run, link list.
    実際の取り込み（チャンキング・ベクトル化）は行わず、
    クロール対象となるページのタイトルとURLの一覧を返す。

    Args:
        url: リンク集ページのURL
        pattern: URLフィルタリング用の正規表現パターン（任意）

    Returns:
        クロール対象ページの一覧テキスト
    """
    settings = get_settings()
    # crawl_preview は配置を行わないため、PipelineController の重い初期化を避ける
    source_store_dir = Path(settings.source_store_dir)
    source_store_dir.mkdir(parents=True, exist_ok=True)
    source_store = SourceStore(source_store_dir)
    web_ingester = _create_web_ingester(source_store)

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_crawl_request_timeout,
            request_interval=settings.rag_crawl_delay_sec,
        ) as client:
            pages = await web_ingester.crawl_preview(
                url, pattern=pattern, client=client,
            )

        if not pages:
            return "対象ページが見つかりませんでした"

        lines: list[str] = [f"クロール対象: {len(pages)}ページ", ""]
        for i, page in enumerate(pages, start=1):
            title = page.get("title", "") or "(タイトル取得不可)"
            page_url = page.get("url", "")
            lines.append(f"{i}. {title}")
            lines.append(f"   {page_url}")
        return "\n".join(lines)
    except ValueError as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to preview crawl: %s", url)
        return f"エラー: プレビューに失敗しました。URL: {url}"


_VALID_ZENN_CONTENT_TYPES: frozenset[str] = frozenset({"articles", "scraps", "all"})


@mcp.tool()
async def rag_crawl_zenn(
    username: str,
    max_articles: int | None = None,
    content_type: str = "all",
) -> str:
    """[rag-knowledge] RAG crawl Zenn - Zenn コンテンツを API 経由で取得し一括取り込み.

    knowledge base, Zenn, ingest, articles, scraps, API.
    指定ユーザーの Zenn 記事・スクラップを API 経由で取得し、ナレッジベースに取り込む。
    同一コンテンツの再取り込み時は既存の知識を最新に置き換える。

    Args:
        username: Zenn ユーザー名
        max_articles: 取得する最大コンテンツ数（未指定時は設定値を使用、許容範囲: 1〜100）
        content_type: 取得対象（"articles": 記事のみ、"scraps": スクラップのみ、"all": 両方。デフォルト: "all"）

    Returns:
        取り込み結果のサマリーテキスト
    """
    settings = get_settings()

    if max_articles is None:
        max_articles = settings.rag_zenn_max_articles

    if not isinstance(max_articles, int) or isinstance(max_articles, bool):
        return f"エラー: max_articles は整数で指定してください（入力値: {max_articles!r}）"
    if max_articles <= 0:
        return f"エラー: max_articles は正の整数で指定してください（入力値: {max_articles}）"

    if not username or not username.strip():
        return "エラー: username を指定してください"

    if content_type not in _VALID_ZENN_CONTENT_TYPES:
        valid = ", ".join(sorted(_VALID_ZENN_CONTENT_TYPES))
        return f"エラー: 無効な content_type: {content_type!r}（有効値: {valid}）"

    controller = await _get_pipeline_controller()
    zenn_ingester = PipelineZennIngester(
        controller.source_store,
        max_articles=max_articles,
    )

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_zenn_request_timeout,
            request_interval=settings.rag_zenn_request_interval,
        ) as client:
            ingest_result = await zenn_ingester.crawl_zenn(
                username.strip(),
                max_articles=max_articles,
                content_type=content_type,
                client=client,
            )

        if ingest_result.placed == 0 and ingest_result.errors == 0:
            return f"コンテンツが見つかりませんでした（ユーザー: {username}）"

        pipeline_summary = await asyncio.to_thread(
            controller.ingest_and_index,
            f"ingest(zenn): {username.strip()}",
        )
        _reset_rag_service()

        return _format_ingest_response(
            ingest_result, pipeline_summary, context=f"ユーザー: {username}",
        )
    except (ValueError, TypeError) as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to crawl Zenn articles for user: %s", username)
        return f"エラー: Zenn 記事の取り込みに失敗しました（ユーザー: {username}）"


@mcp.tool()
async def rag_crawl_bluesky(
    handle: str,
    max_posts: int | None = None,
    include_reposts: bool | None = None,
) -> str:
    """[rag-knowledge] RAG crawl BlueSky - BlueSky 投稿を AT Protocol API 経由で取得し一括取り込み.

    knowledge base, BlueSky, Bluesky, ingest, posts, AT Protocol.
    指定ユーザーの BlueSky 投稿を AT Protocol API 経由で取得し、ナレッジベースに取り込む。
    BlueSky は投稿編集不可のため、既存の投稿はスキップする（上書き不要）。

    Args:
        handle: BlueSky ハンドル（例: user.bsky.social）。DID 形式は不可
        max_posts: 取得する最大投稿数（タイムライン全体に適用、未指定時は設定値を使用、許容範囲: 1〜1000）
        include_reposts: タイムラインにリポストを含めるか（未指定時は設定値を使用）

    Returns:
        取り込み結果のサマリーテキスト
    """
    settings = get_settings()

    if max_posts is None:
        max_posts = settings.rag_bluesky_max_posts
    if include_reposts is None:
        include_reposts = settings.rag_bluesky_include_reposts

    if not isinstance(max_posts, int) or isinstance(max_posts, bool):
        return f"エラー: max_posts は整数で指定してください（入力値: {max_posts!r}）"
    if max_posts <= 0:
        return f"エラー: max_posts は正の整数で指定してください（入力値: {max_posts}）"

    if not handle or not handle.strip():
        return "エラー: handle を指定してください"

    handle = handle.strip()
    if handle.startswith("did:"):
        return (
            f"エラー: DID 形式は使用できません: {handle!r}。"
            "ハンドル（例: user.bsky.social）を指定してください"
        )

    controller = await _get_pipeline_controller()
    bluesky_ingester = PipelineBlueskyIngester(
        controller.source_store,
        appview_url=settings.rag_bluesky_appview_url,
        max_posts=max_posts,
        include_reposts=include_reposts,
    )

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_bluesky_request_timeout,
            request_interval=settings.rag_bluesky_request_interval,
        ) as client:
            ingest_result = await bluesky_ingester.crawl_bluesky(
                handle,
                max_posts=max_posts,
                include_reposts=include_reposts,
                client=client,
            )

        if ingest_result.placed == 0 and ingest_result.errors == 0:
            return f"投稿が見つかりませんでした（ハンドル: {handle}）"

        pipeline_summary = await asyncio.to_thread(
            controller.ingest_and_index,
            f"ingest(bluesky): {handle}",
        )
        _reset_rag_service()

        return _format_ingest_response(
            ingest_result, pipeline_summary, context=f"ハンドル: {handle}",
        )
    except (ValueError, TypeError) as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception(
            "Failed to crawl BlueSky posts for handle: %s", handle
        )
        return f"エラー: BlueSky 投稿の取り込みに失敗しました（ハンドル: {handle}）"


_VALID_UPLOAD_MODES: frozenset[str] = frozenset({"fail", "replace"})


@mcp.tool()
async def rag_add_document(
    file_path: str,
    upload_mode: str = "fail",
) -> str:
    """[rag-knowledge] RAG add document - ドキュメントファイルをナレッジベースに取り込む.

    knowledge base, ingest, document, file, text.
    ドキュメントファイル（Markdown、テキスト、PDF、AsciiDoc）を読み取り、
    ナレッジベースに取り込む。
    stdio モード専用。HTTP モードでは無効。

    Args:
        file_path: 取り込み対象ファイルのパス（絶対パスまたは相対パス）
        upload_mode: 同名ファイル存在時の動作。"fail"（エラー、デフォルト）または "replace"（上書き）

    Returns:
        取り込み結果のメッセージ
    """
    if get_settings().rag_transport == "http":
        return "エラー: rag_add_document は HTTP モードでは無効です（セキュリティ上の制約）"

    if upload_mode not in _VALID_UPLOAD_MODES:
        valid = ", ".join(sorted(_VALID_UPLOAD_MODES))
        return f"エラー: 無効な upload_mode: {upload_mode!r}（有効値: {valid}）"

    controller = await _get_pipeline_controller()
    local_ingester = PipelineLocalIngester(
        controller.source_store,
        supported_extensions=_get_supported_extensions(),
    )

    try:
        ingest_result = await asyncio.to_thread(
            local_ingester.add_document, file_path,
            upload_mode=upload_mode,  # type: ignore[arg-type]
        )

        if ingest_result.placed == 0 and ingest_result.errors == 0:
            if ingest_result.skipped > 0:
                return f"同名ファイルが既に存在するためスキップしました: {file_path}"
            return f"エラー: ファイルの取り込みに失敗しました。パス: {file_path}"

        if ingest_result.errors > 0:
            return f"エラー: {ingest_result.error_details[0]}"

        pipeline_summary = await asyncio.to_thread(
            controller.ingest_and_index, f"ingest(local): add {file_path}",
        )
        _reset_rag_service()

        return _format_ingest_response(
            ingest_result, pipeline_summary, context=file_path,
        )
    except ValueError as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to add document file: %s", file_path)
        return f"エラー: ファイルの取り込みに失敗しました。パス: {file_path}"


@mcp.tool()
async def rag_crawl_documents(
    dir_path: str,
    pattern: str = "**/*",
    upload_mode: str = "fail",
) -> str:
    """[rag-knowledge] RAG crawl documents - ディレクトリ内のドキュメントを一括取り込み.

    knowledge base, ingest, document directory, bulk import, glob.
    指定ディレクトリ内のドキュメントファイルを glob パターンで検索し、
    一括でナレッジベースに取り込む。
    stdio モード専用。HTTP モードでは無効。

    Args:
        dir_path: 取り込み対象ディレクトリのパス（絶対パスまたは相対パス）
        pattern: glob パターン（デフォルト: ``**/*`` で再帰的に全対応ファイルを検索）
        upload_mode: 同名ファイル存在時の動作。"fail"（スキップ、デフォルト）または "replace"（上書き）

    Returns:
        取り込み結果のサマリーテキスト
    """
    if get_settings().rag_transport == "http":
        return "エラー: rag_crawl_documents は HTTP モードでは無効です（セキュリティ上の制約）"

    if upload_mode not in _VALID_UPLOAD_MODES:
        valid = ", ".join(sorted(_VALID_UPLOAD_MODES))
        return f"エラー: 無効な upload_mode: {upload_mode!r}（有効値: {valid}）"

    controller = await _get_pipeline_controller()
    local_ingester = PipelineLocalIngester(
        controller.source_store,
        supported_extensions=_get_supported_extensions(),
    )

    try:
        ingest_result = await asyncio.to_thread(
            local_ingester.crawl_documents, dir_path, pattern,
            upload_mode=upload_mode,  # type: ignore[arg-type]
        )

        if ingest_result.placed == 0 and ingest_result.errors == 0:
            return f"対象ファイルが見つかりませんでした（ディレクトリ: {dir_path}）"

        pipeline_summary = await asyncio.to_thread(
            controller.ingest_and_index, f"ingest(local): crawl {dir_path}",
        )
        _reset_rag_service()

        return _format_ingest_response(
            ingest_result, pipeline_summary, context=f"ディレクトリ: {dir_path}",
        )
    except ValueError as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to crawl documents: %s", dir_path)
        return f"エラー: ドキュメントの取り込みに失敗しました（ディレクトリ: {dir_path}）"


@mcp.tool()
async def rag_delete(url: str) -> str:
    """[rag-knowledge] RAG delete - ソースURL指定でナレッジから論理削除.

    knowledge base, remove source, delete document, soft delete.
    source_store 内のファイルは削除せず、metadata.db で論理削除する。
    検索インデックスからは即座に除去される。
    復旧は rag_rebuild で全再構築を行えば可能。

    Args:
        url: 削除するソースURL（source_id）

    Returns:
        削除結果のメッセージ
    """
    # BM25 はインメモリインデックスのため、別プロセス（CLI）が
    # ディスク上のインデックスを更新していても反映されない。
    # delete 前にコントローラをリセットし、最新のディスク状態をロードする。
    _reset_pipeline_controller()
    controller = await _get_pipeline_controller()

    try:
        # source_id として url をそのまま使用
        source_id = url

        def _do_delete() -> bool:
            try:
                controller.source_store.soft_delete(source_id)
            except KeyError:
                return False
            controller.indexer.delete(source_id)
            return True

        deleted = await asyncio.to_thread(_do_delete)
        if not deleted:
            return f"該当するソースが見つかりませんでした: {url}"

        _reset_rag_service()
        return f"論理削除しました: {url}"
    except Exception:
        logger.exception("Failed to delete: %s", url)
        return f"エラー: 削除に失敗しました。URL: {url}"


# --- 再構築 ---

_VALID_REBUILD_MODES: frozenset[str] = frozenset({
    "full", "convert", "index", "incremental",
})
_VALID_PIPELINE_SOURCE_TYPES: frozenset[str] = frozenset({
    "web", "bluesky", "zenn", "local",
})
_rebuild_lock = threading.Lock()


def _format_size(size_bytes: int) -> str:
    """バイト数を人間が読みやすい単位に変換する."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _format_rebuild_summary(summary: PipelineSummary, elapsed: float) -> str:
    """PipelineSummary をテキストに変換する."""
    mode_names = {
        PipelineMode.FULL_REBUILD: "全再構築",
        PipelineMode.CONVERT_ONLY: "コンバートのみ再実行",
        PipelineMode.INDEX_ONLY: "インデックスのみ再構築",
        PipelineMode.INCREMENTAL: "差分更新",
    }
    mode_name = mode_names.get(summary.mode, str(summary.mode.value))

    parts = [
        f"再構築完了 ({mode_name})",
        f"  処理件数: {summary.processed}",
        f"  スキップ: {summary.skipped}",
        f"  エラー: {len(summary.errors)}",
        f"  所要時間: {elapsed:.1f} 秒",
    ]
    if summary.errors:
        parts.append("  エラーファイル:")
        for err_file in summary.errors[:10]:
            parts.append(f"    - {err_file}")
        if len(summary.errors) > 10:
            parts.append(f"    ... 他 {len(summary.errors) - 10} 件")

    return "\n".join(parts)


def _collect_source_store_stats(
    source_store_dir: Path,
) -> dict[str, Any]:
    """source_store のファイル統計を収集する."""
    if not source_store_dir.exists():
        return {"total_files": 0, "total_size": 0, "by_type": {}}

    total_files = 0
    total_size = 0
    by_type: dict[str, dict[str, int]] = {}

    for file in source_store_dir.rglob("*"):
        if not file.is_file():
            continue

        rel = file.relative_to(source_store_dir)
        rel_posix = rel.as_posix()

        # 除外: .git (ディレクトリ/ファイル), .meta, metadata.db*, .gitignore
        if (
            rel_posix == ".git"
            or rel_posix.startswith(".git/")
            or rel_posix == ".gitignore"
        ):
            continue
        name = file.name
        if name.endswith(".meta") or name.startswith("metadata.db"):
            continue

        size = file.stat().st_size
        total_files += 1
        total_size += size

        st = detect_source_type(rel_posix)
        if st not in by_type:
            by_type[st] = {"files": 0, "size": 0}
        by_type[st]["files"] += 1
        by_type[st]["size"] += size

    return {"total_files": total_files, "total_size": total_size, "by_type": by_type}


def _collect_converted_store_stats(
    converted_store_dir: Path,
) -> dict[str, Any]:
    """converted_store のファイル統計を収集する."""
    if not converted_store_dir.exists():
        return {"total_files": 0, "total_size": 0}

    total_files = 0
    total_size = 0

    for file in converted_store_dir.rglob("*"):
        if not file.is_file():
            continue
        total_files += 1
        total_size += file.stat().st_size

    return {"total_files": total_files, "total_size": total_size}


def _collect_pipeline_stats(
    source_store_dir: Path,
) -> dict[str, Any] | None:
    """metadata.db からパイプライン統計を収集する."""
    db_path = source_store_dir / "metadata.db"
    if not db_path.exists():
        return None

    db = MetadataDB(db_path)
    try:
        db.initialize()
        history = db.get_pipeline_history()
        last_commit_id = db.get_last_commit_id()
        deleted_count = db.source_count(status="deleted")
        last_processed_at = history[-1].processed_at if history else None

        return {
            "last_processed_at": last_processed_at,
            "execution_count": len(history),
            "last_commit_id": last_commit_id,
            "deleted_count": deleted_count,
        }
    finally:
        db.close()


@mcp.tool()
async def rag_rebuild(mode: str, source_type: str | None = None) -> str:
    """[rag-knowledge] RAG rebuild - ナレッジベースの再構築を実行する.

    knowledge base, rebuild, pipeline, reindex, convert.
    パイプラインの再構築を指定モードで実行する。
    注意: full / index モードは Embedding API を呼び出すため、
    データ量に比例したコスト（API 利用料）が発生します。

    Args:
        mode: 再構築モード。
            "full" — 全再構築（データ破損時・大規模設計変更時）
            "convert" — コンバートのみ再実行（変換ロジック改修時）
            "index" — インデックスのみ再構築（Embedding モデル変更時）
            "incremental" — 差分更新（通常運用）
        source_type: 対象媒体フィルタ: "web", "bluesky", "zenn", "local"。
            未指定時は全媒体。incremental モードでは指定不可。

    Returns:
        処理結果サマリ（処理件数、スキップ件数、エラー件数、所要時間）
    """
    # パラメータ検証
    if mode not in _VALID_REBUILD_MODES:
        valid = ", ".join(sorted(_VALID_REBUILD_MODES))
        return f"エラー: 無効なモード: {mode!r}（有効値: {valid}）"

    if source_type is not None and source_type not in _VALID_PIPELINE_SOURCE_TYPES:
        valid = ", ".join(sorted(_VALID_PIPELINE_SOURCE_TYPES))
        return f"エラー: 無効な source_type: {source_type!r}（有効値: {valid}）"

    if mode == "incremental" and source_type is not None:
        return (
            "エラー: incremental モードでは source_type を指定できません"
            "（git diff に従います）"
        )

    # 設定の検証
    settings = get_settings()
    if not settings.source_store_dir:
        return "エラー: SOURCE_STORE_DIR が設定されていません"
    if not settings.converted_store_dir:
        return "エラー: CONVERTED_STORE_DIR が設定されていません"

    source_dir = Path(settings.source_store_dir)
    if not source_dir.exists():
        return f"エラー: source_store ディレクトリが存在しません: {source_dir}"

    # 排他制御
    if not _rebuild_lock.acquire(blocking=False):
        return "エラー: 別の再構築が実行中です"

    # バリデーション済みの source_type を SourceType にキャスト
    st: SourceType | None = source_type  # type: ignore[assignment]

    try:
        def _run_rebuild() -> PipelineSummary:
            controller = _build_pipeline_controller()
            if mode == "full":
                return controller.run_full_rebuild(source_type=st)
            if mode == "convert":
                return controller.run_convert_only(source_type=st)
            if mode == "index":
                return controller.run_index_only(source_type=st)
            # mode == "incremental"
            return controller.run_incremental()

        start = time.monotonic()
        task = asyncio.ensure_future(asyncio.to_thread(_run_rebuild))
        try:
            summary = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        elapsed = time.monotonic() - start

        # rebuild はインデックスを全操作するため、両方リセット
        _reset_pipeline_controller()
        _reset_rag_service()

        return _format_rebuild_summary(summary, elapsed)
    except Exception:
        logger.exception("再構築中にエラーが発生しました")
        return "エラー: 再構築中にエラーが発生しました"
    finally:
        _rebuild_lock.release()


@mcp.tool()
async def rag_stats() -> str:
    """[rag-knowledge] RAG stats - ナレッジベースの統計情報と蓄積データ概要を表示.

    knowledge base, statistics, chunk count, source count, source list.
    蓄積されているナレッジの概要を4セクション
    （source_store / converted_store / インデックス / パイプライン）で返す。
    検索前にこのツールを呼ぶことで、ナレッジベースの内容を把握し
    適切な検索キーワードを構成できる。

    Returns:
        統計情報のテキスト
    """
    settings = get_settings()

    parts: list[str] = ["📊 RAG Knowledge 統計"]

    # --- source_store セクション ---
    parts.append("")
    parts.append("■ source_store")
    if not settings.source_store_dir:
        parts.append("  未設定")
    else:
        try:
            ss_stats = await asyncio.to_thread(
                _collect_source_store_stats, Path(settings.source_store_dir),
            )
            parts.append(f"  総ファイル数: {ss_stats['total_files']:,}")
            parts.append(f"  総サイズ: {_format_size(ss_stats['total_size'])}")
            by_type = ss_stats.get("by_type", {})
            if by_type:
                parts.append("  媒体別:")
                for st in sorted(by_type.keys()):
                    info = by_type[st]
                    parts.append(
                        f"    {st}: {info['files']} files"
                        f" ({_format_size(info['size'])})"
                    )
        except Exception:
            logger.exception("source_store 統計の取得に失敗")
            parts.append("  エラー: 統計の取得に失敗しました")

    # --- converted_store セクション ---
    parts.append("")
    parts.append("■ converted_store")
    if not settings.converted_store_dir:
        parts.append("  未設定")
    else:
        try:
            cs_stats = await asyncio.to_thread(
                _collect_converted_store_stats,
                Path(settings.converted_store_dir),
            )
            parts.append(f"  総ファイル数: {cs_stats['total_files']:,}")
            parts.append(f"  総サイズ: {_format_size(cs_stats['total_size'])}")
        except Exception:
            logger.exception("converted_store 統計の取得に失敗")
            parts.append("  エラー: 統計の取得に失敗しました")

    # --- インデックスセクション ---
    parts.append("")
    parts.append("■ インデックス")
    try:
        service = await _get_rag_service()
        index_stats = await service.get_stats()
        total_chunks = index_stats.get("total_chunks", 0)
        source_count = index_stats.get("source_count", 0)
        sources = index_stats.get("sources", [])

        parts.append(f"  総チャンク数: {total_chunks:,}")
        parts.append(f"  ソース数: {source_count:,}")

        if sources and isinstance(sources, list):
            max_sources = settings.rag_stats_max_sources
            parts.append("  ドメイン別:")

            displayed = 0
            truncated = False
            for group in sources:
                if displayed >= max_sources:
                    truncated = True
                    break
                if not isinstance(group, dict):
                    continue
                domain = group.get("domain", "unknown")
                pages = group.get("pages", [])
                if not isinstance(pages, list):
                    continue

                page_count = len(pages)
                domain_chunks = sum(
                    int(p.get("chunks", 0))
                    for p in pages
                    if isinstance(p, dict)
                )
                parts.append(
                    f"    {domain}: {page_count} pages"
                    f" ({domain_chunks:,} chunks)"
                )
                displayed += 1

            if truncated:
                parts.append(
                    f"  (以下省略、{max_sources}件まで表示)"
                )
    except Exception:
        logger.exception("インデックス統計の取得に失敗")
        parts.append("  エラー: 統計の取得に失敗しました")

    # --- パイプラインセクション ---
    parts.append("")
    parts.append("■ パイプライン")
    if not settings.source_store_dir:
        parts.append("  未設定")
    else:
        try:
            pl_stats = await asyncio.to_thread(
                _collect_pipeline_stats, Path(settings.source_store_dir),
            )
            if pl_stats is None:
                parts.append("  未初期化")
            else:
                last_at = pl_stats["last_processed_at"] or "（未実行）"
                parts.append(f"  最終処理: {last_at}")
                parts.append(f"  実行回数: {pl_stats['execution_count']}")
                commit_id = str(pl_stats["last_commit_id"])
                if commit_id == NULL_COMMIT_HASH:
                    parts.append("  last_commit_id: （未実行）")
                else:
                    parts.append(f"  last_commit_id: {commit_id[:7]}")
                parts.append(f"  論理削除: {pl_stats['deleted_count']} 件")
        except Exception:
            logger.exception("パイプライン統計の取得に失敗")
            parts.append("  エラー: 統計の取得に失敗しました")

    return "\n".join(parts)


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

    try:
        if transport == "http":
            mcp.run(transport="streamable-http")
        else:
            mcp.run()
    except KeyboardInterrupt:
        logger.info("MCP server shut down")
        raise SystemExit(130)
    else:
        logger.info("MCP server shut down")


if __name__ == "__main__":
    _configure_and_run()

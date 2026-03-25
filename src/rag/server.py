"""RAG MCP サーバー

仕様: docs/specs/rag-knowledge.md, docs/specs/search-response.md
独立リポジトリとして動作する。

FastMCP を使用して 20 個の RAG ツールを公開する:
- rag_search: ナレッジベース検索（チャンク単位返却）
- rag_get_document: ソース全文取得
- rag_add: 単一ページをナレッジベースに取り込み
- rag_crawl: リンク集ページからクロール＆一括取り込み
- rag_crawl_preview: クロール対象ページのプレビュー（タイトル・URL一覧）
- rag_crawl_zenn: Zenn 記事の一括取り込み
- rag_crawl_bluesky: BlueSky 投稿の一括取り込み
- rag_add_youtube: YouTube 動画の字幕・文字起こしをナレッジベースに取り込み
- rag_crawl_youtube: YouTube プレイリスト・チャンネルの一括取り込み
- rag_add_document: ドキュメントファイルをナレッジベースに取り込み
- rag_add_journal: ジャーナルエントリをナレッジベースに登録
- rag_crawl_documents: ディレクトリ内ドキュメントを一括取り込み
- rag_site_ingest: Scrapy によるサイト一括取り込み（大規模サイト向け）
- rag_update_aozora_catalog: 青空文庫カタログ更新
- rag_search_aozora: 青空文庫カタログ検索
- rag_add_aozora: 青空文庫作品の単一取り込み
- rag_crawl_aozora: 青空文庫著者作品の一括取り込み
- rag_delete: ソースURL指定でナレッジから論理削除
- rag_rebuild: ナレッジベースの再構築
- rag_stats: ナレッジベースの統計情報を表示
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import sys
import threading
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
from .filter_parser import parse_filters
from .rag_knowledge import format_raw_search_results

with contextlib.redirect_stdout(io.StringIO()):
    from .bm25_index import BM25Index
    from .config import get_settings
    from .embedding.factory import get_embedding_provider
    from .rag_knowledge import (
        RAGKnowledgeService,
        format_document_response,
        get_document,
    )
    from .vector_store import VectorStore
    from .web_crawler import WebCrawler

    # パイプライン関連
    from .pipeline.controller import PipelineController
    from .pipeline.ingesters._common import IngestResult
    from .pipeline.ingesters.aozora import AozoraIngester as PipelineAozoraIngester
    from .pipeline.ingesters.bluesky import BlueskyIngester as PipelineBlueskyIngester
    from .pipeline.ingesters.journal import JournalIngester as PipelineJournalIngester
    from .pipeline.ingesters.local import LocalIngester as PipelineLocalIngester
    from .pipeline.ingesters.web import WebIngester as PipelineWebIngester
    from .pipeline.ingesters.youtube import YoutubeIngester as PipelineYoutubeIngester
    from .pipeline.ingesters.zenn import ZennIngester as PipelineZennIngester
    from .pipeline.models import PipelineMode, PipelineSummary, detect_source_type
    from .store.metadata_db import MetadataDB
    from .store.models import NULL_COMMIT_HASH
    from .store.source_store import SourceStore

from py_common_lib.httpx import ConstrainedClient  # safety:allowed

from .safe_browsing import (
    SafeBrowsingClient,
    SafeBrowsingConfigError,
    SafetyCheckError,
    create_safe_browsing_client,
)

from mcp.server.fastmcp import Context, FastMCP

# MCP Context の具象型パラメータ（ツール関数では型パラメータ不要のため Any で統一）
MCPContext = Context[Any, Any, Any]

# Windows 環境で stderr が cp932 等の場合に UTF-8 へ再構成する
# stdout は MCP stdio プロトコルが使うため変更しない
ensure_utf8_streams()

logger = logging.getLogger(__name__)

mcp = FastMCP("rag")

# --- 遅延初期化: RAGKnowledgeService（検索用） ---

_rag_service: RAGKnowledgeService | None = None
_init_lock = asyncio.Lock()


def _reset_rag_service() -> None:
    """グローバルな RAGKnowledgeService をリセットする.

    SharedSystemClient キャッシュをクリアしてから破棄する。
    サブプロセスが ChromaDB を更新した後、キャッシュが残っていると
    古い HNSW インメモリ状態が再利用され where フィルタ付き検索が失敗する。
    """
    global _rag_service
    if _rag_service is not None:
        try:
            _rag_service.close()
        except Exception:
            logger.warning("Failed to close RAG service", exc_info=True)
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
    )


# --- 遅延初期化: PipelineController（取り込み・再構築用） ---

_pipeline_controller: PipelineController | None = None
_pipeline_lock = asyncio.Lock()


def _reset_pipeline_controller() -> None:
    """グローバルな PipelineController をリセットする.

    既存インスタンスが保持する SQLite 接続等を解放してから破棄する。
    """
    global _pipeline_controller
    if _pipeline_controller is not None:
        with contextlib.suppress(Exception):
            _pipeline_controller.source_store.close()
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
    from .pipeline.factory import build_pipeline_controller

    return build_pipeline_controller()


_safe_browsing_client_cache: SafeBrowsingClient | None = None
_safe_browsing_client_initialized = False


def _get_safe_browsing_client() -> SafeBrowsingClient | None:
    """SafeBrowsingClient を取得する（プロセス内キャッシュ）.

    Returns:
        SafeBrowsingClient または None（無効時）

    Raises:
        SafeBrowsingConfigError: API キー未登録・空・keyring アクセス失敗時
    """
    global _safe_browsing_client_cache, _safe_browsing_client_initialized
    if _safe_browsing_client_initialized:
        return _safe_browsing_client_cache
    # SafeBrowsingConfigError 時は initialized を True にしない
    # （設定修正まで毎回エラー送出）

    settings = get_settings()
    _safe_browsing_client_cache = create_safe_browsing_client(settings)
    _safe_browsing_client_initialized = True
    return _safe_browsing_client_cache


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


def _create_web_ingester(
    source_store: SourceStore,
    safe_browsing_client: SafeBrowsingClient | None = None,
) -> PipelineWebIngester:
    """設定に基づいて PipelineWebIngester を生成する."""
    settings = get_settings()
    return PipelineWebIngester(
        source_store,
        max_crawl_pages=settings.rag_max_crawl_pages,
        crawl_request_timeout=settings.rag_crawl_request_timeout,
        crawl_max_errors=settings.rag_crawl_max_errors,
        respect_robots_txt=settings.rag_respect_robots_txt,
        robots_txt_cache_ttl=settings.rag_robots_txt_cache_ttl,
        safe_browsing_client=safe_browsing_client,
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

_VALID_SOURCE_TYPES: frozenset[str] = frozenset({"web", "zenn", "bluesky", "youtube", "local", "aozora", "journal"})


@mcp.tool()
async def rag_search(
    query: str,
    n_results: int | None = None,
    source_type: str | None = None,
    filters: str | None = None,
) -> str:
    """[rag-knowledge] RAG search - ナレッジベース検索。挨拶・雑談以外の質問では必ずこのツールを最初に呼び出すこと。

    knowledge base, vector search, BM25, retrieval-augmented generation.
    ナレッジベースにはゲーム攻略情報・技術文書等が格納されている。
    蓄積データの詳細は rag_stats ツールで確認できる。
    知らない用語や固有名詞を含む質問でも必ず検索すること。

    Args:
        query: 検索クエリ（ユーザーの質問からキーワードを抽出して構成する）
        n_results: 各エンジンから取得する結果数（未指定時は設定値を使用）
        source_type: ソース種別フィルタ（"web", "zenn", "bluesky", "youtube", "aozora", "local", "journal"）。
            指定時はそのソース種別のチャンクのみを検索対象とする。未指定時は全種別を検索。
        filters: メタデータフィルタ（key=value 形式、カンマ区切りで複数指定可）。
            .meta のカスタムフィールドで検索結果を絞り込む。完全一致。
            例: "repository=rag-knowledge" / "repository=rag-knowledge,tag=dev"
            未指定時はフィルタなし。

    Returns:
        検索結果テキスト。ベクトル検索結果とBM25検索結果をセクション分けして返す。
        各結果はチャンク単位で返却される。
        詳細が必要な場合は rag_get_document でソースの全文を取得できる。
        結果が0件の場合は「該当する情報が見つかりませんでした」を返す。
    """
    if source_type is not None and source_type not in _VALID_SOURCE_TYPES:
        valid = ", ".join(sorted(_VALID_SOURCE_TYPES))
        return f"無効な source_type: {source_type!r}（有効値: {valid}）"

    # filters パラメータのパース
    parsed_filters: dict[str, str] | None = None
    if filters is not None:
        try:
            parsed_filters = parse_filters(filters)
        except ValueError as e:
            return f"エラー: {e}"

    service = await _get_rag_service()
    if n_results is None:
        n_results = get_settings().rag_retrieval_count

    raw = await service.retrieve_raw_results(
        query, n_results=n_results, source_type=source_type,
        filters=parsed_filters,
    )

    return format_raw_search_results(raw)


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
async def rag_add(url: str, ctx: MCPContext | None = None) -> str:
    """[rag-knowledge] RAG add - 単一ページをナレッジベースに取り込む（非推奨: rag_site_ingest を推奨）.

    knowledge base, ingest, web page, crawl single URL.
    非推奨: 大規模サイトには rag_site_ingest を使用してください。

    Args:
        url: 取り込むページのURL

    Returns:
        取り込み結果のメッセージ
    """
    controller = await _get_pipeline_controller()
    try:
        sb_client = _get_safe_browsing_client()
    except SafeBrowsingConfigError as e:
        return f"Safe Browsing 設定エラー: {e}"
    web_ingester = _create_web_ingester(controller.source_store, sb_client)
    settings = get_settings()

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_crawl_request_timeout,
            request_interval=settings.rag_crawl_delay_sec,
        ) as client:
            ingest_result = await web_ingester.add(
                url, client=client,
            )

        if ingest_result.placed == 0 and ingest_result.errors == 0:
            return f"取り込み対象がありませんでした: {url}"

        pipeline_summary = await _run_ingest_and_index_subprocess(
            f"ingest(web): add {url}",
            ctx=ctx,
        )
        _reset_pipeline_controller()
        _reset_rag_service()

        return _format_ingest_response(ingest_result, pipeline_summary, context=url)
    except (SafetyCheckError, SafeBrowsingConfigError) as e:
        return f"Safe Browsing エラー: {e}"
    except ValueError as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to add page: %s", url)
        return f"エラー: ページの取り込みに失敗しました。URL: {url}"


@mcp.tool()
async def rag_crawl(
    url: str, pattern: str = "", depth: int | None = None,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG crawl - リンク集ページからクロール＆一括取り込み（非推奨: rag_site_ingest を推奨）.

    knowledge base, bulk ingest, web crawl, link index, recursive crawl.
    非推奨: 大規模サイトには rag_site_ingest を使用してください。上限500ページ。
    再帰クロール対応: depth > 1 でリンクを複数階層辿れる。

    Args:
        url: リンク集ページのURL
        pattern: URLフィルタリング用の正規表現パターン（depth >= 2 の場合は必須）
        depth: クロール深度（1〜10。未指定時は設定値を使用。1 = 直接リンクのみ）

    Returns:
        クロール結果のサマリー
    """
    settings = get_settings()

    if depth is None:
        depth = settings.rag_crawl_default_depth

    # depth >= 2 の場合は pattern 必須
    if depth >= 2 and not pattern:
        return (
            "エラー: depth が 2 以上の場合は pattern の指定が必須です。"
            "再帰クロールではパターンなしだと無関係なページまで辿る恐れがあります"
        )

    # MSYS パス変換検出（Git Bash 環境で /pattern が C:/Program Files/... に変換される）
    from .pipeline.ingesters.web import _looks_like_msys_path
    if pattern and _looks_like_msys_path(pattern):
        return (
            f"エラー: pattern が Windows パスに変換されています: {pattern!r}。"
            "Git Bash 環境では先頭の / が自動変換されます。"
            "先頭の / を除去するか、MSYS_NO_PATHCONV=1 を設定してください"
        )

    controller = await _get_pipeline_controller()
    try:
        sb_client = _get_safe_browsing_client()
    except SafeBrowsingConfigError as e:
        return f"Safe Browsing 設定エラー: {e}"
    web_ingester = _create_web_ingester(controller.source_store, sb_client)

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_crawl_request_timeout,
            request_interval=settings.rag_crawl_delay_sec,
        ) as client:
            ingest_result = await web_ingester.crawl(
                url, pattern=pattern, depth=depth, client=client,
            )

        if ingest_result.placed == 0 and ingest_result.errors == 0:
            return f"対象ページが見つかりませんでした: {url}"

        pipeline_summary = await _run_ingest_and_index_subprocess(
            f"ingest(web): crawl {url}",
            ctx=ctx,
        )
        _reset_pipeline_controller()
        _reset_rag_service()

        return _format_ingest_response(ingest_result, pipeline_summary, context=url)
    except (SafetyCheckError, SafeBrowsingConfigError) as e:
        return f"Safe Browsing エラー: {e}"
    except ValueError as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to crawl: %s", url)
        return f"エラー: クロールに失敗しました。URL: {url}"


@mcp.tool()
async def rag_crawl_preview(
    url: str, pattern: str = "", depth: int | None = None
) -> str:
    """[rag-knowledge] RAG crawl preview - クロール対象ページのプレビュー（非推奨: rag_site_ingest を推奨）.

    knowledge base, crawl preview, dry run, link list, recursive.
    非推奨: 大規模サイトには rag_site_ingest を使用してください。
    実際の取り込み（チャンキング・ベクトル化）は行わず、
    クロール対象となるページのタイトルとURLの一覧を返す。

    Args:
        url: リンク集ページのURL
        pattern: URLフィルタリング用の正規表現パターン（depth >= 2 の場合は必須）
        depth: クロール深度（1〜10。未指定時は設定値を使用。1 = 直接リンクのみ）

    Returns:
        クロール対象ページの一覧テキスト
    """
    settings = get_settings()

    if depth is None:
        depth = settings.rag_crawl_default_depth

    # depth >= 2 の場合は pattern 必須
    if depth >= 2 and not pattern:
        return (
            "エラー: depth が 2 以上の場合は pattern の指定が必須です。"
            "再帰クロールではパターンなしだと無関係なページまで辿る恐れがあります"
        )

    # MSYS パス変換検出
    from .pipeline.ingesters.web import _looks_like_msys_path
    if pattern and _looks_like_msys_path(pattern):
        return (
            f"エラー: pattern が Windows パスに変換されています: {pattern!r}。"
            "Git Bash 環境では先頭の / が自動変換されます。"
            "先頭の / を除去するか、MSYS_NO_PATHCONV=1 を設定してください"
        )

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
                url, pattern=pattern, depth=depth, client=client,
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
    force: bool = False,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG crawl Zenn - Zenn コンテンツを API 経由で取得し一括取り込み.

    knowledge base, Zenn, ingest, articles, scraps, API.
    指定ユーザーの Zenn 記事・スクラップを API 経由で取得し、ナレッジベースに取り込む。
    デフォルトでは既存コンテンツはスキップする。force=True で上書き取り込み。

    Args:
        username: Zenn ユーザー名
        max_articles: 取得する最大コンテンツ数（未指定時は設定値を使用、許容範囲: 1〜100）
        content_type: 取得対象（"articles": 記事のみ、"scraps": スクラップのみ、"all": 両方。デフォルト: "all"）
        force: 既存ファイルを上書きするか（デフォルト: false＝スキップモード）

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
                force=force,
                client=client,
            )

        if ingest_result.placed == 0 and ingest_result.errors == 0:
            if ingest_result.skipped > 0:
                return f"全 {ingest_result.skipped} 件のコンテンツが既に取り込み済みです（ユーザー: {username}）。上書きするには force=true を指定してください"
            return f"コンテンツが見つかりませんでした（ユーザー: {username}）"

        pipeline_summary = await _run_ingest_and_index_subprocess(
            f"ingest(zenn): {username.strip()}",
            ctx=ctx,
        )
        _reset_pipeline_controller()
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
    ctx: MCPContext | None = None,
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
            ingest_result, placed_items = await bluesky_ingester.crawl_bluesky(
                handle,
                max_posts=max_posts,
                include_reposts=include_reposts,
                client=client,
            )

            # 投稿内 URL の自動取り込み
            url_stats: dict[str, int] = {}
            if placed_items:
                try:
                    sb_client = _get_safe_browsing_client()
                except SafeBrowsingConfigError as e:
                    return f"Safe Browsing 設定エラー: {e}"
                web_ingester = _create_web_ingester(controller.source_store, sb_client)
                youtube_ingester = PipelineYoutubeIngester(
                    controller.source_store,
                    max_videos=settings.rag_youtube_max_videos,
                    request_interval=settings.rag_youtube_request_interval,
                    request_timeout=settings.rag_youtube_request_timeout,
                    whisper_model=settings.rag_youtube_whisper_model,
                    whisper_device=settings.rag_youtube_whisper_device,
                    transcript_languages=settings.rag_youtube_transcript_languages,
                    max_duration=settings.rag_youtube_max_duration,
                )
                url_stats = await bluesky_ingester.follow_urls(
                    placed_items,
                    client=client,
                    web_ingester=web_ingester,
                    youtube_ingester=youtube_ingester,
                )

        if ingest_result.placed == 0 and ingest_result.errors == 0:
            return f"投稿が見つかりませんでした（ハンドル: {handle}）"

        pipeline_summary = await _run_ingest_and_index_subprocess(
            f"ingest(bluesky): {handle}",
            ctx=ctx,
        )
        _reset_pipeline_controller()
        _reset_rag_service()

        summary = _format_ingest_response(
            ingest_result, pipeline_summary, context=f"ハンドル: {handle}",
        )
        if url_stats and any(url_stats.get(k, 0) > 0 for k in ("web_placed", "youtube_placed", "errors")):
            parts = ["\n\nURL 自動取り込み:"]
            web_n = url_stats.get("web_placed", 0)
            yt_n = url_stats.get("youtube_placed", 0)
            err_n = url_stats.get("errors", 0)
            if web_n > 0 or yt_n > 0:
                parts.append(f"Web {web_n}件, YouTube {yt_n}件")
            if err_n > 0:
                parts.append(f"エラー {err_n}件")
            summary += " ".join(parts)
        return summary
    except (SafetyCheckError, SafeBrowsingConfigError) as e:
        return f"Safe Browsing エラー: {e}"
    except (ValueError, TypeError) as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception(
            "Failed to crawl BlueSky posts for handle: %s", handle
        )
        return f"エラー: BlueSky 投稿の取り込みに失敗しました（ハンドル: {handle}）"


@mcp.tool()
async def rag_add_youtube(
    video_url: str,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG add YouTube - YouTube 動画の字幕/文字起こしを取り込む.

    knowledge base, YouTube, video, transcript, subtitle, ingest.
    YouTube 動画の字幕または音声文字起こしを取得し、ナレッジベースに取り込む。

    Args:
        video_url: YouTube 動画 URL（youtube.com/watch?v= または youtu.be/ 形式）

    Returns:
        取り込み結果のサマリーテキスト
    """
    if not video_url or not video_url.strip():
        return "エラー: video_url を指定してください"

    video_url = video_url.strip()
    settings = get_settings()

    controller = await _get_pipeline_controller()
    youtube_ingester = PipelineYoutubeIngester(
        controller.source_store,
        max_videos=settings.rag_youtube_max_videos,
        request_interval=settings.rag_youtube_request_interval,
        request_timeout=settings.rag_youtube_request_timeout,
        whisper_model=settings.rag_youtube_whisper_model,
        whisper_device=settings.rag_youtube_whisper_device,
        transcript_languages=settings.rag_youtube_transcript_languages,
        max_duration=settings.rag_youtube_max_duration,
    )

    try:
        ingest_result = await youtube_ingester.ingest_video(video_url)

        if ingest_result.placed == 0:
            return ingest_result.summary(context=f"動画: {video_url}")

        pipeline_summary = await _run_ingest_and_index_subprocess(
            f"ingest(youtube): {video_url}",
            ctx=ctx,
        )
        _reset_pipeline_controller()
        _reset_rag_service()

        return _format_ingest_response(
            ingest_result, pipeline_summary, context=f"動画: {video_url}",
        )
    except (ValueError, TypeError) as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception(
            "Failed to ingest YouTube video: %s", video_url
        )
        return f"エラー: YouTube 動画の取り込みに失敗しました: {video_url}"


@mcp.tool()
async def rag_crawl_youtube(
    playlist_url: str,
    max_videos: int | None = None,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG crawl YouTube playlist - YouTube プレイリストの動画を一括取り込み.

    knowledge base, YouTube, playlist, video, transcript, ingest, crawl.
    YouTube プレイリスト内の動画の字幕/文字起こしを一括取得し、ナレッジベースに取り込む。

    Args:
        playlist_url: YouTube プレイリスト URL（youtube.com/playlist?list= 形式）
        max_videos: 取得する最大動画数（未指定時は設定値を使用、許容範囲: 1〜500）

    Returns:
        取り込み結果のサマリーテキスト
    """
    settings = get_settings()

    if max_videos is None:
        max_videos = settings.rag_youtube_max_videos

    if not isinstance(max_videos, int) or isinstance(max_videos, bool):
        return f"エラー: max_videos は整数で指定してください（入力値: {max_videos!r}）"
    if max_videos <= 0:
        return f"エラー: max_videos は正の整数で指定してください（入力値: {max_videos}）"
    from .pipeline.ingesters.youtube import MAX_VIDEOS_HARD_LIMIT as _YT_MAX
    if max_videos > _YT_MAX:
        logger.warning("max_videos (%d) が上限 %d を超えています。クランプします", max_videos, _YT_MAX)
        max_videos = _YT_MAX

    if not playlist_url or not playlist_url.strip():
        return "エラー: playlist_url を指定してください"

    playlist_url = playlist_url.strip()

    controller = await _get_pipeline_controller()
    youtube_ingester = PipelineYoutubeIngester(
        controller.source_store,
        max_videos=max_videos,
        request_interval=settings.rag_youtube_request_interval,
        request_timeout=settings.rag_youtube_request_timeout,
        whisper_model=settings.rag_youtube_whisper_model,
        whisper_device=settings.rag_youtube_whisper_device,
        transcript_languages=settings.rag_youtube_transcript_languages,
        max_duration=settings.rag_youtube_max_duration,
    )

    try:
        ingest_result = await youtube_ingester.crawl_playlist(
            playlist_url,
            max_videos=max_videos,
        )

        if ingest_result.placed == 0:
            return ingest_result.summary(context=f"プレイリスト: {playlist_url}")

        pipeline_summary = await _run_ingest_and_index_subprocess(
            f"ingest(youtube-playlist): {playlist_url}",
            ctx=ctx,
        )
        _reset_pipeline_controller()
        _reset_rag_service()

        return _format_ingest_response(
            ingest_result, pipeline_summary, context=f"プレイリスト: {playlist_url}",
        )
    except (ValueError, TypeError) as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception(
            "Failed to crawl YouTube playlist: %s", playlist_url
        )
        return f"エラー: YouTube プレイリストの取り込みに失敗しました: {playlist_url}"


_VALID_UPLOAD_MODES: frozenset[str] = frozenset({"fail", "replace"})


@mcp.tool()
async def rag_add_document(
    file_path: str,
    upload_mode: str = "fail",
    ctx: MCPContext | None = None,
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
            return f"エラー: ファイルの取り込みに失敗しました。パス: {file_path}"

        if ingest_result.errors > 0:
            return f"エラー: {ingest_result.error_details[0]}"

        pipeline_summary = await _run_ingest_and_index_subprocess(
            f"ingest(local): add {file_path}",
            ctx=ctx,
        )
        _reset_pipeline_controller()
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
async def rag_add_journal(
    title: str,
    body: str,
    repository: str,
    entry_id: str | None = None,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG add journal - ジャーナルエントリをナレッジベースに登録する.

    journal, session log, work record, development diary.
    セッションごとの作業記録（ジャーナル）をナレッジベースに追加する。
    stdio モード専用。HTTP モードでは無効。

    Args:
        title: エントリタイトル
        body: 本文（Markdown）
        repository: リポジトリ名（例: rag-knowledge）
        entry_id: エントリ識別子（更新時に使用。未指定時は自動生成。命名規則: YYYYMMDD-HHMMSS-topic）

    Returns:
        取り込み結果のメッセージ
    """
    if get_settings().rag_transport == "http":
        return "エラー: rag_add_journal は HTTP モードでは無効です（セキュリティ上の制約）"

    controller = await _get_pipeline_controller()
    journal_ingester = PipelineJournalIngester(controller.source_store)

    try:
        ingest_result = await asyncio.to_thread(
            journal_ingester.add_entry,
            title,
            body,
            repository,
            entry_id=entry_id,
        )

        if ingest_result.placed == 0 and ingest_result.errors == 0:
            return "エラー: ジャーナルエントリの登録に失敗しました"

        if ingest_result.errors > 0:
            return f"エラー: {ingest_result.error_details[0]}"

        pipeline_summary = await _run_ingest_and_index_subprocess(
            f"ingest(journal): {repository}/{entry_id or title}",
            ctx=ctx,
        )
        _reset_pipeline_controller()
        _reset_rag_service()

        resolved_entry_id = journal_ingester.last_entry_id or entry_id or title
        return _format_ingest_response(
            ingest_result, pipeline_summary,
            context=f"journal: {repository}/{resolved_entry_id}",
        )
    except ValueError as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to add journal entry: %s/%s", repository, title)
        return f"エラー: ジャーナルエントリの登録に失敗しました: {title}"


@mcp.tool()
async def rag_crawl_documents(
    dir_path: str,
    pattern: str = "**/*",
    upload_mode: str = "fail",
    ctx: MCPContext | None = None,
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

        pipeline_summary = await _run_ingest_and_index_subprocess(
            f"ingest(local): crawl {dir_path}",
            ctx=ctx,
        )
        _reset_pipeline_controller()
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
async def rag_site_ingest(
    url: str,
    url_pattern: str = "",
    max_pages: int | None = None,
    force: bool = False,
    download_only: bool = False,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG site ingest - Scrapy でサイトを一括取り込み.

    knowledge base, bulk ingest, site crawl, large scale, scrapy.
    数千ページ規模の大規模サイトを Scrapy subprocess で一括取り込みする。
    既存の rag_crawl（上限500ページ）では足りない大規模サイト向け。

    Args:
        url: クロール開始 URL
        url_pattern: URL フィルタパターン（正規表現、任意）
        max_pages: ページ数上限（未指定時は設定値を使用）
        force: True の場合、JOBDIR を削除して最初からクロール
        download_only: True の場合、Scrapy クロール + Bridge まで実行し、
            パイプライン処理（コンバート・インデックス構築）をスキップする

    Returns:
        取り込み結果のサマリー
    """
    import re
    import time as time_mod

    from .scrapy.bridge import import_to_source_store
    from .scrapy.runner import ScrapyRunner
    from .utils.url import check_ssrf, validate_url

    settings = get_settings()

    # URL バリデーション
    try:
        url = validate_url(url)
        check_ssrf(url)
    except ValueError as e:
        return f"エラー: {e}"

    # url_pattern バリデーション
    if url_pattern:
        try:
            re.compile(url_pattern)
        except re.error as e:
            return f"エラー: 無効な正規表現パターン: {e}"

    # max_pages のクランプ
    effective_max_pages = max_pages if max_pages is not None else settings.site_ingest_max_pages
    if effective_max_pages < 1:
        effective_max_pages = 1
        logger.warning("max_pages を 1 にクランプしました")
    elif effective_max_pages > 50000:
        effective_max_pages = 50000
        logger.warning("max_pages を 50000 にクランプしました")

    # ドメイン導出
    from urllib.parse import urlparse
    parsed = urlparse(url)
    allowed_domains = parsed.hostname or ""

    controller = await _get_pipeline_controller()

    start_time = time_mod.monotonic()

    try:
        # Scrapy Runner で クロール
        runner = ScrapyRunner(
            temp_dir=settings.site_ingest_temp_dir,
            delay_sec=settings.site_ingest_delay_sec,
            max_pages=effective_max_pages,
            download_timeout=settings.site_ingest_download_timeout,
            timeout_sec=settings.site_ingest_timeout_sec,
            error_count=settings.site_ingest_error_count,
        )

        crawl_result = await runner.run(
            start_url=url,
            allowed_domains=allowed_domains,
            url_pattern=url_pattern,
            max_pages=effective_max_pages,
            force=force,
        )

        if not crawl_result.jsonl_path.exists():
            elapsed = time_mod.monotonic() - start_time
            return (
                f"クロールが完了しましたが、メタデータが出力されませんでした。"
                f" exit_code={crawl_result.exit_code}, 所要時間={elapsed:.1f}秒"
            )

        # Bridge: JSONL + HTML → source_store
        bridge_result = await asyncio.to_thread(
            import_to_source_store,
            jsonl_path=crawl_result.jsonl_path,
            html_dir=crawl_result.output_dir,
            source_store=controller.source_store,
        )

        # パイプライン処理
        pipeline_summary: PipelineSummary | None = None
        has_changes = (bridge_result.ingest.placed + bridge_result.ingest.overwritten) > 0
        if has_changes and not download_only:
            pipeline_summary = await _run_ingest_and_index_subprocess(
                f"ingest(web): site-ingest {url}",
                ctx=ctx,
            )
            _reset_pipeline_controller()
            _reset_rag_service()
        elif has_changes and download_only:
            # download_only でもコミットは実行する（パイプライン処理のみスキップ）
            await asyncio.to_thread(
                controller.commit, f"ingest(web): site-ingest {url} (download_only)",
            )

        # 操作全体の所要時間（クロール + Bridge + パイプライン）
        elapsed = time_mod.monotonic() - start_time

        # 結果サマリー構築
        parts: list[str] = []
        parts.append(
            f"サイト取り込み完了: {bridge_result.ingest.placed}件新規配置"
            f", {bridge_result.ingest.overwritten}件上書き"
            f", {bridge_result.ingest.skipped}件スキップ"
            f", {bridge_result.ingest.errors}件エラー"
        )
        parts.append(f"所要時間: {elapsed:.1f}秒")
        if download_only:
            parts.append("パイプライン処理: スキップ（download_only）")
        elif pipeline_summary is not None:
            parts.append(f"パイプライン: {pipeline_summary.processed}件処理")
            if pipeline_summary.errors:
                parts.append(f"パイプラインエラー: {len(pipeline_summary.errors)}件")
        if not crawl_result.success:
            parts.append(f"Scrapy exit_code={crawl_result.exit_code}（部分的な結果）")

        return " / ".join(parts)
    except Exception:
        logger.exception("Failed to site-ingest: %s", url)
        return f"エラー: サイト取り込みに失敗しました。URL: {url}"


@mcp.tool()
async def rag_update_aozora_catalog(
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG update Aozora catalog - 青空文庫の作品カタログを更新.

    knowledge base, Aozora, aozora bunko, catalog, update, CSV.
    青空文庫の作品カタログ CSV をダウンロードし、source_store に配置する。
    前回カタログとの差分から新着・更新作品を検出して結果を返す。

    Returns:
        カタログ更新結果のサマリーテキスト
    """
    settings = get_settings()

    controller = await _get_pipeline_controller()
    aozora_ingester = PipelineAozoraIngester(controller.source_store)

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_aozora_request_timeout,
            request_interval=settings.rag_aozora_request_interval,
        ) as client:
            result_text = await aozora_ingester.update_catalog(client=client)
        # カタログはパイプライン処理対象外だが、未コミット変更が残ると
        # 後続の rebuild で失敗するためコミットしておく
        controller.commit("update_aozora_catalog")
        return result_text
    except (ValueError, TypeError) as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to update Aozora catalog")
        return "エラー: 青空文庫カタログの更新に失敗しました"


@mcp.tool()
async def rag_search_aozora(
    author: str | None = None,
    title: str | None = None,
    limit: int = 20,
) -> str:
    """[rag-knowledge] RAG search Aozora catalog - 青空文庫カタログを検索.

    knowledge base, Aozora, aozora bunko, search, catalog, author, title.
    ローカルカタログ CSV を著者名・作品名で部分一致検索する。ネットワークアクセス不要。

    Args:
        author: 著者名（部分一致検索）
        title: 作品タイトル（部分一致検索）
        limit: 最大表示件数（デフォルト: 20、許容範囲: 1〜100）

    Returns:
        検索結果リスト（作品 ID、タイトル、著者名、著作権フラグ）
    """
    controller = await _get_pipeline_controller()
    aozora_ingester = PipelineAozoraIngester(controller.source_store)

    try:
        results = aozora_ingester.search(
            author=author,
            title=title,
            limit=limit,
        )
    except (ValueError, TypeError) as e:
        return f"エラー: {e}"

    if not results:
        parts = []
        if author:
            parts.append(f"著者: {author}")
        if title:
            parts.append(f"タイトル: {title}")
        return f"検索結果: 0件（{', '.join(parts)}）"

    lines = [f"検索結果: {len(results)}件", ""]
    for r in results:
        lines.append(
            f"- [{r['book_id']}] {r['title']} / {r['author']} "
            f"({r['copyright']})"
        )
    return "\n".join(lines)


@mcp.tool()
async def rag_add_aozora(
    book_id: str,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG add Aozora - 青空文庫の作品を取り込み.

    knowledge base, Aozora, aozora bunko, ingest, book, work.
    指定作品の XHTML を取得し、ナレッジベースに取り込む。著作権フリーの作品のみ対応。

    Args:
        book_id: 青空文庫の作品 ID（カタログ検索で取得）

    Returns:
        取り込み結果のサマリーテキスト
    """
    if not book_id or not book_id.strip():
        return "エラー: book_id を指定してください"

    book_id = book_id.strip()
    settings = get_settings()

    controller = await _get_pipeline_controller()
    aozora_ingester = PipelineAozoraIngester(controller.source_store)

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_aozora_request_timeout,
            request_interval=settings.rag_aozora_request_interval,
        ) as client:
            ingest_result = await aozora_ingester.add_work(
                book_id, client=client,
            )

        if ingest_result.placed == 0:
            return ingest_result.summary(context=f"作品ID: {book_id}")

        pipeline_summary = await _run_ingest_and_index_subprocess(
            f"ingest(aozora): book_id={book_id}",
            ctx=ctx,
        )
        _reset_pipeline_controller()
        _reset_rag_service()

        return _format_ingest_response(
            ingest_result, pipeline_summary, context=f"作品ID: {book_id}",
        )
    except (ValueError, TypeError) as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to add Aozora work: %s", book_id)
        return f"エラー: 青空文庫作品の取り込みに失敗しました（作品ID: {book_id}）"


@mcp.tool()
async def rag_crawl_aozora(
    person_id: str,
    max_works: int | None = None,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG crawl Aozora - 青空文庫の著者作品を一括取り込み.

    knowledge base, Aozora, aozora bunko, ingest, crawl, author, works, person.
    指定著者（人物 ID）の著作権フリー作品を一括取り込みする。

    Args:
        person_id: 著者の人物 ID（rag_search_aozora で確認可能）
        max_works: 取得する最大作品数（未指定時は設定値を使用、許容範囲: 1〜500）

    Returns:
        取り込み結果のサマリーテキスト
    """
    settings = get_settings()

    if max_works is None:
        max_works = settings.rag_aozora_max_works

    if not isinstance(max_works, int) or isinstance(max_works, bool):
        return f"エラー: max_works は整数で指定してください（入力値: {max_works!r}）"
    if max_works <= 0:
        return f"エラー: max_works は正の整数で指定してください（入力値: {max_works}）"

    if not person_id or not person_id.strip():
        return "エラー: person_id を指定してください"

    person_id = person_id.strip()

    controller = await _get_pipeline_controller()
    aozora_ingester = PipelineAozoraIngester(
        controller.source_store,
        max_works=max_works,
    )

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_aozora_request_timeout,
            request_interval=settings.rag_aozora_request_interval,
        ) as client:
            ingest_result = await aozora_ingester.crawl_author(
                person_id,
                max_works=max_works,
                client=client,
            )

        if ingest_result.placed == 0 and ingest_result.errors == 0 and ingest_result.skipped == 0:
            return f"対象作品が見つかりませんでした（人物ID: {person_id}）"

        pipeline_summary = None
        if ingest_result.placed > 0:
            pipeline_summary = await _run_ingest_and_index_subprocess(
                f"ingest(aozora): person_id={person_id}",
                ctx=ctx,
            )
            _reset_pipeline_controller()
            _reset_rag_service()

        return _format_ingest_response(
            ingest_result, pipeline_summary, context=f"人物ID: {person_id}",
        )
    except (ValueError, TypeError) as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception(
            "Failed to crawl Aozora works for person_id: %s", person_id
        )
        return f"エラー: 青空文庫作品の取り込みに失敗しました（人物ID: {person_id}）"


@mcp.tool()
async def rag_delete(url: str, ctx: MCPContext | None = None) -> str:
    """[rag-knowledge] RAG delete - ソースURL指定でナレッジから削除.

    knowledge base, remove source, delete document.
    source_store からファイルを物理削除し、パイプライン経由で
    インデックス・metadata.db を更新する。
    git 管理下のため、削除後も git checkout で復旧可能。

    Args:
        url: 削除するソースURL（source_id）

    Returns:
        削除結果のメッセージ
    """
    # BM25 はインメモリインデックスのため、別プロセス（CLI）が
    # ディスク上のインデックスを更新していても反映されない。
    # delete 前にコントローラをリセットし、最新のディスク状態をロードする。
    _reset_pipeline_controller()

    try:
        result = await _run_delete_subprocess(url, ctx=ctx)
        if result.get("not_found"):
            return f"該当するソースが見つかりませんでした: {url}"

        _reset_pipeline_controller()
        _reset_rag_service()

        pipeline = result.get("pipeline")
        if pipeline and pipeline.errors:
            errors_text = "; ".join(pipeline.errors)
            return f"削除しましたが、パイプラインでエラーが発生しました: {url} ({errors_text})"
        return f"削除しました: {url}"
    except Exception:
        logger.exception("Failed to delete: %s", url)
        return f"エラー: 削除に失敗しました。URL: {url}"


# --- 再構築 ---

_VALID_REBUILD_MODES: frozenset[str] = frozenset({
    "full", "convert", "index", "incremental",
})
_VALID_PIPELINE_SOURCE_TYPES: frozenset[str] = frozenset({
    "web", "bluesky", "zenn", "youtube", "aozora", "local",
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


# SEGFAULT を示す exit code
# Windows: 0xC0000005 は signed (-1073741819) / unsigned (3221225477) 両方で返りうる
# Unix: SIGSEGV=11, shell: 128+11=139
_SEGFAULT_EXIT_CODES: frozenset[int] = frozenset({-1073741819, 3221225477, -11, 139})


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
async def rag_rebuild(
    mode: str,
    source_type: str | None = None,
    ctx: MCPContext | None = None,
) -> str:
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
            "incremental" — 差分更新（通常運用。未コミット変更は自動コミット）
        source_type: 対象媒体フィルタ: "web", "bluesky", "zenn", "youtube", "aozora", "local", "journal"。
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

    try:
        result = await _run_rebuild_subprocess(mode, source_type, ctx=ctx)

        # rebuild はインデックスを全操作するため、両方リセット
        _reset_pipeline_controller()
        _reset_rag_service()

        return result
    except Exception:
        logger.exception("再構築中にエラーが発生しました")
        return "エラー: 再構築中にエラーが発生しました"
    finally:
        _rebuild_lock.release()


async def _run_worker_subprocess(
    subcommand: str,
    args: list[str],
    ctx: MCPContext | None = None,
) -> tuple[int, str, str]:
    """worker.py サブコマンドをサブプロセスで実行する.

    C 拡張（BM25s 等）の SEGFAULT がサーバープロセスを巻き込まないよう、
    別プロセスで実行してエラーを安全にハンドリングする。

    Args:
        subcommand: worker サブコマンド名
        args: サブコマンド引数
        ctx: MCP Context（進捗通知用、任意）

    Returns:
        (exit_code, stdout_text, stderr_tail) のタプル
        stdout_text には progress 行を除いた最終結果行のみが含まれる
    """
    cmd = [
        sys.executable, "-m", "rag.pipeline.worker",
        subcommand, *args,
    ]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    result_line = ""
    try:
        assert process.stdout is not None  # noqa: S101
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            # JSON パースを試みて type フィールドで分岐
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    msg_type = data.get("type")
                    if msg_type == "progress":
                        if ctx is not None:
                            processed = data.get("processed", 0)
                            total = data.get("total", 0)
                            current = data.get("current", "")
                            await ctx.info(f"処理中: {processed}/{total} - {current}")
                            await ctx.report_progress(float(processed), float(total))
                        continue
                    # result/error のみ最終結果として保持（ログ等の非JSON行で上書きしない）
                    if msg_type in {"result", "error"}:
                        result_line = line
            except json.JSONDecodeError:
                # 非 JSON 行（ログ等）は result_line を上書きしない
                pass
    except asyncio.CancelledError:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(asyncio.CancelledError):
                await process.wait()
        raise

    # stderr 読み取り + wait
    # NOTE: stderr はプロセス終了後に読み取る。worker が stderr に大量出力
    # （64KB超）した場合、stdout readline 中にパイプバッファが満杯になり
    # デッドロックするリスクがある。現時点では worker の stderr 出力量は
    # 少量のため問題ないが、大規模処理で顕在化する場合は stderr の
    # 並行読み取りへの変更を検討する。
    stderr_bytes = await process.stderr.read() if process.stderr else b""
    await process.wait()

    assert process.returncode is not None  # noqa: S101
    exit_code = process.returncode

    stderr_text = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    stderr_lines = stderr_text.rstrip().splitlines()
    stderr_tail = "\n".join(stderr_lines[-10:])

    return exit_code, result_line, stderr_tail


def _parse_pipeline_summary(data: dict[str, Any]) -> PipelineSummary | None:
    """JSON dict から PipelineSummary を復元する.

    パース失敗時は None を返す。
    """
    try:
        return PipelineSummary(
            mode=PipelineMode(data.get("mode", "incremental")),
            total_files=data.get("total_files", 0),
            processed=data.get("processed", 0),
            skipped=data.get("skipped", 0),
            errors=data.get("errors", []),
        )
    except ValueError:
        return None


async def _run_ingest_and_index_subprocess(
    commit_message: str,
    ctx: MCPContext | None = None,
) -> PipelineSummary:
    """ingest_and_index をサブプロセスで実行し PipelineSummary を返す.

    Raises:
        RuntimeError: サブプロセスの異常終了・クラッシュ・結果パース失敗時
    """
    exit_code, stdout_text, stderr_tail = await _run_worker_subprocess(
        "ingest-and-index", ["--commit-message", commit_message],
        ctx=ctx,
    )

    if exit_code != 0:
        if exit_code in _SEGFAULT_EXIT_CODES:
            raise RuntimeError(
                f"ingest-and-index がクラッシュしました (SEGFAULT, exit_code={exit_code})"
            )
        # worker がエラー JSON を出力している場合
        if stdout_text:
            try:
                error_data = json.loads(stdout_text.splitlines()[-1])
                if error_data.get("error"):
                    raise RuntimeError(
                        f"ingest-and-index エラー: {error_data.get('message', '不明なエラー')}"
                    )
            except (json.JSONDecodeError, IndexError):
                pass
        raise RuntimeError(
            f"ingest-and-index 異常終了 (exit_code={exit_code})\n{stderr_tail}"
        )

    if not stdout_text:
        raise RuntimeError("ingest-and-index の出力が空です")

    last_line = stdout_text.splitlines()[-1]
    try:
        result = json.loads(last_line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ingest-and-index の結果パースに失敗: {stdout_text}") from exc

    summary = _parse_pipeline_summary(result)
    if summary is None:
        raise RuntimeError(f"ingest-and-index の結果解析に失敗: {stdout_text}")
    return summary


async def _run_delete_subprocess(
    source_id: str,
    ctx: MCPContext | None = None,
) -> dict[str, Any]:
    """delete をサブプロセスで実行する.

    Returns:
        {"not_found": True} or {"deleted": True, "pipeline": PipelineSummary | None}
    """
    exit_code, stdout_text, stderr_tail = await _run_worker_subprocess(
        "delete", ["--source-id", source_id],
        ctx=ctx,
    )

    if exit_code != 0:
        if exit_code in _SEGFAULT_EXIT_CODES:
            raise RuntimeError(f"delete プロセスがクラッシュしました (SEGFAULT, exit_code={exit_code})")
        if stdout_text:
            try:
                error_data = json.loads(stdout_text.splitlines()[-1])
                if error_data.get("error"):
                    raise RuntimeError(error_data.get("message", "不明なエラー"))
            except (json.JSONDecodeError, IndexError):
                pass
        raise RuntimeError(f"delete プロセスが異常終了しました (exit_code={exit_code})\n{stderr_tail}")

    if not stdout_text:
        raise RuntimeError("delete プロセスの出力が空です")

    last_line = stdout_text.splitlines()[-1]
    try:
        result = json.loads(last_line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"delete の結果パースに失敗: {stdout_text}") from exc

    if result.get("not_found"):
        return {"not_found": True}

    # pipeline summary の復元
    pipeline_data = result.get("pipeline", {})
    pipeline_summary = _parse_pipeline_summary(pipeline_data) if pipeline_data else None

    return {"deleted": True, "pipeline": pipeline_summary}


async def _run_rebuild_subprocess(
    mode: str,
    source_type: str | None,
    ctx: MCPContext | None = None,
) -> str:
    """rebuild を CLI サブプロセスで実行する."""
    args = ["--mode", mode]
    if source_type is not None:
        args.extend(["--source-type", source_type])

    exit_code, stdout_text, stderr_tail = await _run_worker_subprocess(
        "rebuild", args, ctx=ctx,
    )

    # クラッシュ検出
    if exit_code != 0:
        if exit_code in _SEGFAULT_EXIT_CODES:
            return (
                f"エラー: 再構築プロセスがクラッシュしました"
                f"（SEGFAULT, exit_code={exit_code}）\n"
                f"インデックスが破損している可能性があります。"
                f"インデックスを手動削除して再実行してください。"
            )
        # worker がエラー JSON を stdout に出力している場合はそちらを優先
        if stdout_text:
            try:
                error_data = json.loads(stdout_text.splitlines()[-1])
                if error_data.get("error"):
                    return f"エラー: {error_data.get('message', '不明なエラー')}"
            except (json.JSONDecodeError, IndexError):
                pass
        return (
            f"エラー: 再構築プロセスが異常終了しました"
            f"（exit_code={exit_code}）\n{stderr_tail}"
        )

    # 正常終了: stdout の最終行を JSON としてパース
    # （BM25s 等が stdout に警告を出す場合があるため、最終行のみを対象にする）
    if not stdout_text:
        return "再構築完了（結果なし）"

    last_line = stdout_text.splitlines()[-1]
    try:
        result = json.loads(last_line)
    except json.JSONDecodeError:
        return f"再構築完了（結果の解析に失敗）\n{stdout_text}"

    summary = _parse_pipeline_summary(result)
    if summary is None:
        return f"再構築完了（結果の解析に失敗）\n{stdout_text}"

    elapsed = result.get("elapsed", 0)

    return _format_rebuild_summary(summary, elapsed)


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

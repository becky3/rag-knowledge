"""RAG MCP サーバー

仕様: docs/specs/rag-knowledge.md, docs/specs/search-response.md
独立リポジトリとして動作する。

FastMCP を使用して 21 個の RAG ツールを公開する:
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
- rag_list_recent: 指定 source_type のソースを新しい順で一覧取得
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
from .rag_knowledge import format_file_size, format_raw_search_results

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
    from .upload import sanitize_filename as sanitize_upload_filename
    from .pipeline.ingesters.aozora import AozoraIngester as PipelineAozoraIngester
    from .pipeline.ingesters.local import _UPLOAD_DIR as _LOCAL_UPLOAD_DIR
    from .pipeline.ingesters.web import WebIngester as PipelineWebIngester
    from .pipeline.models import PipelineMode, PipelineSummary, detect_source_type
    from .store.metadata_db import MetadataDB
    from .store.models import NULL_COMMIT_HASH
    from .store.source_store import SourceStore

from py_common_lib.httpx import ConstrainedClient  # safety:allowed

from .safe_browsing import (
    SafeBrowsingClient,
    create_safe_browsing_client,
)

from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

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

    CLI サブプロセスが BM25 インデックスをディスク上で更新した後、
    MCP プロセス内のインメモリ BM25 キャッシュが陳腐化するためリセットが必要。
    ChromaDB は HttpClient 経由のためクライアント側キャッシュの問題はない。
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
        # HttpClient で ChromaDB サーバーに接続。
        # ChromaDB サーバーの起動確保は _configure_and_run() の
        # ChromaDBServerManager.ensure_server_running() で実施済み。
        vector_store = VectorStore.create_http(
            embedding_provider=embedding_provider,
            host=settings.chromadb_server_host,
            port=settings.chromadb_server_port,
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
        (ext.strip() if ext.strip().startswith(".") else f".{ext.strip()}").lower()
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
    try:
        result = await _run_cli_subprocess("add", [url], ctx=ctx)
        return _format_cli_ingest_result(result, context=url)
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別のインジェストが実行中です。しばらく待ってから再試行してください"
        return f"エラー: ページの取り込みに失敗しました。URL: {url} ({e})"
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
    args: list[str] = [url]
    if pattern:
        args.extend(["--pattern", pattern])
    if depth is not None:
        args.extend(["--depth", str(depth)])

    try:
        result = await _run_cli_subprocess("crawl", args, ctx=ctx)
        return _format_cli_ingest_result(result, context=url)
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別のインジェストが実行中です。しばらく待ってから再試行してください"
        return f"エラー: クロールに失敗しました。URL: {url} ({e})"
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
    args: list[str] = [username]
    if content_type != "all":
        args.extend(["--content-type", content_type])
    if max_articles is not None:
        args.extend(["--max-articles", str(max_articles)])
    if force:
        args.append("--force")

    try:
        result = await _run_cli_subprocess("crawl-zenn", args, ctx=ctx)
        return _format_cli_ingest_result(result, context=f"ユーザー: {username}")
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別のインジェストが実行中です。しばらく待ってから再試行してください"
        return f"エラー: Zenn 記事の取り込みに失敗しました（ユーザー: {username}） ({e})"
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
    args: list[str] = [handle]
    if max_posts is not None:
        args.extend(["--max-posts", str(max_posts)])
    if include_reposts is True:
        args.append("--include-reposts")

    try:
        result = await _run_cli_subprocess("crawl-bluesky", args, ctx=ctx)
        return _format_cli_ingest_result(result, context=f"ハンドル: {handle}")
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別のインジェストが実行中です。しばらく待ってから再試行してください"
        return f"エラー: BlueSky 投稿の取り込みに失敗しました（ハンドル: {handle}） ({e})"
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
    try:
        result = await _run_cli_subprocess("ingest-youtube", [video_url], ctx=ctx)
        return _format_cli_ingest_result(result, context=f"動画: {video_url}")
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別のインジェストが実行中です。しばらく待ってから再試行してください"
        return f"エラー: YouTube 動画の取り込みに失敗しました: {video_url} ({e})"
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
    args: list[str] = [playlist_url]
    if max_videos is not None:
        args.extend(["--max-videos", str(max_videos)])

    try:
        result = await _run_cli_subprocess("ingest-youtube-playlist", args, ctx=ctx)
        return _format_cli_ingest_result(result, context=f"プレイリスト: {playlist_url}")
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別のインジェストが実行中です。しばらく待ってから再試行してください"
        return f"エラー: YouTube プレイリストの取り込みに失敗しました: {playlist_url} ({e})"
    except Exception:
        logger.exception(
            "Failed to crawl YouTube playlist: %s", playlist_url
        )
        return f"エラー: YouTube プレイリストの取り込みに失敗しました: {playlist_url}"


_VALID_UPLOAD_MODES: frozenset[str] = frozenset({"fail", "replace"})


@mcp.tool()
async def rag_add_document(
    content: str,
    filename: str,
    encoding: str = "text",
    upload_mode: str = "fail",
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG add document - ドキュメントファイルをナレッジベースに取り込む.

    knowledge base, ingest, document, file, text, upload.
    ドキュメントファイル（Markdown、テキスト、PDF、AsciiDoc）のコンテンツを受け取り、
    ナレッジベースに取り込む。stdio・HTTP 両モードで使用可能。

    Args:
        content: ファイルのコンテンツ。encoding=text の場合は UTF-8 文字列、
            encoding=base64 の場合は base64 エンコードされた文字列
        filename: 元ファイルのファイル名（例: resume.pdf, notes.md）。
            拡張子バリデーションおよびファイル配置先の命名に使用する
        encoding: コンテンツのエンコーディング。"text"（デフォルト）または "base64"。
            テキストファイルは "text"、バイナリファイル（PDF 等）は "base64" を使用する
        upload_mode: 同名ファイル存在時の動作。"fail"（エラー、デフォルト）または "replace"（上書き）

    Returns:
        取り込み結果のメッセージ
    """
    # MCP 側バリデーション（仕様: content-upload.md）
    if encoding not in ("text", "base64"):
        return f"エラー: 無効な encoding: {encoding!r}（有効値: text, base64）"
    if not content:
        return "エラー: content が空です"

    try:
        sanitized_filename = sanitize_upload_filename(filename)
    except ValueError as e:
        return f"エラー: {e}"

    args: list[str] = ["--stdin", "--filename", sanitized_filename]
    if encoding != "text":
        args.extend(["--encoding", encoding])
    if upload_mode != "fail":
        args.extend(["--upload-mode", upload_mode])

    try:
        result = await _run_cli_subprocess(
            "add-document", args, ctx=ctx, stdin_data=content,
        )
        return _format_cli_ingest_result(result, context=sanitized_filename)
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別のインジェストが実行中です。しばらく待ってから再試行してください"
        return f"エラー: ファイルの取り込みに失敗しました: {sanitized_filename} ({e})"
    except Exception:
        logger.exception("Failed to add document: %s", sanitized_filename)
        return f"エラー: ファイルの取り込みに失敗しました: {sanitized_filename}"


@mcp.tool()
async def rag_add_journal(
    title: str,
    content: str,
    filename: str,
    repository: str,
    entry_id: str | None = None,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG add journal - ジャーナルエントリをナレッジベースに登録する.

    journal, session log, work record, development diary.
    セッションごとの作業記録（ジャーナル）をナレッジベースに追加する。
    stdio・HTTP 両モードで使用可能。

    Args:
        title: エントリタイトル
        content: ジャーナル本文（Markdown）。MCP クライアントがファイルを読み込んでテキスト文字列として渡す
        filename: 元ファイルのファイル名（例: session-summary.md）。.md 拡張子であることの確認に使用する
        repository: リポジトリ名（例: rag-knowledge）
        entry_id: エントリ識別子（更新時に使用。未指定時は自動生成。命名規則: YYYYMMDD-HHMMSS-topic）

    Returns:
        取り込み結果のメッセージ
    """
    try:
        sanitized_filename = sanitize_upload_filename(filename)
    except ValueError as e:
        return f"エラー: {e}"

    if not sanitized_filename.lower().endswith(".md"):
        return f"エラー: filename の拡張子が .md ではありません: {sanitized_filename!r}"

    args: list[str] = ["--stdin", "--title", title, "--repository", repository]
    if entry_id:
        args.extend(["--entry-id", entry_id])

    try:
        result = await _run_cli_subprocess(
            "add-journal", args, ctx=ctx, stdin_data=content,
        )
        return _format_cli_ingest_result(
            result, context=f"journal: {repository}/{entry_id or title}",
        )
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別のインジェストが実行中です。しばらく待ってから再試行してください"
        return f"エラー: ジャーナルエントリの登録に失敗しました: {title} ({e})"
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

    args: list[str] = [dir_path]
    if pattern != "**/*":
        args.extend(["--pattern", pattern])
    if upload_mode != "fail":
        args.extend(["--upload-mode", upload_mode])

    try:
        result = await _run_cli_subprocess("crawl-documents", args, ctx=ctx)
        return _format_cli_ingest_result(result, context=f"ディレクトリ: {dir_path}")
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別のインジェストが実行中です。しばらく待ってから再試行してください"
        return f"エラー: ドキュメントの取り込みに失敗しました（ディレクトリ: {dir_path}） ({e})"
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

    並行実行非対応: Bridge が source_store へのファイル配置をロック保護外で
    実行するため、同一サイトに対する同時実行はデータ競合のリスクがある。

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
            # CLI サブプロセスで差分更新（commit も CLI 内でロック保護下で実行）
            commit_msg = f"ingest(web): site-ingest {url}"
            rebuild_result = await _run_cli_subprocess(
                "rebuild",
                ["--mode", "incremental", "--commit-message", commit_msg],
                ctx=ctx,
            )
            pipeline_summary = _parse_pipeline_summary(rebuild_result)
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
    try:
        result = await _run_cli_subprocess("update-aozora-catalog", ctx=ctx)
        return str(result.get("message", "カタログ更新完了"))
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別のインジェストが実行中です。しばらく待ってから再試行してください"
        return f"エラー: 青空文庫カタログの更新に失敗しました ({e})"
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
    try:
        result = await _run_cli_subprocess("ingest-aozora", [book_id], ctx=ctx)
        return _format_cli_ingest_result(result, context=f"作品ID: {book_id}")
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別のインジェストが実行中です。しばらく待ってから再試行してください"
        return f"エラー: 青空文庫作品の取り込みに失敗しました（作品ID: {book_id}） ({e})"
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
    args: list[str] = [person_id]
    if max_works is not None:
        args.extend(["--max-works", str(max_works)])

    try:
        result = await _run_cli_subprocess("ingest-aozora-author", args, ctx=ctx)
        return _format_cli_ingest_result(result, context=f"人物ID: {person_id}")
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別のインジェストが実行中です。しばらく待ってから再試行してください"
        return f"エラー: 青空文庫作品の取り込みに失敗しました（人物ID: {person_id}） ({e})"
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
    try:
        result = await _run_cli_subprocess("delete", [url], ctx=ctx)

        if result.get("not_found"):
            return f"該当するソースが見つかりませんでした: {url}"

        pipeline_data = result.get("pipeline")
        if pipeline_data:
            pipeline_summary = _parse_pipeline_summary(pipeline_data)
            if pipeline_summary and pipeline_summary.errors:
                errors_text = "; ".join(pipeline_summary.errors)
                return f"削除しましたが、パイプラインでエラーが発生しました: {url} ({errors_text})"
        return f"削除しました: {url}"
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別の操作が実行中です。しばらく待ってから再試行してください"
        return f"エラー: 削除に失敗しました。URL: {url} ({e})"
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


_format_size = format_file_size


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
    args: list[str] = ["--mode", mode]
    if source_type is not None:
        args.extend(["--source-type", source_type])

    try:
        result = await _run_cli_subprocess("rebuild", args, ctx=ctx)

        # result dict → フォーマット済みテキスト
        summary = _parse_pipeline_summary(result)
        elapsed = result.get("elapsed", 0.0)
        if summary is not None:
            return _format_rebuild_summary(summary, float(elapsed))
        return "再構築完了（結果の解析に失敗）"
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別の再構築が実行中です"
        return f"エラー: 再構築中にエラーが発生しました ({e})"
    except Exception:
        logger.exception("再構築中にエラーが発生しました")
        return "エラー: 再構築中にエラーが発生しました"


# --- ロック競合判定キーワード ---
_LOCK_CONFLICT_KEYWORDS: frozenset[str] = frozenset({
    "ロック競合",
    "lock conflict",
    "already locked",
})


def _is_lock_conflict_error(message: str) -> bool:
    """エラーメッセージがロック競合を示すかを判定する."""
    lower = message.lower()
    return any(kw in lower for kw in _LOCK_CONFLICT_KEYWORDS)


class CLISubprocessError(Exception):
    """CLI サブプロセスの実行エラー."""

    def __init__(self, message: str, *, lock_conflict: bool = False) -> None:
        super().__init__(message)
        self.lock_conflict = lock_conflict


async def _run_cli_subprocess(
    command: str,
    args: list[str] | None = None,
    *,
    ctx: MCPContext | None = None,
    stdin_data: str | None = None,
) -> dict[str, Any]:
    """CLI コマンドをサブプロセスで実行し結果を返す.

    MCP 薄層アダプターの中核関数。書き込み系ツールを CLI サブプロセスとして実行し、
    C 拡張（BM25s 等）の SEGFAULT からサーバープロセスを隔離する。

    Args:
        command: CLI サブコマンド名（例: "add", "crawl", "rebuild"）
        args: サブコマンド引数のリスト
        ctx: MCP Context（進捗通知用、任意）
        stdin_data: stdin に書き込むデータ（add-journal, add-document 用）

    Returns:
        CLI が出力した result JSON の dict

    Raises:
        CLISubprocessError: サブプロセスの異常終了・クラッシュ・結果パース失敗時。
            lock_conflict=True の場合はロック競合エラー。
    """
    cmd = [
        sys.executable, "-m", "rag.cli",
        command, "--output", "json",
        *(args or []),
    ]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    stdin_mode = asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stdin=stdin_mode,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    # stdin にデータを書き込んでクローズする
    if stdin_data is not None:
        assert process.stdin is not None  # noqa: S101
        try:
            process.stdin.write(stdin_data.encode("utf-8"))
            await process.stdin.drain()
        finally:
            process.stdin.close()
            await process.stdin.wait_closed()

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
                    # result/error のみ最終結果として保持
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
    stderr_bytes = await process.stderr.read() if process.stderr else b""
    await process.wait()

    assert process.returncode is not None  # noqa: S101
    exit_code = process.returncode

    stderr_text = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    stderr_lines = stderr_text.rstrip().splitlines()
    stderr_tail = "\n".join(stderr_lines[-10:])

    # キャッシュリセット（サブプロセスが DB を更新するため）
    _reset_pipeline_controller()
    _reset_rag_service()

    # SEGFAULT 検出
    if exit_code in _SEGFAULT_EXIT_CODES:
        raise CLISubprocessError(
            f"CLI サブプロセスがクラッシュしました (SEGFAULT, exit_code={exit_code})"
        )

    # エラー処理
    if exit_code != 0:
        if result_line:
            try:
                error_data = json.loads(result_line)
                if error_data.get("error") or error_data.get("type") == "error":
                    error_msg = error_data.get("message", "不明なエラー")
                    raise CLISubprocessError(
                        error_msg,
                        lock_conflict=_is_lock_conflict_error(error_msg),
                    )
            except json.JSONDecodeError:
                pass
        raise CLISubprocessError(
            f"CLI サブプロセスが異常終了しました (exit_code={exit_code})\n{stderr_tail}"
        )

    # 結果パース
    if not result_line:
        raise CLISubprocessError("CLI サブプロセスの出力が空です")

    try:
        result: dict[str, Any] = json.loads(result_line)
    except json.JSONDecodeError as exc:
        raise CLISubprocessError(
            f"CLI サブプロセスの結果パースに失敗: {result_line}"
        ) from exc

    return result


def _format_cli_ingest_result(
    result: dict[str, Any],
    *,
    context: str = "",
) -> str:
    """CLI サブプロセスの result dict を MCP レスポンス文字列に変換する.

    _run_cli_subprocess の戻り値（IngestResult + PipelineSummary の dict 表現）を
    既存の _format_ingest_response と同等のフォーマットに変換する。
    """
    ingest_result = IngestResult(
        placed=result.get("placed", 0),
        skipped=result.get("skipped", 0),
        overwritten=result.get("overwritten", 0),
        errors=result.get("errors", 0),
        error_details=result.get("error_details", []),
    )

    pipeline_data = result.get("pipeline")
    pipeline_summary = _parse_pipeline_summary(pipeline_data) if pipeline_data else None

    return _format_ingest_response(ingest_result, pipeline_summary, context=context)


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


@mcp.tool()
async def rag_list_recent(
    source_type: str,
    limit: int | None = None,
) -> str:
    """[rag-knowledge] List recent sources - 指定した source_type のソースを新しい順で一覧取得する.

    content listing, recent sources, source list, browse.
    ナレッジベースに取り込んだコンテンツを source_type 別に一覧で確認できる。
    最近取り込んだコンテンツの確認や、ナレッジベースの内容把握に使用する。

    Args:
        source_type: ソース種別: "web", "bluesky", "zenn", "youtube", "aozora", "local", "journal"
        limit: 取得件数（1〜100、未指定時は設定値を使用）

    Returns:
        ソース一覧テキスト（タイトル、source_id、collected_at、ファイルサイズ）
    """
    if source_type not in _VALID_SOURCE_TYPES:
        valid = ", ".join(sorted(_VALID_SOURCE_TYPES))
        return f"無効な source_type: {source_type!r}（有効値: {valid}）"

    settings = get_settings()
    if limit is None:
        limit = settings.rag_list_recent_limit
    if limit < 1 or limit > 100:
        return "エラー: limit は 1〜100 の範囲で指定してください"

    from .rag_knowledge import list_recent_sources

    return await asyncio.to_thread(
        list_recent_sources,
        settings.source_store_dir,
        source_type,
        limit,
    )


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


# --- Upload HTTP API ---


async def _check_api_key(request: Request) -> str | None:
    """API キー認証のプレースホルダー.

    TODO:#402 認証仕様完成後に実装を差し替える。
    現時点では常に認証成功として None を返す。

    Returns:
        None: 認証成功、str: エラーメッセージ（認証失敗）
    """
    return None


def _upload_error(status_code: int, message: str) -> JSONResponse:
    """Upload API のエラーレスポンスを生成する."""
    return JSONResponse(
        {"status": "error", "message": message},
        status_code=status_code,
    )


def _upload_success(message: str, source_id: str) -> JSONResponse:
    """Upload API の成功レスポンスを生成する."""
    return JSONResponse(
        {"status": "ok", "message": message, "source_id": source_id},
    )


async def _read_upload_file(
    request: Request,
    max_size_bytes: int,
) -> tuple[bytes, str] | JSONResponse:
    """multipart/form-data からファイルを読み取る.

    Returns:
        (data, filename) タプル、またはエラー時は JSONResponse
    """
    # Content-Length による事前チェック
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_size_bytes:
                max_mb = max_size_bytes // (1024 * 1024)
                return _upload_error(
                    413,
                    f"ファイルサイズが上限を超えています（上限: {max_mb} MB）",
                )
        except ValueError:
            pass

    form = await request.form()
    file_field = form.get("file")
    if file_field is None or not hasattr(file_field, "read"):
        return _upload_error(400, "file フィールドが未指定です")

    filename = getattr(file_field, "filename", "") or ""

    # ストリーミング読み取りでサイズチェック
    data = bytearray()
    total = 0
    while True:
        chunk = await file_field.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size_bytes:
            max_mb = max_size_bytes // (1024 * 1024)
            return _upload_error(
                413,
                f"ファイルサイズが上限を超えています（上限: {max_mb} MB）",
            )
        data.extend(chunk)

    if total == 0:
        return _upload_error(400, "アップロードファイルが空です（0 バイト）")

    return bytes(data), filename


@mcp.custom_route("/upload/document", methods=["POST"])  # type: ignore[untyped-decorator]
async def upload_document(request: Request) -> Response:
    """ドキュメントファイルをアップロードしてインジェストする."""
    import tempfile

    # 認証チェック
    auth_error = await _check_api_key(request)
    if auth_error is not None:
        return _upload_error(401, auth_error)

    sanitized = ""
    try:
        settings = get_settings()
        max_size_bytes = settings.rag_upload_max_file_size_mb * 1024 * 1024

        # ファイル読み取り
        result = await _read_upload_file(request, max_size_bytes)
        if isinstance(result, JSONResponse):
            return result
        data, raw_filename = result

        # フォームフィールド取得
        form = await request.form()
        upload_mode = str(form.get("upload_mode", "fail"))
        if upload_mode not in _VALID_UPLOAD_MODES:
            valid = ", ".join(sorted(_VALID_UPLOAD_MODES))
            return _upload_error(400, f"無効な upload_mode: {upload_mode!r}（有効値: {valid}）")

        # ファイル名サニタイズ・拡張子チェック
        try:
            sanitized = sanitize_upload_filename(raw_filename)
        except ValueError as e:
            return _upload_error(400, str(e))

        supported = _get_supported_extensions()
        ext = Path(sanitized).suffix.lower()
        if not ext or ext not in supported:
            return _upload_error(
                400,
                f"対応していないファイル形式です: {sanitized!r}（対応: {', '.join(supported)}）",
            )

        # 一時ファイルにコンテンツを書き出し、CLI --file 経由で処理
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=Path(sanitized).suffix,
            ) as tmp:
                tmp.write(data)
                tmp_path = tmp.name

            args = ["--file", tmp_path, "--filename", sanitized]
            if upload_mode != "fail":
                args.extend(["--upload-mode", upload_mode])

            await _run_cli_subprocess("add-document", args)

            import datetime as _dt
            _today = _dt.date.today()
            source_id = f"local/{_LOCAL_UPLOAD_DIR}/{_today.year}/{_today.month:02d}/{_today.day:02d}/{sanitized}"
            return _upload_success(
                f"ドキュメントを取り込みました: {sanitized}",
                source_id=source_id,
            )
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

    except CLISubprocessError as e:
        if e.lock_conflict:
            return _upload_error(409, "別のインジェストが実行中です")
        message = str(e)
        if "同名" in message:
            return _upload_error(409, message)
        logger.error("Upload document CLI error for %s: %s", sanitized, e)
        return _upload_error(500, "インジェスト処理中にエラーが発生しました")
    except Exception:
        logger.exception("Upload document failed: %s", sanitized)
        return _upload_error(500, "インジェスト処理中にエラーが発生しました")


@mcp.custom_route("/upload/journal", methods=["POST"])  # type: ignore[untyped-decorator]
async def upload_journal(request: Request) -> Response:
    """ジャーナル Markdown ファイルをアップロードしてインジェストする."""
    import tempfile

    # 認証チェック
    auth_error = await _check_api_key(request)
    if auth_error is not None:
        return _upload_error(401, auth_error)

    title = ""
    repository = ""
    try:
        settings = get_settings()
        max_size_bytes = settings.rag_upload_max_file_size_mb * 1024 * 1024

        # ファイル読み取り
        result = await _read_upload_file(request, max_size_bytes)
        if isinstance(result, JSONResponse):
            return result
        data, raw_filename = result

        # ファイル名サニタイズ・拡張子チェック
        try:
            sanitized = sanitize_upload_filename(raw_filename)
        except ValueError as e:
            return _upload_error(400, str(e))

        if not sanitized.lower().endswith(".md"):
            return _upload_error(
                400,
                f"ジャーナルは .md ファイルのみ対応しています: {sanitized!r}",
            )

        # フォームフィールド取得
        form = await request.form()
        title = str(form.get("title", "")).strip()
        repository = str(form.get("repository", "")).strip()
        entry_id = form.get("entry_id")
        entry_id_str = str(entry_id).strip() if entry_id else None

        if not title:
            return _upload_error(400, "title が未指定です")
        if not repository:
            return _upload_error(400, "repository が未指定です")

        # UTF-8 デコードチェック（ジャーナルは Markdown テキストのため）
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return _upload_error(400, "ファイルが UTF-8 としてデコードできません")

        # 一時ファイルにコンテンツを書き出し、CLI --file 経由で処理
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=".md",
            ) as tmp:
                tmp.write(data)
                tmp_path = tmp.name

            args = ["--file", tmp_path, "--title", title, "--repository", repository]
            if entry_id_str:
                args.extend(["--entry-id", entry_id_str])

            cli_result = await _run_cli_subprocess("add-journal", args)

            # CLI の result から entry_id を取得（利用可能な場合）
            resolved_entry_id = cli_result.get("entry_id") or entry_id_str or title
            source_id = f"journal/{repository}/{resolved_entry_id}.md"
            return _upload_success(
                f"ジャーナルエントリを登録しました: {repository}/{resolved_entry_id}",
                source_id=source_id,
            )
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

    except CLISubprocessError as e:
        if e.lock_conflict:
            return _upload_error(409, "別のインジェストが実行中です")
        message = str(e)
        if "同名" in message:
            return _upload_error(409, message)
        logger.error("Upload journal CLI error for %s/%s: %s", repository, title, e)
        return _upload_error(500, "インジェスト処理中にエラーが発生しました")
    except Exception:
        logger.exception("Upload journal failed: %s/%s", repository, title)
        return _upload_error(500, "インジェスト処理中にエラーが発生しました")


def _configure_and_run() -> None:
    """トランスポート設定に基づいて MCP サーバーを起動する."""
    settings = get_settings()

    # ChromaDB サーバーの起動確保（グレースフルデグレード: 失敗しても MCP は稼働継続）
    import atexit

    from .infrastructure.chromadb_manager import ChromaDBServerManager

    chromadb_manager = ChromaDBServerManager(
        host=settings.chromadb_server_host,
        port=settings.chromadb_server_port,
        persist_dir=settings.chromadb_persist_dir,
        auto_start=settings.chromadb_auto_start,
    )
    chromadb_manager.ensure_server_running()
    atexit.register(chromadb_manager.shutdown)

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

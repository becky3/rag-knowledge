"""RAG MCP サーバー

仕様: docs/specs/rag-knowledge.md, docs/specs/search-response.md
独立リポジトリとして動作する。

FastMCP を使用して 18 個の RAG ツールを公開する:
- rag_search: ナレッジベース検索（チャンク単位返却）
- rag_get_document: ソース全文取得
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
- rag_delete: ソース識別子指定でナレッジから論理削除
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
import re
import sys
from pathlib import Path
from typing import Any

# ChromaDB テレメトリを無効化（import 前に設定する必要がある）
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

# bm25s が "resource module not available on Windows" を stdout に print する
# 問題への対策として、import 時に stdout を抑制する。
from .config import ensure_utf8_streams
from .rag_knowledge import format_file_size

with contextlib.redirect_stdout(io.StringIO()):
    from .config import UPLOAD_API_KEY_NAME, UPLOAD_API_KEY_SERVICE, get_settings

    # パイプライン関連
    from .pipeline.ingesters._common import IngestResult
    from .upload import sanitize_filename as sanitize_upload_filename
    from .pipeline.ingesters.local import _UPLOAD_DIR as _LOCAL_UPLOAD_DIR

    from .pipeline.models import PipelineMode, PipelineSummary
from .safe_browsing import (
    SafeBrowsingClient,
    SafeBrowsingConfigError,
    SafetyCheckError,
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

_safe_browsing_client_cache: SafeBrowsingClient | None = None
_safe_browsing_client_initialized = False


def _reset_safe_browsing_client() -> None:
    """SafeBrowsingClient のキャッシュをリセットする（テスト用）."""
    global _safe_browsing_client_cache, _safe_browsing_client_initialized
    _safe_browsing_client_cache = None
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
        if pipeline_summary.warnings:
            details = "; ".join(pipeline_summary.warnings[:5])
            parts.append(f"パイプライン警告: {len(pipeline_summary.warnings)}件 ({details})")
        if pipeline_summary.errors:
            details = "; ".join(pipeline_summary.errors[:5])
            parts.append(f"パイプラインエラー: {len(pipeline_summary.errors)}件 ({details})")
    return " / ".join(parts)


# --- ファクトリヘルパー ---


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

    args: list[str] = ["--query", query]
    if n_results is not None:
        args.extend(["--n-results", str(n_results)])
    if source_type is not None:
        args.extend(["--source-type", source_type])
    if filters is not None:
        args.extend(["--filters", filters])

    try:
        result = await _run_cli_subprocess("search", args)
        return _format_cli_search_result(result)
    except CLISubprocessError as e:
        return f"エラー: 検索に失敗しました ({e})"


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

    args: list[str] = [source_id]
    if format != "text":
        args.extend(["--format", format])

    try:
        result = await _run_cli_subprocess("get-document", args)
        return _format_cli_document_result(result)
    except CLISubprocessError as e:
        return f"エラー: ドキュメント取得に失敗しました ({e})"


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
    stdio モード専用。HTTP モードではクライアントとサーバーが別マシンの可能性があり、
    ローカルパスを解決できないため無効。

    Args:
        dir_path: 取り込み対象ディレクトリのパス（絶対パスまたは相対パス）
        pattern: glob パターン（デフォルト: ``**/*`` で再帰的に全対応ファイルを検索）
        upload_mode: 同名ファイル存在時の動作。"fail"（スキップ、デフォルト）または "replace"（上書き）

    Returns:
        取り込み結果のサマリーテキスト
    """
    if get_settings().rag_transport == "http":
        return "エラー: rag_crawl_documents は HTTP モードでは無効です（クライアントとサーバーが別マシンの可能性があり、ローカルパスを解決できないため）"

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
    url: str = "",
    urls: list[str] | None = None,
    url_pattern: str = "",
    max_pages: int | None = None,
    force: bool = False,
    download_only: bool = False,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG site ingest - Scrapy でサイトを一括取り込み.

    knowledge base, bulk ingest, site crawl, large scale, scrapy, multi url.
    Scrapy subprocess で Web ページを一括取り込みする。
    単一 URL: リンクを辿るクロールモード（大規模サイト向け）。
    複数 URL: 指定 URL のみ取得する複数 URL モード。

    並行実行非対応: Bridge が source_store へのファイル配置をロック保護外で
    実行するため、同一サイトに対する同時実行はデータ競合のリスクがある。

    Args:
        url: クロール開始 URL（クロールモード、urls と排他）
        urls: 取得対象 URL のリスト（複数 URL モード、url と排他）
        url_pattern: URL フィルタパターン（正規表現、クロールモードのみ）
        max_pages: ページ数上限（クロールモードのみ、未指定時は設定値を使用）
        force: True の場合、JOBDIR を削除して最初からクロール（クロールモードのみ）
        download_only: True の場合、Scrapy クロール + Bridge まで実行し、
            パイプライン処理（コンバート・インデックス構築）をスキップする

    Returns:
        取り込み結果のサマリー
    """
    from .utils.url import check_ssrf, validate_url

    # url / urls の排他チェック
    effective_urls = urls or []
    if url and effective_urls:
        return "エラー: url と urls は排他です。どちらか一方のみ指定してください"
    if not url and not effective_urls:
        return "エラー: url または urls を指定してください"

    # 単一 URL モード → リストに統一
    if url:
        effective_urls = [url]

    multi_url_mode = len(effective_urls) >= 2

    # 全 URL バリデーション
    validated_urls: list[str] = []
    for u in effective_urls:
        try:
            validated = validate_url(u)
            check_ssrf(validated)
            validated_urls.append(validated)
        except ValueError as e:
            return f"エラー: {e}"

    # Safe Browsing チェック（クロールモードのみ: 起点 URL）
    # 複数 URL モードでは数百件の URL に対する Google Safe Browsing API 呼び出しは
    # 非現実的なためスキップする（仕様: site-ingest.md「Safe Browsing チェック」）
    if not multi_url_mode:
        try:
            sb_client = _get_safe_browsing_client()
            if sb_client is not None:
                sb_result = await sb_client.check_url(validated_urls[0])
                if not sb_result.is_safe:
                    threat_types = ", ".join(t.threat_type.value for t in sb_result.threats)
                    return f"エラー: 起点URLが安全でないと判定されました: {threat_types} — {validated_urls[0]}"
        except SafeBrowsingConfigError:
            logger.warning("Safe Browsing の設定エラーのためチェックをスキップします: %s", validated_urls[0])
        except SafetyCheckError as e:
            return f"エラー: URL安全性チェックに失敗しました: {e}"

    # CLI subprocess に委譲
    cli_args: list[str] = list(validated_urls)
    if not multi_url_mode:
        if url_pattern:
            import re
            try:
                re.compile(url_pattern)
            except re.error as e:
                return f"エラー: 無効な正規表現パターン: {e}"
            cli_args.extend(["--url-pattern", url_pattern])
        if max_pages is not None:
            cli_args.extend(["--max-pages", str(max_pages)])
        if force:
            cli_args.append("--force")
    if download_only:
        cli_args.append("--download-only")

    display_url = validated_urls[0] if not multi_url_mode else f"{len(validated_urls)} URLs"

    try:
        result = await _run_cli_subprocess("site-ingest", cli_args, ctx=ctx)
        return _format_cli_ingest_result(result, context=f"サイト: {display_url}")
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別のインジェストが実行中です。しばらく待ってから再試行してください"
        return f"エラー: サイト取り込みに失敗しました（{display_url}） ({e})"
    except Exception:
        logger.exception("Failed to site-ingest: %s", display_url)
        return f"エラー: サイト取り込みに失敗しました（{display_url}）"


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
        limit: 最大表示件数（デフォルト: 20、許容範囲: 1〜2000）

    Returns:
        検索結果リスト（作品 ID、タイトル、著者名、著作権フラグ）
    """
    args: list[str] = []
    if author is not None:
        args.extend(["--author", author])
    if title is not None:
        args.extend(["--title", title])
    if limit != 20:
        args.extend(["--limit", str(limit)])

    try:
        result = await _run_cli_subprocess("search-aozora", args)
        return _format_cli_search_aozora_result(result)
    except CLISubprocessError as e:
        return f"エラー: 青空文庫カタログ検索に失敗しました ({e})"


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
async def rag_delete(source_id: str, ctx: MCPContext | None = None) -> str:
    """[rag-knowledge] RAG delete - ソース識別子指定でナレッジから削除.

    knowledge base, remove source, delete document.
    source_store からファイルを物理削除し、パイプライン経由で
    インデックス・metadata.db を更新する。
    git 管理下のため、削除後も git checkout で復旧可能。

    Args:
        source_id: 削除するソース識別子（source_id）

    Returns:
        削除結果のメッセージ
    """
    try:
        result = await _run_cli_subprocess("delete", [source_id], ctx=ctx)

        if result.get("not_found"):
            return f"該当するソースが見つかりませんでした: {source_id}"

        pipeline_data = result.get("pipeline")
        if pipeline_data:
            pipeline_summary = _parse_pipeline_summary(pipeline_data)
            if pipeline_summary and pipeline_summary.errors:
                errors_text = "; ".join(pipeline_summary.errors)
                return f"削除しましたが、パイプラインでエラーが発生しました: {source_id} ({errors_text})"
        return f"削除しました: {source_id}"
    except CLISubprocessError as e:
        if e.lock_conflict:
            return "エラー: 別の操作が実行中です。しばらく待ってから再試行してください"
        return f"エラー: 削除に失敗しました。source_id: {source_id} ({e})"
    except Exception:
        logger.exception("Failed to delete: %s", source_id)
        return f"エラー: 削除に失敗しました。source_id: {source_id}"


# --- 再構築 ---

_VALID_REBUILD_MODES: frozenset[str] = frozenset({
    "full", "convert", "index", "incremental",
})
_VALID_PIPELINE_SOURCE_TYPES: frozenset[str] = frozenset({
    "web", "bluesky", "zenn", "youtube", "aozora", "local",
})


def _format_phase_summary(phase: str, summary: PipelineSummary) -> list[str]:
    """1フェーズの PipelineSummary をテキスト行リストに変換する."""
    parts = [
        f"  [{phase}]",
        f"    処理件数: {summary.processed}",
        f"    警告: {len(summary.warnings)}",
        f"    エラー: {len(summary.errors)}",
    ]
    if summary.warnings:
        parts.append("    警告詳細:")
        for warn in summary.warnings[:10]:
            parts.append(f"      - {warn}")
        if len(summary.warnings) > 10:
            parts.append(f"      ... 他 {len(summary.warnings) - 10} 件")
    if summary.errors:
        parts.append("    エラーファイル:")
        for err_file in summary.errors[:10]:
            parts.append(f"      - {err_file}")
        if len(summary.errors) > 10:
            parts.append(f"      ... 他 {len(summary.errors) - 10} 件")
    return parts


def _format_rebuild_summary(summary: PipelineSummary, elapsed: float) -> str:
    """PipelineSummary をテキストに変換する."""
    mode_names = {
        PipelineMode.CONVERT_ONLY: "コンバートのみ再実行",
        PipelineMode.INDEX_ONLY: "インデックスのみ再構築",
        PipelineMode.INCREMENTAL: "差分更新",
    }
    mode_name = mode_names.get(summary.mode, str(summary.mode.value))

    parts = [
        f"再構築完了 ({mode_name})",
        f"  処理件数: {summary.processed}",
        f"  警告: {len(summary.warnings)}",
        f"  エラー: {len(summary.errors)}",
        f"  所要時間: {elapsed:.1f} 秒",
    ]
    if summary.warnings:
        parts.append("  警告詳細:")
        for warn in summary.warnings[:10]:
            parts.append(f"    - {warn}")
        if len(summary.warnings) > 10:
            parts.append(f"    ... 他 {len(summary.warnings) - 10} 件")
    if summary.errors:
        parts.append("  エラーファイル:")
        for err_file in summary.errors[:10]:
            parts.append(f"    - {err_file}")
        if len(summary.errors) > 10:
            parts.append(f"    ... 他 {len(summary.errors) - 10} 件")

    return "\n".join(parts)


def _format_full_rebuild_summary(
    convert: PipelineSummary,
    index: PipelineSummary,
    elapsed: float,
) -> str:
    """FullRebuildResult の2フェーズ結果をテキストに変換する."""
    parts = ["再構築完了 (全再構築)"]
    parts.extend(_format_phase_summary("Convert", convert))
    parts.extend(_format_phase_summary("Index", index))
    parts.append(f"  所要時間: {elapsed:.1f} 秒")
    return "\n".join(parts)


# SEGFAULT を示す exit code
# Windows: 0xC0000005 は signed (-1073741819) / unsigned (3221225477) 両方で返りうる
# Unix: SIGSEGV=11, shell: 128+11=139
_SEGFAULT_EXIT_CODES: frozenset[int] = frozenset({-1073741819, 3221225477, -11, 139})


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
        処理結果サマリ（処理件数、エラー件数、所要時間）
    """
    args: list[str] = ["--mode", mode]
    if source_type is not None:
        args.extend(["--source-type", source_type])

    try:
        result = await _run_cli_subprocess("rebuild", args, ctx=ctx)
        elapsed = float(result.get("elapsed", 0.0))

        # full モードは2フェーズ結果
        if mode == "full":
            convert_data = result.get("convert")
            index_data = result.get("index")
            if convert_data and index_data:
                convert_summary = _parse_pipeline_summary(convert_data)
                index_summary = _parse_pipeline_summary(index_data)
                if convert_summary and index_summary:
                    return _format_full_rebuild_summary(
                        convert_summary, index_summary, elapsed,
                    )
            return "再構築完了（結果の解析に失敗）"

        # その他のモードは単一 PipelineSummary
        summary = _parse_pipeline_summary(result)
        if summary is not None:
            return _format_rebuild_summary(summary, elapsed)
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

    async def _read_stdout() -> str:
        """stdout を行単位で読み、progress 通知を処理し、最終 result/error 行を返す."""
        _result_line = ""
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
                            await ctx.report_progress(
                                float(processed), float(total),
                                message=current,
                            )
                        continue
                    # result/error のみ最終結果として保持
                    if msg_type in {"result", "error"}:
                        _result_line = line
            except json.JSONDecodeError:
                # 非 JSON 行（ログ等）は result_line を上書きしない
                pass
        return _result_line

    async def _drain_stderr() -> bytes:
        """stderr を並行に drain する（パイプバッファ溢れ防止）."""
        if process.stderr is None:
            return b""
        return await process.stderr.read()

    # stdout と stderr を並行に読み取る（パイプバッファのデッドロック防止）
    try:
        result_line, stderr_bytes = await asyncio.gather(
            _read_stdout(),
            _drain_stderr(),
        )
    except asyncio.CancelledError:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(asyncio.CancelledError):
                await process.wait()
        raise

    await process.wait()

    assert process.returncode is not None  # noqa: S101
    exit_code = process.returncode

    stderr_text = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    stderr_lines = stderr_text.rstrip().splitlines()
    stderr_tail = "\n".join(stderr_lines[-10:])

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
            errors=data.get("errors", []),
            warnings=data.get("warnings", []),
        )
    except ValueError:
        return None


def _format_chunk_position(chunk_index: int, total_chunks: int) -> str:
    """チャンク位置を表示用文字列にフォーマットする."""
    pos = chunk_index + 1
    if total_chunks > 0:
        return f"{pos}/{total_chunks}"
    return f"{pos}/?"


def _format_cli_search_result(result: dict[str, Any]) -> str:
    """CLI search の JSON 結果を MCP レスポンス文字列に変換する."""
    vector_results = result.get("vector_results", [])
    bm25_results = result.get("bm25_results", [])

    if not vector_results and not bm25_results:
        return "該当する情報が見つかりませんでした"

    sections: list[tuple[str, list[dict[str, Any]], str]] = []
    if vector_results:
        sections.append(("## ベクトル検索結果 (意味的類似度)\n", vector_results, "distance"))
    if bm25_results:
        sections.append(("## BM25 検索結果 (キーワード一致)\n", bm25_results, "score"))

    parts: list[str] = []
    for header, items, score_key in sections:
        parts.append(header)
        for i, item in enumerate(items, start=1):
            score_val = item.get(score_key, 0.0)
            parts.append(f"### Result {i} [{score_key}={score_val:.3f}]")

            chunk_pos = _format_chunk_position(
                item.get("chunk_index", 0), item.get("total_chunks", 0),
            )
            parts.append(f"Source: {item.get('source_url', '')}")
            parts.append(f"Title: {item.get('title', '')}")
            parts.append(f"Chunk: {chunk_pos}")
            parts.append(f"Type: {item.get('source_type', '')}")
            section_path = item.get("section_path", "")
            if section_path:
                parts.append(f"Section: {section_path}")
            collected_at = item.get("collected_at", "")
            if collected_at:
                parts.append(f"Collected: {collected_at}")
            parts.append("")
            parts.append(item.get("text", ""))
            parts.append("")

    return "\n".join(parts).rstrip()


def _format_cli_document_result(result: dict[str, Any]) -> str:
    """CLI get-document の JSON 結果を MCP レスポンス文字列に変換する."""
    parts: list[str] = []
    parts.append(f"Source: {result.get('source_id', '')}")
    if result.get("title"):
        parts.append(f"Title: {result['title']}")
    if result.get("source_type"):
        parts.append(f"Type: {result['source_type']}")
    if result.get("collected_at"):
        parts.append(f"Collected: {result['collected_at']}")
    fmt = result.get("format", "text")
    parts.append(f"Format: {fmt}")
    parts.append("")
    content = result.get("content", "")
    parts.append(content)

    response = "\n".join(parts)

    # MCP 経由の場合、rag_max_response_chars でトランケーション
    settings = get_settings()
    max_chars = settings.rag_max_response_chars
    if max_chars is not None and len(response) > max_chars:
        truncation_notice = (
            "\n\n…（レスポンスが上限の{:,}文字を超えたためトランケートされました。"
            "CLI の get-document コマンド（--output-file オプション）で全文を取得できます）"
        ).format(max_chars)
        truncate_at = max(0, max_chars - len(truncation_notice))
        response = response[:truncate_at] + truncation_notice

    return response


def _format_cli_search_aozora_result(result: dict[str, Any]) -> str:
    """CLI search-aozora の JSON 結果を MCP レスポンス文字列に変換する."""
    results = result.get("results", [])
    count = result.get("count", len(results))

    if not results:
        return f"検索結果: {count}件"

    lines = [f"検索結果: {count}件", ""]
    for r in results:
        lines.append(
            f"- [{r.get('book_id', '')}] {r.get('title', '')} / {r.get('author', '')} "
            f"({r.get('copyright', '')})"
        )
    return "\n".join(lines)


def _format_cli_list_recent_result(result: dict[str, Any]) -> str:
    """CLI list-recent の JSON 結果を MCP レスポンス文字列に変換する."""
    sources = result.get("sources", [])
    source_type = result.get("source_type", "")
    count = result.get("count", len(sources))
    total = result.get("total", count)

    if not sources:
        return f"{source_type}: 0 件"

    order = result.get("order", "desc")
    order_label = "古い順" if order == "asc" else "新しい順"
    lines = [f"{source_type}: {count} 件（全 {total} 件中, {order_label}）", ""]
    for s in sources:
        title = s.get("title", "(無題)")
        source_id = s.get("source_id", "")
        published_at = s.get("published_at", "")
        file_size = s.get("file_size", 0)
        size_str = format_file_size(file_size) if file_size else ""
        line = f"- {title}"
        if published_at:
            line += f"  [{published_at}]"
        if size_str:
            line += f"  ({size_str})"
        lines.append(line)
        lines.append(f"  {source_id}")

    return "\n".join(lines)


def _format_cli_stats_result(result: dict[str, Any]) -> str:
    """CLI stats の JSON 結果を MCP レスポンス文字列に変換する."""
    parts: list[str] = ["📊 RAG Knowledge 統計"]

    # source_store
    ss = result.get("source_store", {})
    parts.append("")
    parts.append("■ source_store")
    if ss.get("status") in {"unconfigured", "not_found"}:
        parts.append("  未設定" if ss.get("status") == "unconfigured" else "  ディレクトリが存在しません")
    else:
        parts.append(f"  総ファイル数: {ss.get('total_files', 0):,}")
        parts.append(f"  総サイズ: {format_file_size(int(ss.get('total_size', 0)))}")
        by_type = ss.get("by_type")
        if by_type and isinstance(by_type, dict):
            parts.append("  媒体別:")
            for st_key in sorted(by_type.keys()):
                info = by_type[st_key]
                parts.append(
                    f"    {st_key}: {info['files']} files"
                    f" ({format_file_size(info['size'])})"
                )

    # converted_store
    cs = result.get("converted_store", {})
    parts.append("")
    parts.append("■ converted_store")
    if cs.get("status") in {"unconfigured", "not_found"}:
        parts.append("  未設定" if cs.get("status") == "unconfigured" else "  ディレクトリが存在しません")
    else:
        parts.append(f"  総ファイル数: {cs.get('total_files', 0):,}")
        parts.append(f"  総サイズ: {format_file_size(int(cs.get('total_size', 0)))}")

    # インデックス
    idx = result.get("index", {})
    parts.append("")
    parts.append("■ インデックス")
    if "error" in idx:
        parts.append(f"  エラー: {idx['error']}")
    else:
        parts.append(f"  総チャンク数: {idx.get('total_chunks', 0):,}")
        parts.append(f"  ソース数: {idx.get('source_count', 0):,}")

    # パイプライン
    pl = result.get("pipeline", {})
    parts.append("")
    parts.append("■ パイプライン")
    if pl.get("status") == "unconfigured":
        parts.append("  未設定")
    elif pl.get("status") == "uninitialized":
        parts.append("  未初期化")
    else:
        last_at = pl.get("last_processed_at") or "（未実行）"
        parts.append(f"  最終処理: {last_at}")
        parts.append(f"  実行回数: {pl.get('run_count', 0)}")
        lci = pl.get("last_commit_id")
        parts.append(f"  last_commit_id: {lci if lci else '（未実行）'}")
        parts.append(f"  論理削除: {pl.get('deleted_count', 0)} 件")

    return "\n".join(parts)


@mcp.tool()
async def rag_list_recent(
    source_type: str,
    limit: int | None = None,
    order: str = "desc",
) -> str:
    """[rag-knowledge] List recent sources - 指定した source_type のソースを公開日時順で一覧取得する.

    content listing, recent sources, source list, browse.
    ナレッジベースに取り込んだコンテンツを source_type 別に一覧で確認できる。
    最近取り込んだコンテンツの確認や、ナレッジベースの内容把握に使用する。

    Args:
        source_type: ソース種別: "web", "bluesky", "zenn", "youtube", "aozora", "local", "journal"
        limit: 取得件数（1〜100、未指定時は設定値を使用）
        order: ソート順（"desc": 新しい順（デフォルト）, "asc": 古い順）

    Returns:
        ソース一覧テキスト（タイトル、source_id、published_at、ファイルサイズ）
    """
    if source_type not in _VALID_SOURCE_TYPES:
        valid = ", ".join(sorted(_VALID_SOURCE_TYPES))
        return f"無効な source_type: {source_type!r}（有効値: {valid}）"

    if limit is not None and (limit < 1 or limit > 100):
        return "エラー: limit は 1〜100 の範囲で指定してください"

    if order not in ("asc", "desc"):
        return f"エラー: order は 'asc' または 'desc' を指定してください（指定値: {order!r}）"

    args: list[str] = ["--source-type", source_type]
    if limit is not None:
        args.extend(["--limit", str(limit)])
    args.extend(["--order", order])

    try:
        result = await _run_cli_subprocess("list-recent", args)
        return _format_cli_list_recent_result(result)
    except CLISubprocessError as e:
        return f"エラー: ソース一覧の取得に失敗しました ({e})"


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
    try:
        result = await _run_cli_subprocess("stats")
        return _format_cli_stats_result(result)
    except CLISubprocessError as e:
        return f"エラー: 統計情報の取得に失敗しました ({e})"


# --- Upload HTTP API ---


async def _check_api_key(request: Request) -> Response | None:
    """Upload HTTP API リクエストの API キー認証.

    仕様: docs/specs/infrastructure/upload-auth.md

    Returns:
        None: 認証成功、Response: エラーレスポンス（認証失敗 or 内部エラー）
    """
    import hmac

    from py_common_lib.secrets import SecretNotFoundError, SecretStoreError, get_secret

    header_value = request.headers.get("X-API-Key")
    if header_value is None:
        logger.warning("API key header missing")
        return _upload_error(401, "Authentication required")

    try:
        stored_key = get_secret(
            UPLOAD_API_KEY_NAME, service=UPLOAD_API_KEY_SERVICE,
        )
    except (SecretNotFoundError, SecretStoreError):
        logger.exception("Failed to retrieve API key from keyring")
        return _upload_error(500, "Internal server error")

    if not hmac.compare_digest(header_value, stored_key):
        logger.warning("Invalid API key")
        return _upload_error(401, "Authentication required")

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


_CJK_PATTERN = re.compile(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]")
"""ひらがな・カタカナ・CJK 漢字の検出パターン."""


def _decode_form_value(value: str) -> str:
    """Starlette の Latin-1 フォールバックで壊れたフォーム値を UTF-8/cp932 に復元する.

    Starlette の MultiPartParser は非 UTF-8 バイト列を受信すると UTF-8 デコードに
    失敗し Latin-1 にフォールバックする。この関数は Latin-1 文字列をバイト列に戻し、
    UTF-8 → cp932 の順で再デコードすることで元の文字列を復元する。

    主な発生パターン:
    - Windows curl（cp932 コンソール）から日本語を送信した場合
    - UTF-8 バイト列が Latin-1 にフォールバックした場合

    cp932 誤判定の防止:
    - cp932 デコード後に日本語文字（ひらがな・カタカナ・CJK 漢字）が含まれない場合、
      正当な Latin-1 入力（例: "¡Hola!"）と判断し元の値を返す。
    """
    try:
        raw_bytes = value.encode("latin-1")
    except UnicodeEncodeError:
        return value
    # UTF-8 を優先（cp932 の一部バイト列が偶然 UTF-8 として解釈されるのを防ぐ）
    try:
        return raw_bytes.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        pass
    # cp932: デコード成功しても日本語文字が含まれなければ誤判定とみなす
    try:
        decoded = raw_bytes.decode("cp932")
        if _CJK_PATTERN.search(decoded):
            return decoded
    except (UnicodeDecodeError, ValueError):
        pass
    return value


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

    filename = _decode_form_value(getattr(file_field, "filename", "") or "")

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
        return auth_error

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
        return auth_error

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
        title = _decode_form_value(str(form.get("title", ""))).strip()
        repository = _decode_form_value(str(form.get("repository", ""))).strip()
        entry_id = form.get("entry_id")
        entry_id_str = _decode_form_value(str(entry_id)).strip() if entry_id else None

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


def _validate_bind_address(
    host: str, *, dns_rebinding_protection: bool
) -> str | None:
    """HTTP モードのバインドアドレスを検証する.

    仕様: docs/specs/infrastructure/upload-auth.md

    Returns:
        None: 検証成功、str: エラーメッセージ
    """
    import ipaddress

    if host == "0.0.0.0":
        if dns_rebinding_protection:
            return (
                "Binding to 0.0.0.0 is not allowed. "
                "Use a specific private IP address (e.g., 192.168.x.x) "
                "for LAN access, or 127.0.0.1 for local access. "
                "Or set RAG_DNS_REBINDING_PROTECTION=false to allow 0.0.0.0."
            )
        return None

    if host in ("127.0.0.1", "localhost"):
        return None

    # IP アドレスとして解析して RFC 1918 プライベートアドレスか判定
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # ホスト名は DNS 解決を行わないため、安全性を判定できない。
        # パブリックアドレスに解決される可能性があるため、HTTPS 未対応の現状では拒否する。
        return (
            f"Binding to hostname {host} may resolve to a public address and "
            "requires HTTPS. HTTPS support is not yet available (see #449). "
            "Use 127.0.0.1, localhost, or a private IP address instead."
        )

    if addr.is_private:
        return None

    # パブリックアドレス: HTTPS が必要（#449 未実装のため常に拒否）
    return (
        f"Binding to public address {host} requires HTTPS. "
        "HTTPS support is not yet available (see #449). "
        "Use 127.0.0.1 or a private IP address instead."
    )


def _check_api_key_registered() -> str | None:
    """keyring に API キーが登録されているか確認する.

    仕様: docs/specs/infrastructure/upload-auth.md

    Returns:
        None: 登録済み、str: エラーメッセージ
    """
    from py_common_lib.secrets import SecretNotFoundError, SecretStoreError, get_secret

    try:
        key = get_secret(UPLOAD_API_KEY_NAME, service=UPLOAD_API_KEY_SERVICE)
    except SecretNotFoundError:
        return (
            "API key is not registered in keyring. "
            "Run 'uv run python -m rag.cli generate-api-key --save' first."
        )
    except SecretStoreError as exc:
        return f"Failed to access keyring: {exc}"

    if not key or not key.strip():
        return (
            "API key in keyring is empty. "
            "Run 'uv run python -m rag.cli generate-api-key --save --force' to regenerate."
        )

    return None


def _configure_and_run() -> None:
    """トランスポート設定に基づいて MCP サーバーを起動する."""
    settings = get_settings()
    transport = settings.rag_transport

    # HTTP モードの事前検証（外部依存の起動前に設定の妥当性を確認する）
    if transport == "http":
        bind_error = _validate_bind_address(
            settings.rag_http_host,
            dns_rebinding_protection=settings.rag_dns_rebinding_protection,
        )
        if bind_error is not None:
            logger.error(bind_error)
            raise SystemExit(1)

        key_error = _check_api_key_registered()
        if key_error is not None:
            logger.error(key_error)
            raise SystemExit(1)

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

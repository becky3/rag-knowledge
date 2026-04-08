"""RAG Knowledge CLIモジュール

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import urldefrag

from .filter_parser import parse_filters
from .pipeline.models import PHASE_FETCH
from .evaluation import (
    EvaluationReport,
    FailureTag,
    evaluate_retrieval,
)

# 失敗タグの日本語説明と改善ターゲット
FAILURE_TAG_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    FailureTag.RETRIEVAL_MISS.value: ("関連文書が検索されなかった", "チャンキング / Embedding"),
    FailureTag.RETRIEVAL_NOISE.value: ("無関係な文書が上位に来た", "スコアリング / フィルタリング"),
    FailureTag.CHUNK_FRAGMENTATION.value: ("回答に必要な情報が分断された", "チャンクサイズ"),
    FailureTag.QUERY_MISMATCH.value: ("クエリと文書の表現が異なる", "クエリ拡張 / Embedding"),
}

if TYPE_CHECKING:
    from collections.abc import Callable

    from .bm25_index import BM25Index
    from .config import RAGSettings as Settings
    from .pipeline.controller import PipelineController
    from .pipeline.ingesters._common import IngestResult
    from .safe_browsing import SafeBrowsingClient
    from .pipeline.ingesters.youtube import YoutubeIngester
    from .pipeline.models import PipelineSummary
    from .rag_knowledge import RAGKnowledgeService
    from .store.source_store import SourceStore


class EvaluationParams(TypedDict):
    """評価パラメータの型定義."""

    threshold: float | None
    vector_weight: float | None
    n_results: int
    k1: float
    b: float
    min_combined_score: float | None


class RegressionInfo(TypedDict):
    """リグレッション検出結果の型定義."""

    detected: bool
    baseline_f1: float
    current_f1: float
    delta: float

logger = logging.getLogger(__name__)


# --- stdout 保護機構 ---
# --output json モード時に stdout を JSON 専用チャネルとして保護する。
# OS レベルで fd 1 を stderr にリダイレクトし、サードパーティライブラリの
# stdout 出力（yt-dlp 等）が JSON 通信を汚染しないようにする。
# JSON 出力は保存した元の fd に直接書き込む。

_json_output_stream: io.TextIOWrapper | None = None


def _install_stdout_guard() -> None:
    """stdout を JSON 専用に保護する.

    1. fd 1 を複製して保存（JSON 出力先）
    2. fd 1 を stderr（fd 2）にリダイレクト
    3. sys.stdout を stderr に差し替え

    これにより print() や C 拡張の stdout 書き込みは全て stderr に流れ、
    _output_json のみが元の stdout に JSON を書き込む。
    冪等: 既にインストール済みの場合は何もしない。
    """
    global _json_output_stream  # noqa: PLW0603

    if _json_output_stream is not None:
        return

    saved_fd = os.dup(sys.stdout.fileno())
    _json_output_stream = io.TextIOWrapper(
        io.FileIO(saved_fd, mode="w", closefd=True),
        encoding="utf-8",
        line_buffering=True,
    )

    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr


# --- JSON 出力ヘルパー ---
# worker.py の JSON Lines プロトコルと互換のフォーマットで出力する。
# 仕様: docs/specs/rag-knowledge.md (MCP 薄層アダプターパターン)


def _output_json(data: dict[str, object]) -> None:
    """JSON Lines 形式で 1 行出力する.

    stdout 保護機構が有効な場合は保存した元の fd に書き込む。
    無効な場合（text モード）は通常の print を使用する。
    """
    line = json.dumps(data, ensure_ascii=False)
    if _json_output_stream is not None:
        _json_output_stream.write(line + "\n")
        _json_output_stream.flush()
    else:
        print(line, flush=True)


def _output_progress(processed: int, total: int, current: str) -> None:
    """進捗 JSON を出力する."""
    _output_json({"type": "progress", "processed": processed, "total": total, "current": current})


def _wrap_progress(
    cb: Callable[[int, int, str], None] | None,
    phase: str,
) -> Callable[[int, int, str], None] | None:
    """progress_callback にフェーズ名プレフィックスを付与するラッパー."""
    if cb is None:
        return None

    def wrapped(processed: int, total: int, current: str) -> None:
        cb(processed, total, f"[{phase}] {current}")

    return wrapped


def _output_error(message: str) -> None:
    """エラー JSON を出力し、exit code 1 で終了する."""
    _output_json({"type": "error", "error": True, "message": message})
    sys.exit(1)


def _output_result(data: dict[str, object]) -> None:
    """結果 JSON を出力する."""
    payload: dict[str, object] = {**data, "type": "result"}
    _output_json(payload)


def _is_json_output(args: argparse.Namespace) -> bool:
    """--output json が指定されているかを判定する."""
    return getattr(args, "output_format", "text") == "json"


def _ingest_result_to_dict(
    ingest_result: "IngestResult",
    pipeline_summary: "PipelineSummary | None",
) -> dict[str, object]:
    """IngestResult + PipelineSummary を JSON 出力用 dict に変換する."""
    data: dict[str, object] = {
        "placed": ingest_result.placed,
        "skipped": ingest_result.skipped,
        "overwritten": ingest_result.overwritten,
        "errors": ingest_result.errors,
        "error_details": ingest_result.error_details,
    }
    if pipeline_summary is not None:
        data["pipeline"] = {
            "mode": pipeline_summary.mode.value,
            "total_files": pipeline_summary.total_files,
            "processed": pipeline_summary.processed,
            "errors": pipeline_summary.errors,
            "warnings": pipeline_summary.warnings,
        }
    return data


def _summary_to_dict(summary: "PipelineSummary") -> dict[str, object]:
    """PipelineSummary を JSON 出力用 dict に変換する."""
    return {
        "mode": summary.mode.value,
        "total_files": summary.total_files,
        "processed": summary.processed,
        "errors": summary.errors,
        "warnings": summary.warnings,
    }


def _log_phase_summary(phase: str, summary: "PipelineSummary") -> None:
    """フェーズ別の PipelineSummary をログ出力する."""
    logger.info(
        "[%s] %d 処理 / %d エラー / %d 警告",
        phase,
        summary.processed,
        len(summary.errors),
        len(summary.warnings),
    )
    for warn in summary.warnings:
        logger.warning("  [%s] 警告: %s", phase, warn)
    for err_file in summary.errors:
        logger.error("  [%s] エラーファイル: %s", phase, err_file)


def _add_output_option(parser: argparse.ArgumentParser) -> None:
    """サブコマンドパーサーに --output オプションを追加する."""
    parser.add_argument(
        "--output",
        dest="output_format",
        choices=["text", "json"],
        default="text",
        help="出力フォーマット（text/json、デフォルト: text）",
    )


def _validate_bm25_k1(value: str) -> float:
    try:
        f = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--bm25-k1: invalid float value: '{value}'"
        ) from None
    if not math.isfinite(f) or f <= 0.0:
        raise argparse.ArgumentTypeError(
            f"--bm25-k1 must be > 0.0 (got {f})"
        )
    return f


def _validate_bm25_b(value: str) -> float:
    try:
        f = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--bm25-b: invalid float value: '{value}'"
        ) from None
    if not math.isfinite(f) or not 0.0 <= f <= 1.0:
        raise argparse.ArgumentTypeError(
            f"--bm25-b must be between 0.0 and 1.0 (got {f})"
        )
    return f


def main() -> None:
    """CLIエントリポイント."""
    parser = argparse.ArgumentParser(description="RAG Knowledge CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # evaluate サブコマンド
    eval_parser = subparsers.add_parser("evaluate", help="RAG検索精度を評価")
    eval_parser.add_argument(
        "--dataset",
        required=True,
        help="評価データセットのパス",
    )
    eval_parser.add_argument(
        "--output-dir",
        default=".tmp/rag-evaluation",
        help="レポート出力ディレクトリ",
    )
    eval_parser.add_argument(
        "--baseline-file",
        help="ベースラインJSONファイルのパス",
    )
    eval_parser.add_argument(
        "--n-results",
        type=int,
        default=5,
        help="各クエリで取得する結果数",
    )
    eval_parser.add_argument(
        "--threshold",
        type=float,
        help="類似度閾値",
    )
    def _validate_vector_weight(value: str) -> float:
        f = float(value)
        if not 0.0 <= f <= 1.0:
            raise argparse.ArgumentTypeError(
                f"--vector-weight must be between 0.0 and 1.0 (got {f})"
            )
        return f

    eval_parser.add_argument(
        "--vector-weight",
        type=_validate_vector_weight,
        required=True,
        help="ベクトル検索の重み α（0.0〜1.0）",
    )
    eval_parser.add_argument(
        "--persist-dir",
        required=True,
        help="ChromaDB永続化ディレクトリ",
    )
    eval_parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="リグレッション検出時に exit code 1 で終了",
    )
    eval_parser.add_argument(
        "--regression-threshold",
        type=float,
        default=0.1,
        help="リグレッション判定閾値（F1スコアの低下量）",
    )
    eval_parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="現在の結果をベースラインとして保存",
    )
    eval_parser.add_argument(
        "--fixture",
        required=True,
        help="BM25インデックス構築用のテストドキュメントフィクスチャ",
    )
    eval_parser.add_argument(
        "--chunk-size",
        type=int,
        required=True,
        help="チャンクサイズ",
    )
    eval_parser.add_argument(
        "--chunk-overlap",
        type=int,
        required=True,
        help="チャンクオーバーラップ",
    )
    eval_parser.add_argument(
        "--bm25-k1",
        type=_validate_bm25_k1,
        required=True,
        help="BM25 k1パラメータ（例: 1.5）",
    )
    eval_parser.add_argument(
        "--bm25-b",
        type=_validate_bm25_b,
        required=True,
        help="BM25 bパラメータ（例: 0.75）",
    )
    eval_parser.add_argument(
        "--min-combined-score",
        type=float,
        default=None,
        help="combined_scoreの下限閾値（デフォルト: None=フィルタなし）",
    )

    # init-test-db サブコマンド
    init_parser = subparsers.add_parser("init-test-db", help="テスト用ChromaDB・BM25初期化")
    init_parser.add_argument(
        "--persist-dir",
        default=".tmp/test_chroma_db",
        help="ChromaDB永続化ディレクトリ",
    )
    init_parser.add_argument(
        "--fixture",
        required=True,
        help="テストドキュメントフィクスチャ",
    )
    init_parser.add_argument(
        "--bm25-persist-dir",
        default=".tmp/test_bm25_index",
        help="BM25インデックス永続化ディレクトリ",
    )
    init_parser.add_argument(
        "--chunk-size",
        type=int,
        required=True,
        help="チャンクサイズ",
    )
    init_parser.add_argument(
        "--chunk-overlap",
        type=int,
        required=True,
        help="チャンクオーバーラップ",
    )
    init_parser.add_argument(
        "--bm25-k1",
        type=_validate_bm25_k1,
        required=True,
        help="BM25 k1パラメータ（例: 1.5）",
    )
    init_parser.add_argument(
        "--bm25-b",
        type=_validate_bm25_b,
        required=True,
        help="BM25 bパラメータ（例: 0.75）",
    )

    # get-document サブコマンド
    doc_parser = subparsers.add_parser("get-document", help="ソースの全文を取得")
    doc_parser.add_argument(
        "source_id",
        help="ソース識別子（rag_search の Source 値）",
    )
    doc_parser.add_argument(
        "--format",
        choices=["text", "original"],
        default="text",
        help="取得形式（text: 変換済みテキスト、original: オリジナル、デフォルト: text）",
    )
    doc_parser.add_argument(
        "--output-file",
        default=None,
        help="出力先ファイルパス（未指定時は標準出力）",
    )
    _add_output_option(doc_parser)

    # rebuild サブコマンド
    rebuild_parser = subparsers.add_parser(
        "rebuild",
        help="ナレッジベースを再構築（注意: full/index は Embedding API コストが発生）",
    )
    rebuild_parser.add_argument(
        "--mode",
        required=True,
        choices=["full", "convert", "index", "incremental"],
        help="再構築モード",
    )
    rebuild_parser.add_argument(
        "--source-type",
        choices=["web", "bluesky", "zenn", "youtube", "aozora", "local", "journal"],
        default=None,
        help="対象媒体フィルタ（incremental では指定不可）",
    )
    rebuild_parser.add_argument(
        "--commit-message",
        default=None,
        help="再構築前に source_store を git commit するメッセージ",
    )
    rebuild_parser.add_argument(
        "--if-needed",
        action="store_true",
        default=False,
        help="前回 index/full rebuild 以降に更新がなければスキップ（index/full のみ有効）",
    )
    rebuild_parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="インデックス再構築の並列数（未指定時は .env の RAG_EMBEDDING_CONCURRENCY）",
    )
    _add_output_option(rebuild_parser)

    # migrate サブコマンド
    subparsers.add_parser(
        "migrate",
        help="metadata.db のスキーマをマイグレーションする",
    )

    # stats サブコマンド
    stats_parser = subparsers.add_parser("stats", help="ナレッジベースの統計情報を表示")
    _add_output_option(stats_parser)

    # list-recent サブコマンド
    list_recent_parser = subparsers.add_parser(
        "list-recent", help="指定 source_type のソースを新しい順で一覧取得",
    )
    list_recent_parser.add_argument(
        "--source-type",
        required=True,
        choices=["web", "bluesky", "zenn", "youtube", "aozora", "local", "journal"],
        help="ソース種別",
    )
    def _validate_list_recent_limit(value: str) -> int:
        n = int(value)
        if n < 1 or n > 100:
            raise argparse.ArgumentTypeError(
                f"--limit must be 1-100 (got {n})"
            )
        return n
    list_recent_parser.add_argument(
        "--limit",
        type=_validate_list_recent_limit,
        default=None,
        help="取得件数（1〜100、未指定時は設定値を使用）",
    )
    list_recent_parser.add_argument(
        "--order",
        choices=["asc", "desc"],
        default="desc",
        help="ソート順（asc: 古い順, desc: 新しい順。デフォルト: desc）",
    )
    _add_output_option(list_recent_parser)

    # search サブコマンド
    search_parser = subparsers.add_parser("search", help="ナレッジベースを検索")
    search_parser.add_argument("--query", required=True, help="検索クエリ")
    def _validate_n_results(value: str) -> int:
        n = int(value)
        if n < 1:
            raise argparse.ArgumentTypeError(
                f"--n-results must be >= 1 (got {n})"
            )
        return n

    search_parser.add_argument(
        "--n-results", type=_validate_n_results, default=None,
        help="各エンジンから取得する結果数（未指定時は設定値を使用）",
    )
    search_parser.add_argument(
        "--source-type",
        choices=["web", "bluesky", "zenn", "youtube", "aozora", "local", "journal"],
        default=None,
        help="ソース種別フィルタ",
    )
    search_parser.add_argument(
        "--filters",
        default=None,
        help="メタデータフィルタ（key=value 形式、例: 'repository=rag-knowledge'）",
    )
    _add_output_option(search_parser)

    # delete サブコマンド
    delete_parser = subparsers.add_parser("delete", help="ソースをナレッジベースから論理削除")
    delete_parser.add_argument("source_id", help="削除するソース識別子（source_id）")
    _add_output_option(delete_parser)

    # --- インジェスト系サブコマンド ---

    # ingest-youtube: YouTube 単一動画取り込み
    yt_parser = subparsers.add_parser("ingest-youtube", help="YouTube 動画を取り込み")
    yt_parser.add_argument("video_url", help="YouTube 動画 URL")
    _add_output_option(yt_parser)

    # ingest-youtube-playlist: YouTube プレイリスト一括取り込み
    ytpl_parser = subparsers.add_parser("ingest-youtube-playlist", help="YouTube プレイリストを一括取り込み")
    ytpl_parser.add_argument("playlist_url", help="YouTube プレイリスト URL")
    ytpl_parser.add_argument("--max-videos", type=int, default=None, help="取得する最大動画数")
    _add_output_option(ytpl_parser)

    # crawl-bluesky: BlueSky 取り込み
    bs_parser = subparsers.add_parser("crawl-bluesky", help="BlueSky 投稿を一括取り込み")
    bs_parser.add_argument("handle", help="BlueSky ハンドル（例: user.bsky.social）")
    bs_parser.add_argument("--max-posts", type=int, default=None, help="取得する最大投稿数")
    bs_parser.add_argument("--include-reposts", action="store_true", default=None, help="リポストを含める")
    bs_parser.add_argument("--force", action="store_true", default=False, help="上書き再取得モード（既存ファイルを上書き + メディア再DL）")
    _add_output_option(bs_parser)

    # crawl-zenn: Zenn 取り込み
    zenn_parser = subparsers.add_parser("crawl-zenn", help="Zenn コンテンツを一括取り込み")
    zenn_parser.add_argument("username", help="Zenn ユーザー名")
    zenn_parser.add_argument("--max-articles", type=int, default=None, help="取得する最大コンテンツ数")
    zenn_parser.add_argument("--content-type", choices=["articles", "scraps", "all"], default="all", help="取得対象")
    zenn_parser.add_argument("--force", action="store_true", default=False, help="既存ファイルを上書きする（デフォルト: スキップ）")
    _add_output_option(zenn_parser)

    # add-document: 単一ドキュメント取り込み
    adddoc_parser = subparsers.add_parser("add-document", help="ドキュメントファイルをナレッジベースに取り込む")
    adddoc_input_group = adddoc_parser.add_mutually_exclusive_group(required=True)
    adddoc_input_group.add_argument("--file", dest="file_path", help="取り込み対象ファイルのパス")
    adddoc_input_group.add_argument("--stdin", action="store_true", default=False, help="stdin からコンテンツを読み取る（--file と排他）")
    adddoc_parser.add_argument("--filename", default=None, help="stdin 入力時のファイル名（--stdin 使用時に必須）")
    adddoc_parser.add_argument("--encoding", choices=["text", "base64"], default="text", help="stdin 入力のエンコーディング（デフォルト: text）")
    adddoc_parser.add_argument("--upload-mode", choices=["fail", "replace"], default="fail", help="同名ファイル存在時の動作")
    _add_output_option(adddoc_parser)

    # crawl-documents: ディレクトリ一括取り込み
    crawldoc_parser = subparsers.add_parser("crawl-documents", help="ディレクトリ内ドキュメントを一括取り込み")
    crawldoc_parser.add_argument("dir_path", help="取り込み対象ディレクトリのパス")
    crawldoc_parser.add_argument("--pattern", default="**/*", help="glob パターン")
    crawldoc_parser.add_argument("--upload-mode", choices=["fail", "replace"], default="fail", help="同名ファイル存在時の動作")
    _add_output_option(crawldoc_parser)

    # site-ingest: Scrapy によるサイト一括取り込み
    siteingest_parser = subparsers.add_parser("site-ingest", help="Scrapy でサイトを一括取り込み（大規模サイト向け）")
    siteingest_parser.add_argument("url", nargs="+", help="取得対象 URL（1件: クロールモード、2件以上: 複数URLモード）")
    siteingest_parser.add_argument("--url-pattern", default="", help="URL フィルタパターン（正規表現、クロールモードのみ）")
    siteingest_parser.add_argument("--max-pages", type=int, default=None, help="ページ数上限（クロールモードのみ）")
    siteingest_parser.add_argument("--force", action="store_true", help="JOBDIR を削除して再クロール（クロールモードのみ）")
    siteingest_parser.add_argument(
        "--download-only",
        action="store_true",
        default=False,
        help="Scrapy クロール + Bridge（source_store 配置 + git commit）まで実行し、パイプライン処理をスキップ",
    )
    _add_output_option(siteingest_parser)

    # update-aozora-catalog: 青空文庫カタログ更新
    update_aozora_parser = subparsers.add_parser("update-aozora-catalog", help="青空文庫カタログを更新")
    _add_output_option(update_aozora_parser)

    # search-aozora: 青空文庫カタログ検索
    search_aozora_parser = subparsers.add_parser("search-aozora", help="青空文庫カタログを検索")
    search_aozora_parser.add_argument("--author", default=None, help="著者名（部分一致）")
    search_aozora_parser.add_argument("--title", default=None, help="作品タイトル（部分一致）")
    search_aozora_parser.add_argument("--limit", type=int, default=20, help="最大表示件数（デフォルト: 20）")
    _add_output_option(search_aozora_parser)

    # ingest-aozora: 青空文庫作品取り込み
    ingest_aozora_parser = subparsers.add_parser("ingest-aozora", help="青空文庫の作品を取り込み")
    ingest_aozora_parser.add_argument("book_id", help="青空文庫の作品 ID")
    _add_output_option(ingest_aozora_parser)

    # ingest-aozora-author: 青空文庫著者一括取り込み
    ingest_aozora_author_parser = subparsers.add_parser("ingest-aozora-author", help="青空文庫の著者作品を一括取り込み")
    ingest_aozora_author_parser.add_argument("person_id", help="著者の人物 ID（search-aozora で確認）")
    ingest_aozora_author_parser.add_argument("--max-works", type=int, default=None, help="取得する最大作品数")
    _add_output_option(ingest_aozora_author_parser)

    # add-journal: 単一ジャーナルエントリの登録
    aj_parser = subparsers.add_parser("add-journal", help="ジャーナルエントリをナレッジベースに登録")
    aj_parser.add_argument("--title", "-t", required=True, help="エントリタイトル")
    aj_input_group = aj_parser.add_mutually_exclusive_group(required=True)
    aj_input_group.add_argument("--file", "-f", help="本文 Markdown ファイルのパス。CLI がファイルを読み込んでコンテンツをインジェスターに渡す")
    aj_input_group.add_argument("--stdin", action="store_true", default=False, help="stdin から UTF-8 テキストを読み取る（--file と排他）")
    aj_parser.add_argument("--repository", "-r", required=True, help="リポジトリ名")
    aj_parser.add_argument("--entry-id", "-e", default=None, help="エントリ識別子（省略時は自動生成）")
    _add_output_option(aj_parser)

    # migrate-journal: 既存ジャーナルファイルの一括取り込み
    mj_parser = subparsers.add_parser("migrate-journal", help="既存ジャーナルファイルを source_store に一括配置")
    mj_parser.add_argument("--dir", "-d", required=True, help="ジャーナルディレクトリパス")
    mj_parser.add_argument("--repository", "-r", required=True, help="リポジトリ名")

    # generate-api-key: Upload HTTP API 用の API キー生成
    genkey_parser = subparsers.add_parser(
        "generate-api-key", help="Upload HTTP API 用の API キーを生成",
    )
    genkey_parser.add_argument(
        "--save", "-s", action="store_true", default=False,
        help="生成したキーを keyring に保存する",
    )
    genkey_parser.add_argument(
        "--force", "-f", action="store_true", default=False,
        help="--save 時に既存キーがある場合、確認なしで上書きする",
    )

    args = parser.parse_args()

    # --output json モード時は stdout を保護する
    if _is_json_output(args):
        _install_stdout_guard()

    # コマンドディスパッチ（sync / async 統一）
    _ASYNC_COMMANDS: dict[str, object] = {
        "evaluate": run_evaluation,
        "init-test-db": init_test_db,
        "ingest-youtube": run_ingest_youtube,
        "ingest-youtube-playlist": run_ingest_youtube_playlist,
        "crawl-bluesky": run_crawl_bluesky,
        "crawl-zenn": run_crawl_zenn,
        "add-document": run_add_document,
        "crawl-documents": run_crawl_documents,
        "site-ingest": run_site_ingest,
        "update-aozora-catalog": run_update_aozora_catalog,
        "ingest-aozora": run_ingest_aozora,
        "ingest-aozora-author": run_ingest_aozora_author,
        "add-journal": run_add_journal,
        "rebuild": run_rebuild,
        "delete": run_delete,
    }
    _SYNC_COMMANDS: dict[str, object] = {
        "get-document": run_get_document,
        "stats": run_stats,
        "list-recent": run_list_recent,
        "search": run_search,
        "search-aozora": run_search_aozora,
        "migrate-journal": run_migrate_journal,
        "migrate": run_migrate,
        "generate-api-key": run_generate_api_key,
    }

    if args.command in _ASYNC_COMMANDS:
        asyncio.run(_ASYNC_COMMANDS[args.command](args))  # type: ignore[operator]
    elif args.command in _SYNC_COMMANDS:
        _SYNC_COMMANDS[args.command](args)  # type: ignore[operator]


async def create_rag_service(
    *,
    chunk_size: int,
    chunk_overlap: int,
    persist_dir: str,
    threshold: float | None = None,
    bm25_index: "BM25Index | None" = None,
    vector_weight: float = 0.6,
    min_combined_score: float | None = None,
) -> "RAGKnowledgeService":
    """RAGKnowledgeServiceを生成する.

    全パラメータは呼び出し元が明示的に指定する。settings へのフォールバックは行わない。

    Args:
        chunk_size: チャンクサイズ
        chunk_overlap: チャンクオーバーラップ
        persist_dir: ChromaDB永続化ディレクトリ
        threshold: 類似度閾値（Noneの場合はフィルタリングなし）
        bm25_index: BM25インデックス（指定時はハイブリッド検索を有効化）
        vector_weight: ベクトル検索の重み α
        min_combined_score: combined_scoreの下限閾値（None=フィルタなし）

    Returns:
        RAGKnowledgeServiceインスタンス
    """
    from .config import get_settings
    from .embedding.factory import get_embedding_provider
    from .vector_store import VectorStore
    from .rag_knowledge import RAGKnowledgeService

    settings = get_settings()
    embedding_provider = get_embedding_provider(settings, settings.embedding_provider)

    vector_store = VectorStore(
        embedding_provider=embedding_provider,
        persist_directory=persist_dir,
        collection_name=settings.chromadb_collection_name,
        hnsw_m=settings.hnsw_m,
        hnsw_construction_ef=settings.hnsw_construction_ef,
        hnsw_search_ef=settings.hnsw_search_ef,
    )

    return RAGKnowledgeService(
        vector_store=vector_store,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        similarity_threshold=threshold,
        bm25_index=bm25_index,
        hybrid_search_enabled=bm25_index is not None,
        vector_weight=vector_weight,
        min_combined_score=min_combined_score,
        debug_log_enabled=settings.rag_debug_log_enabled,
    )


def _build_bm25_index_from_fixture(
    fixture_path: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
    k1: float,
    b: float,
    persist_dir: str | None = None,
) -> "BM25Index":
    """テストドキュメントフィクスチャからBM25インデックスを構築する.

    本番と同じ smart_chunk を適用してチャンク分割してから BM25 に登録する。

    Args:
        fixture_path: フィクスチャファイルのパス
        chunk_size: チャンクサイズ
        chunk_overlap: チャンクオーバーラップ
        k1: BM25 用語頻度の飽和パラメータ
        b: BM25 文書長の正規化パラメータ
        persist_dir: BM25インデックスの永続化ディレクトリ（指定時は自動保存）

    Returns:
        構築済みBM25Indexインスタンス
    """
    from .bm25_index import BM25Index
    from .rag_knowledge import smart_chunk

    with open(fixture_path, encoding="utf-8") as f:
        fixture_data = json.load(f)

    bm25_index = BM25Index(k1=k1, b=b, persist_dir=persist_dir)
    documents: list[tuple[str, str, str, str]] = []
    for doc in fixture_data.get("documents", []):
        source_url = doc.get("source_url", "")
        content = doc.get("content", "")
        if not source_url or not content:
            continue
        chunks = smart_chunk(content, chunk_size, chunk_overlap)
        normalized_url, _ = urldefrag(source_url)
        url_hash = hashlib.sha256(normalized_url.encode()).hexdigest()[:16]
        for i, (chunk_text, section_path) in enumerate(chunks):
            # BM25 には section_path + 本文を結合したテキストを登録
            bm25_text = (
                f"{section_path}\n{chunk_text}" if section_path else chunk_text
            )
            documents.append((f"{url_hash}_{i}", bm25_text, normalized_url, "web"))

    added = bm25_index.add_documents(documents)
    logger.info("BM25 index built with %d chunks from fixture", added)
    return bm25_index


async def run_evaluation(args: argparse.Namespace) -> None:
    """評価を実行しレポートを出力する."""
    logger.info("Starting RAG evaluation...")
    logger.info("Dataset: %s", args.dataset)
    logger.info("Output directory: %s", args.output_dir)

    # データセットファイルの存在確認
    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        logger.error("Dataset file not found: %s", args.dataset)
        sys.exit(1)

    # BM25インデックスをテストドキュメントから構築（ハイブリッド検索用）
    fixture_path = args.fixture
    bm25_k1: float = args.bm25_k1
    bm25_b: float = args.bm25_b
    bm25_index = _build_bm25_index_from_fixture(
        fixture_path,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        k1=bm25_k1,
        b=bm25_b,
    )

    # RAGサービス初期化（BM25込みでハイブリッド検索を有効化）
    min_combined_score: float | None = args.min_combined_score
    rag_service = await create_rag_service(
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        threshold=args.threshold,
        persist_dir=args.persist_dir,
        bm25_index=bm25_index,
        vector_weight=args.vector_weight,
        min_combined_score=min_combined_score,
    )

    # 評価実行
    report = await evaluate_retrieval(
        rag_service=rag_service,
        dataset_path=args.dataset,
        n_results=args.n_results,
    )

    logger.info(
        "Evaluation complete: %d queries, avg_f1=%.3f",
        report.queries_evaluated,
        report.average_f1,
    )

    # ベースライン比較
    regression_info: RegressionInfo | None = None
    if args.baseline_file and Path(args.baseline_file).exists():
        baseline = load_baseline(args.baseline_file)
        summary = baseline.get("summary", {})
        baseline_f1 = float(summary.get("average_f1", 0.0)) if isinstance(summary, dict) else 0.0
        regression_info = detect_regression(
            baseline_f1=baseline_f1,
            current_f1=report.average_f1,
            threshold=args.regression_threshold,
        )
        if regression_info["detected"]:
            logger.warning(
                "Regression detected! Baseline F1: %.3f -> Current F1: %.3f (delta: %.3f)",
                regression_info["baseline_f1"],
                regression_info["current_f1"],
                regression_info["delta"],
            )
        else:
            logger.info(
                "No regression. Baseline F1: %.3f -> Current F1: %.3f (delta: %+.3f)",
                regression_info["baseline_f1"],
                regression_info["current_f1"],
                regression_info["delta"],
            )

    # 評価パラメータ
    eval_params = EvaluationParams(
        threshold=args.threshold,
        vector_weight=args.vector_weight,
        n_results=args.n_results,
        k1=bm25_k1,
        b=bm25_b,
        min_combined_score=min_combined_score,
    )

    # レポート出力
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "report.json"
    md_path = output_dir / "report.md"

    write_json_report(report, regression_info, json_path, args.dataset, eval_params)
    write_markdown_report(report, regression_info, md_path, args.dataset, eval_params)

    logger.info("Reports written to: %s", output_dir)

    if args.save_baseline:
        baseline_path = output_dir / "baseline.json"
        write_json_report(report, None, baseline_path, args.dataset, eval_params)
        logger.info("Baseline saved to: %s", baseline_path)

    # リグレッション時の終了コード
    if args.fail_on_regression and regression_info and regression_info["detected"]:
        logger.error("Exiting with code 1 due to regression")
        sys.exit(1)


def load_baseline(baseline_path: str) -> dict[str, object]:
    """ベースラインJSONを読み込む.

    Args:
        baseline_path: ベースラインファイルのパス

    Returns:
        ベースラインデータ
    """
    with open(baseline_path, encoding="utf-8") as f:
        data: dict[str, object] = json.load(f)
        return data


def detect_regression(
    baseline_f1: float,
    current_f1: float,
    threshold: float,
) -> RegressionInfo:
    """リグレッションを検出する.

    Args:
        baseline_f1: ベースラインのF1スコア
        current_f1: 現在のF1スコア
        threshold: リグレッション判定閾値

    Returns:
        リグレッション情報
    """
    delta = current_f1 - baseline_f1
    detected = delta < -threshold
    return RegressionInfo(
        detected=detected,
        baseline_f1=baseline_f1,
        current_f1=current_f1,
        delta=delta,
    )


def write_json_report(
    report: EvaluationReport,
    regression: RegressionInfo | None,
    output_path: Path,
    dataset_path: str,
    params: EvaluationParams | None = None,
) -> None:
    """JSONレポートを出力する.

    Args:
        report: 評価レポート
        regression: リグレッション情報（オプション）
        output_path: 出力パス
        dataset_path: 評価データセットのパス
        params: 評価パラメータ（オプション）
    """
    data: dict[str, object] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset": dataset_path,
        "summary": {
            "queries_evaluated": report.queries_evaluated,
            "average_precision": report.average_precision,
            "average_recall": report.average_recall,
            "average_f1": report.average_f1,
            "average_ndcg": report.average_ndcg,
            "average_mrr": report.average_mrr,
            "negative_source_violations": len(report.negative_source_violations),
            "failure_tag_summary": report.failure_tag_summary,
        },
        "regression": regression,
        "query_results": [
            {
                "query_id": qr.query_id,
                "query": qr.query,
                "precision": qr.precision,
                "recall": qr.recall,
                "f1": qr.f1,
                "ndcg": qr.ndcg,
                "mrr": qr.mrr,
                "retrieved_sources": qr.retrieved_sources,
                "expected_sources": qr.expected_sources,
                "negative_violations": qr.negative_violations,
                "failure_tags": [tag.value for tag in qr.failure_tags],
            }
            for qr in report.query_results
        ],
    }
    if params is not None:
        data["params"] = dict(params)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_markdown_report(
    report: EvaluationReport,
    regression: RegressionInfo | None,
    output_path: Path,
    dataset_path: str,
    params: EvaluationParams | None = None,
) -> None:
    """Markdownレポートを出力する.

    Args:
        report: 評価レポート
        regression: リグレッション情報（オプション）
        output_path: 出力パス
        dataset_path: 評価データセットのパス
        params: 評価パラメータ（オプション）
    """
    lines = [
        "# RAG評価レポート",
        "",
        f"**実行日時**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"**データセット**: {dataset_path}",
    ]
    if params is not None:
        threshold_str = str(params["threshold"]) if params["threshold"] is not None else "None (設定値)"
        vw_str = str(params["vector_weight"]) if params["vector_weight"] is not None else "None (設定値)"
        k1_str = str(params.get("k1", 1.5))
        b_str = str(params.get("b", 0.75))
        min_sc_str = str(params.get("min_combined_score")) if params.get("min_combined_score") is not None else "None"
        lines.append(
            f"**パラメータ**: threshold={threshold_str}, vector_weight={vw_str}, "
            f"n_results={params['n_results']}, k1={k1_str}, b={b_str}, "
            f"min_combined_score={min_sc_str}",
        )
    lines.append("")
    lines.extend([
        "## サマリー",
        "",
        "| 指標 | 値 |",
        "|------|-----|",
        f"| 評価クエリ数 | {report.queries_evaluated} |",
        f"| 平均Precision | {report.average_precision:.3f} |",
        f"| 平均Recall | {report.average_recall:.3f} |",
        f"| 平均F1 | {report.average_f1:.3f} |",
        f"| 平均NDCG | {report.average_ndcg:.3f} |",
        f"| 平均MRR | {report.average_mrr:.3f} |",
        f"| 禁止ソース違反 | {len(report.negative_source_violations)} |",
        "",
    ])

    if report.failure_tag_summary:
        lines.extend([
            "## 失敗タグ分類",
            "",
            "| タグ | 件数 | 意味 | 改善ターゲット |",
            "|------|------|------|----------------|",
        ])
        for tag_value, count in sorted(report.failure_tag_summary.items(), key=lambda x: -x[1]):
            desc, target = FAILURE_TAG_DESCRIPTIONS.get(tag_value, (tag_value, "-"))
            lines.append(f"| {tag_value} | {count} | {desc} | {target} |")
        lines.append("")

    if regression:
        lines.extend([
            "## リグレッション検出",
            "",
        ])
        if regression["detected"]:
            lines.append(
                f"**リグレッション検出** "
                f"(ベースラインF1: {regression['baseline_f1']:.3f} -> "
                f"現在F1: {regression['current_f1']:.3f}, "
                f"変化: {regression['delta']:+.3f})"
            )
        else:
            lines.append(
                f"リグレッションなし "
                f"(ベースラインF1: {regression['baseline_f1']:.3f} -> "
                f"現在F1: {regression['current_f1']:.3f}, "
                f"変化: {regression['delta']:+.3f})"
            )
        lines.append("")

    lines.extend([
        "## クエリ別詳細",
        "",
    ])

    for qr in report.query_results:
        status = "PASS" if qr.f1 >= 0.5 else "FAIL"
        lines.extend([
            f"### [{status}] {qr.query_id}: {qr.query}",
            "",
            f"- Precision: {qr.precision:.3f}",
            f"- Recall: {qr.recall:.3f}",
            f"- F1: {qr.f1:.3f}",
            f"- NDCG: {qr.ndcg:.3f}",
            f"- MRR: {qr.mrr:.3f}",
            f"- 取得ソース: {len(qr.retrieved_sources)}件",
            f"- 期待ソース: {len(qr.expected_sources)}件",
        ])
        if qr.negative_violations:
            lines.append(f"- **禁止ソース違反**: {qr.negative_violations}")
        if qr.failure_tags:
            tag_strs = [tag.value for tag in qr.failure_tags]
            lines.append(f"- **失敗タグ**: {', '.join(tag_strs)}")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


async def init_test_db(args: argparse.Namespace) -> None:
    """テスト用ChromaDB・BM25インデックスを初期化する.

    フィクスチャ JSON → _ingest_crawled_page() で投入。
    本番と同じチャンキングパスを通ることで、評価結果が本番動作を反映する。
    BM25インデックスも同一フィクスチャから構築・永続化する。

    Args:
        args: コマンドライン引数
    """
    logger.info("Initializing test DB (ChromaDB + BM25)...")
    logger.info("ChromaDB persist directory: %s", args.persist_dir)
    logger.info("BM25 persist directory: %s", args.bm25_persist_dir)
    logger.info("Fixture file: %s", args.fixture)

    # フィクスチャファイルの存在確認
    fixture_path = Path(args.fixture)
    if not fixture_path.exists():
        logger.error("Fixture file not found: %s", args.fixture)
        sys.exit(1)

    # フィクスチャ読み込み
    with open(fixture_path, encoding="utf-8") as f:
        fixture_data = json.load(f)

    documents = fixture_data.get("documents", [])
    if not documents:
        logger.warning("No documents found in fixture")
        return

    # ドキュメントリストを構築
    pages: list[dict[str, str]] = []
    for doc in documents:
        source_url = doc.get("source_url", "")
        content = doc.get("content", "")
        if not source_url or not content:
            logger.warning("Skipping document with missing source_url or content")
            continue
        pages.append({
            "url": source_url,
            "title": doc.get("title", ""),
            "text": content,
            "crawled_at": datetime.now(timezone.utc).isoformat(),
        })

    # RAGKnowledgeService 経由で投入（本番と同じチャンキングパス）
    rag_service = await create_rag_service(
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        persist_dir=args.persist_dir,
    )
    total = 0
    for page in pages:
        count = await rag_service._ingest_crawled_page(**page)
        total += count
    logger.info(
        "Added %d chunks from %d documents to test ChromaDB at %s",
        total, len(pages), args.persist_dir,
    )

    # BM25インデックスも同一フィクスチャから構築・永続化
    bm25_index = _build_bm25_index_from_fixture(
        str(fixture_path),
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        k1=args.bm25_k1,
        b=args.bm25_b,
        persist_dir=args.bm25_persist_dir,
    )
    logger.info(
        "BM25 index persisted at %s (%d documents)",
        args.bm25_persist_dir, bm25_index.get_document_count(),
    )


def run_get_document(args: argparse.Namespace) -> None:
    """ドキュメント全文を取得して出力する.

    Args:
        args: コマンドライン引数
    """
    from .config import get_settings
    from .rag_knowledge import format_document_response, get_document

    json_out = _is_json_output(args)
    settings = get_settings()

    result = get_document(
        source_id=args.source_id,
        format=args.format,
        source_store_dir=settings.source_store_dir,
        converted_store_dir=settings.converted_store_dir,
    )

    if result.error:
        if json_out:
            _output_error(result.error)
        else:
            print(f"エラー: {result.error}", file=sys.stderr)
            sys.exit(1)

    if json_out:
        _output_result({
            "source_id": result.source_id,
            "title": result.title,
            "source_type": result.source_type,
            "format": result.format,
            "content": result.content,
            "is_binary": result.is_binary,
            "collected_at": result.collected_at,
            "extra": result.extra,
        })
        return

    response = format_document_response(result)

    if args.output_file:
        output_path = Path(args.output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(response, encoding="utf-8")
        print(f"出力しました: {args.output_file}")
    else:
        print(response)


def _format_elapsed(seconds: float) -> str:
    """所要時間を時分秒表記にフォーマットする.

    仕様: docs/specs/infrastructure/scheduled-rebuild.md
    """
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes = int(seconds // 60)
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes} 分 {secs:.1f} 秒"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours} 時間 {mins} 分 {secs:.1f} 秒"


def _show_error_dialog(message: str) -> None:
    """Windows エラーダイアログを表示する.

    仕様: docs/specs/infrastructure/scheduled-rebuild.md

    非 Windows 環境ではスキップする。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            message,
            "RAG Knowledge - Rebuild Error",
            0x10,  # MB_ICONERROR
        )
    except Exception:
        logger.warning("Windows ダイアログの表示に失敗しました")


async def run_rebuild(args: argparse.Namespace) -> None:
    """再構築を実行する.

    仕様: docs/specs/rebuild-stats.md

    Args:
        args: コマンドライン引数
    """
    import time

    from .config import get_settings
    from .pipeline.factory import build_pipeline_controller
    from .store.models import SourceType

    json_out = _is_json_output(args)

    mode: str = args.mode
    source_type: SourceType | None = args.source_type
    if_needed: bool = args.if_needed

    # incremental + source_type のバリデーション
    if mode == "incremental" and source_type is not None:
        if json_out:
            _output_error("incremental モードでは source_type を指定できません")
        logger.error(
            "incremental モードでは source_type を指定できません"
        )
        sys.exit(1)

    # --if-needed は index/full のみ有効
    if if_needed and mode not in ("index", "full"):
        msg = "--if-needed は --mode index または --mode full でのみ使用できます"
        if json_out:
            _output_error(msg)
        logger.error(msg)
        sys.exit(1)

    settings = get_settings()

    if not settings.source_store_dir:
        if json_out:
            _output_error("SOURCE_STORE_DIR が設定されていません")
        logger.error("SOURCE_STORE_DIR が設定されていません")
        sys.exit(1)
    if not settings.converted_store_dir:
        if json_out:
            _output_error("CONVERTED_STORE_DIR が設定されていません")
        logger.error("CONVERTED_STORE_DIR が設定されていません")
        sys.exit(1)

    source_store_dir = Path(settings.source_store_dir)
    if not source_store_dir.exists():
        msg = f"source_store ディレクトリが存在しません: {source_store_dir}"
        if json_out:
            _output_error(msg)
        logger.error(
            "source_store ディレクトリが存在しません: %s", source_store_dir,
        )
        sys.exit(1)

    controller = build_pipeline_controller(settings)

    # --if-needed: 更新チェック（ロック取得前に判定）
    if if_needed and not controller.db.needs_index_rebuild():
        if json_out:
            _output_result({
                "mode": mode,
                "skipped": True,
                "reason": "前回の index/full rebuild 以降に更新がありません",
            })
        else:
            logger.info(
                "前回の index/full rebuild 以降に更新がありません（スキップ）"
            )
        return

    # rebuild ロック取得
    from .infrastructure.file_lock import LockAcquisitionError, rebuild_lock

    lock = rebuild_lock(Path(controller.source_store.root_dir))
    try:
        lock.acquire()
    except LockAcquisitionError:
        msg = "別の再構築が実行中です（ロック競合）"
        if json_out:
            _output_error(msg)
        print(f"エラー: {msg}", file=sys.stderr)
        if if_needed:
            _show_error_dialog(msg)
        raise SystemExit(1)

    has_error = False
    try:
        logger.info("再構築を開始します（モード: %s）", mode)
        if source_type:
            logger.info("対象媒体: %s", source_type)

        start = time.monotonic()

        progress_cb = _output_progress if json_out else None

        # --commit-message 指定時は再構築前に source_store を git commit
        commit_message: str | None = getattr(args, "commit_message", None)
        if commit_message:
            sha = controller.commit(commit_message)
            if sha:
                logger.info("source_store コミット: %s", sha)
            else:
                logger.info("source_store に変更なし（コミットなし）")

        concurrency: int = (
            args.concurrency
            if args.concurrency is not None
            else settings.rag_embedding_concurrency
        )
        if args.concurrency is not None and args.concurrency < 1:
            msg = "--concurrency は 1 以上を指定してください"
            if json_out:
                _output_error(msg)
            logger.error(msg)
            sys.exit(1)

        if mode == "full":
            full_result = await controller.run_full_rebuild(
                source_type=source_type,
                progress_callback=progress_cb,
                concurrency=concurrency,
            )
        elif mode == "convert":
            summary = await controller.run_convert_only(
                source_type=source_type,
                progress_callback=progress_cb,
            )
        elif mode == "index":
            summary = await controller.run_index_only(
                source_type=source_type,
                progress_callback=progress_cb,
                concurrency=concurrency,
            )
        else:
            summary = await controller.run_incremental(
                progress_callback=progress_cb,
            )

        elapsed = time.monotonic() - start

        if mode == "full":
            all_errors = full_result.convert.errors + full_result.index.errors
            if json_out:
                _output_result({
                    "mode": "full",
                    "convert": _summary_to_dict(full_result.convert),
                    "index": _summary_to_dict(full_result.index),
                    "elapsed": round(elapsed, 1),
                })
                if all_errors:
                    has_error = True
                    if if_needed:
                        error_files = ", ".join(all_errors)
                        _show_error_dialog(
                            f"rebuild --mode {mode} でエラーが発生しました。\n"
                            f"エラーファイル: {error_files}"
                        )
                return

            _log_phase_summary("Convert", full_result.convert)
            _log_phase_summary("Index", full_result.index)
            logger.info("所要時間: %s", _format_elapsed(elapsed))
            if all_errors:
                has_error = True
        else:
            if json_out:
                _output_result(_summary_to_dict(summary) | {
                    "elapsed": round(elapsed, 1),
                })
                if summary.errors:
                    has_error = True
                    if if_needed:
                        error_files = ", ".join(summary.errors)
                        _show_error_dialog(
                            f"rebuild --mode {mode} でエラーが発生しました。\n"
                            f"エラーファイル: {error_files}"
                        )
                return

            logger.info(
                "再構築完了: %d 処理 / %d エラー / %d 警告 / %s",
                summary.processed,
                len(summary.errors),
                len(summary.warnings),
                _format_elapsed(elapsed),
            )
            if summary.warnings:
                for warn in summary.warnings:
                    logger.warning("  警告: %s", warn)
            if summary.errors:
                has_error = True
                for err_file in summary.errors:
                    logger.error("  エラーファイル: %s", err_file)
    except Exception as e:
        has_error = True
        if if_needed:
            _show_error_dialog(f"rebuild --mode {mode} でエラーが発生しました。\n{e}")
        raise
    else:
        # 処理エラー（例外なしだがエラーファイルあり）
        if has_error and if_needed:
            all_err = (
                full_result.convert.errors + full_result.index.errors
                if mode == "full"
                else summary.errors
            )
            error_files = ", ".join(all_err)
            _show_error_dialog(
                f"rebuild --mode {mode} でエラーが発生しました。\n"
                f"エラーファイル: {error_files}"
            )
    finally:
        lock.release()


def run_stats(args: argparse.Namespace) -> None:
    """ナレッジベースの統計情報を表示する.

    MCP ツール rag_stats と同等の情報を CLI で出力する。

    Args:
        args: コマンドライン引数
    """
    import contextlib
    import io

    from .config import get_settings
    from .embedding.factory import get_embedding_provider
    from .store.metadata_db import MetadataDB
    from .vector_store import VectorStore

    json_out = _is_json_output(args)
    settings = get_settings()

    # --- データ収集 ---
    stats_data: dict[str, object] = {}

    # source_store
    source_store_data: dict[str, object] = {}
    if not settings.source_store_dir:
        source_store_data["status"] = "unconfigured"
    else:
        source_store_dir = Path(settings.source_store_dir)
        if not source_store_dir.exists():
            source_store_data["status"] = "not_found"
        else:
            total_files = 0
            total_size = 0
            by_type: dict[str, dict[str, int]] = {}
            from .pipeline.models import detect_source_type
            for file in source_store_dir.rglob("*"):
                if not file.is_file():
                    continue
                rel = file.relative_to(source_store_dir)
                rel_posix = rel.as_posix()
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

            source_store_data["total_files"] = total_files
            source_store_data["total_size"] = total_size
            if by_type:
                source_store_data["by_type"] = {
                    k: {"files": v["files"], "size": v["size"]}
                    for k, v in sorted(by_type.items())
                }
    stats_data["source_store"] = source_store_data

    # converted_store
    converted_store_data: dict[str, object] = {}
    if not settings.converted_store_dir:
        converted_store_data["status"] = "unconfigured"
    else:
        cs_dir = Path(settings.converted_store_dir)
        if not cs_dir.exists():
            converted_store_data["status"] = "not_found"
        else:
            cs_files = 0
            cs_size = 0
            for file in cs_dir.rglob("*"):
                if file.is_file():
                    cs_files += 1
                    cs_size += file.stat().st_size
            converted_store_data["total_files"] = cs_files
            converted_store_data["total_size"] = cs_size
    stats_data["converted_store"] = converted_store_data

    # インデックス
    index_data: dict[str, object] = {}
    try:
        embedding_provider = get_embedding_provider(settings, settings.embedding_provider)
        with contextlib.redirect_stdout(io.StringIO()):
            vector_store = VectorStore.create_http(
                embedding_provider=embedding_provider,
                host=settings.chromadb_server_host,
                port=settings.chromadb_server_port,
                collection_name=settings.chromadb_collection_name,
                hnsw_m=settings.hnsw_m,
                hnsw_construction_ef=settings.hnsw_construction_ef,
                hnsw_search_ef=settings.hnsw_search_ef,
            )
        raw_stats = vector_store.get_stats()
        index_data["total_chunks"] = int(str(raw_stats.get("total_chunks", 0)))
        index_data["source_count"] = int(str(raw_stats.get("source_count", 0)))
    except Exception:
        logger.exception("インデックス統計の取得に失敗")
        index_data["error"] = "統計の取得に失敗しました"
    stats_data["index"] = index_data

    # パイプライン
    pipeline_data: dict[str, object] = {}
    if not settings.source_store_dir:
        pipeline_data["status"] = "unconfigured"
    else:
        from .store.models import NULL_COMMIT_HASH
        db_path = Path(settings.source_store_dir) / "metadata.db"
        if not db_path.exists():
            pipeline_data["status"] = "uninitialized"
        else:
            db = MetadataDB(db_path)
            try:
                db.initialize()
                history = db.get_pipeline_history()
                last_commit_id = db.get_last_commit_id()
                deleted_count = db.source_count(status="deleted")
                pipeline_data["last_processed_at"] = (
                    history[-1].processed_at if history else None
                )
                pipeline_data["run_count"] = len(history)
                commit_str = str(last_commit_id)
                pipeline_data["last_commit_id"] = (
                    None if commit_str == NULL_COMMIT_HASH else commit_str[:7]
                )
                pipeline_data["deleted_count"] = deleted_count
            finally:
                db.close()
    stats_data["pipeline"] = pipeline_data

    # --- 出力 ---
    if json_out:
        _output_result(stats_data)
        return

    # text 出力
    parts: list[str] = ["RAG Knowledge 統計"]

    parts.append("")
    parts.append("■ source_store")
    if source_store_data.get("status") == "unconfigured":
        parts.append("  未設定")
    elif source_store_data.get("status") == "not_found":
        parts.append("  ディレクトリが存在しません")
    else:
        parts.append(f"  総ファイル数: {source_store_data.get('total_files', 0):,}")
        parts.append(f"  総サイズ: {_format_cli_size(int(str(source_store_data.get('total_size', 0))))}")
        by_type_data = source_store_data.get("by_type")
        if by_type_data and isinstance(by_type_data, dict):
            parts.append("  媒体別:")
            for st_key in sorted(by_type_data.keys()):
                info = by_type_data[st_key]
                parts.append(
                    f"    {st_key}: {info['files']} files"
                    f" ({_format_cli_size(info['size'])})"
                )

    parts.append("")
    parts.append("■ converted_store")
    if converted_store_data.get("status") == "unconfigured":
        parts.append("  未設定")
    elif converted_store_data.get("status") == "not_found":
        parts.append("  ディレクトリが存在しません")
    else:
        parts.append(f"  総ファイル数: {converted_store_data.get('total_files', 0):,}")
        parts.append(f"  総サイズ: {_format_cli_size(int(str(converted_store_data.get('total_size', 0))))}")

    parts.append("")
    parts.append("■ インデックス")
    if "error" in index_data:
        parts.append(f"  エラー: {index_data['error']}")
    else:
        parts.append(f"  総チャンク数: {index_data.get('total_chunks', 0):,}")
        parts.append(f"  ソース数: {index_data.get('source_count', 0):,}")

    parts.append("")
    parts.append("■ パイプライン")
    if pipeline_data.get("status") == "unconfigured":
        parts.append("  未設定")
    elif pipeline_data.get("status") == "uninitialized":
        parts.append("  未初期化")
    else:
        last_at = pipeline_data.get("last_processed_at") or "（未実行）"
        parts.append(f"  最終処理: {last_at}")
        parts.append(f"  実行回数: {pipeline_data.get('run_count', 0)}")
        lci = pipeline_data.get("last_commit_id")
        parts.append(f"  last_commit_id: {lci if lci else '（未実行）'}")
        parts.append(f"  論理削除: {pipeline_data.get('deleted_count', 0)} 件")

    print("\n".join(parts))


def run_list_recent(args: argparse.Namespace) -> None:
    """指定 source_type のソースを published_at でソートして一覧取得する.

    MCP ツール rag_list_recent と同等の一覧取得を CLI で実行する。

    Args:
        args: コマンドライン引数
    """
    from .config import get_settings

    json_out = _is_json_output(args)
    settings = get_settings()
    limit: int = args.limit if args.limit is not None else settings.rag_list_recent_limit
    ascending = args.order == "asc"

    if json_out:
        from .store.metadata_db import MetadataDB
        from .store.models import SourceType
        from typing import cast

        if not settings.source_store_dir:
            _output_result({
                "source_type": args.source_type,
                "sources": [],
                "count": 0,
                "total": 0,
            })
            return
        db_path = Path(settings.source_store_dir) / "metadata.db"
        if not db_path.exists():
            _output_result({
                "source_type": args.source_type,
                "sources": [],
                "count": 0,
                "total": 0,
            })
            return
        db = MetadataDB(db_path)
        try:
            db.initialize()
            st = cast(SourceType, args.source_type)
            sources = db.list_sources(source_type=st, limit=limit, ascending=ascending)
            total = db.count_sources_by_type(source_type=st)
        finally:
            db.close()
        _output_result({
            "source_type": args.source_type,
            "sources": [
                {
                    "source_id": s.source_id,
                    "title": s.title,
                    "published_at": s.published_at,
                    "file_size": s.file_size,
                }
                for s in sources
            ],
            "count": len(sources),
            "total": total,
            "order": args.order,
        })
    else:
        from .rag_knowledge import list_recent_sources
        print(list_recent_sources(
            settings.source_store_dir, args.source_type, limit, ascending=ascending,
        ))


def run_search(args: argparse.Namespace) -> None:
    """ナレッジベースを検索する.

    MCP ツール rag_search と同等の検索を CLI で実行する。

    Args:
        args: コマンドライン引数
    """
    import asyncio as _asyncio
    import contextlib
    import io

    from .bm25_index import BM25Index
    from .config import get_settings
    from .embedding.factory import get_embedding_provider
    from .rag_knowledge import RAGKnowledgeService
    from .vector_store import VectorStore

    json_out = _is_json_output(args)
    settings = get_settings()
    embedding_provider = get_embedding_provider(settings, settings.embedding_provider)

    with contextlib.redirect_stdout(io.StringIO()):
        vector_store = VectorStore.create_http(
            embedding_provider=embedding_provider,
            host=settings.chromadb_server_host,
            port=settings.chromadb_server_port,
            collection_name=settings.chromadb_collection_name,
            hnsw_m=settings.hnsw_m,
            hnsw_construction_ef=settings.hnsw_construction_ef,
            hnsw_search_ef=settings.hnsw_search_ef,
        )
        bm25_index = BM25Index(
            k1=settings.rag_bm25_k1,
            b=settings.rag_bm25_b,
            persist_dir=settings.bm25_persist_dir,
        )

    service = RAGKnowledgeService(
        vector_store=vector_store,
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
        similarity_threshold=None,
        bm25_index=bm25_index,
        hybrid_search_enabled=True,
        vector_weight=settings.rag_vector_weight,
        min_combined_score=settings.rag_min_combined_score,
        debug_log_enabled=settings.rag_debug_log_enabled,
    )

    n_results = args.n_results if args.n_results is not None else settings.rag_retrieval_count
    source_type: str | None = args.source_type

    # filters パラメータのパース
    parsed_filters: dict[str, str] | None = None
    if args.filters is not None:
        try:
            parsed_filters = parse_filters(args.filters)
        except ValueError as e:
            if json_out:
                _output_error(str(e))  # sys.exit(1) で終了
            else:
                logger.error("エラー: %s", e)
                sys.exit(1)

    raw = _asyncio.run(
        service.retrieve_raw_results(
            args.query, n_results=n_results, source_type=source_type,
            filters=parsed_filters,
        )
    )

    if json_out:
        _output_result({
            "query": args.query,
            "vector_results": [r.to_dict() for r in raw.vector_results],
            "bm25_results": [r.to_dict() for r in raw.bm25_results],
        })
    else:
        from .rag_knowledge import format_raw_search_results
        print(format_raw_search_results(raw))


async def run_delete(args: argparse.Namespace) -> None:
    """ソースをナレッジベースから削除する.

    MCP ツール rag_delete と同等の削除を CLI で実行する。
    ファイルを物理削除し、パイプライン経由でインデックス・metadata.db を更新する。

    Args:
        args: コマンドライン引数
    """
    json_out = _is_json_output(args)
    progress_cb = _output_progress if json_out else None

    controller, _settings = _build_cli_pipeline_controller()
    source_id: str = args.source_id

    try:
        controller.source_store.remove_file(source_id)
    except KeyError:
        if json_out:
            _output_result({"deleted": False, "not_found": True})
            return
        print(f"該当するソースが見つかりませんでした: {source_id}", file=sys.stderr)
        sys.exit(1)

    try:
        summary = await controller.ingest_and_index(
            f"delete: {source_id}",
            progress_callback=progress_cb,
        )
    except Exception:
        logger.exception("削除パイプライン実行に失敗: %s", source_id)
        if json_out:
            _output_error(f"削除に失敗しました: {source_id}")
        print(
            f"エラー: 削除に失敗しました: {source_id}",
            file=sys.stderr,
        )
        sys.exit(1)

    if json_out:
        _output_result({
            "deleted": True,
            "pipeline": _summary_to_dict(summary),
        })
        return

    if summary.warnings:
        print(f"パイプライン警告: {len(summary.warnings)}件")
        for warn in summary.warnings:
            print(f"  - {warn}")
    if summary.errors:
        print(f"パイプラインエラー: {len(summary.errors)}件")
        for err in summary.errors:
            print(f"  - {err}")
    print(f"削除しました: {source_id}")


async def run_add_journal(args: argparse.Namespace) -> None:
    """単一ジャーナルエントリを登録する.

    Args:
        args: コマンドライン引数（--title, --file/--stdin, --repository, --entry-id）
    """
    from .infrastructure.file_lock import LockAcquisitionError, ingest_lock
    from .pipeline.ingesters.journal import JournalIngester

    json_out = _is_json_output(args)
    progress_cb = _output_progress if json_out else None

    controller, _settings = _build_cli_pipeline_controller()

    # コンテンツの取得: --stdin または --file
    if getattr(args, "stdin", False):
        body = sys.stdin.read()
        if not body:
            if json_out:
                _output_error("stdin からの入力が空です")
            print("エラー: stdin からの入力が空です", file=sys.stderr)
            raise SystemExit(1)
    else:
        file_path = Path(args.file)
        if not file_path.is_file():
            if json_out:
                _output_error(f"ファイルが見つかりません: {file_path}")
            print(f"エラー: ファイルが見つかりません: {file_path}", file=sys.stderr)
            raise SystemExit(1)
        body = file_path.read_text(encoding="utf-8")

    # インジェストロックを取得
    lock = ingest_lock(Path(controller.source_store.root_dir))
    try:
        lock.acquire()
    except LockAcquisitionError:
        msg = "別のインジェストが実行中です（ロック競合）"
        if json_out:
            _output_error(msg)
        print(f"エラー: {msg}", file=sys.stderr)
        raise SystemExit(1)

    try:
        ingester = JournalIngester(controller.source_store)

        ingest_result = ingester.add_entry(
            title=args.title,
            body=body,
            repository=args.repository,
            entry_id=args.entry_id,
        )

        if ingest_result.errors > 0:
            if json_out:
                _output_error(ingest_result.error_details[0])
            print(f"エラー: {ingest_result.error_details[0]}", file=sys.stderr)
            raise SystemExit(1)

        pipeline_summary = await controller.ingest_and_index(
            f"ingest(journal): add {args.title}",
            progress_callback=progress_cb,
        )
        _print_ingest_result(
            ingest_result, pipeline_summary, context=f"journal/{args.repository}",
            json_output=json_out,
        )
    finally:
        lock.release()


def run_migrate_journal(args: argparse.Namespace) -> None:
    """既存ジャーナルファイルを source_store に一括配置する.

    Args:
        args: コマンドライン引数（--dir, --repository）
    """
    from .pipeline.ingesters.journal import JournalIngester
    from .store.source_store import SourceStore

    from .config import get_settings
    settings = get_settings()

    if not settings.source_store_dir:
        print("エラー: source_store_dir が設定されていません", file=sys.stderr)
        raise SystemExit(1)

    store = SourceStore(Path(settings.source_store_dir))
    store.initialize()

    try:
        ingester = JournalIngester(store)
        result = ingester.import_directory(
            dir_path=args.dir,
            repository=args.repository,
        )

        print(result.summary(context=f"repository={args.repository}"))

        if result.placed > 0:
            print(
                "\n事後処理: 以下のコマンドでインデックスを構築してください:\n"
                "  uv run python -m rag.cli rebuild --mode incremental"
            )
    finally:
        store.close()


def run_migrate(args: argparse.Namespace) -> None:
    """metadata.db のスキーマをマイグレーションする."""
    from rag.config import get_settings
    from rag.store.metadata_db import MetadataDB

    settings = get_settings()
    source_store_dir = settings.source_store_dir
    if not source_store_dir:
        print("エラー: source_store_dir が設定されていません", file=sys.stderr)
        raise SystemExit(1)

    db_path = Path(source_store_dir) / "metadata.db"
    if not db_path.exists():
        print(f"エラー: metadata.db が見つかりません: {db_path}", file=sys.stderr)
        raise SystemExit(1)

    db = MetadataDB(db_path)
    try:
        db.initialize()
        applied = db.migrate()
    finally:
        db.close()

    if applied:
        print(f"マイグレーション完了（{len(applied)} 件適用）:")
        for desc in applied:
            print(f"  - {desc}")
    else:
        print("マイグレーション不要（スキーマは最新です）")


def run_generate_api_key(args: argparse.Namespace) -> None:
    """Upload HTTP API 用の API キーを生成する.

    仕様: docs/specs/infrastructure/upload-auth.md
    """
    import secrets

    import keyring

    from .config import UPLOAD_API_KEY_NAME, UPLOAD_API_KEY_SERVICE

    api_key = secrets.token_urlsafe(32)

    if not args.save:
        print(f"Generated API key: {api_key}")
        print()
        print("Usage:")
        print("  Set the X-API-Key header in your HTTP requests.")
        print("  To save this key to keyring, re-run with --save option.")
        return

    # --save: keyring に保存
    try:
        existing = keyring.get_password(UPLOAD_API_KEY_SERVICE, UPLOAD_API_KEY_NAME)
    except Exception as exc:
        print(f"エラー: keyring へのアクセスに失敗しました: {exc}", file=sys.stderr)
        raise SystemExit(1)

    if existing is not None and not args.force:
        print("既存の API キーが keyring に登録されています。")
        answer = input("上書きしますか？ [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("keyring への保存はキャンセルされました。既存のキーが引き続き有効です。")
            return

    try:
        keyring.set_password(UPLOAD_API_KEY_SERVICE, UPLOAD_API_KEY_NAME, api_key)
    except Exception as exc:
        print(f"エラー: keyring への保存に失敗しました: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print(f"Generated API key: {api_key}")
    print(f"keyring に保存しました (service={UPLOAD_API_KEY_SERVICE}, key={UPLOAD_API_KEY_NAME})")


def _format_cli_size(size_bytes: int) -> str:
    """バイト数を人間が読みやすい単位に変換する."""
    from .rag_knowledge import format_file_size

    return format_file_size(size_bytes)


# --- インジェスト系 CLI コマンド ---


def _build_cli_pipeline_controller() -> tuple[
    "PipelineController", "Settings"
]:
    """CLI 用の PipelineController を構築する.

    Returns:
        (PipelineController, Settings) のタプル
    """
    import contextlib
    import io

    from .bm25_index import BM25Index
    from .config import get_settings
    from .converter import Converter
    from .converter.pdf_extractor import PdfBackendConfig
    from .embedding.factory import get_embedding_provider
    from .indexer import Indexer
    from .media.analyzer import MediaAnalyzer
    from .pipeline.controller import PipelineController
    from .store.source_store import SourceStore
    from .vector_store import VectorStore

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
    media_analyzer = MediaAnalyzer(
        lmstudio_base_url=settings.lmstudio_base_url,
        vision_model=settings.rag_vision_model,
        reasoning_effort=settings.rag_vision_reasoning_effort,
        frame_interval=settings.rag_vision_frame_interval,
        max_tokens=settings.rag_vision_max_tokens,
        api_timeout=settings.rag_vision_api_timeout,
    )
    converter = Converter(
        regen_option="force",
        pdf_config=pdf_config,
        youtube_merge_gap_sec=settings.rag_youtube_merge_gap_sec,
        youtube_merge_max_chars=settings.rag_youtube_merge_max_chars,
        media_analyzer=media_analyzer,
    )

    embedding_provider = get_embedding_provider(settings, settings.embedding_provider)

    with contextlib.redirect_stdout(io.StringIO()):
        vector_store = VectorStore.create_http(
            embedding_provider=embedding_provider,
            host=settings.chromadb_server_host,
            port=settings.chromadb_server_port,
            collection_name=settings.chromadb_collection_name,
            hnsw_m=settings.hnsw_m,
            hnsw_construction_ef=settings.hnsw_construction_ef,
            hnsw_search_ef=settings.hnsw_search_ef,
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
        embedding_prefix_enabled=settings.embedding_prefix_enabled,
        embedding_context_length=settings.rag_embedding_context_length,
        worst_token_char_ratio=settings.rag_worst_token_char_ratio,
    )

    controller = PipelineController(
        source_store=source_store,
        converted_store_dir=converted_store_dir,
        converter=converter,
        indexer=indexer,
    )
    return controller, settings


def _create_safe_browsing_client_cli(
    settings: "Settings",
) -> "SafeBrowsingClient | None":
    """CLI 用: SafeBrowsingClient を生成する.

    SafeBrowsingConfigError 時はエラーメッセージを表示して終了する。
    """
    from .safe_browsing import SafeBrowsingConfigError, create_safe_browsing_client

    try:
        return create_safe_browsing_client(settings)
    except SafeBrowsingConfigError as e:
        logger.error("Safe Browsing 設定エラー: %s", e)
        sys.exit(1)


def _print_ingest_result(
    ingest_result: "IngestResult",
    pipeline_summary: "PipelineSummary | None",
    *,
    context: str = "",
    json_output: bool = False,
) -> None:
    """取り込み結果を標準出力に表示する."""
    if json_output:
        _output_result(_ingest_result_to_dict(ingest_result, pipeline_summary))
        return
    print(ingest_result.summary(context=context))
    if pipeline_summary is not None:
        print(f"パイプライン: {pipeline_summary.processed}件処理")
        if pipeline_summary.warnings:
            print(f"パイプライン警告: {len(pipeline_summary.warnings)}件")
            for warn in pipeline_summary.warnings:
                print(f"  - {warn}")
        if pipeline_summary.errors:
            print(f"パイプラインエラー: {len(pipeline_summary.errors)}件")
            for err in pipeline_summary.errors:
                print(f"  - {err}")


async def run_ingest_youtube(args: argparse.Namespace) -> None:
    """YouTube 単一動画取り込み."""
    from .pipeline.ingesters.youtube import YoutubeIngester

    json_out = _is_json_output(args)
    progress_cb = _output_progress if json_out else None

    controller, settings = _build_cli_pipeline_controller()

    youtube_ingester = YoutubeIngester(
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
        ingest_result = await youtube_ingester.ingest_video(args.video_url)
    except (ValueError, TypeError) as e:
        if json_out:
            _output_error(str(e))
        logger.error("エラー: %s", e)
        sys.exit(1)

    if ingest_result.placed == 0:
        _print_ingest_result(ingest_result, None, context=f"動画: {args.video_url}", json_output=json_out)
        return

    pipeline_summary = await controller.ingest_and_index(
        f"ingest(youtube): {args.video_url}",
        progress_callback=progress_cb,
    )
    _print_ingest_result(ingest_result, pipeline_summary, context=f"動画: {args.video_url}", json_output=json_out)


async def run_ingest_youtube_playlist(args: argparse.Namespace) -> None:
    """YouTube プレイリスト一括取り込み."""
    from .pipeline.ingesters.youtube import YoutubeIngester

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    max_videos = args.max_videos if args.max_videos is not None else settings.rag_youtube_max_videos

    youtube_ingester = YoutubeIngester(
        controller.source_store,
        max_videos=max_videos,
        request_interval=settings.rag_youtube_request_interval,
        request_timeout=settings.rag_youtube_request_timeout,
        whisper_model=settings.rag_youtube_whisper_model,
        whisper_device=settings.rag_youtube_whisper_device,
        transcript_languages=settings.rag_youtube_transcript_languages,
        max_duration=settings.rag_youtube_max_duration,
    )

    progress_cb = _output_progress if json_out else None

    try:
        ingest_result = await youtube_ingester.crawl_playlist(
            args.playlist_url,
            max_videos=max_videos,
            progress_callback=_wrap_progress(progress_cb, PHASE_FETCH),
        )
    except (ValueError, TypeError) as e:
        if json_out:
            _output_error(str(e))
        logger.error("エラー: %s", e)
        sys.exit(1)

    if ingest_result.placed == 0:
        _print_ingest_result(ingest_result, None, context=f"プレイリスト: {args.playlist_url}", json_output=json_out)
        return

    pipeline_summary = await controller.ingest_and_index(
        f"ingest(youtube-playlist): {args.playlist_url}",
        progress_callback=progress_cb,
    )
    _print_ingest_result(ingest_result, pipeline_summary, context=f"プレイリスト: {args.playlist_url}", json_output=json_out)



def _create_youtube_ingester_cli(
    source_store: SourceStore, settings: Settings,
) -> YoutubeIngester:
    """CLI 用 YoutubeIngester を生成する."""
    from .pipeline.ingesters.youtube import YoutubeIngester

    return YoutubeIngester(
        source_store,
        max_videos=settings.rag_youtube_max_videos,
        request_interval=settings.rag_youtube_request_interval,
        request_timeout=settings.rag_youtube_request_timeout,
        whisper_model=settings.rag_youtube_whisper_model,
        whisper_device=settings.rag_youtube_whisper_device,
        transcript_languages=settings.rag_youtube_transcript_languages,
        max_duration=settings.rag_youtube_max_duration,
    )


async def run_crawl_bluesky(args: argparse.Namespace) -> None:
    """BlueSky 投稿取り込み."""
    from .pipeline.ingesters.bluesky import BlueskyIngester

    from py_common_lib.httpx import ConstrainedClient  # safety:allowed

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    max_posts = args.max_posts if args.max_posts is not None else settings.rag_bluesky_max_posts
    include_reposts = args.include_reposts if args.include_reposts is not None else settings.rag_bluesky_include_reposts

    bluesky_ingester = BlueskyIngester(
        controller.source_store,
        appview_url=settings.rag_bluesky_appview_url,
        max_posts=max_posts,
        include_reposts=include_reposts,
    )

    progress_cb = _output_progress if json_out else None

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_bluesky_request_timeout,
            request_interval=settings.rag_bluesky_request_interval,
        ) as client:
            force = args.force

            ingest_result, placed_items = await bluesky_ingester.crawl_bluesky(
                args.handle,
                max_posts=max_posts,
                include_reposts=include_reposts,
                force=force,
                client=client,
                progress_callback=_wrap_progress(progress_cb, PHASE_FETCH),
            )

            # 投稿内 URL の自動取り込み
            url_stats: dict[str, int] = {}
            if placed_items:
                youtube_ingester = _create_youtube_ingester_cli(controller.source_store, settings)
                url_stats = await bluesky_ingester.follow_urls(
                    placed_items,
                    youtube_ingester=youtube_ingester,
                    force=force,
                    force_youtube_reingest=settings.rag_bluesky_force_youtube_reingest,
                )
    except (ValueError, TypeError) as e:
        if json_out:
            _output_error(str(e))
        logger.error("エラー: %s", e)
        sys.exit(1)

    pipeline_summary = await controller.ingest_and_index(
        f"ingest(bluesky): {args.handle}",
        progress_callback=progress_cb,
    )
    if json_out:
        data = _ingest_result_to_dict(ingest_result, pipeline_summary)
        if url_stats:
            data["url_follow"] = {
                "web_placed": url_stats.get("web_placed", 0),
                "youtube_placed": url_stats.get("youtube_placed", 0),
                "errors": url_stats.get("errors", 0),
            }
        _output_result(data)
    else:
        _print_ingest_result(ingest_result, pipeline_summary, context=f"ハンドル: {args.handle}")
        if url_stats and any(url_stats.get(k, 0) > 0 for k in ("web_placed", "youtube_placed", "errors")):
            parts = ["URL 自動取り込み:"]
            web_n = url_stats.get("web_placed", 0)
            yt_n = url_stats.get("youtube_placed", 0)
            err_n = url_stats.get("errors", 0)
            if web_n > 0 or yt_n > 0:
                parts.append(f"Web {web_n}件, YouTube {yt_n}件")
            if err_n > 0:
                parts.append(f"エラー {err_n}件")
            print(" ".join(parts))


async def run_crawl_zenn(args: argparse.Namespace) -> None:
    """Zenn コンテンツ取り込み."""
    from .pipeline.ingesters.zenn import ZennIngester

    from py_common_lib.httpx import ConstrainedClient  # safety:allowed

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    max_articles = args.max_articles if args.max_articles is not None else settings.rag_zenn_max_articles

    zenn_ingester = ZennIngester(
        controller.source_store,
        max_articles=max_articles,
    )

    progress_cb = _output_progress if json_out else None

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_zenn_request_timeout,
            request_interval=settings.rag_zenn_request_interval,
        ) as client:
            ingest_result = await zenn_ingester.crawl_zenn(
                args.username,
                max_articles=max_articles,
                content_type=args.content_type,
                force=args.force,
                client=client,
                progress_callback=_wrap_progress(progress_cb, PHASE_FETCH),
            )
    except (ValueError, TypeError) as e:
        if json_out:
            _output_error(str(e))
        logger.error("エラー: %s", e)
        sys.exit(1)

    if ingest_result.placed == 0 and ingest_result.errors == 0:
        if json_out:
            _output_result(_ingest_result_to_dict(ingest_result, None))
            return
        if ingest_result.skipped > 0:
            print(f"全 {ingest_result.skipped} 件のコンテンツがスキップされました（ユーザー: {args.username}）。上書きするには --force を指定してください")
        else:
            print(f"コンテンツが見つかりませんでした（ユーザー: {args.username}）")
        return

    pipeline_summary = await controller.ingest_and_index(
        f"ingest(zenn): {args.username}",
        progress_callback=progress_cb,
    )
    _print_ingest_result(ingest_result, pipeline_summary, context=f"ユーザー: {args.username}", json_output=json_out)


async def run_add_document(args: argparse.Namespace) -> None:
    """単一ドキュメント取り込み."""
    from .infrastructure.file_lock import LockAcquisitionError, ingest_lock
    from .pipeline.ingesters.local import LocalIngester
    from .upload import decode_upload_content

    json_out = _is_json_output(args)
    progress_cb = _output_progress if json_out else None

    controller, settings = _build_cli_pipeline_controller()

    supported_extensions = [
        (ext.strip() if ext.strip().startswith(".") else f".{ext.strip()}").lower()
        for ext in settings.rag_document_supported_extensions.split(",")
        if ext.strip()
    ]

    # コンテンツの取得: --stdin または --file
    if getattr(args, "stdin", False):
        # --stdin モード: --filename が必須
        filename = args.filename
        if not filename:
            msg = "--stdin 使用時は --filename が必須です"
            if json_out:
                _output_error(msg)
            print(f"エラー: {msg}", file=sys.stderr)
            raise SystemExit(1)

        raw_input = sys.stdin.read()
        if not raw_input:
            msg = "stdin からの入力が空です"
            if json_out:
                _output_error(msg)
            print(f"エラー: {msg}", file=sys.stderr)
            raise SystemExit(1)

        # encoding に基づいてデコード
        encoding = getattr(args, "encoding", "text")
        try:
            data = decode_upload_content(raw_input, encoding)
        except ValueError as e:
            if json_out:
                _output_error(str(e))
            print(f"エラー: {e}", file=sys.stderr)
            raise SystemExit(1)

        # 拡張子チェック
        ext = Path(filename).suffix.lower()
        if ext not in supported_extensions:
            msg = f"対応していないファイル形式です: {ext!r}（対応: {', '.join(supported_extensions)}）"
            if json_out:
                _output_error(msg)
            print(f"エラー: {msg}", file=sys.stderr)
            raise SystemExit(1)
        display_name = filename
    else:
        # --file モード（従来動作）
        file_path_str = args.file_path
        if not file_path_str or not file_path_str.strip():
            if json_out:
                _output_error("file_path が空です")
            print("エラー: file_path が空です", file=sys.stderr)
            raise SystemExit(1)
        resolved = Path(file_path_str.strip()).resolve()
        if not resolved.exists():
            if json_out:
                _output_error(f"ファイルが見つかりません: {resolved}")
            print(f"エラー: ファイルが見つかりません: {resolved}", file=sys.stderr)
            raise SystemExit(1)
        if resolved.is_dir():
            if json_out:
                _output_error(f"パスはファイルではなくディレクトリです: {resolved}")
            print(f"エラー: パスはファイルではなくディレクトリです: {resolved}", file=sys.stderr)
            raise SystemExit(1)
        data = resolved.read_bytes()
        # --filename が指定されていればそちらを優先（Upload API が一時ファイル経由で呼ぶケース）
        filename = args.filename if args.filename else resolved.name
        ext = Path(filename).suffix.lower()
        if ext not in supported_extensions:
            msg = f"対応していないファイル形式です: {ext!r}（対応: {', '.join(supported_extensions)}）"
            if json_out:
                _output_error(msg)
            print(f"エラー: {msg}", file=sys.stderr)
            raise SystemExit(1)
        display_name = file_path_str

    # インジェストロックを取得
    lock = ingest_lock(Path(controller.source_store.root_dir))
    try:
        lock.acquire()
    except LockAcquisitionError:
        msg = "別のインジェストが実行中です（ロック競合）"
        if json_out:
            _output_error(msg)
        print(f"エラー: {msg}", file=sys.stderr)
        raise SystemExit(1)

    try:
        local_ingester = LocalIngester(
            controller.source_store,
            supported_extensions=supported_extensions,
            http_mode_enabled=False,
            allowed_dirs=None,
        )

        try:
            ingest_result = local_ingester.add_document(
                data, filename, upload_mode=args.upload_mode,
            )
        except FileExistsError as e:
            msg = f"同名ファイルが既に存在します: {filename} ({e})"
            if json_out:
                _output_error(msg)
            print(f"エラー: {msg}", file=sys.stderr)
            raise SystemExit(1)

        if ingest_result.placed == 0 and ingest_result.errors == 0:
            if json_out:
                _output_result(_ingest_result_to_dict(ingest_result, None))
                return
            print(f"取り込み対象がありませんでした: {display_name}")
            return
        if ingest_result.errors > 0:
            if json_out:
                _output_error(ingest_result.error_details[0])
            print(f"エラー: {ingest_result.error_details[0]}", file=sys.stderr)
            raise SystemExit(1)

        pipeline_summary = await controller.ingest_and_index(
            f"ingest(local): add {filename}",
            progress_callback=progress_cb,
        )
        _print_ingest_result(ingest_result, pipeline_summary, context=display_name, json_output=json_out)
    finally:
        lock.release()


async def run_crawl_documents(args: argparse.Namespace) -> None:
    """ディレクトリ一括取り込み."""
    from .pipeline.ingesters.local import LocalIngester

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    supported_extensions = [
        (ext.strip() if ext.strip().startswith(".") else f".{ext.strip()}").lower()
        for ext in settings.rag_document_supported_extensions.split(",")
        if ext.strip()
    ]
    local_ingester = LocalIngester(
        controller.source_store,
        supported_extensions=supported_extensions,
        http_mode_enabled=False,
        allowed_dirs=None,
    )

    progress_cb = _output_progress if json_out else None

    ingest_result = local_ingester.crawl_documents(
        args.dir_path, args.pattern, upload_mode=args.upload_mode,
        progress_callback=_wrap_progress(progress_cb, PHASE_FETCH),
    )

    if ingest_result.placed == 0 and ingest_result.errors == 0:
        if json_out:
            _output_result(_ingest_result_to_dict(ingest_result, None))
            return
        print(f"対象ファイルが見つかりませんでした: {args.dir_path}")
        return
    if ingest_result.errors > 0 and ingest_result.placed == 0:
        if json_out:
            _output_error(ingest_result.error_details[0])
        print(f"エラー: {ingest_result.error_details[0]}", file=sys.stderr)
        raise SystemExit(1)

    pipeline_summary = await controller.ingest_and_index(
        f"ingest(local): crawl {args.dir_path}",
        progress_callback=progress_cb,
    )
    _print_ingest_result(ingest_result, pipeline_summary, context=f"ディレクトリ: {args.dir_path}", json_output=json_out)


async def run_site_ingest(args: argparse.Namespace) -> None:
    """Scrapy によるサイト一括取り込み."""
    import re
    import time as time_mod

    from .scrapy.bridge import import_to_source_store
    from .scrapy.runner import ScrapyRunner
    from .utils.url import check_ssrf, validate_url

    json_out = _is_json_output(args)
    progress_cb = _output_progress if json_out else None

    urls: list[str] = args.url  # nargs='+' なのでリスト
    multi_url_mode = len(urls) >= 2

    # 全 URL バリデーション
    validated_urls: list[str] = []
    for u in urls:
        try:
            validated = validate_url(u)
            check_ssrf(validated)
            validated_urls.append(validated)
        except ValueError as e:
            if json_out:
                _output_error(str(e))
            logger.error("エラー: %s", e)
            sys.exit(1)

    # クロールモード固有のバリデーション
    if not multi_url_mode and args.url_pattern:
        try:
            re.compile(args.url_pattern)
        except re.error as e:
            if json_out:
                _output_error(f"無効な正規表現パターン: {e}")
            logger.error("無効な正規表現パターン: %s", e)
            sys.exit(1)

    controller, settings = _build_cli_pipeline_controller()

    # max_pages のクランプ（クロールモードのみ）
    effective_max_pages: int | None = None
    if not multi_url_mode:
        effective_max_pages = (
            args.max_pages if args.max_pages is not None
            else settings.site_ingest_max_pages
        )
        if effective_max_pages < 1:
            effective_max_pages = 1
            logger.warning("max_pages を 1 にクランプしました")
        elif effective_max_pages > 1000:
            effective_max_pages = 1000
            logger.warning("max_pages を 1000 にクランプしました")

    # ドメイン導出
    from urllib.parse import urlparse
    if multi_url_mode:
        # 複数 URL モード: 全ドメインの和集合
        domains = []
        for u in validated_urls:
            hostname = urlparse(u).hostname
            if hostname and hostname not in domains:
                domains.append(hostname)
        allowed_domains = ",".join(domains)
    else:
        parsed = urlparse(validated_urls[0])
        allowed_domains = parsed.hostname or ""

    start_time = time_mod.monotonic()

    # Scrapy Runner でクロール / 複数 URL 取得
    runner = ScrapyRunner(
        temp_dir=settings.site_ingest_temp_dir,
        delay_sec=settings.site_ingest_delay_sec,
        max_pages=effective_max_pages or settings.site_ingest_max_pages,
        download_timeout=settings.site_ingest_download_timeout,
        timeout_sec=settings.site_ingest_timeout_sec,
        error_count=settings.site_ingest_error_count,
    )

    if multi_url_mode:
        crawl_result = await runner.run(
            start_urls=validated_urls,
            allowed_domains=allowed_domains,
        )
    else:
        crawl_result = await runner.run(
            start_url=validated_urls[0],
            allowed_domains=allowed_domains,
            url_pattern=args.url_pattern,
            max_pages=effective_max_pages,
            force=args.force,
        )

    display_url = validated_urls[0] if not multi_url_mode else f"{len(validated_urls)} URLs"

    if not crawl_result.jsonl_path.exists():
        elapsed = time_mod.monotonic() - start_time
        if json_out:
            _output_result({
                "placed": 0,
                "overwritten": 0,
                "skipped": 0,
                "errors": 0,
                "elapsed": round(elapsed, 1),
                "scrapy_exit_code": crawl_result.exit_code,
                "no_output": True,
            })
        else:
            print(
                f"クロールが完了しましたが、メタデータが出力されませんでした。"
                f" exit_code={crawl_result.exit_code}, 所要時間={elapsed:.1f}秒",
            )
        return

    # Bridge: JSONL + HTML → source_store
    bridge_result = import_to_source_store(
        jsonl_path=crawl_result.jsonl_path,
        html_dir=crawl_result.output_dir,
        source_store=controller.source_store,
    )

    # パイプライン処理
    pipeline_summary = None
    has_changes = (bridge_result.ingest.placed + bridge_result.ingest.overwritten) > 0
    if has_changes and not args.download_only:
        pipeline_summary = await controller.ingest_and_index(
            f"ingest(web): site-ingest {display_url}",
            progress_callback=progress_cb,
        )
    elif has_changes and args.download_only:
        controller.commit(f"ingest(web): site-ingest {display_url} (download_only)")

    # 正常完了後のクリーンアップ（仕様: docs/specs/site-ingest.md）
    if crawl_result.success:
        crawl_result.cleanup()

    # 操作全体の所要時間（クロール + Bridge + パイプライン）
    elapsed = time_mod.monotonic() - start_time

    if json_out:
        data: dict[str, object] = _ingest_result_to_dict(bridge_result.ingest, pipeline_summary)
        data["elapsed"] = round(elapsed, 1)
        data["download_only"] = args.download_only
        if not crawl_result.success:
            data["scrapy_exit_code"] = crawl_result.exit_code
        _output_result(data)
    else:
        # text 出力
        print(
            f"サイト取り込み完了: {bridge_result.ingest.placed}件新規配置"
            f", {bridge_result.ingest.overwritten}件上書き"
            f", {bridge_result.ingest.skipped}件スキップ"
            f", {bridge_result.ingest.errors}件エラー"
        )
        print(f"所要時間: {elapsed:.1f}秒")
        if args.download_only:
            print("パイプライン処理: スキップ（download_only）")
        elif pipeline_summary is not None:
            print(f"パイプライン: {pipeline_summary.processed}件処理")
            if pipeline_summary.errors:
                print(f"パイプラインエラー: {len(pipeline_summary.errors)}件")
        if not crawl_result.success:
            print(f"Scrapy exit_code={crawl_result.exit_code}（部分的な結果）")


async def run_update_aozora_catalog(args: argparse.Namespace) -> None:
    """青空文庫カタログ更新."""
    from .pipeline.ingesters.aozora import AozoraIngester

    from py_common_lib.httpx import ConstrainedClient  # safety:allowed

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    aozora_ingester = AozoraIngester(
        controller.source_store,
        max_works=settings.rag_aozora_max_works,
    )

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_aozora_request_timeout,
            request_interval=settings.rag_aozora_request_interval,
        ) as client:
            result_text = await aozora_ingester.update_catalog(client=client)
    except (ValueError, TypeError) as e:
        if json_out:
            _output_error(str(e))
        logger.error("エラー: %s", e)
        sys.exit(1)

    # カタログはパイプライン処理対象外だが、未コミット変更が残ると
    # 後続の rebuild で失敗するためコミットしておく
    controller.commit("update_aozora_catalog")
    if json_out:
        _output_result({"message": result_text})
    else:
        print(result_text)


def run_search_aozora(args: argparse.Namespace) -> None:
    """青空文庫カタログ検索."""
    from .pipeline.ingesters.aozora import AozoraIngester

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    aozora_ingester = AozoraIngester(
        controller.source_store,
        max_works=settings.rag_aozora_max_works,
    )

    try:
        results = aozora_ingester.search(
            author=args.author,
            title=args.title,
            limit=args.limit,
        )
    except (ValueError, TypeError) as e:
        if json_out:
            _output_error(str(e))
        logger.error("エラー: %s", e)
        sys.exit(1)

    if json_out:
        _output_result({
            "results": [
                {
                    "book_id": r["book_id"],
                    "title": r["title"],
                    "person_id": r["person_id"],
                    "author": r["author"],
                    "copyright": r["copyright"],
                }
                for r in results
            ],
            "count": len(results),
        })
        return

    if not results:
        print("検索結果: 0件")
        return

    print(f"検索結果: {len(results)}件")
    for r in results:
        print(
            f"  [{r['book_id']}] {r['title']} / "
            f"[{r['person_id']}] {r['author']} ({r['copyright']})"
        )


async def run_ingest_aozora(args: argparse.Namespace) -> None:
    """青空文庫作品取り込み."""
    from .pipeline.ingesters.aozora import AozoraIngester

    from py_common_lib.httpx import ConstrainedClient  # safety:allowed

    json_out = _is_json_output(args)
    progress_cb = _output_progress if json_out else None

    controller, settings = _build_cli_pipeline_controller()

    aozora_ingester = AozoraIngester(
        controller.source_store,
        max_works=settings.rag_aozora_max_works,
    )

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_aozora_request_timeout,
            request_interval=settings.rag_aozora_request_interval,
        ) as client:
            ingest_result = await aozora_ingester.add_work(
                args.book_id, client=client,
            )
    except (ValueError, TypeError) as e:
        if json_out:
            _output_error(str(e))
        logger.error("エラー: %s", e)
        sys.exit(1)

    pipeline_summary = await controller.ingest_and_index(
        f"ingest(aozora): book_id={args.book_id}",
        progress_callback=progress_cb,
    )
    _print_ingest_result(ingest_result, pipeline_summary, context=f"作品ID: {args.book_id}", json_output=json_out)


async def run_ingest_aozora_author(args: argparse.Namespace) -> None:
    """青空文庫著者一括取り込み."""
    from .pipeline.ingesters.aozora import AozoraIngester

    from py_common_lib.httpx import ConstrainedClient  # safety:allowed

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    max_works = args.max_works if args.max_works is not None else settings.rag_aozora_max_works

    aozora_ingester = AozoraIngester(
        controller.source_store,
        max_works=max_works,
    )

    progress_cb = _output_progress if json_out else None

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_aozora_request_timeout,
            request_interval=settings.rag_aozora_request_interval,
        ) as client:
            ingest_result = await aozora_ingester.crawl_author(
                args.person_id,
                max_works=max_works,
                client=client,
                progress_callback=_wrap_progress(progress_cb, PHASE_FETCH),
            )
    except (ValueError, TypeError) as e:
        if json_out:
            _output_error(str(e))
        logger.error("エラー: %s", e)
        sys.exit(1)

    pipeline_summary = await controller.ingest_and_index(
        f"ingest(aozora): person_id={args.person_id}",
        progress_callback=progress_cb,
    )
    _print_ingest_result(ingest_result, pipeline_summary, context=f"著者ID: {args.person_id}", json_output=json_out)


if __name__ == "__main__":
    from .config import ensure_utf8_streams

    ensure_utf8_streams(include_stdout=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    main()

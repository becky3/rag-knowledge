"""RAG Knowledge CLIモジュール

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import logging
import math
import os
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast
from urllib.parse import urldefrag

from .errors import CliErrorCode
from .filter_parser import parse_filters
from .pipeline.models import (
    PipelinePhase,
    format_pipeline_error as _format_pipeline_error,
    format_pipeline_warning as _format_pipeline_warning,
)
from .store.models import SourceStatus
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
    from .pipeline.ingesters._common import IngestErrorDetail, IngestResult
    from .safe_browsing import SafeBrowsingClient
    from .pipeline.ingesters.youtube import YoutubeIngester
    from .pipeline.models import PipelineSummary
    from .search.search_port import RealSearchAdapter
    from .store.source_store import SourceStore
    from .vector_store import VectorStore


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


def _output_error(
    code: CliErrorCode,
    message: str,
    details: dict[str, object] | None = None,
    *,
    lock_type: str | None = None,
) -> None:
    """エラー JSON を出力し、exit code 1 で終了する.

    lock_type が指定された場合は details["lock_type"] に追加する（details が
    未指定なら自動生成）。サーバー側の Retry-After 分岐・メッセージ切替のため。
    """
    payload: dict[str, object] = {
        "type": "error",
        "error": True,
        "code": code.value,
        "message": message,
    }
    effective_details = dict(details) if details is not None else None
    if lock_type is not None:
        if effective_details is None:
            effective_details = {}
        effective_details["lock_type"] = lock_type
    if effective_details is not None:
        payload["details"] = effective_details
    _output_json(payload)
    logger.error("CLI error: %s (code=%s)", message, code.value)
    sys.exit(1)


# write_lock 取得失敗（kind=write）時の指数バックオフ間隔（秒）。
# CLI 連続実行時の OS ファイルロック解放遅延を吸収する目的。
# 4 回再試行 = 初回 + 4 回 = 最大 5 回試行。合計待機時間 ~1.85s。
# Issue #755
_WRITE_LOCK_RETRY_BACKOFFS_SEC: tuple[float, ...] = (0.1, 0.25, 0.5, 1.0)

# write 操作の rebuild_lock プリチェック失敗（kind=rebuild）時の指数バックオフ間隔（秒）。
# rebuild プロセスの visible log 完了後も python interpreter shutdown 処理
# （chroma client teardown / GC / file flush 等）の間は OS ファイルロックが
# 保持され続け、その期間 write 操作が即失敗する事象（QA で 10〜15 秒観察）を
# 吸収する目的。rebuild 本体処理の長時間 retry は意図しないが、shutdown 期間の
# 短時間 retry は安全に吸収できる（Issue #779）。
# 5 回再試行 = 初回 + 5 回 = 最大 6 回試行。合計待機時間 ~15.5s。
_REBUILD_PROBE_RETRY_BACKOFFS_SEC: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0)


@contextlib.contextmanager
def _write_lock_or_exit(
    source_store_root: Path,
    *,
    json_out: bool,
) -> Iterator[None]:
    """書き込み系 CLI 用の write_lock を取得するコンテキストマネージャ.

    ロック取得に失敗した場合はロック種別に応じたメッセージで _output_error 経由で
    exit する。with 文を抜ける際にロックは自動解放される。

    kind=write の競合に対しては ``_WRITE_LOCK_RETRY_BACKOFFS_SEC`` の指数バック
    オフでリトライする（CLI 連続実行時の OS ロック解放遅延を吸収するため）。
    kind=rebuild の競合に対しては ``_REBUILD_PROBE_RETRY_BACKOFFS_SEC`` の
    短時間バックオフでリトライする（rebuild プロセスの visible log 完了後も
    続く python interpreter shutdown 期間の lock 保持を吸収するため）。
    両者ともリトライ全失敗時は exit する。

    仕様: docs/specs/infrastructure/content-upload.md の「rebuild との相互排他」

    使い方::

        with _write_lock_or_exit(source_store_root, json_out=json_out):
            # ロック保持下の処理
            ...
    """
    import time

    from .infrastructure.file_lock import LockAcquisitionError, write_lock

    lock = write_lock(source_store_root)
    last_error: LockAcquisitionError | None = None
    write_iter = iter(_WRITE_LOCK_RETRY_BACKOFFS_SEC)
    rebuild_iter = iter(_REBUILD_PROBE_RETRY_BACKOFFS_SEC)
    next_backoff: float | None = None
    while True:
        if next_backoff is not None:
            time.sleep(next_backoff)
        try:
            lock.acquire()
        except LockAcquisitionError as e:
            last_error = e
            if e.kind == "rebuild":
                next_backoff = next(rebuild_iter, None)
            else:
                next_backoff = next(write_iter, None)
            if next_backoff is None:
                break
        else:
            last_error = None
            break

    if last_error is not None:
        if last_error.kind == "rebuild":
            msg = "別の再構築が実行中です（ロック競合）"
        else:
            msg = "別の取り込みが実行中です（ロック競合）"
        if json_out:
            _output_error(
                CliErrorCode.LOCK_CONFLICT, msg, lock_type=last_error.kind,
            )
        else:
            print(f"エラー: {msg}", file=sys.stderr)
        raise SystemExit(1) from last_error
    try:
        yield
    finally:
        lock.release()


class _JsonAwareArgumentParser(argparse.ArgumentParser):
    """argparse のバリデーションエラーを CLI 2 値契約（0/1）に統一する.

    標準の argparse はバリデーション失敗で exit code 2（POSIX usage error 慣習）を
    返すが、これは CLI exit code の 2 値契約（0=完走 / 1=致命、仕様:
    docs/specs/rebuild-stats.md）と矛盾する。
    本プロジェクトでは以下を優先して慣習より契約の一貫性を取る:

    - `--output json`: `_output_error` 経由で stdout に `type:"error"` JSON 行を
      出力してから `sys.exit(1)`（MCP 応答経路が構造化エラーを受け取れる）
    - 上記以外（text モード）: argparse 標準のメッセージを stderr に書いてから
      `sys.exit(1)`（exit code のみ統一、メッセージ形式は argparse 既定）
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        if self._output_json_requested():
            _output_error(CliErrorCode.VALIDATION_ERROR, f"{self.prog}: {message}")
        # text モード: argparse 標準の usage + エラーメッセージを stderr に出す
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")

    @staticmethod
    def _output_json_requested() -> bool:
        argv = sys.argv[1:]
        if "--output=json" in argv:
            return True
        for i, arg in enumerate(argv):
            if arg == "--output" and i + 1 < len(argv) and argv[i + 1] == "json":
                return True
        return False


def _error_detail_message(detail: IngestErrorDetail) -> str:
    """IngestErrorDetail から表示用メッセージを抽出する."""
    message = detail.get("message")
    if message:
        return message
    target = detail["target"]
    category = detail["category"]
    return f"[{category}] {target}" if target else f"[{category}]"


def _output_result(data: dict[str, object]) -> None:
    """結果 JSON を出力する."""
    payload: dict[str, object] = {**data, "type": "result"}
    _output_json(payload)


def _output_result_logged(
    data: dict[str, object], log_msg: str, *log_args: object
) -> None:
    """結果 JSON を出力し、stderr にもログを記録する."""
    _output_result(data)
    logger.info(log_msg, *log_args)


def _is_json_output(args: argparse.Namespace) -> bool:
    """--output json が指定されているかを判定する."""
    return getattr(args, "output_format", "text") == "json"


def _ingest_result_to_dict(
    ingest_result: "IngestResult",
    pipeline_summary: "PipelineSummary | None",
    *,
    entry_id: str | None = None,
) -> dict[str, object]:
    """IngestResult + PipelineSummary を JSON 出力用 dict に変換する.

    仕様: docs/specs/ingesters/common.md「JSON シリアライズ」
    """
    data: dict[str, object] = {
        "placed": ingest_result.placed,
        "skipped": ingest_result.skipped,
        "overwritten": ingest_result.overwritten,
        "errors": ingest_result.errors,
        "error_details": ingest_result.error_details,
        "partial_failures": ingest_result.partial_failures,
        "partial_failure_details": ingest_result.partial_failure_details,
        "aborted": ingest_result.aborted,
        "abort_reason": ingest_result.abort_reason,
    }
    if entry_id is not None:
        data["entry_id"] = entry_id
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
        logger.warning("  [%s] 警告: %s", phase, _format_pipeline_warning(warn))
    for entry in summary.errors:
        logger.error("  [%s] エラー: %s", phase, _format_pipeline_error(entry))


def _add_output_option(parser: argparse.ArgumentParser) -> None:
    """サブコマンドパーサーに --output オプションを追加する."""
    parser.add_argument(
        "--output",
        dest="output_format",
        choices=["text", "json"],
        default="text",
        help="出力フォーマット（text/json、デフォルト: text）",
    )


def _add_skip_pipeline_option(parser: argparse.ArgumentParser) -> None:
    """サブコマンドパーサーに --skip-pipeline オプションを追加する.

    共通仕様は docs/specs/ingesters/common.md「`--skip-pipeline` フラグ共通仕様」を参照。
    """
    parser.add_argument(
        "--skip-pipeline",
        action="store_true",
        default=False,
        help=(
            "取り込み後のパイプライン処理（converter + indexer）を行わない。"
            "source_store への git commit は実行される。"
            "一括取り込み時の高速化用。後で `rebuild --mode incremental` を実行する必要がある"
        ),
    )


def _is_skip_pipeline(args: argparse.Namespace) -> bool:
    """args から --skip-pipeline 指定の有無を取得する.

    `getattr` でデフォルトを偽として扱うため、--skip-pipeline 未対応のサブコマンドが
    呼ばれた場合でも安全に False を返す。
    """
    return bool(getattr(args, "skip_pipeline", False))


def _print_skip_pipeline_notice(json_out: bool) -> None:
    """--skip-pipeline 指定時に stdout へ案内文を出力する.

    `json_out=True` で呼ばれた場合は早期 return し何も出力しない（JSON 構造への反映は
    呼び出し側で `pipeline` フィールドの省略等で行う前提）。
    共通仕様は docs/specs/ingesters/common.md「`--skip-pipeline` フラグ共通仕様」を参照。
    """
    if json_out:
        return
    print(
        "パイプライン未実行（--skip-pipeline 指定）。後で `uv run python -m rag.cli rebuild --mode incremental` を実行してください。",
    )


def _commit_for_skip_pipeline(
    controller: "PipelineController",
    commit_message: str,
) -> None:
    """`--skip-pipeline` 経路の commit のみ実行（pipeline 処理はスキップ）.

    `controller.ingest_and_index()` 内では git commit + converter + indexer が一括実行されるが、
    `--skip-pipeline` 指定時は converter / indexer をスキップしつつ source_store の commit は
    実行する必要がある（後段の `rebuild --mode incremental` が差分を検出できるようにするため）。
    本ヘルパーは commit のみ実行し pipeline は呼ばない。

    仕様: docs/specs/ingesters/common.md「`--skip-pipeline` フラグ共通仕様」
    """
    controller.commit(commit_message)


def _merge_ingest_results(results: "list[IngestResult]") -> "IngestResult":
    """複数の IngestResult を 1 件に集約する.

    bulk 系 CLI handler が 1 件ずつ ingester を呼んだ結果をまとめる用途。
    集約規則:
    - counters (placed / skipped / overwritten / errors / partial_failures): 単純加算
    - details (error_details / partial_failure_details): リスト連結（順序保持）
    - aborted: OR 集約（いずれか True なら True）
    - abort_reason: **最初に aborted=True になった IngestResult の abort_reason を採用**
      （後続の aborted=True は無視）。bulk 処理は最初のサーキットブレーカー発動が
      もっとも有意な情報のため
    """
    from .pipeline.ingesters._common import IngestResult as _IngestResult

    merged = _IngestResult()
    for r in results:
        merged.placed += r.placed
        merged.skipped += r.skipped
        merged.overwritten += r.overwritten
        merged.errors += r.errors
        merged.error_details.extend(r.error_details)
        merged.partial_failures += r.partial_failures
        merged.partial_failure_details.extend(r.partial_failure_details)
        if r.aborted and not merged.aborted:
            merged.aborted = True
            merged.abort_reason = r.abort_reason
    return merged


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


def _build_parser() -> "_JsonAwareArgumentParser":
    """CLI 用のサブコマンド定義済み ArgumentParser を返す.

    main() から argparse 構築部分を切り出した関数。
    テストから直接 parse_args するために公開する。
    """
    # reduce-pdf の既定値は削減モジュールが SSoT（help 表示にも使う）
    from .converter.pdf_media_reducer import (
        DEFAULT_DPI_TARGET as PDF_DEFAULT_DPI_TARGET,
    )
    from .converter.pdf_media_reducer import (
        DEFAULT_DPI_THRESHOLD as PDF_DEFAULT_DPI_THRESHOLD,
    )
    from .converter.pdf_media_reducer import (
        DEFAULT_JPEG_QUALITY as PDF_DEFAULT_JPEG_QUALITY,
    )

    parser = _JsonAwareArgumentParser(description="RAG Knowledge CLI")
    # サブパーサーにも _JsonAwareArgumentParser を使わせる。
    # argparse のデフォルトは ArgumentParser 固定で、親クラスを継承しない。
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=_JsonAwareArgumentParser,
    )

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
        help="対象媒体フィルタ（incremental では指定不可、--path と排他）",
    )
    rebuild_parser.add_argument(
        "--path",
        default=None,
        help=(
            "対象パスフィルタ。source_store ルート相対のディレクトリパスを指定し、"
            "配下（再帰的）のソースのみを対象にする。"
            "空文字列・絶対パス・`..`/`.` を含むパスはバリデーションエラー。"
            "incremental では指定不可、--source-type と排他"
        ),
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
        help=(
            "source_store のデータ補正を実行する"
            "（現在: journal の collected_at JST→UTC, Issue #795）"
        ),
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
    list_recent_parser.add_argument(
        "--filters",
        default=None,
        help="メタデータフィルタ（key=value 形式、例: 'repository=rag-knowledge'）",
    )
    _add_output_option(list_recent_parser)

    # list-by-date-range サブコマンド
    list_by_date_range_parser = subparsers.add_parser(
        "list-by-date-range",
        help="指定日付範囲（published_at, JST 解釈, 両端 inclusive）でソースを取得",
    )
    list_by_date_range_parser.add_argument(
        "--date-from",
        required=True,
        help="開始日（YYYY-MM-DD、JST 起点で inclusive）",
    )
    list_by_date_range_parser.add_argument(
        "--date-to",
        required=True,
        help="終了日（YYYY-MM-DD、JST 起点で inclusive）",
    )
    list_by_date_range_parser.add_argument(
        "--source-type",
        default=None,
        choices=["web", "bluesky", "zenn", "youtube", "aozora", "local", "journal"],
        help="ソース種別（任意、未指定で全種別横断）",
    )
    list_by_date_range_parser.add_argument(
        "--limit",
        type=_validate_list_recent_limit,
        default=None,
        help="取得件数（1〜100、未指定時は設定値を使用）",
    )
    list_by_date_range_parser.add_argument(
        "--order",
        choices=["asc", "desc"],
        default="desc",
        help="ソート順（asc: 古い順, desc: 新しい順。デフォルト: desc）",
    )
    list_by_date_range_parser.add_argument(
        "--filters",
        default=None,
        help="メタデータフィルタ（key=value 形式、例: 'repository=rag-knowledge'）",
    )
    _add_output_option(list_by_date_range_parser)

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
    delete_parser.add_argument("source_id", nargs="+", help="削除するソース識別子（source_id、1 件以上）")
    _add_skip_pipeline_option(delete_parser)
    _add_output_option(delete_parser)

    # --- インジェスト系サブコマンド ---

    # ingest-youtube: YouTube 単一動画取り込み
    yt_parser = subparsers.add_parser("ingest-youtube", help="YouTube 動画を取り込み")
    yt_parser.add_argument("video_url", nargs="+", help="YouTube 動画 URL（1 件以上）")
    _add_skip_pipeline_option(yt_parser)
    _add_output_option(yt_parser)

    # ingest-youtube-playlist: YouTube プレイリスト一括取り込み
    ytpl_parser = subparsers.add_parser("ingest-youtube-playlist", help="YouTube プレイリストを一括取り込み")
    ytpl_parser.add_argument("playlist_url", help="YouTube プレイリスト URL")
    ytpl_parser.add_argument("--max-videos", type=int, default=None, help="取得する最大動画数")
    _add_skip_pipeline_option(ytpl_parser)
    _add_output_option(ytpl_parser)

    # crawl-bluesky: BlueSky 取り込み
    bs_parser = subparsers.add_parser("crawl-bluesky", help="BlueSky 投稿を一括取り込み")
    bs_parser.add_argument("handle", help="BlueSky ハンドル（例: user.bsky.social）")
    bs_parser.add_argument("--max-posts", type=int, default=None, help="取得する最大投稿数")
    # BooleanOptionalAction で --include-reposts / --no-include-reposts の双方を受け付ける。
    # default=None は「指定なし → 設定値 (rag_bluesky_include_reposts) を使用」のセマンティクス。
    bs_parser.add_argument(
        "--include-reposts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="リポストを含めるか（--no-include-reposts で除外）",
    )
    bs_parser.add_argument("--force", action="store_true", default=False, help="上書き再取得モード（既存ファイルを上書き + メディア再DL）")
    _add_skip_pipeline_option(bs_parser)
    _add_output_option(bs_parser)

    # ingest-bluesky: BlueSky 単一投稿取り込み
    rbs_parser = subparsers.add_parser("ingest-bluesky", help="BlueSky 投稿を URL 指定で取り込み")
    rbs_parser.add_argument("url", nargs="+", help="BlueSky 投稿の URL（1 件以上）")
    _add_skip_pipeline_option(rbs_parser)
    _add_output_option(rbs_parser)

    # crawl-zenn: Zenn 取り込み
    zenn_parser = subparsers.add_parser("crawl-zenn", help="Zenn コンテンツを一括取り込み")
    zenn_parser.add_argument("username", help="Zenn ユーザー名")
    zenn_parser.add_argument("--max-articles", type=int, default=None, help="取得する最大コンテンツ数")
    zenn_parser.add_argument("--content-type", choices=["articles", "scraps", "all"], default="all", help="取得対象")
    zenn_parser.add_argument("--force", action="store_true", default=False, help="既存ファイルを上書きする（デフォルト: スキップ）")
    _add_skip_pipeline_option(zenn_parser)
    _add_output_option(zenn_parser)

    # ingest-zenn: Zenn 単一コンテンツ取り込み
    rzenn_parser = subparsers.add_parser("ingest-zenn", help="Zenn コンテンツを URL 指定で取り込み")
    rzenn_parser.add_argument("url", nargs="+", help="Zenn コンテンツの URL（1 件以上）")
    _add_skip_pipeline_option(rzenn_parser)
    _add_output_option(rzenn_parser)

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
    _add_skip_pipeline_option(crawldoc_parser)
    _add_output_option(crawldoc_parser)

    # reduce-pptx: pptx/ppsx の埋め込みメディア除去コピー生成（配置前の事前処理）
    reduce_pptx_parser = subparsers.add_parser(
        "reduce-pptx",
        help="pptx/ppsx の埋め込みメディアを除去した軽量コピーを生成（source_store 配置前の事前処理）",
    )
    reduce_pptx_parser.add_argument(
        "paths",
        nargs="+",
        help="対象ファイルまたはディレクトリのパス（1 件以上。ディレクトリは再帰走査）",
    )
    reduce_pptx_parser.add_argument(
        "--output-dir",
        default=None,
        help="削減コピーの出力先ディレクトリ（--alongside と排他。存在しない場合は作成）",
    )
    reduce_pptx_parser.add_argument(
        "--alongside",
        action="store_true",
        default=False,
        help="原本と同じフォルダに <元名>.reduced.<拡張子> で削減コピーを出力する（--output-dir と排他）",
    )
    reduce_pptx_parser.add_argument(
        "--report-only",
        action="store_true",
        default=False,
        help="削減対象パート占有量レポートの表示のみで削減コピーを生成しない",
    )

    # reduce-pdf: PDF の高解像度画像・埋め込みメディア削減（配置前の事前処理）
    reduce_pdf_parser = subparsers.add_parser(
        "reduce-pdf",
        help="PDF の高解像度画像を再圧縮し埋め込みメディアを除去した軽量コピーを生成（source_store 配置前の事前処理）",
    )
    reduce_pdf_parser.add_argument(
        "paths",
        nargs="+",
        help="対象ファイルまたはディレクトリのパス（1 件以上。ディレクトリは再帰走査）",
    )
    reduce_pdf_parser.add_argument(
        "--output-dir",
        default=None,
        help="削減コピーの出力先ディレクトリ（--alongside と排他。存在しない場合は作成）",
    )
    reduce_pdf_parser.add_argument(
        "--alongside",
        action="store_true",
        default=False,
        help="原本と同じフォルダに <元名>.reduced.pdf で削減コピーを出力する（--output-dir と排他）",
    )
    reduce_pdf_parser.add_argument(
        "--report-only",
        action="store_true",
        default=False,
        help="削減対象の占有量レポートの表示のみで削減コピーを生成しない",
    )
    reduce_pdf_parser.add_argument(
        "--dpi-threshold",
        type=int,
        default=PDF_DEFAULT_DPI_THRESHOLD,
        help=f"この実効 DPI を超える画像を再圧縮対象とする（既定: {PDF_DEFAULT_DPI_THRESHOLD}）",
    )
    reduce_pdf_parser.add_argument(
        "--dpi-target",
        type=int,
        default=PDF_DEFAULT_DPI_TARGET,
        help=f"再圧縮後の目標 DPI（既定: {PDF_DEFAULT_DPI_TARGET}）",
    )
    reduce_pdf_parser.add_argument(
        "--quality",
        type=int,
        default=PDF_DEFAULT_JPEG_QUALITY,
        help=f"再圧縮時の JPEG 品質（既定: {PDF_DEFAULT_JPEG_QUALITY}）",
    )
    reduce_pdf_parser.add_argument(
        "--include-scanned",
        action="store_true",
        default=False,
        help="テキスト層の乏しい PDF（スキャン文書等）も削減対象に含める（既定では除外する）",
    )

    # site-ingest: 指定 URL のページ取得（リンク辿りなし、複数 URL OK）
    siteingest_parser = subparsers.add_parser(
        "site-ingest",
        help="指定 URL の Web ページを取得（リンク辿りなし、複数 URL 可）",
    )
    siteingest_parser.add_argument(
        "url",
        nargs="+",
        help="取得対象 URL（1 件以上）。リンク辿りは行わない",
    )
    _add_skip_pipeline_option(siteingest_parser)
    _add_output_option(siteingest_parser)

    # site-crawl: Scrapy による単一 URL 起点のサイトクロール（リンク辿りあり）
    sitecrawl_parser = subparsers.add_parser(
        "site-crawl",
        help="単一 URL を起点に Scrapy でサイトをクロール（リンク辿りあり）",
    )
    sitecrawl_parser.add_argument("url", help="クロール開始 URL（単一）")
    sitecrawl_parser.add_argument(
        "--url-pattern", default="", help="URL フィルタパターン（正規表現）",
    )
    sitecrawl_parser.add_argument(
        "--max-pages", type=int, default=None, help="ページ数上限",
    )
    sitecrawl_parser.add_argument(
        "--restart",
        action="store_true",
        help="JOBDIR + 一時 HTML/JSONL を削除して最初から再クロール",
    )
    _add_skip_pipeline_option(sitecrawl_parser)
    _add_output_option(sitecrawl_parser)

    # update-aozora-catalog: 青空文庫カタログ更新
    update_aozora_parser = subparsers.add_parser("update-aozora-catalog", help="青空文庫カタログを更新")
    _add_output_option(update_aozora_parser)

    # search-aozora: 青空文庫カタログ検索
    search_aozora_parser = subparsers.add_parser("search-aozora", help="青空文庫カタログを検索")
    search_aozora_parser.add_argument("--author", default=None, help="著者名（部分一致）")
    search_aozora_parser.add_argument("--title", default=None, help="作品タイトル（部分一致）")
    search_aozora_parser.add_argument("--limit", type=int, default=20, help="最大表示件数（デフォルト: 20）")
    _add_output_option(search_aozora_parser)

    # ingest-aozora: 青空文庫作品取り込み（バルク対応）
    ingest_aozora_parser = subparsers.add_parser("ingest-aozora", help="青空文庫の作品を取り込み")
    ingest_aozora_parser.add_argument("book_id", nargs="+", help="青空文庫の作品 ID（1 件以上）")
    _add_skip_pipeline_option(ingest_aozora_parser)
    _add_output_option(ingest_aozora_parser)

    # ingest-aozora-author: 青空文庫著者一括取り込み
    ingest_aozora_author_parser = subparsers.add_parser("ingest-aozora-author", help="青空文庫の著者作品を一括取り込み")
    ingest_aozora_author_parser.add_argument("person_id", help="著者の人物 ID（search-aozora で確認）")
    ingest_aozora_author_parser.add_argument("--max-works", type=int, default=None, help="取得する最大作品数")
    _add_skip_pipeline_option(ingest_aozora_author_parser)
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

    return parser


def main() -> None:
    """CLIエントリポイント."""
    parser = _build_parser()
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
        "ingest-bluesky": run_ingest_bluesky,
        "crawl-zenn": run_crawl_zenn,
        "ingest-zenn": run_ingest_zenn,
        "add-document": run_add_document,
        "crawl-documents": run_crawl_documents,
        "site-ingest": run_site_ingest,
        "site-crawl": run_site_crawl,
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
        "list-by-date-range": run_list_by_date_range,
        "search": run_search,
        "search-aozora": run_search_aozora,
        "migrate-journal": run_migrate_journal,
        "migrate": run_migrate,
        "generate-api-key": run_generate_api_key,
        "reduce-pptx": run_reduce_pptx,
        "reduce-pdf": run_reduce_pdf,
    }

    if args.command in _ASYNC_COMMANDS:
        asyncio.run(_ASYNC_COMMANDS[args.command](args))  # type: ignore[operator]
    elif args.command in _SYNC_COMMANDS:
        _SYNC_COMMANDS[args.command](args)  # type: ignore[operator]


async def _ingest_page_for_testing(
    *,
    vector_store: "VectorStore",
    url: str,
    title: str,
    text: str,
    crawled_at: str,
    chunk_size: int,
    chunk_overlap: int,
) -> int:
    """評価フィクスチャ投入専用のチャンキング + ChromaDB 投入.

    `init-test-db` CLI コマンド専用。本番取り込みパス（インジェスター →
    コンバーター → インデクサー）とは別系統。Issue #739 で運用判断中。

    Args:
        vector_store: 投入先の VectorStore
        url: ページ URL
        title: ページタイトル
        text: ページ本文テキスト
        crawled_at: 取得日時（ISO 8601 形式）
        chunk_size: チャンクの最大文字数
        chunk_overlap: チャンク間のオーバーラップ文字数

    Returns:
        保存されたチャンク数
    """
    from .indexer.smart_chunking import smart_chunk
    from .vector_store import DocumentChunk

    chunks = smart_chunk(text, chunk_size, chunk_overlap)
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

    count = await vector_store.add_documents(document_chunks)
    await vector_store.delete_stale_chunks(normalized_url, new_ids)
    logger.info("Ingested page %s: %d chunks", normalized_url, count)
    return count


async def create_search_adapter(
    *,
    persist_dir: str,
    threshold: float | None,
    bm25_index: "BM25Index | None",
    vector_weight: float,
    min_combined_score: float | None,
) -> "RealSearchAdapter":
    """SearchPort 実装（RealSearchAdapter）を生成する.

    全パラメータは呼び出し元が明示的に指定する。settings へのフォールバックは行わない。

    Args:
        persist_dir: ChromaDB 永続化ディレクトリ
        threshold: 類似度閾値（None の場合はフィルタリングなし）
        bm25_index: BM25 インデックス（指定時はハイブリッド検索を有効化）
        vector_weight: ベクトル検索の重み α
        min_combined_score: combined_score の下限閾値（None=フィルタなし）

    Returns:
        RealSearchAdapter インスタンス
    """
    from .config import get_settings
    from .embedding.factory import get_embedding_provider
    from .search.search_port import RealSearchAdapter
    from .vector_store import VectorStore

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

    return RealSearchAdapter(
        vector_store=vector_store,
        bm25_index=bm25_index,
        similarity_threshold=threshold,
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
    from .indexer.smart_chunking import smart_chunk

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
        for i, (chunk_text, _section_path) in enumerate(chunks):
            documents.append((f"{url_hash}_{i}", chunk_text, normalized_url, "web"))

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

    # SearchPort 初期化（BM25込みでハイブリッド検索を有効化）
    min_combined_score: float | None = args.min_combined_score
    search = await create_search_adapter(
        threshold=args.threshold,
        persist_dir=args.persist_dir,
        bm25_index=bm25_index,
        vector_weight=args.vector_weight,
        min_combined_score=min_combined_score,
    )

    # 評価実行
    report = await evaluate_retrieval(
        search=search,
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

    # 評価フィクスチャ投入（init-test-db 専用ロジック、Issue #739 で運用判断中）
    # 本番取り込みパスとは独立した経路として CLI 内に閉じる
    from .config import get_settings
    from .embedding.factory import get_embedding_provider
    from .vector_store import VectorStore

    settings = get_settings()
    embedding_provider = get_embedding_provider(settings, settings.embedding_provider)
    vector_store = VectorStore(
        embedding_provider=embedding_provider,
        persist_directory=args.persist_dir,
        collection_name=settings.chromadb_collection_name,
        hnsw_m=settings.hnsw_m,
        hnsw_construction_ef=settings.hnsw_construction_ef,
        hnsw_search_ef=settings.hnsw_search_ef,
    )
    total = 0
    for page in pages:
        count = await _ingest_page_for_testing(
            vector_store=vector_store,
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
            **page,
        )
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
    from .admin.formatting import format_document_response
    from .admin.source_management_port import get_document
    from .config import get_settings

    json_out = _is_json_output(args)
    settings = get_settings()

    result = get_document(
        source_id=args.source_id,
        format_type=args.format,
        source_store_dir=settings.source_store_dir,
        converted_store_dir=settings.converted_store_dir,
    )

    if result.error:
        if json_out:
            _output_error(CliErrorCode.NOT_FOUND, result.error)
        else:
            print(f"エラー: {result.error}", file=sys.stderr)
            sys.exit(1)

    if json_out:
        _output_result_logged(
            {
                "source_id": result.source_id,
                "title": result.title,
                "source_type": result.source_type,
                "format": result.format,
                "content": result.content,
                "is_binary": result.is_binary,
                "collected_at": result.collected_at,
                "extra": result.extra,
            },
            "get-document: source_id=%s, format=%s, size=%d",
            result.source_id,
            result.format,
            len(result.content) if result.content else 0,
        )
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

    from .store.path_filter import PathFilterError, normalize_path_prefix

    mode: str = args.mode
    source_type: SourceType | None = args.source_type
    path: str | None = args.path
    if_needed: bool = args.if_needed

    # path 指定時は早期に正規化・バリデーション（空文字列・パストラバーサル拒否）
    if path is not None:
        try:
            path = normalize_path_prefix(path)
        except PathFilterError as e:
            msg = f"--path が不正です: {e}"
            if json_out:
                _output_error(CliErrorCode.VALIDATION_ERROR, msg)
            logger.error(msg)
            sys.exit(1)

    # incremental + source_type / path のバリデーション
    if mode == "incremental" and source_type is not None:
        if json_out:
            _output_error(CliErrorCode.VALIDATION_ERROR, "incremental モードでは source_type を指定できません")
        logger.error(
            "incremental モードでは source_type を指定できません"
        )
        sys.exit(1)
    if mode == "incremental" and path is not None:
        msg = "incremental モードでは path を指定できません"
        if json_out:
            _output_error(CliErrorCode.VALIDATION_ERROR, msg)
        logger.error(msg)
        sys.exit(1)
    if source_type is not None and path is not None:
        msg = "--source-type と --path は同時に指定できません"
        if json_out:
            _output_error(CliErrorCode.VALIDATION_ERROR, msg)
        logger.error(msg)
        sys.exit(1)

    # --if-needed は index/full のみ有効
    if if_needed and mode not in ("index", "full"):
        msg = "--if-needed は --mode index または --mode full でのみ使用できます"
        if json_out:
            _output_error(CliErrorCode.VALIDATION_ERROR, msg)
        logger.error(msg)
        sys.exit(1)

    # --if-needed と filter（--source-type / --path）の併用は禁止
    # Why: filter 付き rebuild は pipeline_history の filter 列に記録され、
    # needs_index_rebuild() の判定対象から除外される（subset しか触っていない
    # ため「全体 rebuild 完了」と誤認するとスキップ漏れが発生する）。
    # filter 付きで --if-needed を実行すると「filter スコープが古ければ
    # rebuild される」と誤期待されやすいため明示的に禁止する。
    # 詳細は docs/specs/infrastructure/scheduled-rebuild.md を参照
    if if_needed and (source_type is not None or path is not None):
        msg = (
            "--if-needed は --source-type / --path と併用できません"
            "（filter 付き rebuild は --if-needed の判定対象外）"
        )
        if json_out:
            _output_error(CliErrorCode.VALIDATION_ERROR, msg)
        logger.error(msg)
        sys.exit(1)

    settings = get_settings()

    if not settings.source_store_dir:
        if json_out:
            _output_error(CliErrorCode.CONFIG_MISSING, "SOURCE_STORE_DIR が設定されていません")
        logger.error("SOURCE_STORE_DIR が設定されていません")
        sys.exit(1)
    if not settings.converted_store_dir:
        if json_out:
            _output_error(CliErrorCode.CONFIG_MISSING, "CONVERTED_STORE_DIR が設定されていません")
        logger.error("CONVERTED_STORE_DIR が設定されていません")
        sys.exit(1)

    source_store_dir = Path(settings.source_store_dir)
    if not source_store_dir.exists():
        msg = f"source_store ディレクトリが存在しません: {source_store_dir}"
        if json_out:
            _output_error(CliErrorCode.CONFIG_MISSING, msg)
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
    except LockAcquisitionError as e:
        if e.kind == "rebuild":
            msg = "別の再構築が実行中です（ロック競合）"
        else:
            msg = "別の取り込みが実行中です（ロック競合）"
        if json_out:
            _output_error(
                CliErrorCode.LOCK_CONFLICT, msg, lock_type=e.kind,
            )
        else:
            print(f"エラー: {msg}", file=sys.stderr)
            if if_needed:
                _show_error_dialog(msg)
        raise SystemExit(1) from e

    has_error = False
    try:
        logger.info("再構築を開始します（モード: %s）", mode)
        if source_type:
            logger.info("対象媒体: %s", source_type)
        if path:
            logger.info("対象パス: %s", path)

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
                _output_error(CliErrorCode.VALIDATION_ERROR, msg)
            logger.error(msg)
            sys.exit(1)

        if mode == "full":
            full_result = await controller.run_full_rebuild(
                source_type=source_type,
                path=path,
                progress_callback=progress_cb,
                concurrency=concurrency,
            )
        elif mode == "convert":
            summary = await controller.run_convert_only(
                source_type=source_type,
                path=path,
                progress_callback=progress_cb,
                concurrency=concurrency,
            )
        elif mode == "index":
            summary = await controller.run_index_only(
                source_type=source_type,
                path=path,
                progress_callback=progress_cb,
                concurrency=concurrency,
            )
        else:
            summary = await controller.run_incremental(
                progress_callback=progress_cb,
                concurrency=concurrency,
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
                        error_files = ", ".join(
                            _format_pipeline_error(e) for e in all_errors
                        )
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
                        error_files = ", ".join(
                            _format_pipeline_error(e) for e in summary.errors
                        )
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
                    logger.warning("  警告: %s", _format_pipeline_warning(warn))
            if summary.errors:
                has_error = True
                for entry in summary.errors:
                    logger.error("  エラー: %s", _format_pipeline_error(entry))
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
            error_files = ", ".join(_format_pipeline_error(e) for e in all_err)
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
    # 列挙は SourceStore.list_files() 経由とし、is_source_file 判定で
    # sidecar・ロックファイル・attachment 等を一元的に除外する
    # （仕様: pipeline-controller.md「ソース列挙経路」）
    source_store_data: dict[str, object] = {}
    if not settings.source_store_dir:
        source_store_data["status"] = "unconfigured"
    else:
        source_store_dir = Path(settings.source_store_dir)
        if not source_store_dir.exists():
            source_store_data["status"] = "not_found"
        else:
            from .store.source_store import SourceStore, detect_source_type

            store = SourceStore(source_store_dir)
            try:
                rel_paths = store.list_files()
            finally:
                store.close()

            total_files = 0
            total_size = 0
            by_type: dict[str, dict[str, int]] = {}
            for rel in rel_paths:
                full = source_store_dir / rel
                try:
                    size = full.stat().st_size
                except OSError:
                    continue
                total_files += 1
                total_size += size
                # invariant: is_source_file == True のファイルのみが列挙されるため
                # detect_source_type は必ず SourceType を返す
                st = detect_source_type(rel.as_posix())
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
    index_data: dict[str, object] = {"source_count": 0}
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
                deleted_count = db.source_count(status=SourceStatus.DELETED)
                active_count = db.source_count(status=SourceStatus.ACTIVE)
                index_data["source_count"] = active_count
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
        _output_result_logged(
            stats_data,
            "stats: chunks=%s, sources=%s",
            index_data.get("total_chunks", "N/A"),
            index_data.get("source_count", "N/A"),
        )
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

    # filters パラメータのパース
    parsed_filters: dict[str, str] | None = None
    if args.filters is not None:
        try:
            parsed_filters = parse_filters(args.filters)
        except ValueError as e:
            if json_out:
                _output_error(CliErrorCode.VALIDATION_ERROR, str(e))
            else:
                logger.error("エラー: %s", e)
                sys.exit(1)

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
            try:
                sources = db.list_sources(source_type=st, limit=limit, ascending=ascending, filters=parsed_filters)
                total = db.count_sources_by_type(source_type=st, filters=parsed_filters)
            except ValueError as e:
                _output_error(CliErrorCode.VALIDATION_ERROR, str(e))
                return
        finally:
            db.close()
        for i, s in enumerate(sources, 1):
            logger.info(
                "list-recent result %d: source_id=%s, title=%r",
                i,
                s.source_id,
                s.title,
            )
        _output_result_logged(
            {
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
            },
            "list-recent: source_type=%s, total=%d, returned=%d",
            args.source_type,
            total,
            len(sources),
        )
    else:
        from .admin.stats_port import list_recent_sources
        print(list_recent_sources(
            settings.source_store_dir, args.source_type, limit, ascending=ascending,
            filters=parsed_filters,
        ))


def run_list_by_date_range(args: argparse.Namespace) -> None:
    """published_at の日付範囲でソースを一覧取得する.

    MCP ツール rag_list_by_date_range と同等の一覧取得を CLI で実行する。
    """
    from .admin.stats_port import to_jst_range_iso
    from .config import get_settings

    json_out = _is_json_output(args)
    settings = get_settings()
    limit: int = args.limit if args.limit is not None else settings.rag_list_recent_limit
    ascending = args.order == "asc"

    parsed_filters: dict[str, str] | None = None
    if args.filters is not None:
        try:
            parsed_filters = parse_filters(args.filters)
        except ValueError as e:
            if json_out:
                _output_error(CliErrorCode.VALIDATION_ERROR, str(e))
                return
            logger.error("エラー: %s", e)
            sys.exit(1)

    try:
        date_from_iso, date_to_iso = to_jst_range_iso(args.date_from, args.date_to)
    except ValueError as e:
        if json_out:
            _output_error(CliErrorCode.VALIDATION_ERROR, str(e))
            return
        logger.error("エラー: %s", e)
        sys.exit(1)

    source_type = args.source_type  # None で全種別

    if json_out:
        from .store.metadata_db import MetadataDB
        from .store.models import SourceType

        if not settings.source_store_dir:
            _output_result({
                "date_from": args.date_from,
                "date_to": args.date_to,
                "source_type": source_type,
                "sources": [],
                "count": 0,
                "total": 0,
                "order": args.order,
            })
            return
        db_path = Path(settings.source_store_dir) / "metadata.db"
        if not db_path.exists():
            _output_result({
                "date_from": args.date_from,
                "date_to": args.date_to,
                "source_type": source_type,
                "sources": [],
                "count": 0,
                "total": 0,
                "order": args.order,
            })
            return
        db = MetadataDB(db_path)
        try:
            db.initialize()
            st = cast(SourceType, source_type) if source_type else None
            try:
                sources = db.list_sources_by_date_range(
                    date_from_iso=date_from_iso,
                    date_to_iso=date_to_iso,
                    source_type=st,
                    limit=limit,
                    ascending=ascending,
                    filters=parsed_filters,
                )
                total = db.count_sources_by_date_range(
                    date_from_iso=date_from_iso,
                    date_to_iso=date_to_iso,
                    source_type=st,
                    filters=parsed_filters,
                )
            except ValueError as e:
                _output_error(CliErrorCode.VALIDATION_ERROR, str(e))
                return
        finally:
            db.close()
        for i, s in enumerate(sources, 1):
            logger.info(
                "list-by-date-range result %d: source_id=%s, title=%r",
                i, s.source_id, s.title,
            )
        _output_result_logged(
            {
                "date_from": args.date_from,
                "date_to": args.date_to,
                "source_type": source_type,
                "sources": [
                    {
                        "source_id": s.source_id,
                        "source_type": s.source_type,
                        "title": s.title,
                        "published_at": s.published_at,
                        "file_size": s.file_size,
                    }
                    for s in sources
                ],
                "count": len(sources),
                "total": total,
                "order": args.order,
            },
            "list-by-date-range: date_from=%s, date_to=%s, source_type=%s,"
            " total=%d, returned=%d",
            args.date_from,
            args.date_to,
            source_type or "all",
            total,
            len(sources),
        )
    else:
        from .admin.stats_port import list_sources_by_date_range
        if not settings.source_store_dir:
            print(
                f"date_range: {args.date_from}〜{args.date_to}"
                f"（{source_type or 'all'}, 0件 / 全0件）"
            )
            return
        print(list_sources_by_date_range(
            settings.source_store_dir,
            args.date_from,
            args.date_to,
            source_type,
            limit,
            ascending=ascending,
            filters=parsed_filters,
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
    from .config import RAGSettings, get_settings
    from .embedding.factory import get_embedding_provider
    from .search.search_port import RealSearchAdapter
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

    search = RealSearchAdapter(
        vector_store=vector_store,
        bm25_index=bm25_index,
        similarity_threshold=None,
        hybrid_search_enabled=True,
        vector_weight=settings.rag_vector_weight,
        min_combined_score=settings.rag_min_combined_score,
        debug_log_enabled=settings.rag_debug_log_enabled,
    )

    n_results = args.n_results if args.n_results is not None else settings.rag_retrieval_count
    source_type: str | None = args.source_type

    # n_results 推奨範囲チェック（pydantic Field le= と同期）
    n_results_max = next(
        (
            m.le for m in RAGSettings.model_fields["rag_retrieval_count"].metadata
            if hasattr(m, "le")
        ),
        None,
    )
    warnings: list[str] = []
    if n_results_max is not None and n_results > n_results_max:
        msg = f"n_results={n_results} は設定上限（{n_results_max}）を超えています。パフォーマンスに影響する可能性があります。"
        warnings.append(msg)
        logger.warning(msg)

    # filters パラメータのパース
    parsed_filters: dict[str, str] | None = None
    if args.filters is not None:
        try:
            parsed_filters = parse_filters(args.filters)
        except ValueError as e:
            if json_out:
                _output_error(CliErrorCode.VALIDATION_ERROR, str(e))
            else:
                logger.error("エラー: %s", e)
                sys.exit(1)

    raw = _asyncio.run(
        search.retrieve_raw_results(
            query=args.query,
            n_results=n_results,
            source_type=source_type,
            filters=parsed_filters,
        ),
    )

    if json_out:
        result_data: dict[str, object] = {
            "query": args.query,
            "vector_results": [r.to_dict() for r in raw.vector_results],
            "bm25_results": [r.to_dict() for r in raw.bm25_results],
        }
        if warnings:
            result_data["warnings"] = warnings
        _output_result(result_data)
    else:
        from .search.formatting import format_raw_search_results
        print(format_raw_search_results(raw))


async def run_delete(args: argparse.Namespace) -> None:
    """ソースをナレッジベースから削除する（1 件以上を逐次削除）.

    MCP ツール rag_delete と同等の削除を CLI で実行する。
    ファイルを物理削除し、パイプライン経由でインデックス・metadata.db を更新する。
    `--skip-pipeline` 指定時は pipeline をスキップし git commit のみ実行する。

    Args:
        args: コマンドライン引数
    """
    json_out = _is_json_output(args)
    progress_cb = _output_progress if json_out else None
    skip_pipeline = _is_skip_pipeline(args)

    controller, settings = _build_cli_pipeline_controller()
    source_ids: list[str] = list(args.source_id)

    with _write_lock_or_exit(
        Path(controller.source_store.root_dir), json_out=json_out,
    ):
        deleted: list[str] = []
        not_found: list[str] = []
        for sid in source_ids:
            try:
                controller.source_store.remove_file(sid)
                deleted.append(sid)
            except KeyError:
                not_found.append(sid)

        if not deleted:
            # 全件 not_found → エラー終了 (既存挙動と整合)
            if json_out:
                _output_result_logged(
                    {"deleted": False, "not_found_ids": not_found},
                    "delete: not_found_count=%d",
                    len(not_found),
                )
                return
            print(
                f"該当するソースが見つかりませんでした: {', '.join(not_found)}",
                file=sys.stderr,
            )
            sys.exit(1)

        commit_message = (
            f"delete: {deleted[0]}"
            if len(deleted) == 1
            else f"delete: {len(deleted)} 件"
        )
        summary = None
        if skip_pipeline:
            controller.commit(commit_message)
        else:
            try:
                summary = await controller.ingest_and_index(
                    commit_message,
                    progress_callback=progress_cb,
                    concurrency=settings.rag_embedding_concurrency,
                )
            except Exception:
                logger.exception("削除パイプライン実行に失敗: %s", commit_message)
                if json_out:
                    _output_error(CliErrorCode.INTERNAL_ERROR, f"削除に失敗しました: {commit_message}")
                print(
                    f"エラー: 削除に失敗しました: {commit_message}",
                    file=sys.stderr,
                )
                sys.exit(1)

        if json_out:
            data: dict[str, object] = {
                "deleted": True,
                "deleted_ids": deleted,
                "deleted_count": len(deleted),
                "not_found_ids": not_found,
            }
            # 共通仕様: --skip-pipeline 時は pipeline フィールドを省略する
            # （docs/specs/ingesters/common.md「stdout 案内文」セクション）
            if summary is not None:
                data["pipeline"] = _summary_to_dict(summary)
            _output_result_logged(
                data,
                "delete: deleted_count=%d, not_found_count=%d",
                len(deleted),
                len(not_found),
            )
            return

        if summary is not None:
            if summary.warnings:
                print(f"パイプライン警告: {len(summary.warnings)}件")
                for warn in summary.warnings:
                    print(f"  - {_format_pipeline_warning(warn)}")
            if summary.errors:
                print(f"パイプラインエラー: {len(summary.errors)}件")
                for entry in summary.errors:
                    print(f"  - {_format_pipeline_error(entry)}")
        if not_found:
            print(f"見つからなかったソース: {len(not_found)}件")
            for sid in not_found:
                print(f"  - {sid}")
        # 単一削除時は source_id を併記（運用時のログ追跡性のため）
        if len(deleted) == 1:
            print(f"削除しました: {deleted[0]}")
        else:
            print(f"削除しました: {len(deleted)}件")
            for sid in deleted:
                print(f"  - {sid}")
        if skip_pipeline:
            _print_skip_pipeline_notice(json_out)


async def run_add_journal(args: argparse.Namespace) -> None:
    """単一ジャーナルエントリを登録する.

    Args:
        args: コマンドライン引数（--title, --file/--stdin, --repository, --entry-id）
    """
    from .pipeline.ingesters.journal import JournalIngester

    json_out = _is_json_output(args)
    progress_cb = _output_progress if json_out else None

    controller, settings = _build_cli_pipeline_controller()

    # コンテンツの取得: --stdin または --file
    if getattr(args, "stdin", False):
        body = sys.stdin.read()
        if not body:
            if json_out:
                _output_error(CliErrorCode.VALIDATION_ERROR, "stdin からの入力が空です")
            print("エラー: stdin からの入力が空です", file=sys.stderr)
            raise SystemExit(1)
    else:
        file_path = Path(args.file)
        if not file_path.is_file():
            if json_out:
                _output_error(CliErrorCode.NOT_FOUND, f"ファイルが見つかりません: {file_path}")
            print(f"エラー: ファイルが見つかりません: {file_path}", file=sys.stderr)
            raise SystemExit(1)
        body = file_path.read_text(encoding="utf-8")

    with _write_lock_or_exit(
        Path(controller.source_store.root_dir), json_out=json_out,
    ):
        ingester = JournalIngester(controller.source_store)

        ingest_result = ingester.add_entry(
            title=args.title,
            body=body,
            repository=args.repository,
            entry_id=args.entry_id,
        )

        if ingest_result.errors > 0:
            first_detail = _error_detail_message(ingest_result.error_details[0])
            if json_out:
                _output_error(CliErrorCode.INTERNAL_ERROR, first_detail)
            print(f"エラー: {first_detail}", file=sys.stderr)
            raise SystemExit(1)

        pipeline_summary = await controller.ingest_and_index(
            f"ingest(journal): add {args.title}",
            progress_callback=progress_cb,
            concurrency=settings.rag_embedding_concurrency,
        )
        _print_ingest_result(
            ingest_result, pipeline_summary, context=f"journal/{args.repository}",
            json_output=json_out,
            entry_id=ingester.last_entry_id,
        )


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
        applied = db.migrate(source_store_dir=Path(source_store_dir))
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
    from .admin.formatting import format_file_size

    return format_file_size(size_bytes)


def _collect_pptx_targets(raw_paths: list[str]) -> tuple[list[Path], int]:
    """reduce-pptx の対象ファイルを収集する.

    Args:
        raw_paths: CLI で指定されたパス（ファイルまたはディレクトリ）

    Returns:
        (対象ファイルのリスト, パス解決エラー数)
    """
    from .converter.media_reduction import collect_reduce_targets
    from .converter.pptx_extractor import PPTX_EXTENSIONS

    return collect_reduce_targets(
        raw_paths,
        PPTX_EXTENSIONS,
        on_skip=lambda p: print(
            f"警告: pptx/ppsx ではない（または削減済み）ためスキップ: {p}",
        ),
        on_error=lambda p: print(f"エラー: パスが存在しません: {p}", file=sys.stderr),
    )


def run_reduce_pptx(args: argparse.Namespace) -> None:
    """pptx/ppsx の埋め込みメディア等（メディア・OLE・フォント）を除去した軽量コピーを生成する.

    仕様: docs/specs/infrastructure/pptx-media-reduction.md

    入力は読み取り専用で開き、削減コピーは --output-dir 配下、または
    --alongside 指定時は原本と同じフォルダに <元名>.reduced.<拡張子> の
    別ファイルとして生成する（非破壊）。出力先の既存ファイルは上書きしない。
    """
    from .converter.media_reduction import alongside_output_path
    from .converter.pptx_extractor import (
        PptxExtractionError,
        analyze_pptx_media,
        reduce_pptx,
    )

    _validate_reduce_output_args(args, label="pptx/ppsx")

    targets, path_errors = _collect_pptx_targets(args.paths)
    if not targets:
        print("対象の pptx/ppsx ファイルがありません")
        if path_errors:
            raise SystemExit(1)
        return

    output_dir: Path | None = None
    if not args.report_only and args.output_dir is not None:
        output_dir = Path(args.output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

    reduced = 0
    reported = 0
    skipped = 0
    errors = path_errors
    produced: set[Path] = set()
    for target in targets:
        try:
            report = analyze_pptx_media(target)
        except PptxExtractionError as exc:
            print(f"エラー: {exc}", file=sys.stderr)
            errors += 1
            continue
        reported += 1

        ext_summary = ", ".join(
            f"{ext}: {count}" for ext, count in sorted(report.media_ext_counts.items())
        ) or "なし"
        print(f"{target}")
        print(f"  全体: {_format_cli_size(report.total_bytes)}")
        print(
            f"  削減対象: {report.media_count} 件 "
            f"{_format_cli_size(report.media_bytes)} ({ext_summary})",
        )
        print(f"  削減後推定: {_format_cli_size(report.reduced_estimate_bytes)}")

        if args.report_only:
            continue

        if output_dir is not None:
            out_path = output_dir / target.name
        else:
            # --alongside: 原本と同じフォルダに <元名>.reduced.<拡張子> で出力
            out_path = alongside_output_path(target)
        if out_path in produced:
            print(
                f"  警告: 同一実行内で出力名が衝突するためスキップ: {out_path}"
                f"（入力: {target}）",
            )
            skipped += 1
            continue
        if out_path.exists():
            print(f"  警告: 出力先に同名ファイルが存在するためスキップ: {out_path}")
            skipped += 1
            continue
        try:
            reduce_pptx(target, out_path)
        except PptxExtractionError as exc:
            print(f"  エラー: 削減に失敗しました: {exc}", file=sys.stderr)
            errors += 1
            continue
        print(
            f"  削減完了: {out_path} "
            f"({_format_cli_size(report.total_bytes)} -> "
            f"{_format_cli_size(out_path.stat().st_size)})",
        )
        produced.add(out_path)
        reduced += 1

    if args.report_only:
        print(f"\nレポート完了: 対象 {len(targets)} 件 / エラー {errors} 件")
        if reported == 0 and errors > 0:
            raise SystemExit(1)
    else:
        print(
            f"\n削減完了: 生成 {reduced} 件 / スキップ {skipped} 件 / "
            f"エラー {errors} 件",
        )
        if reduced == 0 and errors > 0:
            raise SystemExit(1)


def _collect_pdf_targets(raw_paths: list[str]) -> tuple[list[Path], int]:
    """reduce-pdf の対象ファイルを収集する.

    Args:
        raw_paths: CLI で指定されたパス（ファイルまたはディレクトリ）

    Returns:
        (対象ファイルのリスト, パス解決エラー数)
    """
    from .converter.media_reduction import collect_reduce_targets
    from .converter.pdf_media_reducer import PDF_EXTENSIONS

    return collect_reduce_targets(
        raw_paths,
        PDF_EXTENSIONS,
        on_skip=lambda p: print(
            f"警告: PDF ではない（または削減済み）ためスキップ: {p}",
        ),
        on_error=lambda p: print(f"エラー: パスが存在しません: {p}", file=sys.stderr),
    )


def _validate_reduce_output_args(args: argparse.Namespace, *, label: str) -> None:
    """削減系コマンドの出力先指定を検証する（--output-dir と --alongside は排他必須）."""
    if args.report_only:
        return
    if args.output_dir is not None and args.alongside:
        print(
            "エラー: --output-dir と --alongside は同時に指定できません",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if args.output_dir is None and not args.alongside:
        print(
            f"エラー: {label} の削減実行には --output-dir または --alongside が必要です"
            "（レポートのみの場合は --report-only を指定）",
            file=sys.stderr,
        )
        raise SystemExit(1)


def run_reduce_pdf(args: argparse.Namespace) -> None:
    """PDF の高解像度画像を再圧縮し、埋め込みメディアを除去した軽量コピーを生成する.

    仕様: docs/specs/infrastructure/pdf-media-reduction.md

    入力は読み取り専用で開き、削減コピーは --output-dir 配下、または
    --alongside 指定時は原本と同じフォルダに <元名>.reduced.pdf の
    別ファイルとして生成する（非破壊）。出力先の既存ファイルは上書きしない。

    テキスト層の乏しい PDF（スキャン文書等）は画像がテキスト抽出の入力に
    なるため、既定では削減対象から除外する（--include-scanned で解除）。
    """
    from .config import get_settings
    from .converter.media_reduction import alongside_output_path
    from .converter.pdf_media_reducer import (
        PdfMediaReport,
        PdfReductionError,
        analyze_pdf_media,
        reduce_pdf,
    )

    _validate_reduce_output_args(args, label="PDF")

    # 範囲外の値は静かに全画像の再エンコードを失敗させる（quality）、
    # または 1x1 px まで縮める（dpi_target=0）ため、実行前に弾く
    for name, value, low, high in (
        ("--quality", args.quality, 1, 100),
        ("--dpi-target", args.dpi_target, 1, 10000),
        ("--dpi-threshold", args.dpi_threshold, 1, 10000),
    ):
        if not low <= value <= high:
            print(
                f"エラー: {name} は {low}〜{high} の範囲で指定してください（指定値: {value}）",
                file=sys.stderr,
            )
            raise SystemExit(1)

    targets, path_errors = _collect_pdf_targets(args.paths)
    if not targets:
        print("対象の PDF ファイルがありません")
        if path_errors:
            raise SystemExit(1)
        return

    output_dir: Path | None = None
    if not args.report_only and args.output_dir is not None:
        output_dir = Path(args.output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

    # テキスト層の判定閾値は、PDF テキスト抽出の事前判定と同じ設定値を使う
    settings = get_settings()
    scan_args = {
        "scan_sample_pages": settings.rag_pdf_quality_sample_pages,
        "scan_min_chars_per_page": settings.rag_pdf_quality_min_chars_per_page,
    }

    def _print_report(target: Path, report: PdfMediaReport) -> None:
        ext_summary = ", ".join(
            f"{ext}: {count}" for ext, count in sorted(report.image_ext_counts.items())
        ) or "なし"
        print(f"{target}")
        print(f"  全体: {_format_cli_size(report.total_bytes)} / {report.page_count} ページ")
        print(
            f"  画像: {report.image_count} 件 "
            f"{_format_cli_size(report.image_bytes)} ({ext_summary})",
        )
        print(
            f"  うち {args.dpi_threshold} DPI 超: {report.oversized_image_count} 件 "
            f"{_format_cli_size(report.oversized_image_bytes)}",
        )
        print(
            f"  埋め込みメディア: {report.embedded_count} 件 "
            f"{_format_cli_size(report.embedded_bytes)}",
        )
        print(f"  削減対象合計: {_format_cli_size(report.reducible_bytes)}")
        if report.is_low_text_layer:
            print(
                "  注意: テキスト層が乏しい PDF です"
                "（画像がテキスト抽出の入力になるため削減で抽出品質が劣化しうる）",
            )

    reduced = 0
    reported = 0
    skipped = 0
    errors = path_errors
    produced: set[Path] = set()
    for target in targets:
        if args.report_only:
            try:
                report = analyze_pdf_media(
                    target, dpi_threshold=args.dpi_threshold, **scan_args,
                )
            except PdfReductionError as exc:
                print(f"エラー: {exc}", file=sys.stderr)
                errors += 1
                continue
            reported += 1
            _print_report(target, report)
            continue

        # 出力先の決定は PDF を開く前に行う（衝突なら解析ごと不要になるため）
        if output_dir is not None:
            out_path = output_dir / target.name
        else:
            # --alongside: 原本と同じフォルダに <元名>.reduced.pdf で出力
            out_path = alongside_output_path(target)
        if out_path in produced:
            print(f"{target}")
            print(
                f"  警告: 同一実行内で出力名が衝突するためスキップ: {out_path}",
            )
            skipped += 1
            continue
        if out_path.exists():
            print(f"{target}")
            print(f"  警告: 出力先に同名ファイルが存在するためスキップ: {out_path}")
            skipped += 1
            continue

        try:
            result = reduce_pdf(
                target,
                out_path,
                dpi_threshold=args.dpi_threshold,
                dpi_target=args.dpi_target,
                quality=args.quality,
                include_low_text_layer=args.include_scanned,
                **scan_args,
            )
        except PdfReductionError as exc:
            print(f"エラー: {exc}", file=sys.stderr)
            errors += 1
            continue
        reported += 1
        _print_report(target, result.report)

        if not result.reduced:
            print(
                "  スキップ: テキスト層が乏しいため削減対象から除外しました"
                "（--include-scanned で削減できます）",
            )
            skipped += 1
            continue

        print(
            f"  削減完了: {out_path} "
            f"({_format_cli_size(result.report.total_bytes)} -> "
            f"{_format_cli_size(out_path.stat().st_size)})",
        )
        produced.add(out_path)
        reduced += 1

    if args.report_only:
        print(f"\nレポート完了: 対象 {len(targets)} 件 / エラー {errors} 件")
        if reported == 0 and errors > 0:
            raise SystemExit(1)
    else:
        print(
            f"\n削減完了: 生成 {reduced} 件 / スキップ {skipped} 件 / "
            f"エラー {errors} 件",
        )
        if reduced == 0 and errors > 0:
            raise SystemExit(1)


# --- インジェスト系 CLI コマンド ---


def _build_cli_pipeline_controller() -> tuple[
    "PipelineController", "Settings"
]:
    """CLI 用の PipelineController を構築する.

    factory.build_pipeline_controller() に委譲し、settings も返す。

    Returns:
        (PipelineController, Settings) のタプル
    """
    from .config import get_settings
    from .pipeline.factory import build_pipeline_controller

    settings = get_settings()
    controller = build_pipeline_controller(settings)
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
    entry_id: str | None = None,
) -> None:
    """取り込み結果を標準出力に表示する."""
    if json_output:
        _output_result(_ingest_result_to_dict(ingest_result, pipeline_summary, entry_id=entry_id))
        return
    print(ingest_result.summary(context=context))
    if pipeline_summary is not None:
        print(f"パイプライン: {pipeline_summary.processed}件処理")
        if pipeline_summary.warnings:
            print(f"パイプライン警告: {len(pipeline_summary.warnings)}件")
            for warn in pipeline_summary.warnings:
                print(f"  - {_format_pipeline_warning(warn)}")
        if pipeline_summary.errors:
            print(f"パイプラインエラー: {len(pipeline_summary.errors)}件")
            for entry in pipeline_summary.errors:
                print(f"  - {_format_pipeline_error(entry)}")


def _emit_bluesky_result(
    *,
    json_out: bool,
    ingest_result: "IngestResult",
    pipeline_summary: "PipelineSummary | None",
    url_stats: dict[str, int],
    context: str,
) -> None:
    """BlueSky 取り込み結果（パイプライン + URL 自動取り込み統計）を出力する.

    run_crawl_bluesky / run_ingest_bluesky で共通利用する。
    """
    if json_out:
        data = _ingest_result_to_dict(ingest_result, pipeline_summary)
        if url_stats:
            data["url_follow"] = {
                "web_placed": url_stats.get("web_placed", 0),
                "web_overwritten": url_stats.get("web_overwritten", 0),
                "youtube_placed": url_stats.get("youtube_placed", 0),
                "youtube_overwritten": url_stats.get("youtube_overwritten", 0),
                "errors": url_stats.get("errors", 0),
            }
        _output_result(data)
        return

    _print_ingest_result(ingest_result, pipeline_summary, context=context)
    if not url_stats:
        return
    web_placed = url_stats.get("web_placed", 0)
    web_overwritten = url_stats.get("web_overwritten", 0)
    yt_placed = url_stats.get("youtube_placed", 0)
    yt_overwritten = url_stats.get("youtube_overwritten", 0)
    err_n = url_stats.get("errors", 0)
    web_total = web_placed + web_overwritten
    yt_total = yt_placed + yt_overwritten
    if web_total == 0 and yt_total == 0 and err_n == 0:
        return
    parts = ["URL 自動取り込み:"]
    if web_total > 0 or yt_total > 0:
        parts.append(
            f"Web {web_total}件 (新規 {web_placed}, 上書き {web_overwritten}),"
            f" YouTube {yt_total}件 (新規 {yt_placed}, 上書き {yt_overwritten})",
        )
    if err_n > 0:
        parts.append(f"エラー {err_n}件")
    print(" ".join(parts))


async def run_ingest_youtube(args: argparse.Namespace) -> None:
    """YouTube 動画取り込み（1 件以上の URL を逐次取り込み）."""
    from .pipeline.ingesters.youtube import YoutubeIngester

    json_out = _is_json_output(args)
    progress_cb = _output_progress if json_out else None
    skip_pipeline = _is_skip_pipeline(args)

    controller, settings = _build_cli_pipeline_controller()

    from .pipeline.ingesters.youtube_fetcher import create_youtube_fetcher

    youtube_ingester = YoutubeIngester(
        controller.source_store,
        fetcher=create_youtube_fetcher(settings),
        max_videos=settings.rag_youtube_max_videos,
        request_interval=settings.rag_youtube_request_interval,
        request_timeout=settings.rag_youtube_request_timeout,
        whisper_model=settings.rag_youtube_whisper_model,
        whisper_device=settings.rag_youtube_whisper_device,
        transcript_languages=settings.rag_youtube_transcript_languages,
        max_duration=settings.rag_youtube_max_duration,
    )

    video_urls: list[str] = list(args.video_url)
    context = f"動画: {video_urls[0]}" if len(video_urls) == 1 else f"動画 {len(video_urls)} 件"

    with _write_lock_or_exit(
        Path(controller.source_store.root_dir), json_out=json_out,
    ):
        # ingest_videos は bulk 末尾で Whisper モデルを必ずアンロードし、
        # 個別 URL の例外は per-item の IngestResult.errors に変換して処理継続する
        # （`KeyboardInterrupt` 等の `BaseException` 系は伝播）。
        results: "list[IngestResult]" = await youtube_ingester.ingest_videos(video_urls)

        ingest_result = _merge_ingest_results(results)

        # 全件失敗（placed=0 and overwritten=0 and errors>0）は設定エラー疑いのため exit 1
        # 仕様: docs/specs/rebuild-stats.md「CLI exit code 体系」
        if (
            ingest_result.placed == 0
            and ingest_result.overwritten == 0
            and ingest_result.errors > 0
        ):
            _print_ingest_result(ingest_result, None, context=context, json_output=json_out)
            sys.exit(1)

        if ingest_result.is_empty():
            _print_ingest_result(ingest_result, None, context=context, json_output=json_out)
            return

        commit_message = f"ingest(youtube): {len(video_urls)} 件"
        if skip_pipeline:
            _commit_for_skip_pipeline(controller, commit_message)
            pipeline_summary = None
        else:
            pipeline_summary = await controller.ingest_and_index(
                commit_message,
                progress_callback=progress_cb,
                concurrency=settings.rag_embedding_concurrency,
            )
        _print_ingest_result(ingest_result, pipeline_summary, context=context, json_output=json_out)
        if skip_pipeline:
            _print_skip_pipeline_notice(json_out)


async def run_ingest_youtube_playlist(args: argparse.Namespace) -> None:
    """YouTube プレイリスト一括取り込み."""
    from .pipeline.ingesters.youtube import YoutubeIngester

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    max_videos = args.max_videos if args.max_videos is not None else settings.rag_youtube_max_videos

    from .pipeline.ingesters.youtube_fetcher import create_youtube_fetcher

    youtube_ingester = YoutubeIngester(
        controller.source_store,
        fetcher=create_youtube_fetcher(settings),
        max_videos=max_videos,
        request_interval=settings.rag_youtube_request_interval,
        request_timeout=settings.rag_youtube_request_timeout,
        whisper_model=settings.rag_youtube_whisper_model,
        whisper_device=settings.rag_youtube_whisper_device,
        transcript_languages=settings.rag_youtube_transcript_languages,
        max_duration=settings.rag_youtube_max_duration,
    )

    progress_cb = _output_progress if json_out else None

    with _write_lock_or_exit(
        Path(controller.source_store.root_dir), json_out=json_out,
    ):
        try:
            ingest_result = await youtube_ingester.crawl_playlist(
                args.playlist_url,
                max_videos=max_videos,
                progress_callback=_wrap_progress(progress_cb, PipelinePhase.FETCH.display),
            )
        except (ValueError, TypeError) as e:
            if json_out:
                _output_error(CliErrorCode.DEPENDENCY_UNAVAILABLE, str(e))
            logger.error("エラー: %s", e)
            sys.exit(1)

        if ingest_result.is_empty():
            _print_ingest_result(ingest_result, None, context=f"プレイリスト: {args.playlist_url}", json_output=json_out)
            return

        skip_pipeline = _is_skip_pipeline(args)
        commit_message = f"ingest(youtube-playlist): {args.playlist_url}"
        if skip_pipeline:
            _commit_for_skip_pipeline(controller, commit_message)
            pipeline_summary = None
        else:
            pipeline_summary = await controller.ingest_and_index(
                commit_message,
                progress_callback=progress_cb,
                concurrency=settings.rag_embedding_concurrency,
            )
        _print_ingest_result(ingest_result, pipeline_summary, context=f"プレイリスト: {args.playlist_url}", json_output=json_out)
        if skip_pipeline:
            _print_skip_pipeline_notice(json_out)



def _create_youtube_ingester_cli(
    source_store: SourceStore, settings: Settings,
) -> YoutubeIngester:
    """CLI 用 YoutubeIngester を生成する."""
    from .pipeline.ingesters.youtube import YoutubeIngester
    from .pipeline.ingesters.youtube_fetcher import create_youtube_fetcher

    return YoutubeIngester(
        source_store,
        fetcher=create_youtube_fetcher(settings),
        max_videos=settings.rag_youtube_max_videos,
        request_interval=settings.rag_youtube_request_interval,
        request_timeout=settings.rag_youtube_request_timeout,
        whisper_model=settings.rag_youtube_whisper_model,
        whisper_device=settings.rag_youtube_whisper_device,
        transcript_languages=settings.rag_youtube_transcript_languages,
        max_duration=settings.rag_youtube_max_duration,
    )


def _build_bluesky_ingester(
    source_store: SourceStore,
    settings: Settings,
    client: Any,
    *,
    max_posts: int,
    include_reposts: bool,
) -> Any:
    """CLI 用 BlueskyIngester を factory 経由で組み立てる.

    fetcher / media_downloader / youtube_classifier / youtube_delegator /
    web_delegator を Protocol 注入する。``client`` は fetcher / media_downloader
    に内包されるため、Ingester 本体メソッドに渡す必要はない。
    """
    from .pipeline.ingesters.bluesky import BlueskyIngester
    from .pipeline.ingesters.bluesky_fetcher import create_bluesky_fetcher
    from .pipeline.ingesters.bluesky_media_downloader import (
        create_bluesky_media_downloader,
    )
    from .pipeline.ingesters.web import create_web_delegator
    from .pipeline.ingesters.youtube_protocols import (
        create_youtube_classifier,
        create_youtube_delegator,
    )

    youtube_ingester = _create_youtube_ingester_cli(source_store, settings)

    return BlueskyIngester(
        source_store,
        fetcher=create_bluesky_fetcher(settings, client),
        media_downloader=create_bluesky_media_downloader(settings, client),
        youtube_classifier=create_youtube_classifier(),
        youtube_delegator=create_youtube_delegator(youtube_ingester),
        web_delegator=create_web_delegator(settings, source_store),
        max_posts=max_posts,
        include_reposts=include_reposts,
        force_youtube_reingest=settings.rag_bluesky_force_youtube_reingest,
        youtube_request_interval=settings.rag_youtube_request_interval,
    )


async def run_crawl_bluesky(args: argparse.Namespace) -> None:
    """BlueSky 投稿取り込み."""
    from py_common_lib.httpx import ConstrainedClient  # safety:allowed

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    max_posts = args.max_posts if args.max_posts is not None else settings.rag_bluesky_max_posts
    include_reposts = args.include_reposts if args.include_reposts is not None else settings.rag_bluesky_include_reposts

    progress_cb = _output_progress if json_out else None

    with _write_lock_or_exit(
        Path(controller.source_store.root_dir), json_out=json_out,
    ):
        try:
            async with ConstrainedClient(
                request_timeout=settings.rag_bluesky_request_timeout,
                request_interval=settings.rag_bluesky_request_interval,
            ) as client:
                bluesky_ingester = _build_bluesky_ingester(
                    controller.source_store,
                    settings,
                    client,
                    max_posts=max_posts,
                    include_reposts=include_reposts,
                )

                force = args.force

                ingest_result, placed_items = await bluesky_ingester.crawl_bluesky(
                    args.handle,
                    max_posts=max_posts,
                    include_reposts=include_reposts,
                    force=force,
                    progress_callback=_wrap_progress(progress_cb, PipelinePhase.FETCH.display),
                )

                # 投稿内 URL の自動取り込み
                url_stats: dict[str, int] = {}
                if placed_items:
                    url_stats = await bluesky_ingester.follow_urls(
                        placed_items,
                        result=ingest_result,
                    )
        except (ValueError, TypeError) as e:
            if json_out:
                _output_error(CliErrorCode.DEPENDENCY_UNAVAILABLE, str(e))
            logger.error("エラー: %s", e)
            sys.exit(1)

        skip_pipeline = _is_skip_pipeline(args)
        commit_message = f"ingest(bluesky): {args.handle}"
        if skip_pipeline:
            _commit_for_skip_pipeline(controller, commit_message)
            pipeline_summary = None
        else:
            pipeline_summary = await controller.ingest_and_index(
                commit_message,
                progress_callback=progress_cb,
                concurrency=settings.rag_embedding_concurrency,
            )
        _emit_bluesky_result(
            json_out=json_out,
            ingest_result=ingest_result,
            pipeline_summary=pipeline_summary,
            url_stats=url_stats,
            context=f"ハンドル: {args.handle}",
        )
        if skip_pipeline:
            _print_skip_pipeline_notice(json_out)


async def run_crawl_zenn(args: argparse.Namespace) -> None:
    """Zenn コンテンツ取り込み."""
    from .pipeline.ingesters.zenn import ZennIngester, create_zenn_fetcher

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    max_articles = args.max_articles if args.max_articles is not None else settings.rag_zenn_max_articles

    progress_cb = _output_progress if json_out else None

    with _write_lock_or_exit(
        Path(controller.source_store.root_dir), json_out=json_out,
    ):
        try:
            async with create_zenn_fetcher(settings) as fetcher:
                zenn_ingester = ZennIngester(
                    controller.source_store,
                    fetcher=fetcher,
                    max_articles=max_articles,
                )
                ingest_result = await zenn_ingester.crawl_zenn(
                    args.username,
                    max_articles=max_articles,
                    content_type=args.content_type,
                    force=args.force,
                    progress_callback=_wrap_progress(progress_cb, PipelinePhase.FETCH.display),
                )
        except (ValueError, TypeError) as e:
            if json_out:
                _output_error(CliErrorCode.DEPENDENCY_UNAVAILABLE, str(e))
            logger.error("エラー: %s", e)
            sys.exit(1)

        if ingest_result.is_empty():
            _print_ingest_result(
                ingest_result, None, context=f"ユーザー: {args.username}", json_output=json_out,
            )
            if not json_out and ingest_result.skipped > 0:
                print("（上書きするには --force を指定してください）")
            return

        skip_pipeline = _is_skip_pipeline(args)
        commit_message = f"ingest(zenn): {args.username}"
        if skip_pipeline:
            _commit_for_skip_pipeline(controller, commit_message)
            pipeline_summary = None
        else:
            pipeline_summary = await controller.ingest_and_index(
                commit_message,
                progress_callback=progress_cb,
                concurrency=settings.rag_embedding_concurrency,
            )
        _print_ingest_result(ingest_result, pipeline_summary, context=f"ユーザー: {args.username}", json_output=json_out)
        if skip_pipeline:
            _print_skip_pipeline_notice(json_out)


async def run_ingest_bluesky(args: argparse.Namespace) -> None:
    """BlueSky 投稿取り込み（URL 指定）."""
    from py_common_lib.httpx import ConstrainedClient  # safety:allowed

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    with _write_lock_or_exit(
        Path(controller.source_store.root_dir), json_out=json_out,
    ):
        url_stats: dict[str, int] = {}
        try:
            async with ConstrainedClient(
                request_timeout=settings.rag_bluesky_request_timeout,
                request_interval=settings.rag_bluesky_request_interval,
            ) as client:
                bluesky_ingester = _build_bluesky_ingester(
                    controller.source_store,
                    settings,
                    client,
                    max_posts=settings.rag_bluesky_max_posts,
                    include_reposts=settings.rag_bluesky_include_reposts,
                )

                ingest_result, placed_items = await bluesky_ingester.ingest_posts(
                    args.url,
                )

                # 投稿内 URL の自動取り込み（仕様: 投稿取得フロー（rag_add_bluesky））。
                # placed_items は _suppress_youtube_reingest=False で渡されるため、
                # force_youtube_reingest 設定の値に関わらず YouTube/Web ともに取り込まれる
                if placed_items:
                    url_stats = await bluesky_ingester.follow_urls(
                        placed_items,
                        result=ingest_result,
                    )
        except (ValueError, TypeError) as e:
            if json_out:
                _output_error(CliErrorCode.DEPENDENCY_UNAVAILABLE, str(e))
            logger.error("エラー: %s", e)
            sys.exit(1)

        if ingest_result.is_empty():
            _print_ingest_result(ingest_result, None, context="BlueSky ingest", json_output=json_out)
            return

        skip_pipeline = _is_skip_pipeline(args)
        commit_message = "ingest(bluesky/url)"
        if skip_pipeline:
            _commit_for_skip_pipeline(controller, commit_message)
            pipeline_summary = None
        else:
            pipeline_summary = await controller.ingest_and_index(
                commit_message,
                progress_callback=_output_progress if json_out else None,
                concurrency=settings.rag_embedding_concurrency,
            )
        _emit_bluesky_result(
            json_out=json_out,
            ingest_result=ingest_result,
            pipeline_summary=pipeline_summary,
            url_stats=url_stats,
            context="BlueSky ingest",
        )
        if skip_pipeline:
            _print_skip_pipeline_notice(json_out)


async def run_ingest_zenn(args: argparse.Namespace) -> None:
    """Zenn コンテンツ取り込み（URL 指定）."""
    from .pipeline.ingesters.zenn import ZennIngester, create_zenn_fetcher

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    with _write_lock_or_exit(
        Path(controller.source_store.root_dir), json_out=json_out,
    ):
        try:
            async with create_zenn_fetcher(settings) as fetcher:
                zenn_ingester = ZennIngester(
                    controller.source_store,
                    fetcher=fetcher,
                    max_articles=settings.rag_zenn_max_articles,
                )
                ingest_result = await zenn_ingester.ingest_contents(args.url)
        except (ValueError, TypeError) as e:
            if json_out:
                _output_error(CliErrorCode.DEPENDENCY_UNAVAILABLE, str(e))
            logger.error("エラー: %s", e)
            sys.exit(1)

        if ingest_result.is_empty():
            _print_ingest_result(ingest_result, None, context="Zenn ingest", json_output=json_out)
            return

        skip_pipeline = _is_skip_pipeline(args)
        commit_message = "ingest(zenn/url)"
        if skip_pipeline:
            _commit_for_skip_pipeline(controller, commit_message)
            pipeline_summary = None
        else:
            pipeline_summary = await controller.ingest_and_index(
                commit_message,
                progress_callback=_output_progress if json_out else None,
                concurrency=settings.rag_embedding_concurrency,
            )
        _print_ingest_result(ingest_result, pipeline_summary, context="Zenn ingest", json_output=json_out)
        if skip_pipeline:
            _print_skip_pipeline_notice(json_out)


async def run_add_document(args: argparse.Namespace) -> None:
    """単一ドキュメント取り込み."""
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
                _output_error(CliErrorCode.VALIDATION_ERROR, msg)
            print(f"エラー: {msg}", file=sys.stderr)
            raise SystemExit(1)

        raw_input = sys.stdin.read()
        if not raw_input:
            msg = "stdin からの入力が空です"
            if json_out:
                _output_error(CliErrorCode.VALIDATION_ERROR, msg)
            print(f"エラー: {msg}", file=sys.stderr)
            raise SystemExit(1)

        # encoding に基づいてデコード
        encoding = getattr(args, "encoding", "text")
        try:
            data = decode_upload_content(raw_input, encoding)
        except ValueError as e:
            if json_out:
                _output_error(CliErrorCode.VALIDATION_ERROR, str(e))
            print(f"エラー: {e}", file=sys.stderr)
            raise SystemExit(1)

        # 拡張子チェック
        ext = Path(filename).suffix.lower()
        if ext not in supported_extensions:
            msg = f"対応していないファイル形式です: {ext!r}（対応: {', '.join(supported_extensions)}）"
            if json_out:
                _output_error(CliErrorCode.VALIDATION_ERROR, msg)
            print(f"エラー: {msg}", file=sys.stderr)
            raise SystemExit(1)
        display_name = filename
    else:
        # --file モード（従来動作）
        file_path_str = args.file_path
        if not file_path_str or not file_path_str.strip():
            if json_out:
                _output_error(CliErrorCode.VALIDATION_ERROR, "file_path が空です")
            print("エラー: file_path が空です", file=sys.stderr)
            raise SystemExit(1)
        resolved = Path(file_path_str.strip()).resolve()
        if not resolved.exists():
            if json_out:
                _output_error(CliErrorCode.NOT_FOUND, f"ファイルが見つかりません: {resolved}")
            print(f"エラー: ファイルが見つかりません: {resolved}", file=sys.stderr)
            raise SystemExit(1)
        if resolved.is_dir():
            if json_out:
                _output_error(CliErrorCode.VALIDATION_ERROR, f"パスはファイルではなくディレクトリです: {resolved}")
            print(f"エラー: パスはファイルではなくディレクトリです: {resolved}", file=sys.stderr)
            raise SystemExit(1)
        data = resolved.read_bytes()
        # --filename が指定されていればそちらを優先（Upload API が一時ファイル経由で呼ぶケース）
        filename = args.filename if args.filename else resolved.name
        ext = Path(filename).suffix.lower()
        if ext not in supported_extensions:
            msg = f"対応していないファイル形式です: {ext!r}（対応: {', '.join(supported_extensions)}）"
            if json_out:
                _output_error(CliErrorCode.VALIDATION_ERROR, msg)
            print(f"エラー: {msg}", file=sys.stderr)
            raise SystemExit(1)
        display_name = file_path_str

    with _write_lock_or_exit(
        Path(controller.source_store.root_dir), json_out=json_out,
    ):
        from .pipeline.ingesters.local import create_local_fetcher
        local_ingester = LocalIngester(
            controller.source_store,
            fetcher=create_local_fetcher(settings),
            supported_extensions=supported_extensions,
            http_mode_enabled=False,
            allowed_dirs=None,
        )

        try:
            ingest_result = local_ingester.add_document(
                data, filename, upload_mode=args.upload_mode,
            )
        except FileExistsError:
            msg = f"同名ファイルが既に存在します: {filename}"
            if json_out:
                _output_error(CliErrorCode.FILE_EXISTS, msg)
            print(f"エラー: {msg}", file=sys.stderr)
            raise SystemExit(1)

        if ingest_result.is_empty():
            _print_ingest_result(
                ingest_result, None, context=display_name, json_output=json_out,
            )
            return
        if ingest_result.errors > 0:
            first_detail = _error_detail_message(ingest_result.error_details[0])
            if json_out:
                _output_error(CliErrorCode.INTERNAL_ERROR, first_detail)
            print(f"エラー: {first_detail}", file=sys.stderr)
            raise SystemExit(1)

        pipeline_summary = await controller.ingest_and_index(
            f"ingest(local): add {filename}",
            progress_callback=progress_cb,
            concurrency=settings.rag_embedding_concurrency,
        )
        _print_ingest_result(ingest_result, pipeline_summary, context=display_name, json_output=json_out)


async def run_crawl_documents(args: argparse.Namespace) -> None:
    """ディレクトリ一括取り込み."""
    from .pipeline.ingesters.local import LocalIngester, create_local_fetcher

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    supported_extensions = [
        (ext.strip() if ext.strip().startswith(".") else f".{ext.strip()}").lower()
        for ext in settings.rag_document_supported_extensions.split(",")
        if ext.strip()
    ]
    local_ingester = LocalIngester(
        controller.source_store,
        fetcher=create_local_fetcher(settings),
        supported_extensions=supported_extensions,
        http_mode_enabled=False,
        allowed_dirs=None,
    )

    progress_cb = _output_progress if json_out else None

    with _write_lock_or_exit(
        Path(controller.source_store.root_dir), json_out=json_out,
    ):
        ingest_result = local_ingester.crawl_documents(
            args.dir_path, args.pattern, upload_mode=args.upload_mode,
            progress_callback=_wrap_progress(progress_cb, PipelinePhase.FETCH.display),
        )

        if ingest_result.is_empty():
            _print_ingest_result(
                ingest_result, None, context=f"ディレクトリ: {args.dir_path}",
                json_output=json_out,
            )
            return
        if ingest_result.errors > 0 and ingest_result.placed == 0:
            # 早期終了: エラー詳細を先頭 1 件だけ表示してから exit
            first_detail = _error_detail_message(ingest_result.error_details[0])
            if json_out:
                _output_error(CliErrorCode.INTERNAL_ERROR, first_detail)
            print(f"エラー: {first_detail}", file=sys.stderr)
            raise SystemExit(1)

        skip_pipeline = _is_skip_pipeline(args)
        commit_message = f"ingest(local): crawl {args.dir_path}"
        if skip_pipeline:
            _commit_for_skip_pipeline(controller, commit_message)
            pipeline_summary = None
        else:
            pipeline_summary = await controller.ingest_and_index(
                commit_message,
                progress_callback=progress_cb,
                concurrency=settings.rag_embedding_concurrency,
            )
        _print_ingest_result(ingest_result, pipeline_summary, context=f"ディレクトリ: {args.dir_path}", json_output=json_out)
        if skip_pipeline:
            _print_skip_pipeline_notice(json_out)


async def run_site_ingest(args: argparse.Namespace) -> None:
    """指定 URL リストを取得（リンク辿りなし）して source_store に配置する.

    Issue #797: クロールモード（リンク辿り）は `site-crawl` サブコマンドに分離済み。
    本コマンドはリンク辿りを発動しない（取得対象 URL = 配置 URL）。
    """
    import time as time_mod

    from .pipeline.ingesters.web import WebIngester
    from .scrapy.runner import create_scrapy_runner
    from .utils.url import check_ssrf, validate_url

    json_out = _is_json_output(args)
    progress_cb = _output_progress if json_out else None

    urls: list[str] = args.url  # nargs='+' なのでリスト

    # 全 URL バリデーション
    validated_urls: list[str] = []
    for u in urls:
        try:
            validated = validate_url(u)
            check_ssrf(validated)
            validated_urls.append(validated)
        except ValueError as e:
            if json_out:
                _output_error(CliErrorCode.VALIDATION_ERROR, str(e))
            logger.error("エラー: %s", e)
            sys.exit(1)

    controller, settings = _build_cli_pipeline_controller()

    with _write_lock_or_exit(
        Path(controller.source_store.root_dir), json_out=json_out,
    ):
        outer_start = time_mod.monotonic()
        web_ingester = WebIngester(
            controller.source_store,
            scrapy_runner=create_scrapy_runner(settings),
        )
        execution = await web_ingester.fetch_urls(urls=validated_urls)

        display_url = (
            validated_urls[0]
            if len(validated_urls) == 1
            else f"{len(validated_urls)} URLs"
        )

        if execution.no_output:
            elapsed = time_mod.monotonic() - outer_start
            if json_out:
                _output_result({
                    "placed": 0,
                    "overwritten": 0,
                    "skipped": 0,
                    "errors": 0,
                    "elapsed": round(elapsed, 1),
                    "scrapy_exit_code": execution.scrapy_exit_code,
                    "no_output": True,
                })
            else:
                print(
                    f"取得が完了しましたが、メタデータが出力されませんでした。"
                    f" exit_code={execution.scrapy_exit_code},"
                    f" 所要時間={elapsed:.1f}秒",
                )
            return

        # パイプライン処理
        skip_pipeline = _is_skip_pipeline(args)
        pipeline_summary = None
        has_changes = (execution.ingest.placed + execution.ingest.overwritten) > 0
        if has_changes and not skip_pipeline:
            pipeline_summary = await controller.ingest_and_index(
                f"ingest(web): site-ingest {display_url}",
                progress_callback=progress_cb,
                concurrency=settings.rag_embedding_concurrency,
            )
        elif has_changes and skip_pipeline:
            controller.commit(f"ingest(web): site-ingest {display_url} (skip_pipeline)")

        # 正常完了後のクリーンアップ（仕様: docs/specs/site-ingest.md）
        if execution.scrapy_success and execution.crawl_result is not None:
            execution.crawl_result.cleanup()

        # 操作全体の所要時間（クロール + Bridge + パイプライン）
        elapsed = time_mod.monotonic() - outer_start

        if json_out:
            data: dict[str, object] = _ingest_result_to_dict(
                execution.ingest, pipeline_summary,
            )
            data["elapsed"] = round(elapsed, 1)
            data["skip_pipeline"] = skip_pipeline
            if not execution.scrapy_success:
                data["scrapy_exit_code"] = execution.scrapy_exit_code
            _output_result(data)
        else:
            _print_ingest_result(
                execution.ingest,
                pipeline_summary,
                context=f"サイト: {display_url}",
                json_output=False,
            )
            print(f"所要時間: {elapsed:.1f}秒")
            if skip_pipeline:
                _print_skip_pipeline_notice(json_out=False)
            if not execution.scrapy_success:
                print(
                    f"Scrapy exit_code={execution.scrapy_exit_code}（部分的な結果）",
                )


async def run_site_crawl(args: argparse.Namespace) -> None:
    """Scrapy による単一 URL 起点のサイトクロール（リンク辿りあり）."""
    import re
    import time as time_mod

    from .pipeline.ingesters.web import WebIngester
    from .scrapy.runner import create_scrapy_runner
    from .utils.url import check_ssrf, validate_url

    json_out = _is_json_output(args)
    progress_cb = _output_progress if json_out else None

    url: str = args.url
    try:
        validated_url = validate_url(url)
        check_ssrf(validated_url)
    except ValueError as e:
        if json_out:
            _output_error(CliErrorCode.VALIDATION_ERROR, str(e))
        logger.error("エラー: %s", e)
        sys.exit(1)

    if args.url_pattern:
        try:
            re.compile(args.url_pattern)
        except re.error as e:
            if json_out:
                _output_error(
                    CliErrorCode.VALIDATION_ERROR, f"無効な正規表現パターン: {e}",
                )
            logger.error("無効な正規表現パターン: %s", e)
            sys.exit(1)

    controller, settings = _build_cli_pipeline_controller()

    with _write_lock_or_exit(
        Path(controller.source_store.root_dir), json_out=json_out,
    ):
        # max_pages のクランプ
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

        outer_start = time_mod.monotonic()
        web_ingester = WebIngester(
            controller.source_store,
            scrapy_runner=create_scrapy_runner(settings),
        )
        execution = await web_ingester.crawl_url(
            url=validated_url,
            url_pattern=args.url_pattern,
            max_pages=effective_max_pages,
            restart=args.restart,
        )

        display_url = validated_url

        if execution.no_output:
            elapsed = time_mod.monotonic() - outer_start
            if json_out:
                _output_result({
                    "placed": 0,
                    "overwritten": 0,
                    "skipped": 0,
                    "errors": 0,
                    "elapsed": round(elapsed, 1),
                    "scrapy_exit_code": execution.scrapy_exit_code,
                    "no_output": True,
                })
            else:
                print(
                    f"クロールが完了しましたが、メタデータが出力されませんでした。"
                    f" exit_code={execution.scrapy_exit_code},"
                    f" 所要時間={elapsed:.1f}秒",
                )
            return

        # パイプライン処理
        skip_pipeline = _is_skip_pipeline(args)
        pipeline_summary = None
        has_changes = (execution.ingest.placed + execution.ingest.overwritten) > 0
        if has_changes and not skip_pipeline:
            pipeline_summary = await controller.ingest_and_index(
                f"ingest(web): site-crawl {display_url}",
                progress_callback=progress_cb,
                concurrency=settings.rag_embedding_concurrency,
            )
        elif has_changes and skip_pipeline:
            controller.commit(
                f"ingest(web): site-crawl {display_url} (skip_pipeline)",
            )

        # 正常完了後のクリーンアップ（仕様: docs/specs/site-ingest.md）
        if execution.scrapy_success and execution.crawl_result is not None:
            execution.crawl_result.cleanup()

        elapsed = time_mod.monotonic() - outer_start

        if json_out:
            data: dict[str, object] = _ingest_result_to_dict(
                execution.ingest, pipeline_summary,
            )
            data["elapsed"] = round(elapsed, 1)
            data["skip_pipeline"] = skip_pipeline
            if not execution.scrapy_success:
                data["scrapy_exit_code"] = execution.scrapy_exit_code
            _output_result(data)
        else:
            _print_ingest_result(
                execution.ingest,
                pipeline_summary,
                context=f"サイト: {display_url}",
                json_output=False,
            )
            print(f"所要時間: {elapsed:.1f}秒")
            if skip_pipeline:
                _print_skip_pipeline_notice(json_out=False)
            if not execution.scrapy_success:
                print(
                    f"Scrapy exit_code={execution.scrapy_exit_code}（部分的な結果）",
                )


async def run_update_aozora_catalog(args: argparse.Namespace) -> None:
    """青空文庫カタログ更新."""
    from .pipeline.ingesters.aozora import AozoraIngester, create_aozora_fetcher

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    try:
        async with create_aozora_fetcher(settings) as fetcher:
            aozora_ingester = AozoraIngester(
                controller.source_store,
                fetcher=fetcher,
                max_works=settings.rag_aozora_max_works,
            )
            result_text = await aozora_ingester.update_catalog()
    except (ValueError, TypeError) as e:
        if json_out:
            _output_error(CliErrorCode.DEPENDENCY_UNAVAILABLE, str(e))
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
    from .pipeline.ingesters.aozora import AozoraIngester, create_aozora_fetcher

    json_out = _is_json_output(args)

    controller, settings = _build_cli_pipeline_controller()

    # search はローカルカタログ走査のみで HTTP アクセスを行わないが、
    # AozoraIngester のコンストラクタは fetcher を必須とするため factory で生成する。
    # async with は使わないため、Real 実装の場合 client は初期化されないが本処理では呼ばない。
    aozora_ingester = AozoraIngester(
        controller.source_store,
        fetcher=create_aozora_fetcher(settings),
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
            _output_error(CliErrorCode.DEPENDENCY_UNAVAILABLE, str(e))
        logger.error("エラー: %s", e)
        sys.exit(1)

    if json_out:
        _output_result_logged(
            {
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
            },
            "search-aozora: author=%s, title=%s, count=%d",
            args.author,
            args.title,
            len(results),
        )
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


def _normalize_aozora_id(value: str) -> str:
    """青空文庫の book_id / person_id を正規化する.

    aozora ingester の AozoraIngester.add_work / crawl_author 内部で行う
    ``s.strip().zfill(6)`` と同じロジック。CLI の bulk 入力で重複検出に使う。
    SSoT は aozora ingester 側の実装。

    空文字列・空白のみの入力も同じロジック (`"".strip().zfill(6) = "000000"`) で
    正規化する。これにより、ingester 側の `if not book_id or not book_id.strip(): raise` で
    エラーになる入力も bulk 内では一貫した識別子で集約される。
    """
    return value.strip().zfill(6)


async def run_ingest_aozora(args: argparse.Namespace) -> None:
    """青空文庫作品取り込み（1 件以上の book_id を逐次取り込み）."""
    from .pipeline.ingesters.aozora import AozoraIngester, create_aozora_fetcher

    json_out = _is_json_output(args)
    progress_cb = _output_progress if json_out else None
    skip_pipeline = _is_skip_pipeline(args)

    controller, settings = _build_cli_pipeline_controller()

    raw_book_ids: list[str] = list(args.book_id)

    # zfill 正規化後の重複検出（仕様: docs/specs/ingesters/aozora.md
    # 「ingest-aozora 複数 ID 入力時の重複検出」）
    seen: set[str] = set()
    deduped: list[str] = []
    duplicates: dict[str, list[str]] = {}
    for raw in raw_book_ids:
        normalized = _normalize_aozora_id(raw)
        if normalized in seen:
            duplicates.setdefault(normalized, []).append(raw)
        else:
            seen.add(normalized)
            deduped.append(raw)
            # 重複の発端入力を記録（後で WARN 出力時に元の入力もまとめる）
            duplicates.setdefault(normalized, [raw])
    for normalized_id, originals in duplicates.items():
        if len(originals) > 1:
            logger.warning(
                "重複入力を検出: %s は同じ作品 (%s)。1 回のみ処理します",
                ", ".join(repr(o) for o in originals),
                normalized_id,
            )

    book_ids: list[str] = deduped
    context = f"作品ID: {book_ids[0]}" if len(book_ids) == 1 else f"作品 {len(book_ids)} 件"

    with _write_lock_or_exit(
        Path(controller.source_store.root_dir), json_out=json_out,
    ):
        from .pipeline.ingesters._common import IngestErrorCategory, IngestErrorDetail
        from .pipeline.ingesters._common import IngestResult as _IngestResult

        results: "list[IngestResult]" = []
        try:
            async with create_aozora_fetcher(settings) as fetcher:
                aozora_ingester = AozoraIngester(
                    controller.source_store,
                    fetcher=fetcher,
                    max_works=settings.rag_aozora_max_works,
                )
                for bid in book_ids:
                    try:
                        results.append(await aozora_ingester.add_work(bid))
                    except Exception as e:
                        # bulk loop の partial result loss 防止のため、ループ内例外は per-item として
                        # IngestResult.errors に変換し処理継続。Exception 限定のため KeyboardInterrupt
                        # 等の BaseException 系（キャンセル）は捕捉せず正常に伝播する。
                        logger.error("ingest-aozora エラー (book_id=%s): %s", bid, e)
                        err = _IngestResult()
                        err.errors = 1
                        err.error_details.append(IngestErrorDetail(
                            category=IngestErrorCategory.METADATA_FETCH.value,
                            target=bid,
                            message=f"取り込み失敗: {e}",
                        ))
                        results.append(err)
        except (ValueError, TypeError) as e:
            # fetcher 初期化など、ループ外の前提エラー
            if json_out:
                _output_error(CliErrorCode.DEPENDENCY_UNAVAILABLE, str(e))
            logger.error("エラー: %s", e)
            sys.exit(1)

        ingest_result = _merge_ingest_results(results)

        # 全件失敗（placed=0 and overwritten=0 and errors>0）は設定エラー疑いのため exit 1
        # 仕様: docs/specs/rebuild-stats.md「CLI exit code 体系」
        if (
            ingest_result.placed == 0
            and ingest_result.overwritten == 0
            and ingest_result.errors > 0
        ):
            _print_ingest_result(ingest_result, None, context=context, json_output=json_out)
            sys.exit(1)

        if ingest_result.is_empty():
            _print_ingest_result(ingest_result, None, context=context, json_output=json_out)
            return

        commit_message = f"ingest(aozora): {len(book_ids)} 件"
        if skip_pipeline:
            _commit_for_skip_pipeline(controller, commit_message)
            pipeline_summary = None
        else:
            pipeline_summary = await controller.ingest_and_index(
                commit_message,
                progress_callback=progress_cb,
                concurrency=settings.rag_embedding_concurrency,
            )
        _print_ingest_result(ingest_result, pipeline_summary, context=context, json_output=json_out)
        if skip_pipeline:
            _print_skip_pipeline_notice(json_out)


async def run_ingest_aozora_author(args: argparse.Namespace) -> None:
    """青空文庫著者一括取り込み."""
    from .pipeline.ingesters.aozora import AozoraIngester, create_aozora_fetcher

    json_out = _is_json_output(args)
    skip_pipeline = _is_skip_pipeline(args)

    controller, settings = _build_cli_pipeline_controller()

    max_works = args.max_works if args.max_works is not None else settings.rag_aozora_max_works

    progress_cb = _output_progress if json_out else None

    with _write_lock_or_exit(
        Path(controller.source_store.root_dir), json_out=json_out,
    ):
        try:
            async with create_aozora_fetcher(settings) as fetcher:
                aozora_ingester = AozoraIngester(
                    controller.source_store,
                    fetcher=fetcher,
                    max_works=max_works,
                )
                ingest_result = await aozora_ingester.crawl_author(
                    args.person_id,
                    max_works=max_works,
                    progress_callback=_wrap_progress(progress_cb, PipelinePhase.FETCH.display),
                )
        except (ValueError, TypeError) as e:
            if json_out:
                _output_error(CliErrorCode.DEPENDENCY_UNAVAILABLE, str(e))
            logger.error("エラー: %s", e)
            sys.exit(1)

        commit_message = f"ingest(aozora): person_id={args.person_id}"
        if skip_pipeline:
            _commit_for_skip_pipeline(controller, commit_message)
            pipeline_summary = None
        else:
            pipeline_summary = await controller.ingest_and_index(
                commit_message,
                progress_callback=progress_cb,
                concurrency=settings.rag_embedding_concurrency,
            )
        _print_ingest_result(ingest_result, pipeline_summary, context=f"著者ID: {args.person_id}", json_output=json_out)
        if skip_pipeline:
            _print_skip_pipeline_notice(json_out)


if __name__ == "__main__":
    from .config import (
        get_settings,
        log_fake_mode_status,
        validate_utf8_environment,
    )

    validate_utf8_environment()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Fake モード状態の起動時ログ（仕様: docs/specs/infrastructure/fake-mode.md）
    log_fake_mode_status(get_settings())

    main()

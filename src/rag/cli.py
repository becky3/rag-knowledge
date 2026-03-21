"""RAG Knowledge CLIモジュール

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import urldefrag

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
    from .bm25_index import BM25Index
    from .config import RAGSettings as Settings
    from .pipeline.controller import PipelineController
    from .pipeline.ingesters._common import IngestResult
    from .pipeline.models import PipelineSummary
    from .rag_knowledge import RAGKnowledgeService


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

    # crawl-preview サブコマンド
    preview_parser = subparsers.add_parser("crawl-preview", help="クロール対象ページをプレビュー")
    preview_parser.add_argument(
        "--url",
        required=True,
        help="リンク集ページのURL",
    )
    preview_parser.add_argument(
        "--pattern",
        default="",
        help="URLフィルタリング用の正規表現パターン（depth >= 2 の場合は必須）",
    )
    preview_parser.add_argument(
        "--depth",
        type=int,
        default=None,
        help="クロール深度（1〜10。未指定時は設定値を使用）",
    )
    preview_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="出力フォーマット（text/json）",
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
        "--output",
        default=None,
        help="出力先ファイルパス（未指定時は標準出力）",
    )

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
        choices=["web", "bluesky", "zenn", "local"],
        default=None,
        help="対象媒体フィルタ（incremental では指定不可）",
    )

    # stats サブコマンド
    subparsers.add_parser("stats", help="ナレッジベースの統計情報を表示")

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
        choices=["web", "bluesky", "zenn", "local"],
        default=None,
        help="ソース種別フィルタ",
    )

    # delete サブコマンド
    delete_parser = subparsers.add_parser("delete", help="ソースをナレッジベースから論理削除")
    delete_parser.add_argument("source_id", help="削除するソース識別子（source_id）")

    # --- インジェスト系サブコマンド ---

    # add: 単一ページ取り込み
    add_parser = subparsers.add_parser("add", help="単一ページをナレッジベースに取り込む")
    add_parser.add_argument("url", help="取り込むページのURL")

    # crawl: リンク集クロール
    crawl_parser = subparsers.add_parser("crawl", help="リンク集ページからクロール＆一括取り込み")
    crawl_parser.add_argument("url", help="リンク集ページのURL")
    crawl_parser.add_argument("--pattern", default="", help="URLフィルタリング用の正規表現パターン（depth >= 2 の場合は必須）")
    crawl_parser.add_argument("--depth", type=int, default=None, help="クロール深度（1〜10。未指定時は設定値を使用）")

    # crawl-bluesky: BlueSky 取り込み
    bs_parser = subparsers.add_parser("crawl-bluesky", help="BlueSky 投稿を一括取り込み")
    bs_parser.add_argument("handle", help="BlueSky ハンドル（例: user.bsky.social）")
    bs_parser.add_argument("--max-posts", type=int, default=None, help="取得する最大投稿数")
    bs_parser.add_argument("--include-reposts", action="store_true", default=None, help="リポストを含める")

    # crawl-zenn: Zenn 取り込み
    zenn_parser = subparsers.add_parser("crawl-zenn", help="Zenn コンテンツを一括取り込み")
    zenn_parser.add_argument("username", help="Zenn ユーザー名")
    zenn_parser.add_argument("--max-articles", type=int, default=None, help="取得する最大コンテンツ数")
    zenn_parser.add_argument("--content-type", choices=["articles", "scraps", "all"], default="all", help="取得対象")

    # add-document: 単一ドキュメント取り込み
    adddoc_parser = subparsers.add_parser("add-document", help="ドキュメントファイルをナレッジベースに取り込む")
    adddoc_parser.add_argument("file_path", help="取り込み対象ファイルのパス")
    adddoc_parser.add_argument("--upload-mode", choices=["fail", "replace"], default="fail", help="同名ファイル存在時の動作")

    # crawl-documents: ディレクトリ一括取り込み
    crawldoc_parser = subparsers.add_parser("crawl-documents", help="ディレクトリ内ドキュメントを一括取り込み")
    crawldoc_parser.add_argument("dir_path", help="取り込み対象ディレクトリのパス")
    crawldoc_parser.add_argument("--pattern", default="**/*", help="glob パターン")
    crawldoc_parser.add_argument("--upload-mode", choices=["fail", "replace"], default="fail", help="同名ファイル存在時の動作")

    # site-ingest: Scrapy によるサイト一括取り込み
    siteingest_parser = subparsers.add_parser("site-ingest", help="Scrapy でサイトを一括取り込み（大規模サイト向け）")
    siteingest_parser.add_argument("url", help="クロール開始 URL")
    siteingest_parser.add_argument("--url-pattern", default="", help="URL フィルタパターン（正規表現）")
    siteingest_parser.add_argument("--max-pages", type=int, default=None, help="ページ数上限")
    siteingest_parser.add_argument("--force", action="store_true", help="JOBDIR を削除して再クロール")

    args = parser.parse_args()

    # コマンドディスパッチ（sync / async 統一）
    _ASYNC_COMMANDS: dict[str, object] = {
        "evaluate": run_evaluation,
        "init-test-db": init_test_db,
        "crawl-preview": run_crawl_preview,
        "add": run_add,
        "crawl": run_crawl,
        "crawl-bluesky": run_crawl_bluesky,
        "crawl-zenn": run_crawl_zenn,
        "add-document": run_add_document,
        "crawl-documents": run_crawl_documents,
        "site-ingest": run_site_ingest,
    }
    _SYNC_COMMANDS: dict[str, object] = {
        "get-document": run_get_document,
        "rebuild": run_rebuild,
        "stats": run_stats,
        "search": run_search,
        "delete": run_delete,
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
    from .web_crawler import WebCrawler

    settings = get_settings()
    embedding_provider = get_embedding_provider(settings, settings.embedding_provider)

    vector_store = VectorStore(
        embedding_provider=embedding_provider,
        persist_directory=persist_dir,
    )

    # WebCrawlerはダミー（評価時は使用しない）
    web_crawler = WebCrawler()

    return RAGKnowledgeService(
        vector_store=vector_store,
        web_crawler=web_crawler,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        similarity_threshold=threshold,
        bm25_index=bm25_index,
        hybrid_search_enabled=bm25_index is not None,
        vector_weight=vector_weight,
        min_combined_score=min_combined_score,
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
        for i, chunk in enumerate(chunks):
            documents.append((f"{url_hash}_{i}", chunk, normalized_url, "web"))

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

    フィクスチャ JSON → CrawledPage 変換 → _ingest_crawled_page() で投入。
    本番と同じチャンキングパスを通ることで、評価結果が本番動作を反映する。
    BM25インデックスも同一フィクスチャから構築・永続化する。

    Args:
        args: コマンドライン引数
    """
    from .web_crawler import CrawledPage

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

    # CrawledPage に変換
    pages: list[CrawledPage] = []
    for doc in documents:
        source_url = doc.get("source_url", "")
        content = doc.get("content", "")
        if not source_url or not content:
            logger.warning("Skipping document with missing source_url or content")
            continue
        pages.append(
            CrawledPage(
                url=source_url,
                title=doc.get("title", ""),
                text=content,
                crawled_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    # RAGKnowledgeService 経由で投入（本番と同じチャンキングパス）
    rag_service = await create_rag_service(
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        persist_dir=args.persist_dir,
    )
    total = 0
    for page in pages:
        count = await rag_service._ingest_crawled_page(page)
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


async def run_crawl_preview(args: argparse.Namespace) -> None:
    """クロール対象ページのプレビューを実行する.

    Args:
        args: コマンドライン引数
    """
    from .config import get_settings
    from .pipeline.ingesters.web import WebIngester
    from .store.source_store import SourceStore

    from py_common_lib.httpx import ConstrainedClient  # safety:allowed

    logger.info("Starting crawl preview for: %s", args.url)

    settings = get_settings()
    # crawl_preview は配置を行わないため、ディレクトリ作成のみ（DB 初期化不要）
    source_store_dir = Path(settings.source_store_dir)
    source_store_dir.mkdir(parents=True, exist_ok=True)
    source_store = SourceStore(source_store_dir)

    depth = args.depth if args.depth is not None else settings.rag_crawl_default_depth

    # depth >= 2 の場合は pattern 必須
    if depth >= 2 and not args.pattern:
        logger.error("depth が 2 以上の場合は --pattern の指定が必須です")
        sys.exit(1)

    # MSYS パス変換検出
    from .pipeline.ingesters.web import _looks_like_msys_path
    if args.pattern and _looks_like_msys_path(args.pattern):
        logger.error(
            "pattern が Windows パスに変換されています: %r。"
            "Git Bash 環境では先頭の / が自動変換されます。"
            "先頭の / を除去するか、MSYS_NO_PATHCONV=1 を設定してください",
            args.pattern,
        )
        sys.exit(1)

    web_ingester = WebIngester(
        source_store,
        max_crawl_pages=settings.rag_max_crawl_pages,
        crawl_request_timeout=settings.rag_crawl_request_timeout,
        crawl_max_errors=settings.rag_crawl_max_errors,
        respect_robots_txt=settings.rag_respect_robots_txt,
        robots_txt_cache_ttl=settings.rag_robots_txt_cache_ttl,
    )

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_crawl_request_timeout,
            request_interval=settings.rag_crawl_delay_sec,
        ) as client:
            pages = await web_ingester.crawl_preview(
                args.url, pattern=args.pattern, depth=depth, client=client,
            )
    except ValueError as e:
        logger.error("URL validation failed: %s", e)
        sys.exit(1)

    if not pages:
        print("対象ページが見つかりませんでした")
        return

    if args.format == "json":
        data = [{"title": p.get("title", ""), "url": p.get("url", "")} for p in pages]
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"クロール対象: {len(pages)}ページ")
        print()
        for i, page in enumerate(pages, start=1):
            title = page.get("title", "") or "(タイトル取得不可)"
            print(f"{i}. {title}")
            print(f"   {page.get('url', '')}")


def run_get_document(args: argparse.Namespace) -> None:
    """ドキュメント全文を取得して出力する.

    Args:
        args: コマンドライン引数
    """
    from .config import get_settings
    from .rag_knowledge import format_document_response, get_document

    settings = get_settings()

    result = get_document(
        source_id=args.source_id,
        format=args.format,
        source_store_dir=settings.source_store_dir,
        converted_store_dir=settings.converted_store_dir,
    )

    if result.error:
        print(f"エラー: {result.error}", file=sys.stderr)
        sys.exit(1)

    response = format_document_response(result)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(response, encoding="utf-8")
        print(f"出力しました: {args.output}")
    else:
        print(response)


def run_rebuild(args: argparse.Namespace) -> None:
    """再構築を実行する.

    仕様: docs/specs/rebuild-stats.md

    Args:
        args: コマンドライン引数
    """
    import contextlib
    import io
    import time

    from .bm25_index import BM25Index
    from .config import get_settings
    from .converter import Converter
    from .embedding.factory import get_embedding_provider
    from .indexer import Indexer
    from .ingesters.document_ingester import PdfBackendConfig
    from .pipeline.controller import PipelineController
    from .store.models import SourceType
    from .store.source_store import SourceStore
    from .vector_store import VectorStore

    mode: str = args.mode
    source_type: SourceType | None = args.source_type

    # incremental + source_type のバリデーション
    if mode == "incremental" and source_type is not None:
        logger.error(
            "incremental モードでは source_type を指定できません"
        )
        sys.exit(1)

    settings = get_settings()

    if not settings.source_store_dir:
        logger.error("SOURCE_STORE_DIR が設定されていません")
        sys.exit(1)
    if not settings.converted_store_dir:
        logger.error("CONVERTED_STORE_DIR が設定されていません")
        sys.exit(1)

    source_store_dir = Path(settings.source_store_dir)
    if not source_store_dir.exists():
        logger.error(
            "source_store ディレクトリが存在しません: %s", source_store_dir,
        )
        sys.exit(1)

    converted_store_dir = Path(settings.converted_store_dir)
    converted_store_dir.mkdir(parents=True, exist_ok=True)

    source_store = SourceStore(source_store_dir)
    source_store.db.initialize()

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

    controller = PipelineController(
        source_store=source_store,
        converted_store_dir=converted_store_dir,
        converter=converter,
        indexer=indexer,
    )

    logger.info("再構築を開始します（モード: %s）", mode)
    if source_type:
        logger.info("対象媒体: %s", source_type)

    start = time.monotonic()

    if mode == "full":
        summary = controller.run_full_rebuild(source_type=source_type)
    elif mode == "convert":
        summary = controller.run_convert_only(source_type=source_type)
    elif mode == "index":
        summary = controller.run_index_only(source_type=source_type)
    else:
        summary = controller.run_incremental()

    elapsed = time.monotonic() - start

    logger.info(
        "再構築完了: %d 処理 / %d スキップ / %d エラー / %.1f 秒",
        summary.processed,
        summary.skipped,
        len(summary.errors),
        elapsed,
    )
    if summary.errors:
        for err_file in summary.errors:
            logger.error("  エラーファイル: %s", err_file)


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

    settings = get_settings()

    parts: list[str] = ["RAG Knowledge 統計"]

    # --- source_store セクション ---
    parts.append("")
    parts.append("■ source_store")
    if not settings.source_store_dir:
        parts.append("  未設定")
    else:
        source_store_dir = Path(settings.source_store_dir)
        if not source_store_dir.exists():
            parts.append("  ディレクトリが存在しません")
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

            parts.append(f"  総ファイル数: {total_files:,}")
            parts.append(f"  総サイズ: {_format_cli_size(total_size)}")
            if by_type:
                parts.append("  媒体別:")
                for st_key in sorted(by_type.keys()):
                    info = by_type[st_key]
                    parts.append(
                        f"    {st_key}: {info['files']} files"
                        f" ({_format_cli_size(info['size'])})"
                    )

    # --- converted_store セクション ---
    parts.append("")
    parts.append("■ converted_store")
    if not settings.converted_store_dir:
        parts.append("  未設定")
    else:
        cs_dir = Path(settings.converted_store_dir)
        if not cs_dir.exists():
            parts.append("  ディレクトリが存在しません")
        else:
            cs_files = 0
            cs_size = 0
            for file in cs_dir.rglob("*"):
                if file.is_file():
                    cs_files += 1
                    cs_size += file.stat().st_size
            parts.append(f"  総ファイル数: {cs_files:,}")
            parts.append(f"  総サイズ: {_format_cli_size(cs_size)}")

    # --- インデックスセクション ---
    parts.append("")
    parts.append("■ インデックス")
    try:
        embedding_provider = get_embedding_provider(settings, settings.embedding_provider)
        with contextlib.redirect_stdout(io.StringIO()):
            vector_store = VectorStore(
                embedding_provider=embedding_provider,
                persist_directory=settings.chromadb_persist_dir,
            )
        index_stats = vector_store.get_stats()
        total_chunks = int(str(index_stats.get("total_chunks", 0)))
        source_count = int(str(index_stats.get("source_count", 0)))
        parts.append(f"  総チャンク数: {total_chunks:,}")
        parts.append(f"  ソース数: {source_count:,}")
    except Exception:
        logger.exception("インデックス統計の取得に失敗")
        parts.append("  エラー: 統計の取得に失敗しました")

    # --- パイプラインセクション ---
    parts.append("")
    parts.append("■ パイプライン")
    if not settings.source_store_dir:
        parts.append("  未設定")
    else:
        from .store.models import NULL_COMMIT_HASH
        db_path = Path(settings.source_store_dir) / "metadata.db"
        if not db_path.exists():
            parts.append("  未初期化")
        else:
            db = MetadataDB(db_path)
            try:
                db.initialize()
                history = db.get_pipeline_history()
                last_commit_id = db.get_last_commit_id()
                deleted_count = db.source_count(status="deleted")
                last_at = history[-1].processed_at if history else "（未実行）"
                parts.append(f"  最終処理: {last_at}")
                parts.append(f"  実行回数: {len(history)}")
                commit_str = str(last_commit_id)
                if commit_str == NULL_COMMIT_HASH:
                    parts.append("  last_commit_id: （未実行）")
                else:
                    parts.append(f"  last_commit_id: {commit_str[:7]}")
                parts.append(f"  論理削除: {deleted_count} 件")
            finally:
                db.close()

    print("\n".join(parts))


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
    from .web_crawler import WebCrawler

    settings = get_settings()
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

    service = RAGKnowledgeService(
        vector_store=vector_store,
        web_crawler=WebCrawler(),
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
        similarity_threshold=None,
        bm25_index=bm25_index,
        hybrid_search_enabled=True,
        vector_weight=settings.rag_vector_weight,
    )

    n_results = args.n_results if args.n_results is not None else settings.rag_retrieval_count
    source_type: str | None = args.source_type

    raw = _asyncio.run(
        service.retrieve_raw_results(
            args.query, n_results=n_results, source_type=source_type,
        )
    )

    if not raw.vector_results and not raw.bm25_results:
        print("該当する情報が見つかりませんでした")
        return

    parts: list[str] = []

    if raw.vector_results:
        parts.append("## ベクトル検索結果 (意味的類似度)\n")
        for i, item in enumerate(raw.vector_results, start=1):
            chunk_pos = _format_cli_chunk_position(item.chunk_index, item.total_chunks)
            parts.append(f"### Result {i} [distance={item.distance:.3f}]")
            parts.append(f"Source: {item.source_url}")
            parts.append(f"Title: {item.title}")
            parts.append(f"Chunk: {chunk_pos}")
            parts.append(f"Type: {item.source_type}")
            if item.collected_at:
                parts.append(f"Collected: {item.collected_at}")
            parts.append("")
            parts.append(item.text)
            parts.append("")

    if raw.bm25_results:
        parts.append("## BM25 検索結果 (キーワード一致)\n")
        for i, bm25_item in enumerate(raw.bm25_results, start=1):
            chunk_pos = _format_cli_chunk_position(bm25_item.chunk_index, bm25_item.total_chunks)
            parts.append(f"### Result {i} [score={bm25_item.score:.3f}]")
            parts.append(f"Source: {bm25_item.source_url}")
            parts.append(f"Title: {bm25_item.title}")
            parts.append(f"Chunk: {chunk_pos}")
            parts.append(f"Type: {bm25_item.source_type}")
            if bm25_item.collected_at:
                parts.append(f"Collected: {bm25_item.collected_at}")
            parts.append("")
            parts.append(bm25_item.text)
            parts.append("")

    print("\n".join(parts).rstrip())


def run_delete(args: argparse.Namespace) -> None:
    """ソースをナレッジベースから削除する.

    MCP ツール rag_delete と同等の削除を CLI で実行する。
    ファイルを物理削除し、パイプライン経由でインデックス・metadata.db を更新する。

    Args:
        args: コマンドライン引数
    """
    controller, _settings = _build_cli_pipeline_controller()
    source_id: str = args.source_id

    try:
        controller.source_store.remove_file(source_id)
    except KeyError:
        print(f"該当するソースが見つかりませんでした: {source_id}", file=sys.stderr)
        sys.exit(1)

    try:
        summary = controller.ingest_and_index(f"delete: {source_id}")
    except Exception:
        logger.exception("削除パイプライン実行に失敗: %s", source_id)
        print(
            f"エラー: 削除に失敗しました: {source_id}",
            file=sys.stderr,
        )
        sys.exit(1)

    if summary.errors:
        print(f"警告: パイプラインでエラーが発生しました: {source_id}", file=sys.stderr)
        for err in summary.errors:
            print(f"  - {err}", file=sys.stderr)
    print(f"削除しました: {source_id}")


def _format_cli_size(size_bytes: int) -> str:
    """バイト数を人間が読みやすい単位に変換する."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _format_cli_chunk_position(chunk_index: int, total_chunks: int) -> str:
    """チャンク位置を表示用にフォーマットする."""
    if total_chunks > 0:
        return f"{chunk_index + 1}/{total_chunks}"
    return str(chunk_index + 1)


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
    from .embedding.factory import get_embedding_provider
    from .indexer import Indexer
    from .ingesters.document_ingester import PdfBackendConfig
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

    controller = PipelineController(
        source_store=source_store,
        converted_store_dir=converted_store_dir,
        converter=converter,
        indexer=indexer,
    )
    return controller, settings


def _get_safe_browsing_api_key_for_cli(settings: "Settings") -> str:
    """CLI 用: Safe Browsing API キーを取得する."""
    from py_common_lib.secrets import SecretNotFoundError, SecretStoreError, get_secret

    if not settings.rag_url_safety_check:
        return ""
    try:
        return get_secret("GOOGLE_SAFE_BROWSING_API_KEY", service="rag-knowledge") or ""
    except (SecretNotFoundError, SecretStoreError):
        logger.warning("Safe Browsing API key not available")
        return ""


def _print_ingest_result(
    ingest_result: "IngestResult",
    pipeline_summary: "PipelineSummary | None",
    *,
    context: str = "",
) -> None:
    """取り込み結果を標準出力に表示する."""
    print(ingest_result.summary(context=context))
    if pipeline_summary is not None:
        print(f"パイプライン: {pipeline_summary.processed}件処理")
        if pipeline_summary.errors:
            print(f"パイプラインエラー: {len(pipeline_summary.errors)}件")


async def run_add(args: argparse.Namespace) -> None:
    """単一ページ取り込み."""
    from .pipeline.ingesters.web import WebIngester

    from py_common_lib.httpx import ConstrainedClient  # safety:allowed

    controller, settings = _build_cli_pipeline_controller()
    web_ingester = WebIngester(
        controller.source_store,
        max_crawl_pages=settings.rag_max_crawl_pages,
        crawl_request_timeout=settings.rag_crawl_request_timeout,
        respect_robots_txt=settings.rag_respect_robots_txt,
        robots_txt_cache_ttl=settings.rag_robots_txt_cache_ttl,
        url_safety_check=settings.rag_url_safety_check,
        url_safety_cache_ttl=settings.rag_url_safety_cache_ttl,
        url_safety_fail_open=settings.rag_url_safety_fail_open,
        url_safety_timeout=settings.rag_url_safety_timeout,
    )
    api_key = _get_safe_browsing_api_key_for_cli(settings)

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_crawl_request_timeout,
            request_interval=settings.rag_crawl_delay_sec,
        ) as client:
            ingest_result = await web_ingester.add(
                args.url, client=client, safe_browsing_api_key=api_key,
            )
    except ValueError as e:
        logger.error("エラー: %s", e)
        sys.exit(1)

    pipeline_summary = controller.ingest_and_index(f"ingest(web): add {args.url}")
    _print_ingest_result(ingest_result, pipeline_summary, context=args.url)


async def run_crawl(args: argparse.Namespace) -> None:
    """リンク集クロール."""
    from .pipeline.ingesters.web import WebIngester

    from py_common_lib.httpx import ConstrainedClient  # safety:allowed

    controller, settings = _build_cli_pipeline_controller()

    depth = args.depth if args.depth is not None else settings.rag_crawl_default_depth

    # depth >= 2 の場合は pattern 必須
    if depth >= 2 and not args.pattern:
        logger.error("depth が 2 以上の場合は --pattern の指定が必須です")
        sys.exit(1)

    # MSYS パス変換検出
    from .pipeline.ingesters.web import _looks_like_msys_path
    if args.pattern and _looks_like_msys_path(args.pattern):
        logger.error(
            "pattern が Windows パスに変換されています: %r。"
            "Git Bash 環境では先頭の / が自動変換されます。"
            "先頭の / を除去するか、MSYS_NO_PATHCONV=1 を設定してください",
            args.pattern,
        )
        sys.exit(1)

    web_ingester = WebIngester(
        controller.source_store,
        max_crawl_pages=settings.rag_max_crawl_pages,
        crawl_request_timeout=settings.rag_crawl_request_timeout,
        crawl_max_errors=settings.rag_crawl_max_errors,
        respect_robots_txt=settings.rag_respect_robots_txt,
        robots_txt_cache_ttl=settings.rag_robots_txt_cache_ttl,
        url_safety_check=settings.rag_url_safety_check,
        url_safety_cache_ttl=settings.rag_url_safety_cache_ttl,
        url_safety_fail_open=settings.rag_url_safety_fail_open,
        url_safety_timeout=settings.rag_url_safety_timeout,
    )
    api_key = _get_safe_browsing_api_key_for_cli(settings)

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_crawl_request_timeout,
            request_interval=settings.rag_crawl_delay_sec,
        ) as client:
            ingest_result = await web_ingester.crawl(
                args.url, pattern=args.pattern, depth=depth, client=client,
                safe_browsing_api_key=api_key,
            )
    except ValueError as e:
        logger.error("エラー: %s", e)
        sys.exit(1)

    pipeline_summary = controller.ingest_and_index(f"ingest(web): crawl {args.url}")
    _print_ingest_result(ingest_result, pipeline_summary, context=args.url)


async def run_crawl_bluesky(args: argparse.Namespace) -> None:
    """BlueSky 投稿取り込み."""
    from .pipeline.ingesters.bluesky import BlueskyIngester

    from py_common_lib.httpx import ConstrainedClient  # safety:allowed

    controller, settings = _build_cli_pipeline_controller()

    max_posts = args.max_posts if args.max_posts is not None else settings.rag_bluesky_max_posts
    include_reposts = args.include_reposts if args.include_reposts is not None else settings.rag_bluesky_include_reposts

    bluesky_ingester = BlueskyIngester(
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
                args.handle,
                max_posts=max_posts,
                include_reposts=include_reposts,
                client=client,
            )
    except (ValueError, TypeError) as e:
        logger.error("エラー: %s", e)
        sys.exit(1)

    pipeline_summary = controller.ingest_and_index(f"ingest(bluesky): {args.handle}")
    _print_ingest_result(ingest_result, pipeline_summary, context=f"ハンドル: {args.handle}")


async def run_crawl_zenn(args: argparse.Namespace) -> None:
    """Zenn コンテンツ取り込み."""
    from .pipeline.ingesters.zenn import ZennIngester

    from py_common_lib.httpx import ConstrainedClient  # safety:allowed

    controller, settings = _build_cli_pipeline_controller()

    max_articles = args.max_articles if args.max_articles is not None else settings.rag_zenn_max_articles

    zenn_ingester = ZennIngester(
        controller.source_store,
        max_articles=max_articles,
    )

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_zenn_request_timeout,
            request_interval=settings.rag_zenn_request_interval,
        ) as client:
            ingest_result = await zenn_ingester.crawl_zenn(
                args.username,
                max_articles=max_articles,
                content_type=args.content_type,
                client=client,
            )
    except (ValueError, TypeError) as e:
        logger.error("エラー: %s", e)
        sys.exit(1)

    pipeline_summary = controller.ingest_and_index(f"ingest(zenn): {args.username}")
    _print_ingest_result(ingest_result, pipeline_summary, context=f"ユーザー: {args.username}")


async def run_add_document(args: argparse.Namespace) -> None:
    """単一ドキュメント取り込み."""
    from .pipeline.ingesters.local import LocalIngester

    controller, settings = _build_cli_pipeline_controller()

    supported_extensions = [
        ext.strip() if ext.strip().startswith(".") else f".{ext.strip()}"
        for ext in settings.rag_document_supported_extensions.split(",")
        if ext.strip()
    ]
    local_ingester = LocalIngester(
        controller.source_store,
        supported_extensions=supported_extensions,
    )

    ingest_result = local_ingester.add_document(
        args.file_path, upload_mode=args.upload_mode,
    )

    if ingest_result.placed == 0 and ingest_result.errors == 0:
        print(f"取り込み対象がありませんでした: {args.file_path}")
        return
    if ingest_result.errors > 0:
        print(f"エラー: {ingest_result.error_details[0]}", file=sys.stderr)
        raise SystemExit(1)

    pipeline_summary = controller.ingest_and_index(f"ingest(local): add {args.file_path}")
    _print_ingest_result(ingest_result, pipeline_summary, context=args.file_path)


async def run_crawl_documents(args: argparse.Namespace) -> None:
    """ディレクトリ一括取り込み."""
    from .pipeline.ingesters.local import LocalIngester

    controller, settings = _build_cli_pipeline_controller()

    supported_extensions = [
        ext.strip() if ext.strip().startswith(".") else f".{ext.strip()}"
        for ext in settings.rag_document_supported_extensions.split(",")
        if ext.strip()
    ]
    local_ingester = LocalIngester(
        controller.source_store,
        supported_extensions=supported_extensions,
    )

    ingest_result = local_ingester.crawl_documents(
        args.dir_path, args.pattern, upload_mode=args.upload_mode,
    )

    if ingest_result.placed == 0 and ingest_result.errors == 0:
        print(f"対象ファイルが見つかりませんでした: {args.dir_path}")
        return
    if ingest_result.errors > 0 and ingest_result.placed == 0:
        print(f"エラー: {ingest_result.error_details[0]}", file=sys.stderr)
        raise SystemExit(1)

    pipeline_summary = controller.ingest_and_index(f"ingest(local): crawl {args.dir_path}")
    _print_ingest_result(ingest_result, pipeline_summary, context=f"ディレクトリ: {args.dir_path}")


async def run_site_ingest(args: argparse.Namespace) -> None:
    """Scrapy によるサイト一括取り込み."""
    import re
    import time as time_mod

    from .pipeline.ingesters.web import _check_ssrf, _validate_url
    from .scrapy.bridge import import_to_source_store
    from .scrapy.runner import ScrapyRunner

    # URL バリデーション
    try:
        url = _validate_url(args.url)
        _check_ssrf(url)
    except ValueError as e:
        logger.error("エラー: %s", e)
        sys.exit(1)

    # url_pattern バリデーション
    if args.url_pattern:
        try:
            re.compile(args.url_pattern)
        except re.error as e:
            logger.error("無効な正規表現パターン: %s", e)
            sys.exit(1)

    controller, settings = _build_cli_pipeline_controller()

    # max_pages のクランプ
    effective_max_pages = (
        args.max_pages if args.max_pages is not None
        else settings.site_ingest_max_pages
    )
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

    start_time = time_mod.monotonic()

    # Scrapy Runner で クロール
    runner = ScrapyRunner(
        temp_dir=settings.site_ingest_temp_dir,
        delay_sec=settings.site_ingest_delay_sec,
        max_pages=effective_max_pages,
        download_timeout=settings.site_ingest_download_timeout,
    )

    crawl_result = await runner.run(
        start_url=url,
        allowed_domains=allowed_domains,
        url_pattern=args.url_pattern,
        max_pages=effective_max_pages,
        force=args.force,
    )

    elapsed = time_mod.monotonic() - start_time

    if not crawl_result.jsonl_path.exists():
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
    if bridge_result.ingest.placed > 0:
        pipeline_summary = controller.ingest_and_index(f"ingest(web): site-ingest {url}")

    # 結果表示
    print(
        f"サイト取り込み完了: {bridge_result.ingest.placed}件配置"
        f", {bridge_result.ingest.skipped}件スキップ"
        f", {bridge_result.ingest.errors}件エラー"
    )
    print(f"所要時間: {elapsed:.1f}秒")
    if pipeline_summary is not None:
        print(f"パイプライン: {pipeline_summary.processed}件処理")
        if pipeline_summary.errors:
            print(f"パイプラインエラー: {len(pipeline_summary.errors)}件")
    if not crawl_result.success:
        print(f"Scrapy exit_code={crawl_result.exit_code}（部分的な結果）")


if __name__ == "__main__":
    from .config import ensure_utf8_streams

    ensure_utf8_streams(include_stdout=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    main()

"""RAG評価CLIモジュール

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
    evaluate_retrieval,
)

if TYPE_CHECKING:
    from .bm25_index import BM25Index
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
    parser = argparse.ArgumentParser(description="RAG評価CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # evaluate サブコマンド
    eval_parser = subparsers.add_parser("evaluate", help="RAG検索精度を評価")
    eval_parser.add_argument(
        "--dataset",
        default="tests/fixtures/rag_evaluation_dataset.json",
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
        default="tests/fixtures/rag_test_documents.json",
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
        default="tests/fixtures/rag_test_documents.json",
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

    args = parser.parse_args()

    if args.command == "evaluate":
        asyncio.run(run_evaluation(args))
    elif args.command == "init-test-db":
        asyncio.run(init_test_db(args))


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
    documents: list[tuple[str, str, str]] = []
    for doc in fixture_data.get("documents", []):
        source_url = doc.get("source_url", "")
        content = doc.get("content", "")
        if not source_url or not content:
            continue
        chunks = smart_chunk(content, chunk_size, chunk_overlap)
        normalized_url, _ = urldefrag(source_url)
        url_hash = hashlib.sha256(normalized_url.encode()).hexdigest()[:16]
        for i, chunk in enumerate(chunks):
            documents.append((f"{url_hash}_{i}", chunk, normalized_url))

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


if __name__ == "__main__":
    from .config import ensure_utf8_streams

    ensure_utf8_streams(include_stdout=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    main()

"""Embeddingプレフィックスあり/なし比較スクリプト.

Embeddingモデルのタスク固有プレフィックス（検索文書: / 検索クエリ:）が
検索品質に与える影響を計測する。

Usage:
    # テスト用ChromaDBの初期化（初回のみ）
    uv run python -m rag.cli init-test-db \
      --persist-dir .tmp/prefix-comparison/chroma_db_no_prefix
    uv run python -m rag.cli init-test-db \
      --persist-dir .tmp/prefix-comparison/chroma_db_with_prefix

    # 比較実行
    uv run python scripts/embedding_prefix_comparison.py

    # 拡充データで比較
    uv run python scripts/embedding_prefix_comparison.py \
      --dataset tests/fixtures/rag_evaluation_extended/rag_evaluation_dataset_extended.json \
      --fixture tests/fixtures/rag_evaluation_extended/rag_test_documents_extended.json

Note:
    LM Studio が起動中で Embeddingモデルがロード済みである必要がある。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.cli import _ingest_page_for_testing
from rag.config import get_settings
from rag.embedding.factory import get_embedding_provider
from rag.evaluation import evaluate_retrieval
from rag.search.search_port import RealSearchAdapter
from rag.vector_store import VectorStore

OUTPUT_DIR = Path(".tmp/prefix-comparison")
DEFAULT_DATASET = "tests/fixtures/rag_evaluation_dataset.json"
DEFAULT_FIXTURE = "tests/fixtures/rag_test_documents.json"

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@dataclass
class ComparisonResult:
    """比較結果."""

    label: str
    avg_f1: float
    avg_precision: float
    avg_recall: float
    avg_ndcg: float
    avg_mrr: float


def _load_fixture_pages(fixture_path: str) -> list[dict[str, str]]:
    """フィクスチャからページデータのリストを生成する."""
    with open(fixture_path, encoding="utf-8") as f:
        fixture_data = json.load(f)

    pages: list[dict[str, str]] = []
    for doc in fixture_data.get("documents", []):
        source_url = doc.get("source_url", "")
        content = doc.get("content", "")
        if not source_url or not content:
            continue
        pages.append({
            "url": source_url,
            "title": doc.get("title", ""),
            "text": content,
            "crawled_at": "2026-01-01T00:00:00Z",
        })
    return pages


async def build_and_evaluate(
    label: str,
    prefix_enabled: bool,
    fixture_path: str,
    dataset_path: str,
    persist_dir: str,
    hybrid: bool = False,
) -> ComparisonResult:
    """指定設定でインデックス構築→評価を実行."""
    # 環境変数で prefix_enabled を制御
    original = os.environ.get("EMBEDDING_PREFIX_ENABLED")
    os.environ["EMBEDDING_PREFIX_ENABLED"] = str(prefix_enabled).lower()
    get_settings.cache_clear()

    try:
        settings = get_settings()
        embedding_provider = get_embedding_provider(settings, settings.embedding_provider)

        # インデックス構築（本番と同じ _ingest_crawled_page 経由）
        vector_store = VectorStore(
            embedding_provider=embedding_provider,
            persist_directory=persist_dir,
        )
        pages = _load_fixture_pages(fixture_path)
        logger.info("[%s] Indexing %d documents...", label, len(pages))

        # BM25インデックス構築（ハイブリッドモード時のみ）
        from rag.bm25_index import BM25Index

        bm25_index: BM25Index | None = None
        if hybrid:
            bm25_index = BM25Index()

        # 評価フィクスチャ投入経路（init-test-db と同じ smart_chunking を使用）
        for page in pages:
            await _ingest_page_for_testing(
                vector_store=vector_store,
                chunk_size=settings.rag_chunk_size,
                chunk_overlap=settings.rag_chunk_overlap,
                **page,
            )

        # SearchPort 構築
        search = RealSearchAdapter(
            vector_store=vector_store,
            bm25_index=bm25_index,
            similarity_threshold=settings.rag_similarity_threshold,
            hybrid_search_enabled=hybrid,
            vector_weight=settings.rag_vector_weight,
            min_combined_score=None,
            debug_log_enabled=settings.rag_debug_log_enabled,
        )

        # 評価実行
        logger.info("[%s] Evaluating...", label)
        report = await evaluate_retrieval(
            search=search,
            dataset_path=dataset_path,
            n_results=5,
        )

        return ComparisonResult(
            label=label,
            avg_f1=report.average_f1,
            avg_precision=report.average_precision,
            avg_recall=report.average_recall,
            avg_ndcg=report.average_ndcg,
            avg_mrr=report.average_mrr,
        )
    finally:
        if original is None:
            os.environ.pop("EMBEDDING_PREFIX_ENABLED", None)
        else:
            os.environ["EMBEDDING_PREFIX_ENABLED"] = original
        get_settings.cache_clear()


def print_results(results: list[ComparisonResult]) -> None:
    """結果テーブルを表示."""
    print("\n" + "=" * 80)
    print("Embedding Prefix Comparison Results")
    print("=" * 80)
    header = f"{'Config':<25} | {'F1':>6} | {'Prec':>6} | {'Rec':>6} | {'NDCG':>6} | {'MRR':>6}"
    print(header)
    print("-" * len(header))

    best_f1 = max(r.avg_f1 for r in results)
    for r in results:
        marker = " ***" if r.avg_f1 == best_f1 else ""
        print(
            f"{r.label:<25} | {r.avg_f1:>6.3f} | {r.avg_precision:>6.3f} | "
            f"{r.avg_recall:>6.3f} | {r.avg_ndcg:>6.3f} | {r.avg_mrr:>6.3f}{marker}"
        )

    # 差分表示
    if len(results) == 2:
        no_prefix, with_prefix = results[0], results[1]
        print("\n--- Difference (with_prefix - no_prefix) ---")
        print(f"  F1:        {with_prefix.avg_f1 - no_prefix.avg_f1:+.3f}")
        print(f"  Precision: {with_prefix.avg_precision - no_prefix.avg_precision:+.3f}")
        print(f"  Recall:    {with_prefix.avg_recall - no_prefix.avg_recall:+.3f}")
        print(f"  NDCG:      {with_prefix.avg_ndcg - no_prefix.avg_ndcg:+.3f}")
        print(f"  MRR:       {with_prefix.avg_mrr - no_prefix.avg_mrr:+.3f}")


async def main_async(args: argparse.Namespace) -> None:
    """メイン処理."""
    dataset_path = args.dataset
    fixture_path = args.fixture
    hybrid = args.hybrid

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    no_prefix_dir = str(OUTPUT_DIR / "chroma_db_no_prefix")
    with_prefix_dir = str(OUTPUT_DIR / "chroma_db_with_prefix")

    mode_label = "hybrid" if hybrid else "vector-only"
    logger.info("Search mode: %s", mode_label)

    results: list[ComparisonResult] = []

    # プレフィックスなし
    result_no_prefix = await build_and_evaluate(
        label=f"no_prefix ({mode_label})",
        prefix_enabled=False,
        fixture_path=fixture_path,
        dataset_path=dataset_path,
        persist_dir=no_prefix_dir,
        hybrid=hybrid,
    )
    results.append(result_no_prefix)

    # プレフィックスあり
    result_with_prefix = await build_and_evaluate(
        label=f"with_prefix ({mode_label})",
        prefix_enabled=True,
        fixture_path=fixture_path,
        dataset_path=dataset_path,
        persist_dir=with_prefix_dir,
        hybrid=hybrid,
    )
    results.append(result_with_prefix)

    print_results(results)

    # JSON出力
    output = {
        "results": [
            {
                "label": r.label,
                "avg_f1": r.avg_f1,
                "avg_precision": r.avg_precision,
                "avg_recall": r.avg_recall,
                "avg_ndcg": r.avg_ndcg,
                "avg_mrr": r.avg_mrr,
            }
            for r in results
        ],
        "recommendation": max(results, key=lambda r: r.avg_f1).label,
    }
    output_path = OUTPUT_DIR / "prefix_comparison.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\nResults saved to: {output_path}")


def main() -> None:
    """エントリポイント."""
    parser = argparse.ArgumentParser(description="Embedding Prefix Comparison")
    parser.add_argument(
        "--dataset",
        type=str,
        default=DEFAULT_DATASET,
        help=f"評価データセットのパス（デフォルト: {DEFAULT_DATASET}）",
    )
    parser.add_argument(
        "--fixture",
        type=str,
        default=DEFAULT_FIXTURE,
        help=f"テストドキュメントフィクスチャのパス（デフォルト: {DEFAULT_FIXTURE}）",
    )
    parser.add_argument(
        "--hybrid",
        action="store_true",
        default=False,
        help="ハイブリッド検索を有効にする（デフォルト: ベクトル検索のみ）",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

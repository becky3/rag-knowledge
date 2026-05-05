"""パラメータスイープスクリプト.

Phase 1: vector_weight (α) を 0.0〜1.0 で 0.1 刻みにスイープ（threshold=None）
Phase 2: Phase 1 のベスト α で threshold を 0.4〜0.8 でスイープ
Phase 3: Phase 2 のベスト α/threshold で n_results を代表値 [3, 5, 7, 10, 15, 20] でスイープ
Phase 4: Phase 3 のベスト n_results で BM25 k1/b をグリッドサーチ
Phase 5: Phase 4 のベスト k1/b で min_combined_score をスイープ

Usage:
    # テスト用ChromaDBの初期化（初回のみ）
    uv run python -m rag.cli init-test-db --persist-dir .tmp/rag-evaluation/chroma_db_test

    # 全Phase自動実行
    uv run python scripts/parameter_sweep.py \
      --chunk-size 200 --chunk-overlap 30

    # 個別Phase実行
    uv run python scripts/parameter_sweep.py --phase 1 \
      --chunk-size 200 --chunk-overlap 30
    uv run python scripts/parameter_sweep.py --phase 2 --best-alpha 0.6 \
      --chunk-size 200 --chunk-overlap 30
    uv run python scripts/parameter_sweep.py --phase 3 --best-alpha 0.6 --best-threshold none \
      --chunk-size 200 --chunk-overlap 30
    uv run python scripts/parameter_sweep.py --phase 4 --best-alpha 0.6 --best-threshold none \
      --best-n-results 3 --chunk-size 200 --chunk-overlap 30
    uv run python scripts/parameter_sweep.py --phase 5 --best-alpha 0.9 --best-threshold none \
      --best-n-results 3 --best-k1 2.5 --best-b 0.50 \
      --chunk-size 200 --chunk-overlap 30

    # 拡充データでスイープ実行
    uv run python scripts/parameter_sweep.py \
      --dataset tests/fixtures/rag_evaluation_extended/rag_evaluation_dataset_extended.json \
      --fixture tests/fixtures/rag_evaluation_extended/rag_test_documents_extended.json \
      --persist-dir .tmp/rag-evaluation-extended/chroma_db_test \
      --chunk-size 200 --chunk-overlap 30
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.cli import (
    _build_bm25_index_from_fixture,
    create_search_adapter,
)
from rag.evaluation import evaluate_retrieval

# 一時ファイル出力先
OUTPUT_DIR = Path(".tmp/rag-evaluation")
DEFAULT_PERSIST_DIR = str(OUTPUT_DIR / "chroma_db_test")

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@dataclass
class SweepResult:
    """スイープ1回分の結果."""

    vector_weight: float
    threshold: float | None
    avg_f1: float
    avg_precision: float
    avg_recall: float
    avg_ndcg: float
    avg_mrr: float
    negative_violations: int
    category_f1: dict[str, float]
    n_results: int = 5
    k1: float = 1.5
    b: float = 0.75
    min_combined_score: float | None = None


DEFAULT_DATASET = "tests/fixtures/rag_evaluation_dataset.json"
DEFAULT_FIXTURE = "tests/fixtures/rag_test_documents.json"


@functools.lru_cache(maxsize=4)
def load_query_categories(dataset_path: str) -> dict[str, str]:
    """評価データセットJSONからカテゴリマッピングを動的に読み取る."""
    with open(dataset_path, encoding="utf-8") as f:
        data = json.load(f)
    queries = data.get("queries", [])
    missing = [q.get("id", "<unknown>") for q in queries if "category" not in q]
    if missing:
        msg = f"クエリに category が設定されていません: ids={missing}"
        raise ValueError(msg)
    return {q["id"]: q["category"] for q in queries}


async def run_single_evaluation(
    vector_weight: float,
    threshold: float | None,
    dataset_path: str,
    fixture_path: str,
    chunk_size: int,
    chunk_overlap: int,
    n_results: int = 5,
    persist_dir: str = DEFAULT_PERSIST_DIR,
    k1: float = 1.5,
    b: float = 0.75,
    min_combined_score: float | None = None,
) -> SweepResult:
    """単一パラメータセットで評価を実行."""
    bm25_index = _build_bm25_index_from_fixture(
        fixture_path, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
        k1=k1, b=b,
    )
    search = await create_search_adapter(
        threshold=threshold,
        bm25_index=bm25_index,
        vector_weight=vector_weight,
        persist_dir=persist_dir,
        min_combined_score=min_combined_score,
    )

    report = await evaluate_retrieval(
        search=search,
        dataset_path=dataset_path,
        n_results=n_results,
    )

    # カテゴリ別F1を計算（JSONから動的に読み取ったカテゴリを使用）
    query_categories = load_query_categories(dataset_path)
    category_scores: dict[str, list[float]] = {}
    for qr in report.query_results:
        cat = query_categories.get(qr.query_id, "unknown")
        if cat not in category_scores:
            category_scores[cat] = []
        category_scores[cat].append(qr.f1)

    category_f1 = {
        cat: sum(scores) / len(scores) for cat, scores in category_scores.items()
    }

    return SweepResult(
        vector_weight=vector_weight,
        threshold=threshold,
        avg_f1=report.average_f1,
        avg_precision=report.average_precision,
        avg_recall=report.average_recall,
        avg_ndcg=report.average_ndcg,
        avg_mrr=report.average_mrr,
        negative_violations=len(report.negative_source_violations),
        category_f1=category_f1,
        n_results=n_results,
        k1=k1,
        b=b,
        min_combined_score=min_combined_score,
    )


def _format_category_cols(cat: dict[str, float]) -> str:
    """カテゴリ別F1のカラム文字列を生成."""
    return (
        f"{cat.get('normal', 0):>7.3f} | {cat.get('close_ranking', 0):>7.3f} | "
        f"{cat.get('method_mismatch', 0):>8.3f} | {cat.get('semantic_only', 0):>8.3f} | "
        f"{cat.get('noise_rejection', 0):>7.3f} | {cat.get('keyword_exact', 0):>7.3f}"
    )


CATEGORY_HEADER = (
    f"{'normal':>7} | {'close':>7} | {'mismatch':>8} | "
    f"{'semantic':>8} | {'noise':>7} | {'keyword':>7}"
)
METRIC_HEADER = f"{'F1':>6} | {'Prec':>6} | {'Rec':>6} | {'NDCG':>6} | {'MRR':>6} | {'NegV':>4}"


def print_results_table(results: list[SweepResult], phase: int) -> None:
    """結果テーブルを表示."""
    if phase == 1:
        print("\n" + "=" * 110)
        print("Phase 1: vector_weight (α) sweep  |  threshold = None")
        print("=" * 110)
        header = f"{'α':>6} | {METRIC_HEADER} | {CATEGORY_HEADER}"
    elif phase == 2:
        print("\n" + "=" * 110)
        print(f"Phase 2: threshold sweep  |  α = {results[0].vector_weight}")
        print("=" * 110)
        header = f"{'thresh':>6} | {METRIC_HEADER} | {CATEGORY_HEADER}"
    elif phase == 3:
        print("\n" + "=" * 110)
        print(
            f"Phase 3: n_results sweep  |  "
            f"α = {results[0].vector_weight}  |  "
            f"threshold = {results[0].threshold}",
        )
        print("=" * 110)
        header = f"{'n_res':>5} | {METRIC_HEADER} | {CATEGORY_HEADER}"
    elif phase == 4:
        print("\n" + "=" * 110)
        print(
            f"Phase 4: BM25 k1/b sweep  |  "
            f"α = {results[0].vector_weight}  |  "
            f"threshold = {results[0].threshold}  |  "
            f"n_results = {results[0].n_results}",
        )
        print("=" * 110)
        header = f"{'k1/b':>9} | {METRIC_HEADER} | {CATEGORY_HEADER}"
    else:
        print("\n" + "=" * 110)
        print(
            f"Phase 5: min_combined_score sweep  |  "
            f"α = {results[0].vector_weight}  |  "
            f"threshold = {results[0].threshold}  |  "
            f"n_results = {results[0].n_results}  |  "
            f"k1 = {results[0].k1}  |  b = {results[0].b}",
        )
        print("=" * 110)
        header = f"{'min_sc':>6} | {METRIC_HEADER} | {CATEGORY_HEADER}"

    print(header)
    print("-" * len(header))

    best_f1 = max(r.avg_f1 for r in results)

    for r in results:
        marker = " ***" if r.avg_f1 == best_f1 else ""
        if phase == 1:
            param = f"{r.vector_weight:>6.2f}"
        elif phase == 2:
            param = f"{r.threshold!s:>6}"
        elif phase == 3:
            param = f"{r.n_results:>5}"
        elif phase == 4:
            param = f"{r.k1:.1f}/{r.b:.2f}"
            param = f"{param:>9}"
        else:
            param = f"{r.min_combined_score!s:>6}"
        print(
            f"{param} | {r.avg_f1:>6.3f} | {r.avg_precision:>6.3f} | {r.avg_recall:>6.3f} | "
            f"{r.avg_ndcg:>6.3f} | {r.avg_mrr:>6.3f} | {r.negative_violations:>4} | "
            f"{_format_category_cols(r.category_f1)}{marker}"
        )


async def run_phase1(
    dataset_path: str, fixture_path: str,
    chunk_size: int, chunk_overlap: int,
    persist_dir: str,
    *,
    alpha_min: float = 0.0,
    alpha_max: float = 1.0,
    alpha_step: float = 0.1,
    n_results: int = 5,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[SweepResult]:
    """Phase 1: α スイープ."""
    alphas: list[float] = []
    val = alpha_min
    while val <= alpha_max + 1e-9:
        alphas.append(round(val, 2))
        val += alpha_step
    results: list[SweepResult] = []

    for alpha in alphas:
        logger.info("Phase 1: α=%.2f, k1=%.1f, b=%.2f ...", alpha, k1, b)
        result = await run_single_evaluation(
            vector_weight=alpha,
            threshold=None,
            dataset_path=dataset_path,
            fixture_path=fixture_path,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            n_results=n_results,
            persist_dir=persist_dir,
            k1=k1,
            b=b,
        )
        results.append(result)

    print_results_table(results, phase=1)

    best = max(results, key=lambda r: r.avg_f1)
    print(f"\n>>> Best α = {best.vector_weight} (F1 = {best.avg_f1:.3f})")
    return results


async def run_phase2(
    best_alpha: float, dataset_path: str, fixture_path: str,
    chunk_size: int, chunk_overlap: int,
    persist_dir: str,
    *,
    n_results: int = 5,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[SweepResult]:
    """Phase 2: threshold スイープ."""
    thresholds: list[float | None] = [None, 0.4, 0.5, 0.6, 0.7, 0.8]
    results: list[SweepResult] = []

    for thresh in thresholds:
        logger.info("Phase 2: threshold=%s, α=%.2f, k1=%.1f, b=%.2f ...", thresh, best_alpha, k1, b)
        result = await run_single_evaluation(
            vector_weight=best_alpha,
            threshold=thresh,
            dataset_path=dataset_path,
            fixture_path=fixture_path,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            n_results=n_results,
            persist_dir=persist_dir,
            k1=k1,
            b=b,
        )
        results.append(result)

    print_results_table(results, phase=2)

    best = max(results, key=lambda r: r.avg_f1)
    print(f"\n>>> Best threshold = {best.threshold} (F1 = {best.avg_f1:.3f})")
    return results


def _sweep_result_to_dict(r: SweepResult) -> dict[str, object]:
    """SweepResult を JSON 出力用の辞書に変換."""
    return {
        "vector_weight": r.vector_weight,
        "threshold": r.threshold,
        "n_results": r.n_results,
        "k1": r.k1,
        "b": r.b,
        "min_combined_score": r.min_combined_score,
        "avg_f1": r.avg_f1,
        "avg_precision": r.avg_precision,
        "avg_recall": r.avg_recall,
        "avg_ndcg": r.avg_ndcg,
        "avg_mrr": r.avg_mrr,
        "negative_violations": r.negative_violations,
        "category_f1": r.category_f1,
    }


def _build_json_output(
    phase1_results: list[SweepResult],
    phase2_results: list[SweepResult],
    phase3_results: list[SweepResult],
    best1: SweepResult,
    best2: SweepResult,
    best3: SweepResult,
    phase4_results: list[SweepResult] | None = None,
    best4: SweepResult | None = None,
    phase5_results: list[SweepResult] | None = None,
    best5: SweepResult | None = None,
) -> dict[str, object]:
    """全Phase結果のJSON出力を構築."""
    output: dict[str, object] = {
        "phase1": [_sweep_result_to_dict(r) for r in phase1_results],
        "phase2": [_sweep_result_to_dict(r) for r in phase2_results],
        "phase3": [_sweep_result_to_dict(r) for r in phase3_results],
    }
    if phase4_results is not None:
        output["phase4"] = [_sweep_result_to_dict(r) for r in phase4_results]
    if phase5_results is not None:
        output["phase5"] = [_sweep_result_to_dict(r) for r in phase5_results]
    recommendation: dict[str, object] = {
        "vector_weight": best1.vector_weight,
        "threshold": best2.threshold,
        "n_results": best3.n_results,
    }
    if best4 is not None:
        recommendation["k1"] = best4.k1
        recommendation["b"] = best4.b
    if best5 is not None:
        recommendation["min_combined_score"] = best5.min_combined_score
        recommendation["expected_f1"] = best5.avg_f1
    elif best4 is not None:
        recommendation["expected_f1"] = best4.avg_f1
    else:
        recommendation["expected_f1"] = best3.avg_f1
    output["recommendation"] = recommendation
    return output


async def run_phase3(
    best_alpha: float, best_threshold: float | None,
    dataset_path: str, fixture_path: str,
    chunk_size: int, chunk_overlap: int,
    persist_dir: str,
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[SweepResult]:
    """Phase 3: n_results スイープ."""
    n_results_values = [3, 5, 7, 10, 15, 20]
    results: list[SweepResult] = []

    for n_res in n_results_values:
        logger.info(
            "Phase 3: n_results=%d, α=%.2f, threshold=%s, k1=%.1f, b=%.2f ...",
            n_res, best_alpha, best_threshold, k1, b,
        )
        result = await run_single_evaluation(
            vector_weight=best_alpha,
            threshold=best_threshold,
            dataset_path=dataset_path,
            fixture_path=fixture_path,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            n_results=n_res,
            persist_dir=persist_dir,
            k1=k1,
            b=b,
        )
        results.append(result)

    print_results_table(results, phase=3)

    best = max(results, key=lambda r: r.avg_f1)
    print(f"\n>>> Best n_results = {best.n_results} (F1 = {best.avg_f1:.3f})")
    return results


async def run_phase4(
    best_alpha: float, best_threshold: float | None,
    best_n_results: int,
    dataset_path: str, fixture_path: str,
    chunk_size: int, chunk_overlap: int,
    persist_dir: str,
) -> list[SweepResult]:
    """Phase 4: BM25 k1/b グリッドサーチ."""
    k1_values = [0.5, 1.0, 1.5, 2.0, 2.5]
    b_values = [0.0, 0.25, 0.5, 0.75, 1.0]
    results: list[SweepResult] = []

    for k1 in k1_values:
        for b_val in b_values:
            logger.info(
                "Phase 4: k1=%.1f, b=%.2f, α=%.2f, threshold=%s, n_results=%d ...",
                k1, b_val, best_alpha, best_threshold, best_n_results,
            )
            result = await run_single_evaluation(
                vector_weight=best_alpha,
                threshold=best_threshold,
                dataset_path=dataset_path,
                fixture_path=fixture_path,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                n_results=best_n_results,
                persist_dir=persist_dir,
                k1=k1,
                b=b_val,
            )
            results.append(result)

    print_results_table(results, phase=4)

    best = max(results, key=lambda r: r.avg_f1)
    print(f"\n>>> Best k1={best.k1}, b={best.b} (F1 = {best.avg_f1:.3f})")
    return results


async def run_phase5(
    best_alpha: float, best_threshold: float | None,
    best_n_results: int,
    best_k1: float, best_b: float,
    dataset_path: str, fixture_path: str,
    chunk_size: int, chunk_overlap: int,
    persist_dir: str,
) -> list[SweepResult]:
    """Phase 5: min_combined_score スイープ."""
    min_score_values: list[float | None] = [
        None, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85,
    ]
    results: list[SweepResult] = []

    for min_sc in min_score_values:
        logger.info(
            "Phase 5: min_combined_score=%s, α=%.2f, threshold=%s, "
            "n_results=%d, k1=%.1f, b=%.2f ...",
            min_sc, best_alpha, best_threshold, best_n_results,
            best_k1, best_b,
        )
        result = await run_single_evaluation(
            vector_weight=best_alpha,
            threshold=best_threshold,
            dataset_path=dataset_path,
            fixture_path=fixture_path,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            n_results=best_n_results,
            persist_dir=persist_dir,
            k1=best_k1,
            b=best_b,
            min_combined_score=min_sc,
        )
        results.append(result)

    print_results_table(results, phase=5)

    best = max(results, key=lambda r: r.avg_f1)
    print(f"\n>>> Best min_combined_score={best.min_combined_score} (F1 = {best.avg_f1:.3f})")
    return results


async def main_async(args: argparse.Namespace) -> None:
    """メイン処理."""
    dataset_path = args.dataset
    fixture_path = args.fixture
    persist_dir = args.persist_dir
    chunk_size = args.chunk_size
    chunk_overlap = args.chunk_overlap

    n_results = args.n_results
    bm25_k1: float = args.bm25_k1
    bm25_b: float = args.bm25_b

    if args.phase in (1, 0):
        _validate_alpha_range(args)
        phase1_results = await run_phase1(
            dataset_path, fixture_path,
            chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            persist_dir=persist_dir,
            alpha_min=args.alpha_min,
            alpha_max=args.alpha_max,
            alpha_step=args.alpha_step,
            n_results=n_results,
            k1=bm25_k1,
            b=bm25_b,
        )

        if args.phase == 0:
            # 全自動: Phase 1 → 2 → 3 → 4 → 5 をチェーン
            best_alpha = max(phase1_results, key=lambda r: r.avg_f1).vector_weight
            phase2_results = await run_phase2(
                best_alpha, dataset_path, fixture_path,
                chunk_size=chunk_size, chunk_overlap=chunk_overlap,
                persist_dir=persist_dir,
                n_results=n_results,
                k1=bm25_k1,
                b=bm25_b,
            )

            best2 = max(phase2_results, key=lambda r: r.avg_f1)
            phase3_results = await run_phase3(
                best_alpha, best2.threshold,
                dataset_path, fixture_path,
                chunk_size=chunk_size, chunk_overlap=chunk_overlap,
                persist_dir=persist_dir,
                k1=bm25_k1,
                b=bm25_b,
            )

            best3 = max(phase3_results, key=lambda r: r.avg_f1)
            phase4_results = await run_phase4(
                best_alpha, best2.threshold, best3.n_results,
                dataset_path, fixture_path,
                chunk_size=chunk_size, chunk_overlap=chunk_overlap,
                persist_dir=persist_dir,
            )

            best4 = max(phase4_results, key=lambda r: r.avg_f1)
            phase5_results = await run_phase5(
                best_alpha, best2.threshold, best3.n_results,
                best4.k1, best4.b,
                dataset_path, fixture_path,
                chunk_size=chunk_size, chunk_overlap=chunk_overlap,
                persist_dir=persist_dir,
            )

            # サマリー出力
            best1 = max(phase1_results, key=lambda r: r.avg_f1)
            best5 = max(phase5_results, key=lambda r: r.avg_f1)
            print("\n" + "=" * 60)
            print("FINAL SUMMARY")
            print("=" * 60)
            print(f"Phase 1 best: α={best1.vector_weight}, F1={best1.avg_f1:.3f}")
            print(f"Phase 2 best: threshold={best2.threshold}, F1={best2.avg_f1:.3f}")
            print(f"Phase 3 best: n_results={best3.n_results}, F1={best3.avg_f1:.3f}")
            print(f"Phase 4 best: k1={best4.k1}, b={best4.b}, F1={best4.avg_f1:.3f}")
            print(
                f"Phase 5 best: min_combined_score={best5.min_combined_score}, "
                f"F1={best5.avg_f1:.3f}",
            )

            # JSON出力
            output = _build_json_output(
                phase1_results, phase2_results, phase3_results,
                best1, best2, best3,
                phase4_results=phase4_results,
                best4=best4,
                phase5_results=phase5_results,
                best5=best5,
            )
            output_path = OUTPUT_DIR / "sweep_results.json"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)
            print(f"\nResults saved to: {output_path}")

    elif args.phase == 2:
        if args.best_alpha is None:
            print("ERROR: --best-alpha is required for Phase 2")
            sys.exit(1)
        await run_phase2(
            args.best_alpha, dataset_path, fixture_path,
            chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            persist_dir=persist_dir,
            n_results=n_results,
            k1=bm25_k1,
            b=bm25_b,
        )

    elif args.phase == 3:
        if args.best_alpha is None:
            print("ERROR: --best-alpha is required for Phase 3")
            sys.exit(1)
        if args.best_threshold is _THRESHOLD_UNSET:
            print("ERROR: --best-threshold is required for Phase 3 (use 'none' for no threshold)")
            sys.exit(1)
        await run_phase3(
            args.best_alpha, args.best_threshold,
            dataset_path, fixture_path,
            chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            persist_dir=persist_dir,
            k1=bm25_k1,
            b=bm25_b,
        )

    elif args.phase == 4:
        if args.best_alpha is None:
            print("ERROR: --best-alpha is required for Phase 4")
            sys.exit(1)
        if args.best_threshold is _THRESHOLD_UNSET:
            print("ERROR: --best-threshold is required for Phase 4 (use 'none' for no threshold)")
            sys.exit(1)
        if args.best_n_results is None:
            print("ERROR: --best-n-results is required for Phase 4")
            sys.exit(1)
        await run_phase4(
            args.best_alpha, args.best_threshold, args.best_n_results,
            dataset_path, fixture_path,
            chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            persist_dir=persist_dir,
        )

    elif args.phase == 5:
        if args.best_alpha is None:
            print("ERROR: --best-alpha is required for Phase 5")
            sys.exit(1)
        if args.best_threshold is _THRESHOLD_UNSET:
            print("ERROR: --best-threshold is required for Phase 5 (use 'none' for no threshold)")
            sys.exit(1)
        if args.best_n_results is None:
            print("ERROR: --best-n-results is required for Phase 5")
            sys.exit(1)
        if args.best_k1 is None:
            print("ERROR: --best-k1 is required for Phase 5")
            sys.exit(1)
        if args.best_b is None:
            print("ERROR: --best-b is required for Phase 5")
            sys.exit(1)
        await run_phase5(
            args.best_alpha, args.best_threshold, args.best_n_results,
            args.best_k1, args.best_b,
            dataset_path, fixture_path,
            chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            persist_dir=persist_dir,
        )


def _parse_threshold(value: str) -> float | None:
    """CLI引数の threshold をパース. 'none' → None, 数値 → float."""
    if value.lower() == "none":
        return None
    return float(value)


def _positive_int(value: str) -> int:
    """正の整数のみ受け付ける argparse type."""
    n = int(value)
    if n < 1:
        msg = f"1以上の整数を指定してください: {value}"
        raise argparse.ArgumentTypeError(msg)
    return n


def _validate_alpha_range(args: argparse.Namespace) -> None:
    """alpha sweep パラメータのバリデーション."""
    if args.alpha_step <= 0:
        print("ERROR: --alpha-step must be > 0")
        sys.exit(1)
    if args.alpha_min > args.alpha_max:
        print("ERROR: --alpha-min must be <= --alpha-max")
        sys.exit(1)


# --best-threshold 未指定を検出するための sentinel
_THRESHOLD_UNSET = object()


def main() -> None:
    """エントリポイント."""
    parser = argparse.ArgumentParser(description="RAG Parameter Sweep")
    parser.add_argument(
        "--phase",
        type=int,
        default=0,
        choices=[0, 1, 2, 3, 4, 5],
        help="Phase to run: 0=all (default), 1=α only, 2=threshold only, "
             "3=n_results only, 4=BM25 k1/b only, 5=min_combined_score only",
    )
    parser.add_argument(
        "--best-alpha",
        type=float,
        help="Best α from Phase 1 (required for --phase 2/3/4)",
    )
    parser.add_argument(
        "--best-threshold",
        type=_parse_threshold,
        default=_THRESHOLD_UNSET,
        help="Best threshold from Phase 2 (required for --phase 3/4). Use 'none' for None",
    )
    parser.add_argument(
        "--best-n-results",
        type=_positive_int,
        help="Best n_results from Phase 3 (required for --phase 4/5)",
    )
    parser.add_argument(
        "--best-k1",
        type=float,
        help="Best k1 from Phase 4 (required for --phase 5)",
    )
    parser.add_argument(
        "--best-b",
        type=float,
        help="Best b from Phase 4 (required for --phase 5)",
    )
    parser.add_argument(
        "--min-combined-score",
        type=float,
        default=None,
        help="combined_scoreの下限閾値（evaluate時のデフォルト: None=フィルタなし）",
    )
    parser.add_argument(
        "--alpha-min",
        type=float,
        default=0.0,
        help="Phase 1 α sweep minimum (default: 0.0)",
    )
    parser.add_argument(
        "--alpha-max",
        type=float,
        default=1.0,
        help="Phase 1 α sweep maximum (default: 1.0)",
    )
    parser.add_argument(
        "--alpha-step",
        type=float,
        default=0.1,
        help="Phase 1 α sweep step size (default: 0.1)",
    )
    parser.add_argument(
        "--n-results",
        type=_positive_int,
        default=5,
        help="各クエリで取得する結果数 (default: 5)",
    )
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
        "--persist-dir",
        type=str,
        default=DEFAULT_PERSIST_DIR,
        help=f"ChromaDB永続化ディレクトリ（デフォルト: {DEFAULT_PERSIST_DIR}）",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        required=True,
        help="チャンクサイズ",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        required=True,
        help="チャンクオーバーラップ",
    )
    def _validate_bm25_k1(value: str) -> float:
        f = float(value)
        if f < 0.0:
            raise argparse.ArgumentTypeError(
                f"--bm25-k1 must be >= 0.0 (got {f})"
            )
        return f

    def _validate_bm25_b(value: str) -> float:
        f = float(value)
        if not 0.0 <= f <= 1.0:
            raise argparse.ArgumentTypeError(
                f"--bm25-b must be between 0.0 and 1.0 (got {f})"
            )
        return f

    parser.add_argument(
        "--bm25-k1",
        type=_validate_bm25_k1,
        default=1.5,
        help="BM25 k1パラメータ（デフォルト: 1.5）",
    )
    parser.add_argument(
        "--bm25-b",
        type=_validate_bm25_b,
        default=0.75,
        help="BM25 bパラメータ（デフォルト: 0.75）",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

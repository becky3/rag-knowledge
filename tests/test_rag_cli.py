"""RAG評価CLIテスト

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rag.cli import (
    RegressionInfo,
    detect_regression,
    load_baseline,
    write_json_report,
    write_markdown_report,
)
from rag.evaluation import EvaluationReport, QueryEvaluationResult


class TestDetectRegression:
    """リグレッション検出関数のテスト."""

    def test_no_regression_when_f1_improved(self) -> None:
        """F1スコアが改善した場合、リグレッションなし."""
        result = detect_regression(
            baseline_f1=0.5,
            current_f1=0.6,
            threshold=0.1,
        )
        assert result["detected"] is False
        assert result["baseline_f1"] == 0.5
        assert result["current_f1"] == 0.6
        assert result["delta"] == pytest.approx(0.1)

    def test_no_regression_when_f1_slightly_decreased(self) -> None:
        """F1スコアの低下が閾値以内なら、リグレッションなし."""
        result = detect_regression(
            baseline_f1=0.6,
            current_f1=0.55,
            threshold=0.1,
        )
        assert result["detected"] is False
        assert result["delta"] == pytest.approx(-0.05)

    def test_regression_detected_when_f1_decreased_beyond_threshold(self) -> None:
        """F1スコアの低下が閾値を超えたら、リグレッション検出."""
        result = detect_regression(
            baseline_f1=0.7,
            current_f1=0.5,
            threshold=0.1,
        )
        assert result["detected"] is True
        assert result["delta"] == pytest.approx(-0.2)


class TestLoadBaseline:
    """ベースライン読み込みテスト."""

    def test_load_baseline_success(self, tmp_path: Path) -> None:
        """正常なベースラインファイルの読み込み."""
        baseline_data = {
            "summary": {
                "average_f1": 0.65,
                "average_precision": 0.7,
                "average_recall": 0.6,
            }
        }
        baseline_file = tmp_path / "baseline.json"
        baseline_file.write_text(json.dumps(baseline_data), encoding="utf-8")

        result = load_baseline(str(baseline_file))
        summary = result.get("summary", {})
        assert isinstance(summary, dict)
        assert summary.get("average_f1") == 0.65


class TestWriteJsonReport:
    """JSONレポート出力テスト."""

    def test_json_report_format(self, tmp_path: Path) -> None:
        """AC6: JSON形式のレポートが出力されること."""
        report = EvaluationReport(
            queries_evaluated=2,
            average_precision=0.75,
            average_recall=0.8,
            average_f1=0.77,
            average_ndcg=0.85,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[
                QueryEvaluationResult(
                    query_id="q1",
                    query="テストクエリ1",
                    precision=0.8,
                    recall=0.9,
                    f1=0.85,
                    ndcg=0.9,
                    mrr=1.0,
                    retrieved_sources=["https://example.com/a"],
                    expected_sources=["https://example.com/a"],
                    negative_violations=[],
                ),
                QueryEvaluationResult(
                    query_id="q2",
                    query="テストクエリ2",
                    precision=0.7,
                    recall=0.7,
                    f1=0.7,
                    ndcg=0.8,
                    mrr=1.0,
                    retrieved_sources=["https://example.com/b"],
                    expected_sources=["https://example.com/b"],
                    negative_violations=[],
                ),
            ],
        )

        output_path = tmp_path / "report.json"
        write_json_report(report, None, output_path, "test_dataset.json")

        assert output_path.exists()
        with open(output_path, encoding="utf-8") as f:
            data = json.load(f)

        # 必須フィールドの存在確認
        assert "timestamp" in data
        assert "dataset" in data
        assert "summary" in data
        assert "query_results" in data

        # サマリーの値確認
        summary = data["summary"]
        assert summary["queries_evaluated"] == 2
        assert summary["average_precision"] == 0.75
        assert summary["average_recall"] == 0.8
        assert summary["average_f1"] == 0.77
        assert summary["negative_source_violations"] == 0

        # クエリ結果の確認
        assert len(data["query_results"]) == 2
        assert data["query_results"][0]["query_id"] == "q1"


class TestWriteMarkdownReport:
    """Markdownレポート出力テスト."""

    def test_markdown_report_format(self, tmp_path: Path) -> None:
        """AC7: Markdown形式のレポートが出力されること."""
        report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.8,
            average_recall=0.9,
            average_f1=0.85,
            average_ndcg=0.9,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[
                QueryEvaluationResult(
                    query_id="q1",
                    query="テストクエリ",
                    precision=0.8,
                    recall=0.9,
                    f1=0.85,
                    ndcg=0.9,
                    mrr=1.0,
                    retrieved_sources=["https://example.com/a"],
                    expected_sources=["https://example.com/a"],
                    negative_violations=[],
                ),
            ],
        )

        output_path = tmp_path / "report.md"
        write_markdown_report(report, None, output_path, "test_dataset.json")

        assert output_path.exists()
        content = output_path.read_text(encoding="utf-8")

        # 必須セクションの存在確認
        assert "# RAG評価レポート" in content
        assert "## サマリー" in content
        assert "## クエリ別詳細" in content
        assert "評価クエリ数 | 1" in content
        assert "平均F1 | 0.850" in content


class TestRegressionInfo:
    """リグレッション情報付きレポートのテスト."""

    def test_regression_detection(self, tmp_path: Path) -> None:
        """AC10: F1スコアの低下がしきい値を超えたらリグレッション判定すること."""
        report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.5,
            average_recall=0.5,
            average_f1=0.5,
            average_ndcg=0.5,
            average_mrr=0.5,
            negative_source_violations=[],
            query_results=[],
        )

        regression_info = RegressionInfo(
            detected=True,
            baseline_f1=0.7,
            current_f1=0.5,
            delta=-0.2,
        )

        json_path = tmp_path / "report.json"
        write_json_report(report, regression_info, json_path, "test_dataset.json")

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        assert data["regression"] is not None
        assert data["regression"]["detected"] is True
        assert data["regression"]["baseline_f1"] == 0.7
        assert data["regression"]["current_f1"] == 0.5

        md_path = tmp_path / "report.md"
        write_markdown_report(report, regression_info, md_path, "test_dataset.json")

        content = md_path.read_text(encoding="utf-8")
        assert "## リグレッション検出" in content
        assert "リグレッション検出" in content


class TestSaveBaseline:
    """ベースライン保存テスト."""

    def test_save_baseline(self, tmp_path: Path) -> None:
        """AC12: --save-baseline指定時に現在の結果をベースラインとして保存すること."""
        report = EvaluationReport(
            queries_evaluated=2,
            average_precision=0.75,
            average_recall=0.8,
            average_f1=0.77,
            average_ndcg=0.85,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[],
        )

        baseline_path = tmp_path / "baseline.json"
        write_json_report(report, None, baseline_path, "test_dataset.json")

        assert baseline_path.exists()
        with open(baseline_path, encoding="utf-8") as f:
            data = json.load(f)

        # ベースラインとして利用可能か確認
        assert data["summary"]["average_f1"] == 0.77
        # リグレッション情報はNone（ベースライン用）
        assert data["regression"] is None


class TestCLIEvaluate:
    """CLI evaluateコマンドのテスト."""

    @pytest.mark.asyncio
    async def test_evaluate_command_runs(self, tmp_path: Path) -> None:
        """AC1: python -m rag.cli evaluate で評価が実行できること."""
        from rag.cli import run_evaluation
        from argparse import Namespace

        # モック用の評価レポート
        mock_report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.8,
            average_recall=0.9,
            average_f1=0.85,
            average_ndcg=0.9,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[
                QueryEvaluationResult(
                    query_id="q1",
                    query="テスト",
                    precision=0.8,
                    recall=0.9,
                    f1=0.85,
                    ndcg=0.9,
                    mrr=1.0,
                    retrieved_sources=["https://example.com/a"],
                    expected_sources=["https://example.com/a"],
                    negative_violations=[],
                ),
            ],
        )

        # テスト用データセット作成
        dataset_path = tmp_path / "test_dataset.json"
        dataset_data = {
            "queries": [
                {
                    "id": "q1",
                    "query": "テスト",
                    "expected_sources": ["https://example.com/a"],
                    "negative_sources": [],
                }
            ]
        }
        dataset_path.write_text(json.dumps(dataset_data), encoding="utf-8")

        output_dir = tmp_path / "output"

        args = Namespace(
            dataset=str(dataset_path),
            output_dir=str(output_dir),
            baseline_file=None,
            n_results=5,
            threshold=None,
            vector_weight=0.6,
            persist_dir=str(tmp_path / "chroma_db"),
            fail_on_regression=False,
            regression_threshold=0.1,
            save_baseline=False,
            chunk_size=200,
            chunk_overlap=30,
            fixture="tests/fixtures/rag_test_documents.json",
            bm25_k1=1.5,
            bm25_b=0.75,
            min_combined_score=None,
        )

        with patch("rag.cli._build_bm25_index_from_fixture", return_value=MagicMock()):
            with patch("rag.cli.create_rag_service") as mock_create_service:
                with patch(
                    "rag.cli.evaluate_retrieval", new=AsyncMock(return_value=mock_report)
                ):
                    mock_service = AsyncMock()
                    mock_create_service.return_value = mock_service

                    await run_evaluation(args)

        # レポートが出力されたか確認
        assert (output_dir / "report.json").exists()
        assert (output_dir / "report.md").exists()

    @pytest.mark.asyncio
    async def test_dataset_option(self, tmp_path: Path) -> None:
        """AC2: --dataset オプションでデータセットパスを指定できること."""
        from rag.cli import run_evaluation
        from argparse import Namespace

        mock_report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.8,
            average_recall=0.9,
            average_f1=0.85,
            average_ndcg=0.9,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[],
        )

        # カスタムデータセットパス
        custom_dataset = tmp_path / "custom_dataset.json"
        custom_dataset.write_text(json.dumps({"queries": []}), encoding="utf-8")

        output_dir = tmp_path / "output"

        args = Namespace(
            dataset=str(custom_dataset),
            output_dir=str(output_dir),
            baseline_file=None,
            n_results=5,
            threshold=None,
            vector_weight=0.6,
            persist_dir=str(tmp_path / "chroma_db"),
            fail_on_regression=False,
            regression_threshold=0.1,
            save_baseline=False,
            chunk_size=200,
            chunk_overlap=30,
            fixture="tests/fixtures/rag_test_documents.json",
            bm25_k1=1.5,
            bm25_b=0.75,
            min_combined_score=None,
        )

        mock_eval = AsyncMock(return_value=mock_report)
        with patch("rag.cli._build_bm25_index_from_fixture", return_value=MagicMock()):
            with patch("rag.cli.create_rag_service") as mock_create_service:
                with patch("rag.cli.evaluate_retrieval", new=mock_eval):
                    mock_service = AsyncMock()
                    mock_create_service.return_value = mock_service

                    await run_evaluation(args)

                    # カスタムデータセットパスが使用されたか確認
                    mock_eval.assert_called_once()
                    call_kwargs = mock_eval.call_args
                    assert call_kwargs[1]["dataset_path"] == str(custom_dataset)

    @pytest.mark.asyncio
    async def test_output_dir_option(self, tmp_path: Path) -> None:
        """AC3: --output-dir オプションでレポート出力先を指定できること."""
        from rag.cli import run_evaluation
        from argparse import Namespace

        mock_report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.8,
            average_recall=0.9,
            average_f1=0.85,
            average_ndcg=0.9,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[],
        )

        dataset = tmp_path / "dataset.json"
        dataset.write_text(json.dumps({"queries": []}), encoding="utf-8")

        # カスタム出力ディレクトリ
        custom_output = tmp_path / "custom_output" / "reports"

        args = Namespace(
            dataset=str(dataset),
            output_dir=str(custom_output),
            baseline_file=None,
            n_results=5,
            threshold=None,
            vector_weight=0.6,
            persist_dir=str(tmp_path / "chroma_db"),
            fail_on_regression=False,
            regression_threshold=0.1,
            save_baseline=False,
            chunk_size=200,
            chunk_overlap=30,
            fixture="tests/fixtures/rag_test_documents.json",
            bm25_k1=1.5,
            bm25_b=0.75,
            min_combined_score=None,
        )

        with patch("rag.cli._build_bm25_index_from_fixture", return_value=MagicMock()):
            with patch("rag.cli.create_rag_service") as mock_create_service:
                with patch(
                    "rag.cli.evaluate_retrieval", new=AsyncMock(return_value=mock_report)
                ):
                    mock_service = AsyncMock()
                    mock_create_service.return_value = mock_service

                    await run_evaluation(args)

        # カスタム出力ディレクトリにレポートが出力されたか確認
        assert custom_output.exists()
        assert (custom_output / "report.json").exists()
        assert (custom_output / "report.md").exists()

    @pytest.mark.asyncio
    async def test_fail_on_regression_exit_code(self, tmp_path: Path) -> None:
        """AC11: --fail-on-regression指定時、リグレッション検出でexit code 1になること."""
        from rag.cli import run_evaluation
        from argparse import Namespace

        mock_report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.3,
            average_recall=0.4,
            average_f1=0.35,  # ベースラインより大幅に低い
            average_ndcg=0.3,
            average_mrr=0.5,
            negative_source_violations=[],
            query_results=[],
        )

        dataset = tmp_path / "dataset.json"
        dataset.write_text(json.dumps({"queries": []}), encoding="utf-8")

        # ベースラインファイル作成（F1=0.7）
        baseline_file = tmp_path / "baseline.json"
        baseline_data = {
            "summary": {"average_f1": 0.7},
        }
        baseline_file.write_text(json.dumps(baseline_data), encoding="utf-8")

        output_dir = tmp_path / "output"

        args = Namespace(
            dataset=str(dataset),
            output_dir=str(output_dir),
            baseline_file=str(baseline_file),
            n_results=5,
            threshold=None,
            vector_weight=0.6,
            persist_dir=str(tmp_path / "chroma_db"),
            fail_on_regression=True,
            regression_threshold=0.1,
            save_baseline=False,
            chunk_size=200,
            chunk_overlap=30,
            fixture="tests/fixtures/rag_test_documents.json",
            bm25_k1=1.5,
            bm25_b=0.75,
            min_combined_score=None,
        )

        with patch("rag.cli._build_bm25_index_from_fixture", return_value=MagicMock()):
            with patch("rag.cli.create_rag_service") as mock_create_service:
                with patch(
                    "rag.cli.evaluate_retrieval", new=AsyncMock(return_value=mock_report)
                ):
                    mock_service = AsyncMock()
                    mock_create_service.return_value = mock_service

                    with pytest.raises(SystemExit) as exc_info:
                        await run_evaluation(args)

                    assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_n_results_option(self, tmp_path: Path) -> None:
        """AC4: --n-results オプションがevaluate_retrievalに正しく伝播すること."""
        from rag.cli import run_evaluation
        from argparse import Namespace

        mock_report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.8,
            average_recall=0.9,
            average_f1=0.85,
            average_ndcg=0.9,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[],
        )

        dataset = tmp_path / "dataset.json"
        dataset.write_text(json.dumps({"queries": []}), encoding="utf-8")
        output_dir = tmp_path / "output"

        # カスタムn_results値を指定
        custom_n_results = 10

        args = Namespace(
            dataset=str(dataset),
            output_dir=str(output_dir),
            baseline_file=None,
            n_results=custom_n_results,
            threshold=None,
            vector_weight=0.6,
            persist_dir=str(tmp_path / "chroma_db"),
            fail_on_regression=False,
            regression_threshold=0.1,
            save_baseline=False,
            chunk_size=200,
            chunk_overlap=30,
            fixture="tests/fixtures/rag_test_documents.json",
            bm25_k1=1.5,
            bm25_b=0.75,
            min_combined_score=None,
        )

        mock_eval = AsyncMock(return_value=mock_report)
        with patch("rag.cli._build_bm25_index_from_fixture", return_value=MagicMock()):
            with patch("rag.cli.create_rag_service") as mock_create_service:
                with patch("rag.cli.evaluate_retrieval", new=mock_eval):
                    mock_service = AsyncMock()
                    mock_create_service.return_value = mock_service

                    await run_evaluation(args)

                    # n_resultsが正しく渡されたか確認
                    mock_eval.assert_called_once()
                    call_kwargs = mock_eval.call_args
                    assert call_kwargs[1]["n_results"] == custom_n_results

    @pytest.mark.asyncio
    async def test_threshold_option(self, tmp_path: Path) -> None:
        """AC5: --threshold オプションがcreate_rag_serviceに正しく伝播すること."""
        from rag.cli import run_evaluation
        from argparse import Namespace

        mock_report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.8,
            average_recall=0.9,
            average_f1=0.85,
            average_ndcg=0.9,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[],
        )

        dataset = tmp_path / "dataset.json"
        dataset.write_text(json.dumps({"queries": []}), encoding="utf-8")
        output_dir = tmp_path / "output"

        # カスタムthreshold値を指定
        custom_threshold = 0.7

        args = Namespace(
            dataset=str(dataset),
            output_dir=str(output_dir),
            baseline_file=None,
            n_results=5,
            threshold=custom_threshold,
            vector_weight=0.6,
            persist_dir=str(tmp_path / "chroma_db"),
            fail_on_regression=False,
            regression_threshold=0.1,
            save_baseline=False,
            chunk_size=200,
            chunk_overlap=30,
            fixture="tests/fixtures/rag_test_documents.json",
            bm25_k1=1.5,
            bm25_b=0.75,
            min_combined_score=None,
        )

        mock_eval = AsyncMock(return_value=mock_report)
        with patch("rag.cli._build_bm25_index_from_fixture", return_value=MagicMock()):
            with patch("rag.cli.create_rag_service") as mock_create_service:
                with patch("rag.cli.evaluate_retrieval", new=mock_eval):
                    mock_service = AsyncMock()
                    mock_create_service.return_value = mock_service

                    await run_evaluation(args)

                    # thresholdが正しく渡されたか確認
                    mock_create_service.assert_called_once()
                    call_kwargs = mock_create_service.call_args
                    assert call_kwargs[1]["threshold"] == custom_threshold

    @pytest.mark.asyncio
    async def test_vector_weight_option(self, tmp_path: Path) -> None:
        """AC8: --vector-weight オプションがcreate_rag_serviceに正しく伝播すること."""
        from rag.cli import run_evaluation
        from argparse import Namespace

        mock_report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.8,
            average_recall=0.9,
            average_f1=0.85,
            average_ndcg=0.9,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[],
        )

        dataset = tmp_path / "dataset.json"
        dataset.write_text(json.dumps({"queries": []}), encoding="utf-8")
        output_dir = tmp_path / "output"

        custom_vector_weight = 0.7

        args = Namespace(
            dataset=str(dataset),
            output_dir=str(output_dir),
            baseline_file=None,
            n_results=5,
            threshold=None,
            vector_weight=custom_vector_weight,
            persist_dir=str(tmp_path / "chroma_db"),
            fail_on_regression=False,
            regression_threshold=0.1,
            save_baseline=False,
            chunk_size=200,
            chunk_overlap=30,
            fixture="tests/fixtures/rag_test_documents.json",
            bm25_k1=1.5,
            bm25_b=0.75,
            min_combined_score=None,
        )

        mock_eval = AsyncMock(return_value=mock_report)
        with patch("rag.cli._build_bm25_index_from_fixture", return_value=MagicMock()):
            with patch("rag.cli.create_rag_service") as mock_create_service:
                with patch("rag.cli.evaluate_retrieval", new=mock_eval):
                    mock_service = AsyncMock()
                    mock_create_service.return_value = mock_service

                    await run_evaluation(args)

                    mock_create_service.assert_called_once()
                    call_kwargs = mock_create_service.call_args
                    assert call_kwargs[1]["vector_weight"] == custom_vector_weight

    @pytest.mark.asyncio
    async def test_chunk_params_propagation(self, tmp_path: Path) -> None:
        """AC9: --chunk-size/--chunk-overlap がcreate_rag_serviceとBM25構築に正しく伝播すること."""
        from rag.cli import run_evaluation
        from argparse import Namespace

        mock_report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.8,
            average_recall=0.9,
            average_f1=0.85,
            average_ndcg=0.9,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[],
        )

        dataset = tmp_path / "dataset.json"
        dataset.write_text(json.dumps({"queries": []}), encoding="utf-8")
        output_dir = tmp_path / "output"

        args = Namespace(
            dataset=str(dataset),
            output_dir=str(output_dir),
            baseline_file=None,
            n_results=5,
            threshold=None,
            vector_weight=0.6,
            persist_dir=str(tmp_path / "chroma_db"),
            fail_on_regression=False,
            regression_threshold=0.1,
            save_baseline=False,
            chunk_size=300,
            chunk_overlap=50,
            fixture="tests/fixtures/rag_test_documents.json",
            bm25_k1=1.5,
            bm25_b=0.75,
            min_combined_score=None,
        )

        mock_eval = AsyncMock(return_value=mock_report)
        mock_bm25_builder = MagicMock(return_value=MagicMock())
        with patch("rag.cli._build_bm25_index_from_fixture", mock_bm25_builder):
            with patch("rag.cli.create_rag_service") as mock_create_service:
                with patch("rag.cli.evaluate_retrieval", new=mock_eval):
                    mock_service = AsyncMock()
                    mock_create_service.return_value = mock_service

                    await run_evaluation(args)

                    # BM25構築にchunkパラメータが渡されたか確認
                    mock_bm25_builder.assert_called_once()
                    bm25_kwargs = mock_bm25_builder.call_args
                    assert bm25_kwargs[1]["chunk_size"] == 300
                    assert bm25_kwargs[1]["chunk_overlap"] == 50

                    # create_rag_serviceにchunkパラメータが渡されたか確認
                    mock_create_service.assert_called_once()
                    call_kwargs = mock_create_service.call_args
                    assert call_kwargs[1]["chunk_size"] == 300
                    assert call_kwargs[1]["chunk_overlap"] == 50

    @pytest.mark.asyncio
    async def test_bm25_params_propagation(self, tmp_path: Path) -> None:
        """AC10: --bm25-k1/--bm25-b が_build_bm25_index_from_fixtureに正しく伝播すること."""
        from rag.cli import run_evaluation
        from argparse import Namespace

        mock_report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.8,
            average_recall=0.9,
            average_f1=0.85,
            average_ndcg=0.9,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[],
        )

        dataset = tmp_path / "dataset.json"
        dataset.write_text(json.dumps({"queries": []}), encoding="utf-8")
        output_dir = tmp_path / "output"

        args = Namespace(
            dataset=str(dataset),
            output_dir=str(output_dir),
            baseline_file=None,
            n_results=5,
            threshold=None,
            vector_weight=0.6,
            persist_dir=str(tmp_path / "chroma_db"),
            fail_on_regression=False,
            regression_threshold=0.1,
            save_baseline=False,
            chunk_size=200,
            chunk_overlap=30,
            bm25_k1=2.0,
            bm25_b=0.5,
            fixture="tests/fixtures/rag_test_documents.json",
            min_combined_score=None,
        )

        mock_eval = AsyncMock(return_value=mock_report)
        mock_bm25_builder = MagicMock(return_value=MagicMock())
        with patch("rag.cli._build_bm25_index_from_fixture", mock_bm25_builder):
            with patch("rag.cli.create_rag_service") as mock_create_service:
                with patch("rag.cli.evaluate_retrieval", new=mock_eval):
                    mock_service = AsyncMock()
                    mock_create_service.return_value = mock_service

                    await run_evaluation(args)

                    # BM25構築にk1/bパラメータが渡されたか確認
                    mock_bm25_builder.assert_called_once()
                    bm25_kwargs = mock_bm25_builder.call_args
                    assert bm25_kwargs[1]["k1"] == 2.0
                    assert bm25_kwargs[1]["b"] == 0.5

    @pytest.mark.asyncio
    async def test_params_in_json_report(self, tmp_path: Path) -> None:
        """AC13: レポートに評価パラメータが含まれること."""
        from rag.cli import run_evaluation
        from argparse import Namespace

        mock_report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.8,
            average_recall=0.9,
            average_f1=0.85,
            average_ndcg=0.9,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[],
        )

        dataset = tmp_path / "dataset.json"
        dataset.write_text(json.dumps({"queries": []}), encoding="utf-8")
        output_dir = tmp_path / "output"

        args = Namespace(
            dataset=str(dataset),
            output_dir=str(output_dir),
            baseline_file=None,
            n_results=5,
            threshold=0.6,
            vector_weight=0.7,
            persist_dir=str(tmp_path / "chroma_db"),
            fail_on_regression=False,
            regression_threshold=0.1,
            save_baseline=False,
            chunk_size=200,
            chunk_overlap=30,
            bm25_k1=2.0,
            bm25_b=0.5,
            fixture="tests/fixtures/rag_test_documents.json",
            min_combined_score=None,
        )

        mock_eval = AsyncMock(return_value=mock_report)
        with patch("rag.cli._build_bm25_index_from_fixture", return_value=MagicMock()):
            with patch("rag.cli.create_rag_service") as mock_create_service:
                with patch("rag.cli.evaluate_retrieval", new=mock_eval):
                    mock_service = AsyncMock()
                    mock_create_service.return_value = mock_service

                    await run_evaluation(args)

        # JSONレポートにパラメータが含まれるか確認
        json_path = output_dir / "report.json"
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        assert "params" in data
        assert data["params"]["threshold"] == 0.6
        assert data["params"]["vector_weight"] == 0.7
        assert data["params"]["n_results"] == 5
        assert data["params"]["k1"] == 2.0
        assert data["params"]["b"] == 0.5

    @pytest.mark.asyncio
    async def test_persist_dir_option(self, tmp_path: Path) -> None:
        """AC6: --persist-dir オプションがcreate_rag_serviceに正しく伝播すること."""
        from rag.cli import run_evaluation
        from argparse import Namespace

        mock_report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.8,
            average_recall=0.9,
            average_f1=0.85,
            average_ndcg=0.9,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[],
        )

        dataset = tmp_path / "dataset.json"
        dataset.write_text(json.dumps({"queries": []}), encoding="utf-8")
        output_dir = tmp_path / "output"

        # カスタムpersist_dirを指定
        custom_persist_dir = str(tmp_path / "custom_chroma_db")

        args = Namespace(
            dataset=str(dataset),
            output_dir=str(output_dir),
            baseline_file=None,
            n_results=5,
            threshold=None,
            vector_weight=0.6,
            persist_dir=custom_persist_dir,
            fail_on_regression=False,
            regression_threshold=0.1,
            save_baseline=False,
            chunk_size=200,
            chunk_overlap=30,
            fixture="tests/fixtures/rag_test_documents.json",
            bm25_k1=1.5,
            bm25_b=0.75,
            min_combined_score=None,
        )

        mock_eval = AsyncMock(return_value=mock_report)
        with patch("rag.cli._build_bm25_index_from_fixture", return_value=MagicMock()):
            with patch("rag.cli.create_rag_service") as mock_create_service:
                with patch("rag.cli.evaluate_retrieval", new=mock_eval):
                    mock_service = AsyncMock()
                    mock_create_service.return_value = mock_service

                    await run_evaluation(args)

                    # persist_dirが正しく渡されたか確認
                    mock_create_service.assert_called_once()
                    call_kwargs = mock_create_service.call_args
                    assert call_kwargs[1]["persist_dir"] == custom_persist_dir


    @pytest.mark.asyncio
    async def test_min_combined_score_propagation(self, tmp_path: Path) -> None:
        """AC11: --min-combined-score が create_rag_service に正しく伝播すること."""
        from rag.cli import run_evaluation
        from argparse import Namespace

        mock_report = EvaluationReport(
            queries_evaluated=1,
            average_precision=0.8,
            average_recall=0.9,
            average_f1=0.85,
            average_ndcg=0.9,
            average_mrr=1.0,
            negative_source_violations=[],
            query_results=[],
        )

        dataset = tmp_path / "dataset.json"
        dataset.write_text(json.dumps({"queries": []}), encoding="utf-8")
        output_dir = tmp_path / "output"

        args = Namespace(
            dataset=str(dataset),
            output_dir=str(output_dir),
            baseline_file=None,
            n_results=5,
            threshold=None,
            vector_weight=0.6,
            persist_dir=str(tmp_path / "chroma_db"),
            fail_on_regression=False,
            regression_threshold=0.1,
            save_baseline=False,
            chunk_size=200,
            chunk_overlap=30,
            fixture="tests/fixtures/rag_test_documents.json",
            bm25_k1=1.5,
            bm25_b=0.75,
            min_combined_score=0.65,
        )

        mock_eval = AsyncMock(return_value=mock_report)
        with patch("rag.cli._build_bm25_index_from_fixture", return_value=MagicMock()):
            with patch("rag.cli.create_rag_service") as mock_create_service:
                with patch("rag.cli.evaluate_retrieval", new=mock_eval):
                    mock_service = AsyncMock()
                    mock_create_service.return_value = mock_service

                    await run_evaluation(args)

                    # min_combined_scoreが正しく渡されたか確認
                    mock_create_service.assert_called_once()
                    call_kwargs = mock_create_service.call_args
                    assert call_kwargs[1]["min_combined_score"] == 0.65


class TestCLIInitTestDb:
    """CLI init-test-dbコマンドのテスト."""

    @pytest.mark.asyncio
    async def test_init_test_db_creates_chromadb(self, tmp_path: Path) -> None:
        """init-test-dbコマンドでChromaDBが初期化されること."""
        from rag.cli import init_test_db
        from argparse import Namespace

        # テスト用フィクスチャ作成
        fixture_path = tmp_path / "fixture.json"
        fixture_data = {
            "documents": [
                {
                    "source_url": "https://example.com/test.html",
                    "title": "テストドキュメント",
                    "content": "これはテスト用のコンテンツです。",
                }
            ]
        }
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        persist_dir = tmp_path / "test_chroma"
        bm25_persist_dir = tmp_path / "test_bm25"

        args = Namespace(
            persist_dir=str(persist_dir),
            bm25_persist_dir=str(bm25_persist_dir),
            fixture=str(fixture_path),
            chunk_size=200,
            chunk_overlap=30,
            bm25_k1=1.5,
            bm25_b=0.75,
        )

        with patch("rag.cli.create_rag_service") as mock_create_service:
            mock_service = AsyncMock()
            mock_service._ingest_crawled_page = AsyncMock(return_value=1)
            mock_create_service.return_value = mock_service

            await init_test_db(args)

            # create_rag_serviceが正しい引数で呼ばれたか確認
            mock_create_service.assert_called_once_with(
                chunk_size=200,
                chunk_overlap=30,
                persist_dir=str(persist_dir),
            )

            # _ingest_crawled_pageが呼ばれたか確認
            mock_service._ingest_crawled_page.assert_called_once()

    @pytest.mark.asyncio
    async def test_init_test_db_creates_bm25_index(self, tmp_path: Path) -> None:
        """init-test-dbコマンドでBM25インデックスも永続化されること."""
        from rag.cli import init_test_db
        from argparse import Namespace

        from factories import make_bm25_index

        # テスト用フィクスチャ作成
        fixture_path = tmp_path / "fixture.json"
        fixture_data = {
            "documents": [
                {
                    "source_url": "https://example.com/test.html",
                    "title": "テストドキュメント",
                    "content": "これはテスト用のコンテンツです。十分な長さのテキスト。",
                }
            ]
        }
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        persist_dir = tmp_path / "test_chroma"
        bm25_persist_dir = tmp_path / "test_bm25"

        args = Namespace(
            persist_dir=str(persist_dir),
            bm25_persist_dir=str(bm25_persist_dir),
            fixture=str(fixture_path),
            chunk_size=200,
            chunk_overlap=30,
            bm25_k1=1.5,
            bm25_b=0.75,
        )

        with patch("rag.cli.create_rag_service") as mock_create_service:
            mock_service = AsyncMock()
            mock_service._ingest_crawled_page = AsyncMock(return_value=1)
            mock_create_service.return_value = mock_service

            await init_test_db(args)

        # BM25インデックスが永続化されたか確認
        assert bm25_persist_dir.exists(), "BM25 persist directory should exist"

        # 永続化されたインデックスをロードして中身を確認
        loaded_index = make_bm25_index(persist_dir=str(bm25_persist_dir))
        assert loaded_index.get_document_count() > 0, "BM25 index should have documents"

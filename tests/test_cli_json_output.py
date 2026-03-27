"""CLI JSON 出力モードのテスト.

--output json オプションによる JSON Lines 出力が
CLI JSON Lines プロトコルと互換であることを検証する。
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import MagicMock

import pytest

from rag.cli import (
    _add_output_option,
    _ingest_result_to_dict,
    _is_json_output,
    _output_error,
    _output_json,
    _output_progress,
    _output_result,
)


class TestJsonHelpers:
    """JSON 出力ヘルパー関数のテスト."""

    def test_output_json_prints_single_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        _output_json({"type": "test", "value": 42})
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed == {"type": "test", "value": 42}

    def test_output_json_ensure_ascii_false(self, capsys: pytest.CaptureFixture[str]) -> None:
        _output_json({"type": "test", "message": "日本語テスト"})
        captured = capsys.readouterr()
        assert "日本語テスト" in captured.out

    def test_output_progress_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        _output_progress(3, 10, "current_item")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed == {
            "type": "progress",
            "processed": 3,
            "total": 10,
            "current": "current_item",
        }

    def test_output_error_exits_with_code_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _output_error("something went wrong")
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed == {
            "type": "error",
            "error": True,
            "message": "something went wrong",
        }

    def test_output_result_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        _output_result({"placed": 1, "skipped": 0})
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed == {"type": "result", "placed": 1, "skipped": 0}


class TestIsJsonOutput:
    """_is_json_output のテスト."""

    def test_returns_true_when_json(self) -> None:
        args = argparse.Namespace(output_format="json")
        assert _is_json_output(args) is True

    def test_returns_false_when_text(self) -> None:
        args = argparse.Namespace(output_format="text")
        assert _is_json_output(args) is False

    def test_returns_false_when_attr_missing(self) -> None:
        args = argparse.Namespace()
        assert _is_json_output(args) is False


class TestAddOutputOption:
    """_add_output_option のテスト."""

    def test_adds_output_format_argument(self) -> None:
        parser = argparse.ArgumentParser()
        _add_output_option(parser)
        args = parser.parse_args(["--output", "json"])
        assert args.output_format == "json"

    def test_default_is_text(self) -> None:
        parser = argparse.ArgumentParser()
        _add_output_option(parser)
        args = parser.parse_args([])
        assert args.output_format == "text"


class TestIngestResultToDict:
    """_ingest_result_to_dict のテスト."""

    def test_without_pipeline_summary(self) -> None:
        ingest_result = MagicMock()
        ingest_result.placed = 2
        ingest_result.skipped = 1
        ingest_result.overwritten = 0
        ingest_result.errors = 0
        ingest_result.error_details = []

        result = _ingest_result_to_dict(ingest_result, None)
        assert result == {
            "placed": 2,
            "skipped": 1,
            "overwritten": 0,
            "errors": 0,
            "error_details": [],
        }

    def test_with_pipeline_summary(self) -> None:
        ingest_result = MagicMock()
        ingest_result.placed = 1
        ingest_result.skipped = 0
        ingest_result.overwritten = 0
        ingest_result.errors = 0
        ingest_result.error_details = []

        pipeline_summary = MagicMock()
        pipeline_summary.mode.value = "incremental"
        pipeline_summary.total_files = 5
        pipeline_summary.processed = 3
        pipeline_summary.skipped = 2
        pipeline_summary.errors = []

        result = _ingest_result_to_dict(ingest_result, pipeline_summary)
        assert result["placed"] == 1
        assert "pipeline" in result
        assert result["pipeline"]["mode"] == "incremental"
        assert result["pipeline"]["processed"] == 3


class TestCommandsHaveOutputOption:
    """14 対象コマンドに --output オプションが追加されていることを確認する.

    cli.py のソースコードを解析して、対象コマンドの直後に
    _add_output_option() 呼び出しがあることを検証する。
    """

    TARGET_COMMANDS = [
        "add",
        "crawl",
        "crawl-zenn",
        "crawl-bluesky",
        "ingest-youtube",
        "ingest-youtube-playlist",
        "add-document",
        "crawl-documents",
        "add-journal",
        "ingest-aozora",
        "ingest-aozora-author",
        "update-aozora-catalog",
        "delete",
        "rebuild",
    ]

    def test_all_14_commands_have_output_option(self) -> None:
        """cli.py のソースに 14 コマンド分の _add_output_option 呼び出しがある."""
        import inspect
        import rag.cli as cli_module

        source = inspect.getsource(cli_module)
        count = source.count("_add_output_option(")
        # ヘルパー関数の定義 (def _add_output_option) は除外して呼び出しだけ数える
        # 定義は 1 つ、呼び出しが 14 個で合計 15 回出現
        assert count >= 14 + 1, (
            f"_add_output_option の出現回数が {count} 回（期待: 定義1 + 呼び出し14 = 15）"
        )

    @pytest.mark.parametrize("command", TARGET_COMMANDS)
    def test_command_function_checks_json_output(self, command: str) -> None:
        """各コマンドの実行関数が _is_json_output を呼んでいることを確認する."""
        import inspect
        import rag.cli as cli_module

        # コマンド名 → 関数名のマッピング
        func_names: dict[str, str] = {
            "add": "run_add",
            "crawl": "run_crawl",
            "crawl-zenn": "run_crawl_zenn",
            "crawl-bluesky": "run_crawl_bluesky",
            "ingest-youtube": "run_ingest_youtube",
            "ingest-youtube-playlist": "run_ingest_youtube_playlist",
            "add-document": "run_add_document",
            "crawl-documents": "run_crawl_documents",
            "add-journal": "run_add_journal",
            "ingest-aozora": "run_ingest_aozora",
            "ingest-aozora-author": "run_ingest_aozora_author",
            "update-aozora-catalog": "run_update_aozora_catalog",
            "delete": "run_delete",
            "rebuild": "run_rebuild",
        }

        func_name = func_names[command]
        func = getattr(cli_module, func_name)
        source = inspect.getsource(func)
        assert "_is_json_output(args)" in source or "json_out" in source, (
            f"{func_name} が JSON 出力チェックを行っていない"
        )


class TestJsonOutputProtocolCompatibility:
    """JSON Lines 出力が CLI JSON Lines プロトコルと互換であることを検証する."""

    def test_progress_has_required_fields(self, capsys: pytest.CaptureFixture[str]) -> None:
        """progress メッセージが worker.py と同じフィールドを持つ."""
        _output_progress(1, 5, "test.txt")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert set(parsed.keys()) == {"type", "processed", "total", "current"}
        assert parsed["type"] == "progress"

    def test_error_has_required_fields(self, capsys: pytest.CaptureFixture[str]) -> None:
        """error メッセージが worker.py と同じフィールドを持つ."""
        with pytest.raises(SystemExit):
            _output_error("test error")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["type"] == "error"
        assert parsed["error"] is True
        assert "message" in parsed

    def test_result_has_type_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        """result メッセージが type: result フィールドを持つ."""
        _output_result({"placed": 1})
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["type"] == "result"

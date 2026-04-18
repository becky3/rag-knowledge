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
    _JsonAwareArgumentParser,
    _output_error,
    _output_json,
    _output_progress,
    _output_result,
)
from rag.errors import CliErrorCode


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
            _output_error(CliErrorCode.INTERNAL_ERROR, "something went wrong")
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed == {
            "type": "error",
            "error": True,
            "code": "INTERNAL_ERROR",
            "message": "something went wrong",
        }

    def test_output_error_with_details(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _output_error(
                CliErrorCode.VALIDATION_ERROR,
                "invalid value",
                details={"field": "mode", "value": "invalid"},
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["code"] == "VALIDATION_ERROR"
        assert parsed["details"] == {"field": "mode", "value": "invalid"}

    def test_output_error_without_details_omits_key(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            _output_error(CliErrorCode.LOCK_CONFLICT, "locked")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert "details" not in parsed

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


class TestJsonAwareArgumentParser:
    """_JsonAwareArgumentParser のバリデーションエラー挙動."""

    @pytest.fixture
    def parser(self) -> _JsonAwareArgumentParser:
        parser = _JsonAwareArgumentParser(prog="testcli")
        parser.add_argument("--mode", choices=["a", "b"], required=True)
        parser.add_argument("--output", choices=["text", "json"], default="text")
        return parser

    def test_json_mode_exits_with_1_and_json_error(
        self,
        parser: _JsonAwareArgumentParser,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--output json 指定時はバリデーション失敗で exit 1 + JSON error を出す."""
        monkeypatch.setattr("sys.argv", ["testcli", "--mode", "invalid", "--output", "json"])
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--mode", "invalid", "--output", "json"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["type"] == "error"
        assert parsed["error"] is True
        assert parsed["code"] == "VALIDATION_ERROR"
        assert "invalid choice" in parsed["message"]

    def test_json_equals_form_exits_with_1(
        self,
        parser: _JsonAwareArgumentParser,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--output=json 形式も検出される."""
        monkeypatch.setattr("sys.argv", ["testcli", "--mode", "invalid", "--output=json"])
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--mode", "invalid", "--output=json"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["type"] == "error"

    def test_text_mode_exits_with_1_and_stderr_message(
        self,
        parser: _JsonAwareArgumentParser,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """text モードでも exit 1 に統一し、メッセージは stderr に出す（2 値契約）.

        argparse 標準の exit 2 は CLI 2 値契約（0/1）と矛盾するため、
        本プロジェクトでは text モードでも exit 1 に統一する。
        POSIX の usage-error 慣習より契約の一貫性を優先する。
        """
        monkeypatch.setattr("sys.argv", ["testcli", "--mode", "invalid"])
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--mode", "invalid"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        # text モードでは stderr にエラー、stdout は空
        assert captured.out == ""
        assert "invalid choice" in captured.err


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

    @staticmethod
    def _make_ingest_result(
        *,
        placed: int = 0,
        skipped: int = 0,
        overwritten: int = 0,
        errors: int = 0,
        error_details: list[dict[str, object]] | None = None,
        partial_failures: int = 0,
        partial_failure_details: list[dict[str, object]] | None = None,
        aborted: bool = False,
        abort_reason: str | None = None,
    ) -> MagicMock:
        """IngestResult のモックを生成する."""
        mock = MagicMock()
        mock.placed = placed
        mock.skipped = skipped
        mock.overwritten = overwritten
        mock.errors = errors
        mock.error_details = error_details or []
        mock.partial_failures = partial_failures
        mock.partial_failure_details = partial_failure_details or []
        mock.aborted = aborted
        mock.abort_reason = abort_reason
        return mock

    def test_without_pipeline_summary(self) -> None:
        ingest_result = self._make_ingest_result(placed=2, skipped=1)

        result = _ingest_result_to_dict(ingest_result, None)
        assert result == {
            "placed": 2,
            "skipped": 1,
            "overwritten": 0,
            "errors": 0,
            "error_details": [],
            "partial_failures": 0,
            "partial_failure_details": [],
            "aborted": False,
            "abort_reason": None,
        }

    def test_with_pipeline_summary(self) -> None:
        ingest_result = self._make_ingest_result(placed=1)

        pipeline_summary = MagicMock()
        pipeline_summary.mode.value = "incremental"
        pipeline_summary.total_files = 5
        pipeline_summary.processed = 3
        pipeline_summary.errors = []
        pipeline_summary.warnings = []

        result = _ingest_result_to_dict(ingest_result, pipeline_summary)
        assert result["placed"] == 1
        assert "pipeline" in result
        assert result["pipeline"]["mode"] == "incremental"
        assert result["pipeline"]["processed"] == 3
        assert "skipped" not in result["pipeline"]

    def test_includes_observability_fields_with_values(self) -> None:
        """partial_failures / aborted 等がゼロ値でない場合も正しくシリアライズされる.

        失敗の観測性（仕様: docs/specs/ingesters/common.md）を production 出力に
        届けるため、IngestResult の全観測性フィールドを JSON に含めなければならない。
        """
        partial_details = [{"target": "a.png", "category": "media_download"}]
        ingest_result = self._make_ingest_result(
            placed=10,
            partial_failures=2,
            partial_failure_details=partial_details,
            aborted=True,
            abort_reason="circuit breaker open",
        )

        result = _ingest_result_to_dict(ingest_result, None)
        assert result["partial_failures"] == 2
        assert result["partial_failure_details"] == partial_details
        assert result["aborted"] is True
        assert result["abort_reason"] == "circuit breaker open"

    def test_aborted_false_is_preserved(self) -> None:
        """aborted が False でも省略されず False として出力される.

        スケジューラが `.get("aborted", False)` で補完に依存せず、
        常にキーが存在する前提で判定できるようにする。
        """
        ingest_result = self._make_ingest_result()

        result = _ingest_result_to_dict(ingest_result, None)
        assert "aborted" in result
        assert result["aborted"] is False
        assert "abort_reason" in result
        assert result["abort_reason"] is None
        assert "partial_failures" in result
        assert "partial_failure_details" in result


class TestCommandsHaveOutputOption:
    """12 対象コマンドに --output オプションが追加されていることを確認する.

    cli.py のソースコードを解析して、対象コマンドの直後に
    _add_output_option() 呼び出しがあることを検証する。
    """

    TARGET_COMMANDS = [
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

    def test_all_12_commands_have_output_option(self) -> None:
        """cli.py のソースに 12 コマンド分の _add_output_option 呼び出しがある."""
        import inspect
        import rag.cli as cli_module

        source = inspect.getsource(cli_module)
        count = source.count("_add_output_option(")
        # ヘルパー関数の定義 (def _add_output_option) は除外して呼び出しだけ数える
        # 定義は 1 つ、呼び出しが 12 個で合計 13 回出現
        assert count >= 12 + 1, (
            f"_add_output_option の出現回数が {count} 回（期待: 定義1 + 呼び出し12 = 13）"
        )

    @pytest.mark.parametrize("command", TARGET_COMMANDS)
    def test_command_function_checks_json_output(self, command: str) -> None:
        """各コマンドの実行関数が _is_json_output を呼んでいることを確認する."""
        import inspect
        import rag.cli as cli_module

        # コマンド名 → 関数名のマッピング
        func_names: dict[str, str] = {
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
        """error メッセージが必須フィールド（type, error, code, message）を持つ."""
        with pytest.raises(SystemExit):
            _output_error(CliErrorCode.INTERNAL_ERROR, "test error")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["type"] == "error"
        assert parsed["error"] is True
        assert parsed["code"] == "INTERNAL_ERROR"
        assert "message" in parsed

    def test_result_has_type_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        """result メッセージが type: result フィールドを持つ."""
        _output_result({"placed": 1})
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["type"] == "result"

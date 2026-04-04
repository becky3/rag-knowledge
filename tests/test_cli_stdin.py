"""CLI --stdin オプションのテスト.

add-journal --stdin と add-document --stdin の動作を検証する。
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestAddJournalStdinParsing:
    """add-journal パーサーの --stdin / --file 排他テスト."""

    def _parse(self, args: list[str]) -> argparse.Namespace:
        """CLI パーサーを構築して引数をパースする."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        aj_parser = subparsers.add_parser("add-journal")
        aj_parser.add_argument("--title", "-t", required=True)
        aj_input_group = aj_parser.add_mutually_exclusive_group(required=True)
        aj_input_group.add_argument("--file", "-f")
        aj_input_group.add_argument("--stdin", action="store_true", default=False)
        aj_parser.add_argument("--repository", "-r", required=True)
        aj_parser.add_argument("--entry-id", "-e", default=None)
        aj_parser.add_argument("--output", dest="output_format", choices=["text", "json"], default="text")
        return parser.parse_args(args)

    def test_stdin_flag_is_set(self) -> None:
        args = self._parse(["add-journal", "--stdin", "--title", "T", "--repository", "R"])
        assert args.stdin is True
        assert args.file is None

    def test_file_flag_is_set(self) -> None:
        args = self._parse(["add-journal", "--file", "test.md", "--title", "T", "--repository", "R"])
        assert args.stdin is False
        assert args.file == "test.md"

    def test_stdin_and_file_are_mutually_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            self._parse(["add-journal", "--stdin", "--file", "test.md", "--title", "T", "--repository", "R"])

    def test_neither_stdin_nor_file_raises_error(self) -> None:
        with pytest.raises(SystemExit):
            self._parse(["add-journal", "--title", "T", "--repository", "R"])


class TestAddDocumentStdinParsing:
    """add-document パーサーの --stdin / --file 排他テスト."""

    def _parse(self, args: list[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        adddoc_parser = subparsers.add_parser("add-document")
        input_group = adddoc_parser.add_mutually_exclusive_group(required=True)
        input_group.add_argument("--file", dest="file_path")
        input_group.add_argument("--stdin", action="store_true", default=False)
        adddoc_parser.add_argument("--filename", default=None)
        adddoc_parser.add_argument("--encoding", choices=["text", "base64"], default="text")
        adddoc_parser.add_argument("--upload-mode", choices=["fail", "replace"], default="fail")
        adddoc_parser.add_argument("--output", dest="output_format", choices=["text", "json"], default="text")
        return parser.parse_args(args)

    def test_stdin_flag_is_set(self) -> None:
        args = self._parse(["add-document", "--stdin", "--filename", "test.md"])
        assert args.stdin is True
        assert args.file_path is None

    def test_file_flag_is_set(self) -> None:
        args = self._parse(["add-document", "--file", "test.md"])
        assert args.stdin is False
        assert args.file_path == "test.md"

    def test_stdin_and_file_are_mutually_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            self._parse(["add-document", "--stdin", "--file", "test.md", "--filename", "test.md"])

    def test_neither_stdin_nor_file_raises_error(self) -> None:
        with pytest.raises(SystemExit):
            self._parse(["add-document"])

    def test_encoding_default_is_text(self) -> None:
        args = self._parse(["add-document", "--stdin", "--filename", "test.md"])
        assert args.encoding == "text"

    def test_encoding_base64(self) -> None:
        args = self._parse(["add-document", "--stdin", "--filename", "test.pdf", "--encoding", "base64"])
        assert args.encoding == "base64"


class TestAddJournalStdinExecution:
    """add-journal --stdin の実行テスト (ロック競合含む)."""

    def test_stdin_empty_input_exits_with_error(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """stdin が空の場合、exit code 1 でエラー終了する."""
        args = argparse.Namespace(
            stdin=True,
            file=None,
            title="Test",
            repository="test-repo",
            entry_id=None,
            output_format="json",
        )

        with (
            patch("sys.stdin", io.StringIO("")),
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
        ):
            mock_controller = MagicMock()
            mock_controller.source_store.root_dir = Path("/tmp/test_store")
            mock_ctrl.return_value = (mock_controller, MagicMock())

            with pytest.raises(SystemExit) as exc_info:
                import asyncio
                from rag.cli import run_add_journal
                asyncio.run(run_add_journal(args))

            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["type"] == "error"
        assert "空" in parsed["message"]

    def test_lock_contention_exits_with_error(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """ロック競合時、exit code 1 でエラー終了する."""
        from rag.infrastructure.file_lock import LockAcquisitionError

        args = argparse.Namespace(
            stdin=True,
            file=None,
            title="Test",
            repository="test-repo",
            entry_id=None,
            output_format="json",
        )

        mock_lock = MagicMock()
        mock_lock.acquire.side_effect = LockAcquisitionError("locked")

        with (
            patch("sys.stdin", io.StringIO("some content")),
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
            patch("rag.infrastructure.file_lock.ingest_lock", return_value=mock_lock),
        ):
            mock_controller = MagicMock()
            mock_controller.source_store.root_dir = Path("/tmp/test_store")
            mock_ctrl.return_value = (mock_controller, MagicMock())

            with pytest.raises(SystemExit) as exc_info:
                import asyncio
                from rag.cli import run_add_journal
                asyncio.run(run_add_journal(args))

            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["type"] == "error"
        assert "ロック競合" in parsed["message"]


class TestAddDocumentStdinExecution:
    """add-document --stdin の実行テスト."""

    def test_stdin_missing_filename_exits_with_error(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--stdin 使用時に --filename が未指定ならエラー終了する."""
        args = argparse.Namespace(
            stdin=True,
            file_path=None,
            filename=None,
            encoding="text",
            upload_mode="fail",
            output_format="json",
        )

        with patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl:
            mock_controller = MagicMock()
            mock_settings = MagicMock()
            mock_settings.rag_document_supported_extensions = ".md,.txt,.pdf,.adoc"
            mock_ctrl.return_value = (mock_controller, mock_settings)

            with pytest.raises(SystemExit) as exc_info:
                import asyncio
                from rag.cli import run_add_document
                asyncio.run(run_add_document(args))

            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["type"] == "error"
        assert "--filename" in parsed["message"]

    def test_stdin_empty_input_exits_with_error(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """stdin が空の場合、exit code 1 でエラー終了する."""
        args = argparse.Namespace(
            stdin=True,
            file_path=None,
            filename="test.md",
            encoding="text",
            upload_mode="fail",
            output_format="json",
        )

        with (
            patch("sys.stdin", io.StringIO("")),
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
        ):
            mock_controller = MagicMock()
            mock_settings = MagicMock()
            mock_settings.rag_document_supported_extensions = ".md,.txt,.pdf,.adoc"
            mock_ctrl.return_value = (mock_controller, mock_settings)

            with pytest.raises(SystemExit) as exc_info:
                import asyncio
                from rag.cli import run_add_document
                asyncio.run(run_add_document(args))

            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["type"] == "error"
        assert "空" in parsed["message"]

    def test_stdin_unsupported_extension_exits_with_error(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """未対応拡張子の場合、exit code 1 でエラー終了する."""
        args = argparse.Namespace(
            stdin=True,
            file_path=None,
            filename="test.xyz",
            encoding="text",
            upload_mode="fail",
            output_format="json",
        )

        with (
            patch("sys.stdin", io.StringIO("content")),
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
        ):
            mock_controller = MagicMock()
            mock_settings = MagicMock()
            mock_settings.rag_document_supported_extensions = ".md,.txt,.pdf,.adoc"
            mock_ctrl.return_value = (mock_controller, mock_settings)

            with pytest.raises(SystemExit) as exc_info:
                import asyncio
                from rag.cli import run_add_document
                asyncio.run(run_add_document(args))

            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["type"] == "error"
        assert "対応していない" in parsed["message"]

    def test_lock_contention_exits_with_error(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """ロック競合時、exit code 1 でエラー終了する."""
        from rag.infrastructure.file_lock import LockAcquisitionError

        args = argparse.Namespace(
            stdin=True,
            file_path=None,
            filename="test.md",
            encoding="text",
            upload_mode="fail",
            output_format="json",
        )

        mock_lock = MagicMock()
        mock_lock.acquire.side_effect = LockAcquisitionError("locked")

        with (
            patch("sys.stdin", io.StringIO("some content")),
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
            patch("rag.infrastructure.file_lock.ingest_lock", return_value=mock_lock),
        ):
            mock_controller = MagicMock()
            mock_controller.source_store.root_dir = Path("/tmp/test_store")
            mock_settings = MagicMock()
            mock_settings.rag_document_supported_extensions = ".md,.txt,.pdf,.adoc"
            mock_ctrl.return_value = (mock_controller, mock_settings)

            with pytest.raises(SystemExit) as exc_info:
                import asyncio
                from rag.cli import run_add_document
                asyncio.run(run_add_document(args))

            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["type"] == "error"
        assert "ロック競合" in parsed["message"]

    def test_file_exists_error_exits_with_error(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """upload_mode=fail で同名ファイルが存在する場合、exit code 1 でエラー終了する."""
        args = argparse.Namespace(
            stdin=True,
            file_path=None,
            filename="test.md",
            encoding="text",
            upload_mode="fail",
            output_format="json",
        )

        mock_lock = MagicMock()
        mock_ingester = MagicMock()
        mock_ingester.return_value.add_document.side_effect = FileExistsError("test.md already exists")

        with (
            patch("sys.stdin", io.StringIO("some content")),
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
            patch("rag.infrastructure.file_lock.ingest_lock", return_value=mock_lock),
            patch("rag.pipeline.ingesters.local.LocalIngester", mock_ingester),
        ):
            mock_controller = MagicMock()
            mock_controller.source_store.root_dir = Path("/tmp/test_store")
            mock_settings = MagicMock()
            mock_settings.rag_document_supported_extensions = ".md,.txt,.pdf,.adoc"
            mock_ctrl.return_value = (mock_controller, mock_settings)

            with pytest.raises(SystemExit) as exc_info:
                import asyncio
                from rag.cli import run_add_document
                asyncio.run(run_add_document(args))

            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["type"] == "error"
        assert "同名ファイル" in parsed["message"]
        assert "test.md" in parsed["message"]


class TestAddDocumentFilenamePriority:
    """--file モードで --filename が指定された場合の優先度テスト."""

    @staticmethod
    def _make_ingest_result_mock() -> MagicMock:
        mock = MagicMock()
        mock.placed = 1
        mock.skipped = 0
        mock.overwritten = 0
        mock.errors = 0
        mock.error_details = []
        return mock

    @staticmethod
    def _make_pipeline_summary_mock() -> MagicMock:
        mock = MagicMock()
        mock.mode.value = "incremental"
        mock.total_files = 1
        mock.processed = 1
        mock.skipped = 0
        mock.errors = []
        mock.warnings = []
        return mock

    def test_filename_overrides_file_path_name(self, tmp_path: Path) -> None:
        """--filename が指定されていれば一時ファイルパスではなくそちらが使われる."""
        temp_file = tmp_path / "tmp12345678.pdf"
        temp_file.write_bytes(b"%PDF-1.4 test content")

        args = argparse.Namespace(
            stdin=False,
            file_path=str(temp_file),
            filename="resume.pdf",
            encoding="text",
            upload_mode="fail",
            output_format="json",
        )

        mock_lock = MagicMock()
        mock_ingester = MagicMock()
        mock_ingester.return_value.add_document.return_value = self._make_ingest_result_mock()

        with (
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
            patch("rag.infrastructure.file_lock.ingest_lock", return_value=mock_lock),
            patch("rag.pipeline.ingesters.local.LocalIngester", mock_ingester),
        ):
            mock_controller = MagicMock()
            mock_controller.source_store.root_dir = tmp_path
            mock_controller.ingest_and_index.return_value = self._make_pipeline_summary_mock()
            mock_settings = MagicMock()
            mock_settings.rag_document_supported_extensions = ".md,.txt,.pdf,.adoc"
            mock_ctrl.return_value = (mock_controller, mock_settings)

            import asyncio
            from rag.cli import run_add_document
            asyncio.run(run_add_document(args))

        call_args = mock_ingester.return_value.add_document.call_args
        actual_filename = call_args[0][1]
        assert actual_filename == "resume.pdf", (
            f"Expected 'resume.pdf' but got '{actual_filename}' — "
            f"--filename が無視され一時ファイル名が使われている"
        )

    def test_file_mode_without_filename_uses_resolved_name(self, tmp_path: Path) -> None:
        """--filename 未指定時はファイルパスのファイル名部分が使われる."""
        test_file = tmp_path / "notes.md"
        test_file.write_text("# Notes", encoding="utf-8")

        args = argparse.Namespace(
            stdin=False,
            file_path=str(test_file),
            filename=None,
            encoding="text",
            upload_mode="fail",
            output_format="json",
        )

        mock_lock = MagicMock()
        mock_ingester = MagicMock()
        mock_ingester.return_value.add_document.return_value = self._make_ingest_result_mock()

        with (
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
            patch("rag.infrastructure.file_lock.ingest_lock", return_value=mock_lock),
            patch("rag.pipeline.ingesters.local.LocalIngester", mock_ingester),
        ):
            mock_controller = MagicMock()
            mock_controller.source_store.root_dir = tmp_path
            mock_controller.ingest_and_index.return_value = self._make_pipeline_summary_mock()
            mock_settings = MagicMock()
            mock_settings.rag_document_supported_extensions = ".md,.txt,.pdf,.adoc"
            mock_ctrl.return_value = (mock_controller, mock_settings)

            import asyncio
            from rag.cli import run_add_document
            asyncio.run(run_add_document(args))

        call_args = mock_ingester.return_value.add_document.call_args
        actual_filename = call_args[0][1]
        assert actual_filename == "notes.md"

"""migrate-journal CLI サブコマンドのテスト

FR-2: 初回マイグレーション CLI
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rag.cli import run_migrate_journal
from rag.pipeline.ingesters._common import IngestResult

_PATCH_GET_SETTINGS = "rag.config.get_settings"
_PATCH_SOURCE_STORE = "rag.store.source_store.SourceStore"
_PATCH_JOURNAL_INGESTER = "rag.pipeline.ingesters.journal.JournalIngester"


class TestRunMigrateJournal:
    """run_migrate_journal CLI 関数のテスト."""

    @pytest.fixture()
    def journal_dir(self, tmp_path: Path) -> Path:
        """テスト用ジャーナルディレクトリを作成する."""
        d = tmp_path / "journals"
        d.mkdir()
        (d / "20260323-143000-test-entry.md").write_text(
            "# Test Entry\n\nBody text.", encoding="utf-8",
        )
        (d / "20260323-150000-another-entry.md").write_text(
            "# Another Entry\n\nMore content.", encoding="utf-8",
        )
        return d

    def _make_args(self, dir_path: str, repository: str) -> argparse.Namespace:
        return argparse.Namespace(dir=dir_path, repository=repository)

    @patch(_PATCH_GET_SETTINGS)
    @patch(_PATCH_SOURCE_STORE)
    @patch(_PATCH_JOURNAL_INGESTER)
    def test_successful_migration(
        self,
        mock_ingester_cls: MagicMock,
        mock_store_cls: MagicMock,
        mock_get_settings: MagicMock,
        journal_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """正常なマイグレーション実行."""
        mock_get_settings.return_value.source_store_dir = "/tmp/source_store"

        result = IngestResult()
        result.placed = 2
        mock_ingester_cls.return_value.import_directory.return_value = result

        args = self._make_args(str(journal_dir), "test-repo")
        run_migrate_journal(args)

        mock_ingester_cls.return_value.import_directory.assert_called_once_with(
            dir_path=str(journal_dir),
            repository="test-repo",
        )
        captured = capsys.readouterr()
        assert "2件配置" in captured.out
        assert "rebuild --mode incremental" in captured.out

    @patch(_PATCH_GET_SETTINGS)
    @patch(_PATCH_SOURCE_STORE)
    @patch(_PATCH_JOURNAL_INGESTER)
    def test_no_files_placed_skips_postprocess_message(
        self,
        mock_ingester_cls: MagicMock,
        mock_store_cls: MagicMock,
        mock_get_settings: MagicMock,
        journal_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """配置0件の場合はインデックス構築メッセージが出ない."""
        mock_get_settings.return_value.source_store_dir = "/tmp/source_store"

        result = IngestResult()
        result.placed = 0
        mock_ingester_cls.return_value.import_directory.return_value = result

        args = self._make_args(str(journal_dir), "test-repo")
        run_migrate_journal(args)

        captured = capsys.readouterr()
        assert "0件配置" in captured.out
        assert "rebuild" not in captured.out

    @patch(_PATCH_GET_SETTINGS)
    def test_missing_source_store_dir_exits(
        self,
        mock_get_settings: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """source_store_dir 未設定時は sys.exit(1)."""
        mock_get_settings.return_value.source_store_dir = ""

        args = self._make_args("/any/dir", "test-repo")
        with pytest.raises(SystemExit) as exc_info:
            run_migrate_journal(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "source_store_dir" in captured.err

    @patch(_PATCH_GET_SETTINGS)
    @patch(_PATCH_SOURCE_STORE)
    @patch(_PATCH_JOURNAL_INGESTER)
    def test_store_close_called_on_success(
        self,
        mock_ingester_cls: MagicMock,
        mock_store_cls: MagicMock,
        mock_get_settings: MagicMock,
        journal_dir: Path,
    ) -> None:
        """正常終了時に store.close() が呼ばれる."""
        mock_get_settings.return_value.source_store_dir = "/tmp/source_store"

        result = IngestResult()
        result.placed = 1
        mock_ingester_cls.return_value.import_directory.return_value = result

        args = self._make_args(str(journal_dir), "test-repo")
        run_migrate_journal(args)

        mock_store_cls.return_value.close.assert_called_once()

    @patch(_PATCH_GET_SETTINGS)
    @patch(_PATCH_SOURCE_STORE)
    @patch(_PATCH_JOURNAL_INGESTER)
    def test_store_close_called_on_error(
        self,
        mock_ingester_cls: MagicMock,
        mock_store_cls: MagicMock,
        mock_get_settings: MagicMock,
        journal_dir: Path,
    ) -> None:
        """例外発生時でも store.close() が呼ばれる."""
        mock_get_settings.return_value.source_store_dir = "/tmp/source_store"
        mock_ingester_cls.return_value.import_directory.side_effect = RuntimeError("fail")

        args = self._make_args(str(journal_dir), "test-repo")
        with pytest.raises(RuntimeError, match="fail"):
            run_migrate_journal(args)

        mock_store_cls.return_value.close.assert_called_once()

    @patch(_PATCH_GET_SETTINGS)
    @patch(_PATCH_SOURCE_STORE)
    @patch(_PATCH_JOURNAL_INGESTER)
    def test_store_initialize_called(
        self,
        mock_ingester_cls: MagicMock,
        mock_store_cls: MagicMock,
        mock_get_settings: MagicMock,
        journal_dir: Path,
    ) -> None:
        """store.initialize() が呼ばれる."""
        mock_get_settings.return_value.source_store_dir = "/tmp/source_store"

        result = IngestResult()
        mock_ingester_cls.return_value.import_directory.return_value = result

        args = self._make_args(str(journal_dir), "test-repo")
        run_migrate_journal(args)

        mock_store_cls.return_value.initialize.assert_called_once()

    @patch(_PATCH_GET_SETTINGS)
    @patch(_PATCH_SOURCE_STORE)
    @patch(_PATCH_JOURNAL_INGESTER)
    def test_result_with_errors_shows_details(
        self,
        mock_ingester_cls: MagicMock,
        mock_store_cls: MagicMock,
        mock_get_settings: MagicMock,
        journal_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """エラー件数を含む結果が表示される."""
        mock_get_settings.return_value.source_store_dir = "/tmp/source_store"

        result = IngestResult()
        result.placed = 1
        result.errors = 1
        result.error_details = [{
            "category": "placement",
            "target": "/path/to/failed.md",
            "message": "permission denied",
        }]
        mock_ingester_cls.return_value.import_directory.return_value = result

        args = self._make_args(str(journal_dir), "test-repo")
        run_migrate_journal(args)

        captured = capsys.readouterr()
        assert "1件配置" in captured.out
        assert "エラー: 1件" in captured.out

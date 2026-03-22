"""再構築・統計ツールのテスト.

仕様: docs/specs/rebuild-stats.md

rag_rebuild MCP ツール・CLI、rag_stats 拡張の振る舞いを検証する。
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rag.pipeline.models import PipelineMode, PipelineSummary
from rag.server import (
    _collect_converted_store_stats,
    _collect_pipeline_stats,
    _collect_source_store_stats,
    _format_rebuild_summary,
    _format_size,
    _rebuild_lock,
    _reset_rag_service,
)
from rag.store.metadata_db import MetadataDB


def _make_mock_process(
    stdout_data: bytes, stderr_data: bytes, returncode: int,
) -> AsyncMock:
    """readline ベースの _run_worker_subprocess 用モックプロセスを生成する."""
    mock_process = AsyncMock()

    # stdout: readline で行単位返却 → b"" で EOF
    lines: list[bytes] = []
    if stdout_data:
        lines = [line + b"\n" for line in stdout_data.split(b"\n") if line]
    lines.append(b"")  # EOF

    mock_stdout = AsyncMock()
    mock_stdout.readline = AsyncMock(side_effect=lines)
    mock_process.stdout = mock_stdout

    # stderr: read で一括返却
    mock_stderr = AsyncMock()
    mock_stderr.read = AsyncMock(return_value=stderr_data)
    mock_process.stderr = mock_stderr

    mock_process.returncode = returncode
    mock_process.wait = AsyncMock()

    return mock_process


@pytest.fixture(autouse=True)
def _reset_rag_global_state() -> None:
    """各テスト前にRAGサービスのグローバル状態をリセットする."""
    _reset_rag_service()
    # rebuild_lock がテスト間で残っている場合に解放
    if _rebuild_lock.locked():
        _rebuild_lock.release()


# --- _format_size テスト ---


class TestFormatSize:
    """サイズ表示の自動単位変換テスト."""

    def test_bytes(self) -> None:
        assert _format_size(0) == "0 B"
        assert _format_size(512) == "512 B"
        assert _format_size(1023) == "1023 B"

    def test_kilobytes(self) -> None:
        assert _format_size(1024) == "1.0 KB"
        assert _format_size(1536) == "1.5 KB"

    def test_megabytes(self) -> None:
        assert _format_size(1024 * 1024) == "1.0 MB"
        assert _format_size(int(45.6 * 1024 * 1024)) == "45.6 MB"

    def test_gigabytes(self) -> None:
        assert _format_size(1024 * 1024 * 1024) == "1.0 GB"
        assert _format_size(int(2.5 * 1024 * 1024 * 1024)) == "2.5 GB"


# --- _format_rebuild_summary テスト ---


class TestFormatRebuildSummary:
    """再構築サマリのフォーマットテスト."""

    def test_success_summary(self) -> None:
        summary = PipelineSummary(
            mode=PipelineMode.FULL_REBUILD,
            total_files=10,
            processed=8,
            skipped=2,
        )
        result = _format_rebuild_summary(summary, 12.5)
        assert "全再構築" in result
        assert "処理件数: 8" in result
        assert "スキップ: 2" in result
        assert "エラー: 0" in result
        assert "12.5 秒" in result

    def test_with_errors(self) -> None:
        summary = PipelineSummary(
            mode=PipelineMode.INDEX_ONLY,
            total_files=5,
            processed=3,
            skipped=2,
            errors=["file1.txt", "file2.txt"],
        )
        result = _format_rebuild_summary(summary, 5.0)
        assert "インデックスのみ再構築" in result
        assert "エラー: 2" in result
        assert "file1.txt" in result
        assert "file2.txt" in result

    def test_all_modes(self) -> None:
        mode_labels = {
            PipelineMode.FULL_REBUILD: "全再構築",
            PipelineMode.CONVERT_ONLY: "コンバートのみ再実行",
            PipelineMode.INDEX_ONLY: "インデックスのみ再構築",
            PipelineMode.INCREMENTAL: "差分更新",
        }
        for mode, label in mode_labels.items():
            summary = PipelineSummary(
                mode=mode, total_files=0, processed=0, skipped=0,
            )
            result = _format_rebuild_summary(summary, 0.0)
            assert label in result


# --- _collect_source_store_stats テスト ---


class TestCollectSourceStoreStats:
    """source_store 統計収集のテスト."""

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        stats = _collect_source_store_stats(tmp_path / "nonexistent")
        assert stats["total_files"] == 0
        assert stats["total_size"] == 0
        assert stats["by_type"] == {}

    def test_empty_dir(self, tmp_path: Path) -> None:
        stats = _collect_source_store_stats(tmp_path)
        assert stats["total_files"] == 0

    def test_counts_data_files_by_type(self, tmp_path: Path) -> None:
        # web ファイル
        web_dir = tmp_path / "web"
        web_dir.mkdir()
        (web_dir / "page1.html").write_text("hello", encoding="utf-8")
        (web_dir / "page2.html").write_text("world!", encoding="utf-8")

        # bluesky ファイル
        bs_dir = tmp_path / "bluesky"
        bs_dir.mkdir()
        (bs_dir / "post1.json").write_text("{}", encoding="utf-8")

        # local ファイル（ルート直下）
        (tmp_path / "doc.md").write_text("# Title", encoding="utf-8")

        stats = _collect_source_store_stats(tmp_path)
        assert stats["total_files"] == 4
        assert stats["by_type"]["web"]["files"] == 2
        assert stats["by_type"]["bluesky"]["files"] == 1
        assert stats["by_type"]["local"]["files"] == 1

    def test_excludes_meta_and_db_files(self, tmp_path: Path) -> None:
        web_dir = tmp_path / "web"
        web_dir.mkdir()
        (web_dir / "page.html").write_text("data", encoding="utf-8")
        (web_dir / "page.html.meta").write_text("meta", encoding="utf-8")
        (tmp_path / "metadata.db").write_bytes(b"\x00" * 100)
        (tmp_path / "metadata.db-wal").write_bytes(b"\x00")
        (tmp_path / ".gitignore").write_text("*.pyc", encoding="utf-8")

        stats = _collect_source_store_stats(tmp_path)
        assert stats["total_files"] == 1  # page.html のみ


# --- _collect_converted_store_stats テスト ---


class TestCollectConvertedStoreStats:
    """converted_store 統計収集のテスト."""

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        stats = _collect_converted_store_stats(tmp_path / "nonexistent")
        assert stats["total_files"] == 0
        assert stats["total_size"] == 0

    def test_counts_all_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("content a", encoding="utf-8")
        sub = tmp_path / "web"
        sub.mkdir()
        (sub / "b.md").write_text("content b", encoding="utf-8")

        stats = _collect_converted_store_stats(tmp_path)
        assert stats["total_files"] == 2
        assert stats["total_size"] > 0


# --- _collect_pipeline_stats テスト ---


class TestCollectPipelineStats:
    """metadata.db からのパイプライン統計収集テスト."""

    def test_no_db(self, tmp_path: Path) -> None:
        result = _collect_pipeline_stats(tmp_path)
        assert result is None

    def test_empty_history(self, tmp_path: Path) -> None:
        db = MetadataDB(tmp_path / "metadata.db")
        db.initialize()
        db.close()

        result = _collect_pipeline_stats(tmp_path)
        assert result is not None
        assert result["last_processed_at"] is None
        assert result["execution_count"] == 0
        assert result["deleted_count"] == 0

    def test_with_history(self, tmp_path: Path) -> None:
        db = MetadataDB(tmp_path / "metadata.db")
        db.initialize()
        db.add_pipeline_history(
            from_commit_id="aaa",
            to_commit_id="bbb",
            processed_at="2026-03-19T10:00:00+09:00",
        )
        db.add_pipeline_history(
            from_commit_id="bbb",
            to_commit_id="ccc",
            processed_at="2026-03-19T11:00:00+09:00",
        )
        db.register_source(
            source_id="src1",
            source_type="web",
            file_path="web/test.html",
            title="Test",
            content_hash="abc",
            file_size=100,
            created_at="2026-01-01",
            updated_at="2026-01-01",
        )
        db.set_status("src1", "deleted")
        db.close()

        result = _collect_pipeline_stats(tmp_path)
        assert result is not None
        assert result["last_processed_at"] == "2026-03-19T11:00:00+09:00"
        assert result["execution_count"] == 2
        assert result["last_commit_id"] == "ccc"
        assert result["deleted_count"] == 1


# --- rag_rebuild MCP ツールテスト ---


class TestRagRebuild:
    """rag_rebuild MCP ツールのテスト."""

    @pytest.mark.asyncio
    async def test_invalid_mode(self) -> None:
        from rag.server import rag_rebuild

        result = await rag_rebuild(mode="invalid")
        assert "エラー" in result
        assert "無効なモード" in result

    @pytest.mark.asyncio
    async def test_invalid_source_type(self) -> None:
        from rag.server import rag_rebuild

        result = await rag_rebuild(mode="full", source_type="unknown")
        assert "エラー" in result
        assert "無効な source_type" in result

    @pytest.mark.asyncio
    async def test_incremental_with_source_type(self) -> None:
        from rag.server import rag_rebuild

        result = await rag_rebuild(mode="incremental", source_type="web")
        assert "エラー" in result
        assert "incremental" in result

    @pytest.mark.asyncio
    async def test_source_store_not_configured(self) -> None:
        from rag.server import rag_rebuild

        mock_settings = MagicMock()
        mock_settings.source_store_dir = ""
        mock_settings.converted_store_dir = "./converted"

        with patch("rag.server.get_settings", return_value=mock_settings):
            result = await rag_rebuild(mode="full")
        assert "SOURCE_STORE_DIR" in result

    @pytest.mark.asyncio
    async def test_converted_store_not_configured(self) -> None:
        from rag.server import rag_rebuild

        mock_settings = MagicMock()
        mock_settings.source_store_dir = "./source"
        mock_settings.converted_store_dir = ""

        with patch("rag.server.get_settings", return_value=mock_settings):
            result = await rag_rebuild(mode="full")
        assert "CONVERTED_STORE_DIR" in result

    @pytest.mark.asyncio
    async def test_source_store_dir_not_exists(
        self, tmp_path: Path,
    ) -> None:
        from rag.server import rag_rebuild

        mock_settings = MagicMock()
        mock_settings.source_store_dir = str(tmp_path / "nonexistent")
        mock_settings.converted_store_dir = str(tmp_path / "converted")

        with patch("rag.server.get_settings", return_value=mock_settings):
            result = await rag_rebuild(mode="full")
        assert "source_store ディレクトリが存在しません" in result

    @pytest.mark.asyncio
    async def test_exclusive_lock(self, tmp_path: Path) -> None:
        from rag.server import rag_rebuild

        source_dir = tmp_path / "source"
        source_dir.mkdir()

        mock_settings = MagicMock()
        mock_settings.source_store_dir = str(source_dir)
        mock_settings.converted_store_dir = str(tmp_path / "converted")

        # ロックを先に取得
        _rebuild_lock.acquire()
        try:
            with patch("rag.server.get_settings", return_value=mock_settings):
                result = await rag_rebuild(mode="full")
            assert "別の再構築が実行中" in result
        finally:
            _rebuild_lock.release()

    @pytest.fixture()
    def _source_dir(self, tmp_path: Path) -> Path:
        """テスト用 source_store ディレクトリを作成する."""
        d = tmp_path / "source"
        d.mkdir()
        return d

    @pytest.mark.asyncio
    async def test_full_rebuild_success(
        self, tmp_path: Path, _source_dir: Path,
    ) -> None:
        """サブプロセス経由の full rebuild が正常結果を返すこと."""
        from rag.server import rag_rebuild

        mock_settings = MagicMock()
        mock_settings.source_store_dir = str(_source_dir)
        mock_settings.converted_store_dir = str(tmp_path / "converted")

        mock_result = (
            "再構築完了 (全再構築)\n"
            "  処理件数: 5\n"
            "  スキップ: 0\n"
            "  エラー: 0\n"
            "  所要時間: 1.0 秒"
        )

        with (
            patch("rag.server.get_settings", return_value=mock_settings),
            patch(
                "rag.server._run_rebuild_subprocess",
                return_value=mock_result,
            ),
        ):
            result = await rag_rebuild(mode="full")

        assert "再構築完了" in result
        assert "全再構築" in result
        assert "処理件数: 5" in result

    @pytest.mark.asyncio
    async def test_rebuild_with_source_type(
        self, tmp_path: Path, _source_dir: Path,
    ) -> None:
        """source_type 指定の rebuild が正常に動作すること."""
        from rag.server import rag_rebuild

        mock_settings = MagicMock()
        mock_settings.source_store_dir = str(_source_dir)
        mock_settings.converted_store_dir = str(tmp_path / "converted")

        mock_result = (
            "再構築完了 (コンバートのみ再実行)\n"
            "  処理件数: 3\n"
            "  スキップ: 0\n"
            "  エラー: 0\n"
            "  所要時間: 0.5 秒"
        )

        with (
            patch("rag.server.get_settings", return_value=mock_settings),
            patch(
                "rag.server._run_rebuild_subprocess",
                return_value=mock_result,
            ) as mock_subprocess,
        ):
            result = await rag_rebuild(mode="convert", source_type="web")

        assert "再構築完了" in result
        mock_subprocess.assert_called_once_with("convert", "web", False, ctx=None)

    @pytest.mark.asyncio
    async def test_incremental_mode(
        self, tmp_path: Path, _source_dir: Path,
    ) -> None:
        """incremental モードが正常に動作すること."""
        from rag.server import rag_rebuild

        mock_settings = MagicMock()
        mock_settings.source_store_dir = str(_source_dir)
        mock_settings.converted_store_dir = str(tmp_path / "converted")

        mock_result = (
            "再構築完了 (差分更新)\n"
            "  処理件数: 2\n"
            "  スキップ: 0\n"
            "  エラー: 0\n"
            "  所要時間: 0.3 秒"
        )

        with (
            patch("rag.server.get_settings", return_value=mock_settings),
            patch(
                "rag.server._run_rebuild_subprocess",
                return_value=mock_result,
            ) as mock_subprocess,
        ):
            result = await rag_rebuild(mode="incremental")

        assert "差分更新" in result
        mock_subprocess.assert_called_once_with("incremental", None, False, ctx=None)

    @pytest.mark.asyncio
    async def test_index_only_mode(
        self, tmp_path: Path, _source_dir: Path,
    ) -> None:
        """index モードが source_type 付きで正常に動作すること."""
        from rag.server import rag_rebuild

        mock_settings = MagicMock()
        mock_settings.source_store_dir = str(_source_dir)
        mock_settings.converted_store_dir = str(tmp_path / "converted")

        mock_result = (
            "再構築完了 (インデックスのみ再構築)\n"
            "  処理件数: 4\n"
            "  スキップ: 0\n"
            "  エラー: 0\n"
            "  所要時間: 2.0 秒"
        )

        with (
            patch("rag.server.get_settings", return_value=mock_settings),
            patch(
                "rag.server._run_rebuild_subprocess",
                return_value=mock_result,
            ) as mock_subprocess,
        ):
            result = await rag_rebuild(mode="index", source_type="local")

        assert "インデックスのみ再構築" in result
        mock_subprocess.assert_called_once_with("index", "local", False, ctx=None)

    @pytest.mark.asyncio
    async def test_exception_releases_lock(
        self, tmp_path: Path, _source_dir: Path,
    ) -> None:
        """再構築中に例外が発生してもロックが解放されること."""
        from rag.server import rag_rebuild

        mock_settings = MagicMock()
        mock_settings.source_store_dir = str(_source_dir)
        mock_settings.converted_store_dir = str(tmp_path / "converted")

        with (
            patch("rag.server.get_settings", return_value=mock_settings),
            patch(
                "rag.server._run_rebuild_subprocess",
                side_effect=RuntimeError("subprocess failed"),
            ),
        ):
            result = await rag_rebuild(mode="full")

        assert "エラー" in result
        assert not _rebuild_lock.locked()

    @pytest.mark.asyncio
    async def test_rebuild_resets_rag_service(
        self, tmp_path: Path, _source_dir: Path,
    ) -> None:
        """再構築成功後に RAG サービスがリセットされること."""
        from rag.server import rag_rebuild

        mock_settings = MagicMock()
        mock_settings.source_store_dir = str(_source_dir)
        mock_settings.converted_store_dir = str(tmp_path / "converted")

        mock_result = (
            "再構築完了 (全再構築)\n"
            "  処理件数: 1\n"
            "  スキップ: 0\n"
            "  エラー: 0\n"
            "  所要時間: 0.5 秒"
        )

        with (
            patch("rag.server.get_settings", return_value=mock_settings),
            patch(
                "rag.server._run_rebuild_subprocess",
                return_value=mock_result,
            ),
            patch("rag.server._reset_rag_service") as mock_reset,
        ):
            result = await rag_rebuild(mode="full")

        assert "再構築完了" in result
        mock_reset.assert_called_once()


# --- _run_rebuild_subprocess ユニットテスト ---


class TestRunRebuildSubprocess:
    """_run_rebuild_subprocess のパース/分岐ロジックのテスト."""

    @pytest.mark.asyncio
    async def test_normal_json_result(self) -> None:
        """正常な JSON 結果がフォーマットされること."""
        from rag.server import _run_rebuild_subprocess

        mock_process = _make_mock_process(
            b'{"type":"result","mode":"full_rebuild","total_files":5,"processed":5,'
            b'"skipped":0,"errors":[],"elapsed":1.2}',
            b"", 0,
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await _run_rebuild_subprocess("full", None, False)

        assert "再構築完了" in result
        assert "全再構築" in result
        assert "処理件数: 5" in result

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_invalid_json_result(self) -> None:
        """非 JSON 行のみの場合、結果なしとして処理されること."""
        from rag.server import _run_rebuild_subprocess

        mock_process = _make_mock_process(b"not json", b"", 0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await _run_rebuild_subprocess("full", None, False)

        assert "結果なし" in result

    @pytest.mark.asyncio
    async def test_nonzero_exit_with_stderr(self) -> None:
        """異常終了時に stderr が返されること."""
        from rag.server import _run_rebuild_subprocess

        mock_process = _make_mock_process(b"", b"RuntimeError: DB locked\n", 1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await _run_rebuild_subprocess("full", None, False)

        assert "異常終了" in result
        assert "DB locked" in result

    @pytest.mark.asyncio
    async def test_segfault_exit_code(self) -> None:
        """SEGFAULT exit code でクラッシュメッセージが返されること."""
        from rag.server import _run_rebuild_subprocess

        mock_process = _make_mock_process(b"", b"", -11)

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await _run_rebuild_subprocess("full", None, False)

        assert "クラッシュ" in result
        assert "SEGFAULT" in result

    @pytest.mark.asyncio
    async def test_worker_error_json_used(self) -> None:
        """worker のエラー JSON が異常終了時に活用されること."""
        from rag.server import _run_rebuild_subprocess

        mock_process = _make_mock_process(
            b'{"type":"error","error":true,"message":"source_store not found"}',
            b"Traceback ...\n", 1,
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await _run_rebuild_subprocess("full", None, False)

        assert "source_store not found" in result


# --- _run_ingest_and_index_subprocess ユニットテスト ---


class TestRunIngestAndIndexSubprocess:
    """_run_ingest_and_index_subprocess のパース/分岐ロジックのテスト."""

    @pytest.mark.asyncio
    async def test_normal_json_result(self) -> None:
        """正常な JSON 結果が PipelineSummary として返されること."""
        from rag.server import _run_ingest_and_index_subprocess

        mock_process = _make_mock_process(
            b'{"type":"result","mode":"incremental","total_files":3,"processed":3,'
            b'"skipped":0,"errors":[],"elapsed":0.5}',
            b"", 0,
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await _run_ingest_and_index_subprocess("test commit")

        assert result.processed == 3
        assert result.mode == PipelineMode.INCREMENTAL

    @pytest.mark.asyncio
    async def test_segfault_raises(self) -> None:
        """SEGFAULT exit code で RuntimeError が送出されること."""
        from rag.server import _run_ingest_and_index_subprocess

        mock_process = _make_mock_process(b"", b"", -11)

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.raises(RuntimeError, match="SEGFAULT"):
                await _run_ingest_and_index_subprocess("test commit")

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises(self) -> None:
        """異常終了時に RuntimeError が送出されること."""
        from rag.server import _run_ingest_and_index_subprocess

        mock_process = _make_mock_process(b"", b"some error\n", 1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.raises(RuntimeError, match="異常終了"):
                await _run_ingest_and_index_subprocess("test commit")

    @pytest.mark.asyncio
    async def test_worker_error_json_raises(self) -> None:
        """worker のエラー JSON メッセージが RuntimeError に含まれること."""
        from rag.server import _run_ingest_and_index_subprocess

        mock_process = _make_mock_process(
            b'{"type":"error","error":true,"message":"DB connection failed"}',
            b"", 1,
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.raises(RuntimeError, match="DB connection failed"):
                await _run_ingest_and_index_subprocess("test commit")

    @pytest.mark.asyncio
    async def test_empty_stdout_raises(self) -> None:
        """stdout が空の場合に RuntimeError が送出されること."""
        from rag.server import _run_ingest_and_index_subprocess

        mock_process = _make_mock_process(b"", b"", 0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.raises(RuntimeError, match="出力が空"):
                await _run_ingest_and_index_subprocess("test commit")


# --- _run_delete_subprocess ユニットテスト ---


class TestRunDeleteSubprocess:
    """_run_delete_subprocess のパース/分岐ロジックのテスト."""

    @pytest.mark.asyncio
    async def test_deleted_result(self) -> None:
        """削除成功時に deleted=True と PipelineSummary が返されること."""
        from rag.server import _run_delete_subprocess

        mock_process = _make_mock_process(
            b'{"type":"result","deleted":true,"pipeline":{"type":"result","mode":"incremental",'
            b'"total_files":1,"processed":1,"skipped":0,"errors":[],"elapsed":0.3}}',
            b"", 0,
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await _run_delete_subprocess("https://example.com/page")

        assert result["deleted"] is True
        assert result["pipeline"].processed == 1

    @pytest.mark.asyncio
    async def test_not_found_result(self) -> None:
        """該当なし時に not_found=True が返されること."""
        from rag.server import _run_delete_subprocess

        mock_process = _make_mock_process(b'{"type":"result","not_found":true}', b"", 0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await _run_delete_subprocess("https://example.com/missing")

        assert result["not_found"] is True

    @pytest.mark.asyncio
    async def test_segfault_raises(self) -> None:
        """SEGFAULT exit code で RuntimeError が送出されること."""
        from rag.server import _run_delete_subprocess

        mock_process = _make_mock_process(b"", b"", -11)

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.raises(RuntimeError, match="SEGFAULT"):
                await _run_delete_subprocess("https://example.com/page")

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises(self) -> None:
        """異常終了時に RuntimeError が送出されること."""
        from rag.server import _run_delete_subprocess

        mock_process = _make_mock_process(b"", b"error trace\n", 1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.raises(RuntimeError, match="異常終了"):
                await _run_delete_subprocess("https://example.com/page")


# --- rag_stats MCP ツールテスト ---


class TestRagStats:
    """rag_stats MCP ツール（拡張版）のテスト."""

    @pytest.mark.asyncio
    async def test_stats_output_format(self) -> None:
        """4セクション構成の出力フォーマット."""
        from rag.server import rag_stats

        mock_settings = MagicMock()
        mock_settings.source_store_dir = ""
        mock_settings.converted_store_dir = ""

        mock_service = AsyncMock()
        mock_service.get_stats.return_value = {
            "total_chunks": 100,
            "source_count": 10,
            "sources": [],
        }

        with (
            patch("rag.server.get_settings", return_value=mock_settings),
            patch("rag.server._get_rag_service", return_value=mock_service),
        ):
            result = await rag_stats()

        assert "📊 RAG Knowledge 統計" in result
        assert "■ source_store" in result
        assert "■ converted_store" in result
        assert "■ インデックス" in result
        assert "■ パイプライン" in result

    @pytest.mark.asyncio
    async def test_stats_source_store_unset(self) -> None:
        """source_store 未設定時は「未設定」表示."""
        from rag.server import rag_stats

        mock_settings = MagicMock()
        mock_settings.source_store_dir = ""
        mock_settings.converted_store_dir = ""

        mock_service = AsyncMock()
        mock_service.get_stats.return_value = {
            "total_chunks": 0,
            "source_count": 0,
            "sources": [],
        }

        with (
            patch("rag.server.get_settings", return_value=mock_settings),
            patch("rag.server._get_rag_service", return_value=mock_service),
        ):
            result = await rag_stats()

        # source_store セクション直後に「未設定」が表示される
        lines = result.split("\n")
        ss_idx = next(
            i for i, line in enumerate(lines) if "■ source_store" in line
        )
        assert "未設定" in lines[ss_idx + 1]

    @pytest.mark.asyncio
    async def test_stats_with_source_store_data(
        self, tmp_path: Path,
    ) -> None:
        """source_store に実データがある場合の統計表示."""
        from rag.server import rag_stats

        # テストデータ作成
        ss_dir = tmp_path / "source"
        web_dir = ss_dir / "web"
        web_dir.mkdir(parents=True)
        (web_dir / "page.html").write_text(
            "<html>test</html>", encoding="utf-8",
        )

        mock_settings = MagicMock()
        mock_settings.source_store_dir = str(ss_dir)
        mock_settings.converted_store_dir = ""

        mock_service = AsyncMock()
        mock_service.get_stats.return_value = {
            "total_chunks": 0,
            "source_count": 0,
            "sources": [],
        }

        with (
            patch("rag.server.get_settings", return_value=mock_settings),
            patch("rag.server._get_rag_service", return_value=mock_service),
        ):
            result = await rag_stats()

        assert "総ファイル数: 1" in result
        assert "web:" in result

    @pytest.mark.asyncio
    async def test_stats_index_section(self) -> None:
        """インデックスセクションの統計表示."""
        from rag.server import rag_stats

        mock_settings = MagicMock()
        mock_settings.source_store_dir = ""
        mock_settings.converted_store_dir = ""
        mock_settings.rag_stats_max_sources = 50

        mock_service = AsyncMock()
        mock_service.get_stats.return_value = {
            "total_chunks": 1234,
            "source_count": 120,
            "sources": [
                {
                    "domain": "example.com",
                    "pages": [
                        {
                            "url": "https://example.com/a",
                            "title": "Page A",
                            "chunks": 50,
                        },
                        {
                            "url": "https://example.com/b",
                            "title": "Page B",
                            "chunks": 30,
                        },
                    ],
                },
            ],
        }

        with (
            patch("rag.server.get_settings", return_value=mock_settings),
            patch("rag.server._get_rag_service", return_value=mock_service),
        ):
            result = await rag_stats()

        assert "総チャンク数: 1,234" in result
        assert "ソース数: 120" in result
        assert "example.com: 2 pages (80 chunks)" in result

    @pytest.mark.asyncio
    async def test_stats_pipeline_section(
        self, tmp_path: Path,
    ) -> None:
        """パイプラインセクションの統計表示."""
        from rag.server import rag_stats

        # metadata.db を作成
        ss_dir = tmp_path / "source"
        ss_dir.mkdir()
        db = MetadataDB(ss_dir / "metadata.db")
        db.initialize()
        db.add_pipeline_history(
            from_commit_id="aaa",
            to_commit_id="bbbcccc",
            processed_at="2026-03-19T10:30:00+09:00",
        )
        db.close()

        mock_settings = MagicMock()
        mock_settings.source_store_dir = str(ss_dir)
        mock_settings.converted_store_dir = ""

        mock_service = AsyncMock()
        mock_service.get_stats.return_value = {
            "total_chunks": 0,
            "source_count": 0,
            "sources": [],
        }

        with (
            patch("rag.server.get_settings", return_value=mock_settings),
            patch("rag.server._get_rag_service", return_value=mock_service),
        ):
            result = await rag_stats()

        assert "最終処理: 2026-03-19T10:30:00+09:00" in result
        assert "実行回数: 1" in result
        assert "last_commit_id: bbbcccc" in result
        assert "論理削除: 0 件" in result

    @pytest.mark.asyncio
    async def test_stats_pipeline_uninitialized(
        self, tmp_path: Path,
    ) -> None:
        """metadata.db が存在しない場合「未初期化」表示."""
        from rag.server import rag_stats

        ss_dir = tmp_path / "source"
        ss_dir.mkdir()
        # metadata.db は作成しない

        mock_settings = MagicMock()
        mock_settings.source_store_dir = str(ss_dir)
        mock_settings.converted_store_dir = ""

        mock_service = AsyncMock()
        mock_service.get_stats.return_value = {
            "total_chunks": 0,
            "source_count": 0,
            "sources": [],
        }

        with (
            patch("rag.server.get_settings", return_value=mock_settings),
            patch("rag.server._get_rag_service", return_value=mock_service),
        ):
            result = await rag_stats()

        lines = result.split("\n")
        pl_idx = next(
            i for i, line in enumerate(lines) if "■ パイプライン" in line
        )
        assert "未初期化" in lines[pl_idx + 1]

    @pytest.mark.asyncio
    async def test_stats_domain_limit(self) -> None:
        """ドメイン別一覧の表示上限テスト."""
        from rag.server import rag_stats

        mock_settings = MagicMock()
        mock_settings.source_store_dir = ""
        mock_settings.converted_store_dir = ""
        mock_settings.rag_stats_max_sources = 2

        # 3ドメイン分のデータを用意
        sources = [
            {
                "domain": f"domain{i}.com",
                "pages": [{"url": f"https://domain{i}.com/p", "title": "P", "chunks": 10}],
            }
            for i in range(3)
        ]

        mock_service = AsyncMock()
        mock_service.get_stats.return_value = {
            "total_chunks": 30,
            "source_count": 3,
            "sources": sources,
        }

        with (
            patch("rag.server.get_settings", return_value=mock_settings),
            patch("rag.server._get_rag_service", return_value=mock_service),
        ):
            result = await rag_stats()

        assert "domain0.com" in result
        assert "domain1.com" in result
        assert "domain2.com" not in result
        assert "以下省略" in result

    @pytest.mark.asyncio
    async def test_stats_domain_no_truncation_at_exact_limit(self) -> None:
        """ドメイン数がちょうど上限と等しい場合に省略メッセージが出ないこと."""
        from rag.server import rag_stats

        mock_settings = MagicMock()
        mock_settings.source_store_dir = ""
        mock_settings.converted_store_dir = ""
        mock_settings.rag_stats_max_sources = 2

        # ちょうど2ドメイン（上限と同数）
        sources = [
            {
                "domain": f"domain{i}.com",
                "pages": [
                    {
                        "url": f"https://domain{i}.com/p",
                        "title": "P",
                        "chunks": 10,
                    },
                ],
            }
            for i in range(2)
        ]

        mock_service = AsyncMock()
        mock_service.get_stats.return_value = {
            "total_chunks": 20,
            "source_count": 2,
            "sources": sources,
        }

        with (
            patch("rag.server.get_settings", return_value=mock_settings),
            patch("rag.server._get_rag_service", return_value=mock_service),
        ):
            result = await rag_stats()

        assert "domain0.com" in result
        assert "domain1.com" in result
        assert "以下省略" not in result


# --- ツール数テスト ---


@pytest.mark.asyncio
async def test_rag_server_exposes_thirteen_tools() -> None:
    """RAG MCPサーバーが13個のツールを公開すること."""
    mod = import_module("rag.server")
    server = mod.mcp

    tools = await server.list_tools()
    tool_names = {t.name for t in tools}

    expected = {
        "rag_search", "rag_get_document", "rag_add", "rag_crawl",
        "rag_crawl_preview", "rag_crawl_zenn", "rag_crawl_bluesky",
        "rag_add_document", "rag_crawl_documents", "rag_site_ingest",
        "rag_delete", "rag_rebuild", "rag_stats",
    }
    assert tool_names == expected, f"Expected {expected}, got {tool_names}"

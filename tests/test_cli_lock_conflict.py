"""CLI のロック競合時の lock_type 伝搬テスト.

仕様: docs/specs/infrastructure/content-upload.md (rebuild との相互排他)

CLI の書き込み系コマンドがロック競合エラーを検出した際、error JSON の
details.lock_type に `e.kind` を正しく詰めることを検証する。サーバー側が
この情報を使って Upload HTTP API の 429/503 分岐や MCP ツールの
メッセージ切替を行うため、伝搬の正確性が重要。

テスト方針:
- 代表 3 関数（run_add_journal / run_add_document / run_ingest_zenn）×
  2 kind（rebuild / write）= 6 ケース
- 既存の run_rebuild は rebuild_lock（別ロック）のためスコープ外
- write_lock をモックして LockAcquisitionError を送出させる
- kind=write はリトライ対象（Issue #755）のため、リトライ実時間を消費しないよう
  `_WRITE_LOCK_RETRY_BACKOFFS_SEC` を空タプルに上書きする
  `_disable_lock_retry_backoff` fixture をクラス単位で適用
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rag.infrastructure.file_lock import LockAcquisitionError, LockKind


@pytest.fixture
def _disable_lock_retry_backoff() -> Iterator[None]:
    """write_lock リトライのバックオフを無効化する fixture.

    Issue #755 で導入した `_WRITE_LOCK_RETRY_BACKOFFS_SEC` を空タプルに置き換え、
    既存テストの実時間消費（最大 ~1.85s × ケース数）を回避する。
    リトライ動作自体の検証は `TestWriteLockRetryBehavior` で別途実施する。
    """
    with patch("rag.cli._WRITE_LOCK_RETRY_BACKOFFS_SEC", ()):
        yield


def _make_lock_mock(kind: LockKind) -> MagicMock:
    """acquire() で LockAcquisitionError を送出するモックロックを生成する."""
    mock_lock = MagicMock()
    mock_lock.acquire.side_effect = LockAcquisitionError(
        Path(f"/tmp/test_store/.{kind}.lock"), kind=kind,
    )
    return mock_lock


def _parse_error_json(captured_out: str) -> dict[str, object]:
    """CLI の stdout から type:error の JSON 行を抽出する."""
    parsed: dict[str, object] = json.loads(captured_out.strip())
    assert parsed["type"] == "error", parsed
    return parsed


@pytest.mark.usefixtures("_disable_lock_retry_backoff")
class TestAddJournalLockConflict:
    """run_add_journal のロック競合 → lock_type 伝搬テスト."""

    @pytest.mark.parametrize("kind", ["rebuild", "write"])
    def test_lock_conflict_outputs_lock_type_in_details(
        self, kind: LockKind, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """LockAcquisitionError.kind が details.lock_type に伝搬する."""
        args = argparse.Namespace(
            stdin=True,
            file=None,
            title="Test",
            repository="test-repo",
            entry_id=None,
            output_format="json",
        )

        with (
            patch("sys.stdin", io.StringIO("some content")),
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
            patch(
                "rag.infrastructure.file_lock.write_lock",
                return_value=_make_lock_mock(kind),
            ),
        ):
            mock_controller = MagicMock()
            mock_controller.source_store.root_dir = Path("/tmp/test_store")
            mock_ctrl.return_value = (mock_controller, MagicMock())

            with pytest.raises(SystemExit) as exc_info:
                from rag.cli import run_add_journal
                asyncio.run(run_add_journal(args))
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        parsed = _parse_error_json(captured.out)
        assert parsed["code"] == "LOCK_CONFLICT"
        details = parsed.get("details")
        assert isinstance(details, dict)
        assert details["lock_type"] == kind

    def test_rebuild_kind_produces_rebuild_message(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """kind='rebuild' のときメッセージに '再構築' を含む."""
        args = argparse.Namespace(
            stdin=True,
            file=None,
            title="Test",
            repository="test-repo",
            entry_id=None,
            output_format="json",
        )

        with (
            patch("sys.stdin", io.StringIO("some content")),
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
            patch(
                "rag.infrastructure.file_lock.write_lock",
                return_value=_make_lock_mock("rebuild"),
            ),
        ):
            mock_controller = MagicMock()
            mock_controller.source_store.root_dir = Path("/tmp/test_store")
            mock_ctrl.return_value = (mock_controller, MagicMock())

            with pytest.raises(SystemExit):
                from rag.cli import run_add_journal
                asyncio.run(run_add_journal(args))

        captured = capsys.readouterr()
        parsed = _parse_error_json(captured.out)
        assert "再構築" in str(parsed["message"])

    def test_write_kind_produces_write_message(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """kind='write' のときメッセージに '取り込み' を含む."""
        args = argparse.Namespace(
            stdin=True,
            file=None,
            title="Test",
            repository="test-repo",
            entry_id=None,
            output_format="json",
        )

        with (
            patch("sys.stdin", io.StringIO("some content")),
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
            patch(
                "rag.infrastructure.file_lock.write_lock",
                return_value=_make_lock_mock("write"),
            ),
        ):
            mock_controller = MagicMock()
            mock_controller.source_store.root_dir = Path("/tmp/test_store")
            mock_ctrl.return_value = (mock_controller, MagicMock())

            with pytest.raises(SystemExit):
                from rag.cli import run_add_journal
                asyncio.run(run_add_journal(args))

        captured = capsys.readouterr()
        parsed = _parse_error_json(captured.out)
        assert "取り込み" in str(parsed["message"])


@pytest.mark.usefixtures("_disable_lock_retry_backoff")
class TestIngestZennLockConflict:
    """run_ingest_zenn（ヘルパー経由）のロック競合 → lock_type 伝搬テスト.

    ヘルパー `_write_lock_or_exit` 経由パスの代表として `run_ingest_zenn` で
    検証する。書き込み系 CLI 関数は `_write_lock_or_exit` 経由に統一されており
    （add_journal / add_document / ingest_zenn 等）、本テストはその統一パスの
    回帰検出を担う。
    """

    @pytest.mark.parametrize("kind", ["rebuild", "write"])
    def test_helper_path_propagates_lock_type(
        self, kind: LockKind, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """ヘルパー経由でも lock_type が正しく伝搬する."""
        args = argparse.Namespace(
            url="https://zenn.dev/test/articles/dummy",
            output_format="json",
        )

        with (
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
            patch(
                "rag.infrastructure.file_lock.write_lock",
                return_value=_make_lock_mock(kind),
            ),
        ):
            mock_controller = MagicMock()
            mock_controller.source_store.root_dir = Path("/tmp/test_store")
            mock_settings = MagicMock()
            mock_settings.rag_zenn_max_articles = 10
            mock_settings.rag_zenn_request_timeout = 30
            mock_settings.rag_zenn_request_interval = 0.1
            mock_ctrl.return_value = (mock_controller, mock_settings)

            with pytest.raises(SystemExit) as exc_info:
                from rag.cli import run_ingest_zenn
                asyncio.run(run_ingest_zenn(args))
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        parsed = _parse_error_json(captured.out)
        assert parsed["code"] == "LOCK_CONFLICT"
        details = parsed.get("details")
        assert isinstance(details, dict)
        assert details["lock_type"] == kind


class TestWriteLockOrExitHelper:
    """_write_lock_or_exit コンテキストマネージャ単体の挙動テスト."""

    def test_successful_acquire_yields_and_releases(self) -> None:
        """正常取得時は yield し、with を抜ける際に release される."""
        mock_lock = MagicMock()
        # acquire は成功（side_effect なし）
        with patch(
            "rag.infrastructure.file_lock.write_lock", return_value=mock_lock,
        ):
            from rag.cli import _write_lock_or_exit
            with _write_lock_or_exit(Path("/tmp/test"), json_out=True):
                pass
        mock_lock.acquire.assert_called_once()
        mock_lock.release.assert_called_once()

    def test_release_runs_even_on_exception(self) -> None:
        """with ブロック内で例外が発生しても release は呼ばれる."""
        mock_lock = MagicMock()
        with patch(
            "rag.infrastructure.file_lock.write_lock", return_value=mock_lock,
        ):
            from rag.cli import _write_lock_or_exit
            with pytest.raises(RuntimeError, match="boom"):
                with _write_lock_or_exit(Path("/tmp/test"), json_out=True):
                    raise RuntimeError("boom")
        mock_lock.release.assert_called_once()

    def test_acquire_failure_exits_without_release(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """取得失敗時は exit、release は呼ばれない（acquire 失敗のため）."""
        mock_lock = MagicMock()
        mock_lock.acquire.side_effect = LockAcquisitionError(
            Path("/tmp/test/.rebuild.lock"), kind="rebuild",
        )
        with patch(
            "rag.infrastructure.file_lock.write_lock", return_value=mock_lock,
        ):
            from rag.cli import _write_lock_or_exit
            with pytest.raises(SystemExit) as exc_info:
                with _write_lock_or_exit(Path("/tmp/test"), json_out=True):
                    pytest.fail("body should not execute when acquire fails")
            assert exc_info.value.code == 1
        mock_lock.acquire.assert_called_once()
        # acquire が失敗したので release は呼ばれない
        mock_lock.release.assert_not_called()

        captured = capsys.readouterr()
        parsed = _parse_error_json(captured.out)
        assert parsed["code"] == "LOCK_CONFLICT"
        details = parsed.get("details")
        assert isinstance(details, dict)
        assert details["lock_type"] == "rebuild"


class TestWriteLockRetryBehavior:
    """_write_lock_or_exit の write 競合時リトライ挙動テスト（Issue #755）.

    CLI 連続実行時の OS ロック解放遅延を吸収するため、kind=write の
    LockAcquisitionError に対してのみ短時間リトライする設計を検証する。

    各テストは `_WRITE_LOCK_RETRY_BACKOFFS_SEC` を `(0.0, 0.0, 0.0, 0.0)` に
    再 patch しつつ `time.sleep` を mock することで、リトライの実時間消費を回避し
    試行回数のみを検証する。
    """

    def test_retry_succeeds_after_initial_failure(self) -> None:
        """初回失敗 → リトライで成功するケース（kind=write）."""
        mock_lock = MagicMock()
        # 1 回目失敗 → 2 回目成功
        mock_lock.acquire.side_effect = [
            LockAcquisitionError(
                Path("/tmp/test/.write.lock"), kind="write",
            ),
            None,  # 成功
        ]
        with (
            patch(
                "rag.infrastructure.file_lock.write_lock",
                return_value=mock_lock,
            ),
            patch("rag.cli._WRITE_LOCK_RETRY_BACKOFFS_SEC", (0.0, 0.0, 0.0, 0.0)),
            patch("time.sleep") as mock_sleep,
        ):
            from rag.cli import _write_lock_or_exit
            with _write_lock_or_exit(Path("/tmp/test"), json_out=True):
                pass
        # acquire は 2 回呼ばれる（初回失敗 + 再試行成功）
        assert mock_lock.acquire.call_count == 2
        mock_lock.release.assert_called_once()
        # backoff の sleep は 1 回呼ばれる（初回失敗後の 1 回目バックオフ）
        assert mock_sleep.call_count == 1

    def test_retry_exhausted_then_exits_with_lock_conflict(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """全リトライ失敗後に LOCK_CONFLICT で exit 1（kind=write）."""
        mock_lock = MagicMock()
        # 全試行で失敗
        mock_lock.acquire.side_effect = LockAcquisitionError(
            Path("/tmp/test/.write.lock"), kind="write",
        )
        with (
            patch(
                "rag.infrastructure.file_lock.write_lock",
                return_value=mock_lock,
            ),
            patch("rag.cli._WRITE_LOCK_RETRY_BACKOFFS_SEC", (0.0, 0.0, 0.0, 0.0)),
            patch("time.sleep"),
        ):
            from rag.cli import _write_lock_or_exit
            with pytest.raises(SystemExit) as exc_info:
                with _write_lock_or_exit(Path("/tmp/test"), json_out=True):
                    pytest.fail("body should not execute when retries exhausted")
            assert exc_info.value.code == 1
        # 初回 + 4 回再試行 = 5 回試行
        assert mock_lock.acquire.call_count == 5
        mock_lock.release.assert_not_called()

        captured = capsys.readouterr()
        parsed = _parse_error_json(captured.out)
        assert parsed["code"] == "LOCK_CONFLICT"
        details = parsed.get("details")
        assert isinstance(details, dict)
        assert details["lock_type"] == "write"

    def test_rebuild_kind_does_not_retry(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """kind=rebuild はリトライ対象外で初回失敗で即 exit."""
        mock_lock = MagicMock()
        mock_lock.acquire.side_effect = LockAcquisitionError(
            Path("/tmp/test/.rebuild.lock"), kind="rebuild",
        )
        with (
            patch(
                "rag.infrastructure.file_lock.write_lock",
                return_value=mock_lock,
            ),
            patch("rag.cli._WRITE_LOCK_RETRY_BACKOFFS_SEC", (0.0, 0.0, 0.0, 0.0)),
            patch("time.sleep") as mock_sleep,
        ):
            from rag.cli import _write_lock_or_exit
            with pytest.raises(SystemExit) as exc_info:
                with _write_lock_or_exit(Path("/tmp/test"), json_out=True):
                    pytest.fail("body should not execute when acquire fails")
            assert exc_info.value.code == 1
        # 初回試行 1 回のみ（リトライしない）
        assert mock_lock.acquire.call_count == 1
        # sleep も呼ばれない
        mock_sleep.assert_not_called()
        mock_lock.release.assert_not_called()

        captured = capsys.readouterr()
        parsed = _parse_error_json(captured.out)
        assert parsed["code"] == "LOCK_CONFLICT"
        details = parsed.get("details")
        assert isinstance(details, dict)
        assert details["lock_type"] == "rebuild"

    def test_default_backoffs_match_spec(self) -> None:
        """`_WRITE_LOCK_RETRY_BACKOFFS_SEC` の値変更時に意図的でない改変を検知する.

        コード側 `_WRITE_LOCK_RETRY_BACKOFFS_SEC` が SSoT（仕様書 content-upload.md
        は SSoT 方針により具体値を記載しない）。本テストはコード側 SSoT のうっかり
        改変検知用として、現行値をハードコードで検証する。値変更時は本テストと
        計画ファイル / PR description の合計待機時間記述を併せて更新すること。
        """
        from rag.cli import _WRITE_LOCK_RETRY_BACKOFFS_SEC
        assert _WRITE_LOCK_RETRY_BACKOFFS_SEC == (0.1, 0.25, 0.5, 1.0)

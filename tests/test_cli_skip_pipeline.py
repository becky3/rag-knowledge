"""--skip-pipeline フラグおよび bulk 受付の振る舞いテスト.

Issue #757 / 仕様: docs/specs/ingesters/common.md「`--skip-pipeline` フラグ共通仕様」
                   docs/specs/ingesters/aozora.md「ingest-aozora 複数 ID 入力時の重複検出」

テスト方針:
- 12 CLI コマンドに --skip-pipeline が argparse で受理されること
- --skip-pipeline 指定時に controller.ingest_and_index が呼ばれないこと
- --skip-pipeline 未指定時は呼ばれること（既存挙動維持）
- 4 コマンドの複数引数受付（nargs='+' / action='append'）
- aozora zfill 重複検出による WARN ログ + 1 回のみ ingest
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestNoPipelineParserRegistration:
    """全 12 サブコマンドに --skip-pipeline が argparse 上で受理されること."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["ingest-youtube", "https://youtu.be/aaa", "--skip-pipeline"],
            ["ingest-youtube-playlist", "https://example.com/pl", "--skip-pipeline"],
            ["crawl-bluesky", "user.bsky.social", "--skip-pipeline"],
            ["crawl-zenn", "username", "--skip-pipeline"],
            ["ingest-bluesky", "https://example.com/post", "--skip-pipeline"],
            ["ingest-zenn", "https://zenn.dev/u/articles/x", "--skip-pipeline"],
            ["crawl-documents", "/tmp/docs", "--skip-pipeline"],
            ["site-ingest", "https://example.com/", "--skip-pipeline"],
            ["site-crawl", "https://example.com/", "--skip-pipeline"],
            ["ingest-aozora", "12345", "--skip-pipeline"],
            ["ingest-aozora-author", "00001", "--skip-pipeline"],
            ["delete", "local/.upload/2026/05/10/x.md", "--skip-pipeline"],
        ],
    )
    def test_skip_pipeline_flag_accepted(self, argv: list[str]) -> None:
        """--skip-pipeline が argparse で True にパースされること."""
        from rag.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(argv)
        assert args.skip_pipeline is True

    @pytest.mark.parametrize(
        "argv",
        [
            ["ingest-youtube", "https://youtu.be/aaa"],
            ["ingest-aozora", "12345"],
        ],
    )
    def test_skip_pipeline_default_false(self, argv: list[str]) -> None:
        """--skip-pipeline 未指定時は False がデフォルト."""
        from rag.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(argv)
        assert args.skip_pipeline is False


class TestBulkInputRegistration:
    """単一引数 → 複数受付化されたコマンドの argparse 検証."""

    def test_ingest_youtube_accepts_multiple_urls(self) -> None:
        from rag.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(
            ["ingest-youtube", "https://youtu.be/a", "https://youtu.be/b"],
        )
        assert args.video_url == ["https://youtu.be/a", "https://youtu.be/b"]

    def test_ingest_aozora_accepts_multiple_book_ids(self) -> None:
        from rag.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["ingest-aozora", "1234", "5678"])
        assert args.book_id == ["1234", "5678"]

    def test_delete_accepts_multiple_source_ids(self) -> None:
        from rag.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["delete", "src/a.md", "src/b.md"])
        assert args.source_id == ["src/a.md", "src/b.md"]


class TestNoPipelineHandlerSkipsIngestAndIndex:
    """--skip-pipeline 指定時に controller.ingest_and_index が呼ばれないこと."""

    def _make_ingest_result(
        self,
        *,
        placed: int = 1,
        errors: int = 0,
    ):
        from rag.pipeline.ingesters._common import IngestResult

        result = IngestResult()
        result.placed = placed
        result.errors = errors
        return result

    @pytest.mark.asyncio
    async def test_ingest_aozora_with_skip_pipeline_skips_index(
        self, tmp_path: Path,
    ) -> None:
        """ingest-aozora --skip-pipeline で controller.ingest_and_index が呼ばれない."""
        args = argparse.Namespace(
            book_id=["00012345"],
            skip_pipeline=True,
            output_format="text",
        )

        mock_ingester = MagicMock()
        mock_ingester.add_work = AsyncMock(return_value=self._make_ingest_result())
        # async context manager for fetcher
        mock_fetcher_cm = MagicMock()
        mock_fetcher_cm.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_fetcher_cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
            patch("rag.cli._write_lock_or_exit") as mock_lock_ctx,
            patch(
                "rag.pipeline.ingesters.aozora.create_aozora_fetcher",
                return_value=mock_fetcher_cm,
            ),
            patch(
                "rag.pipeline.ingesters.aozora.AozoraIngester",
                return_value=mock_ingester,
            ),
        ):
            from contextlib import nullcontext

            mock_lock_ctx.return_value = nullcontext()

            mock_controller = MagicMock()
            mock_controller.source_store.root_dir = tmp_path
            mock_controller.ingest_and_index = AsyncMock()
            mock_settings = MagicMock()
            mock_settings.rag_aozora_max_works = 100
            mock_settings.rag_embedding_concurrency = 1
            mock_ctrl.return_value = (mock_controller, mock_settings)

            from rag.cli import run_ingest_aozora

            await run_ingest_aozora(args)

            mock_controller.ingest_and_index.assert_not_called()
            # --skip-pipeline 指定時も controller.commit() が呼ばれる必要がある
            # (Critical #1: source_store の git commit は実行される)
            mock_controller.commit.assert_called_once()
            mock_ingester.add_work.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingest_aozora_without_skip_pipeline_invokes_index(
        self, tmp_path: Path,
    ) -> None:
        """ingest-aozora（既定）で controller.ingest_and_index が呼ばれる."""
        args = argparse.Namespace(
            book_id=["00012345"],
            skip_pipeline=False,
            output_format="text",
        )

        mock_ingester = MagicMock()
        mock_ingester.add_work = AsyncMock(return_value=self._make_ingest_result())
        mock_fetcher_cm = MagicMock()
        mock_fetcher_cm.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_fetcher_cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
            patch("rag.cli._write_lock_or_exit") as mock_lock_ctx,
            patch(
                "rag.pipeline.ingesters.aozora.create_aozora_fetcher",
                return_value=mock_fetcher_cm,
            ),
            patch(
                "rag.pipeline.ingesters.aozora.AozoraIngester",
                return_value=mock_ingester,
            ),
        ):
            from contextlib import nullcontext

            mock_lock_ctx.return_value = nullcontext()

            mock_controller = MagicMock()
            mock_controller.source_store.root_dir = tmp_path
            mock_controller.ingest_and_index = AsyncMock(return_value=MagicMock())
            mock_settings = MagicMock()
            mock_settings.rag_aozora_max_works = 100
            mock_settings.rag_embedding_concurrency = 1
            mock_ctrl.return_value = (mock_controller, mock_settings)

            from rag.cli import run_ingest_aozora

            await run_ingest_aozora(args)

            mock_controller.ingest_and_index.assert_called_once()


class TestAozoraZfillDuplicateDetection:
    """ingest-aozora bulk 入力での zfill 後重複検出."""

    @pytest.mark.asyncio
    async def test_duplicate_zfill_inputs_warned_and_deduped(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """`['1234', '001234']` は zfill(6) 後同一 → WARN + 1 回のみ ingest."""
        from rag.pipeline.ingesters._common import IngestResult

        # zfill(6) の正規化規則: '1234' → '001234'、'001234' → '001234'
        args = argparse.Namespace(
            book_id=["1234", "001234", "5678"],
            skip_pipeline=True,
            output_format="text",
        )

        mock_ingester = MagicMock()
        mock_ingester.add_work = AsyncMock(return_value=IngestResult(placed=1))
        mock_fetcher_cm = MagicMock()
        mock_fetcher_cm.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_fetcher_cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("rag.cli._build_cli_pipeline_controller") as mock_ctrl,
            patch("rag.cli._write_lock_or_exit") as mock_lock_ctx,
            patch(
                "rag.pipeline.ingesters.aozora.create_aozora_fetcher",
                return_value=mock_fetcher_cm,
            ),
            patch(
                "rag.pipeline.ingesters.aozora.AozoraIngester",
                return_value=mock_ingester,
            ),
            caplog.at_level(logging.WARNING, logger="rag.cli"),
        ):
            from contextlib import nullcontext

            mock_lock_ctx.return_value = nullcontext()

            mock_controller = MagicMock()
            mock_controller.source_store.root_dir = tmp_path
            mock_controller.ingest_and_index = AsyncMock()
            mock_settings = MagicMock()
            mock_settings.rag_aozora_max_works = 100
            mock_settings.rag_embedding_concurrency = 1
            mock_ctrl.return_value = (mock_controller, mock_settings)

            from rag.cli import run_ingest_aozora

            await run_ingest_aozora(args)

            # 重複検出 WARN
            warn_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
            assert any("重複入力を検出" in m for m in warn_messages), warn_messages
            # 重複排除されて 2 回のみ呼び出される (1234/001234 → 1, 5678 → 1)
            assert mock_ingester.add_work.call_count == 2


class TestNormalizeAozoraIdEdgeCases:
    """_normalize_aozora_id のエッジケース."""

    def test_pad_to_six_digits(self) -> None:
        from rag.cli import _normalize_aozora_id

        assert _normalize_aozora_id("1234") == "001234"

    def test_already_six_digits(self) -> None:
        from rag.cli import _normalize_aozora_id

        assert _normalize_aozora_id("001234") == "001234"

    def test_more_than_six_digits_no_pad(self) -> None:
        from rag.cli import _normalize_aozora_id

        assert _normalize_aozora_id("00001234") == "00001234"

    def test_strip_whitespace(self) -> None:
        from rag.cli import _normalize_aozora_id

        assert _normalize_aozora_id("  1234  ") == "001234"

    def test_empty_string_normalizes_to_six_zeros(self) -> None:
        """空文字列は zfill(6) で '000000' になる（ingester 側ロジックと整合）."""
        from rag.cli import _normalize_aozora_id

        assert _normalize_aozora_id("") == "000000"
        assert _normalize_aozora_id("   ") == "000000"


class TestMergeIngestResults:
    """_merge_ingest_results 単体テスト."""

    def test_counters_sum(self) -> None:
        from rag.cli import _merge_ingest_results
        from rag.pipeline.ingesters._common import IngestResult

        r1 = IngestResult(placed=1, skipped=2, overwritten=3, errors=4, partial_failures=5)
        r2 = IngestResult(placed=10, skipped=20, overwritten=30, errors=40, partial_failures=50)
        merged = _merge_ingest_results([r1, r2])

        assert merged.placed == 11
        assert merged.skipped == 22
        assert merged.overwritten == 33
        assert merged.errors == 44
        assert merged.partial_failures == 55

    def test_details_concatenated(self) -> None:
        from rag.cli import _merge_ingest_results
        from rag.pipeline.ingesters._common import (
            IngestErrorCategory,
            IngestErrorDetail,
            IngestResult,
        )

        d1 = IngestErrorDetail(category=IngestErrorCategory.METADATA_FETCH.value, target="a")
        d2 = IngestErrorDetail(category=IngestErrorCategory.MEDIA_DOWNLOAD.value, target="b")
        r1 = IngestResult(error_details=[d1])
        r2 = IngestResult(partial_failure_details=[d2])
        merged = _merge_ingest_results([r1, r2])

        assert merged.error_details == [d1]
        assert merged.partial_failure_details == [d2]

    def test_aborted_first_reason_preserved(self) -> None:
        """最初の aborted=True の abort_reason が採用される."""
        from rag.cli import _merge_ingest_results
        from rag.pipeline.ingesters._common import IngestResult

        r1 = IngestResult(placed=1)
        r2 = IngestResult(aborted=True, abort_reason="first reason")
        r3 = IngestResult(aborted=True, abort_reason="second reason")
        merged = _merge_ingest_results([r1, r2, r3])

        assert merged.aborted is True
        assert merged.abort_reason == "first reason"

    def test_empty_list_returns_zero_result(self) -> None:
        from rag.cli import _merge_ingest_results

        merged = _merge_ingest_results([])
        assert merged.is_empty()

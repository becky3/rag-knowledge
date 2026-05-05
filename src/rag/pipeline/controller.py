"""パイプライン制御 — メインコントローラ.

仕様: docs/specs/pipeline-controller.md

3段パイプライン（インジェスター → コンバーター → インデクサー）の
ステージ間連携を担当するオーケストレーション層。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import subprocess
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from rag.converter.converter import (
    ConversionFailedError,
    ConversionSkippedError,
    get_converted_rel_path,
)
from rag.pipeline.change_detector import RealChangeDetector
from rag.pipeline.change_handler import RealChangeHandler
from rag.pipeline.git_ops import GitOperations
from rag.pipeline.metadata_builder import RealMetadataBuilder
from rag.pipeline.models import (
    ChangeEntry,
    FullRebuildResult,
    PipelineErrorEntry,
    PipelineMode,
    PipelinePhase,
    PipelineWarningEntry,
    PipelineSummary,
)
from rag.pipeline.protocols import ConverterProtocol, IndexerProtocol
from rag.store.metadata_db import MetadataDB
from rag.store.models import (
    NULL_COMMIT_HASH,
    SourceMetadata,
    SourceRecord,
    SourceStatus,
    SourceType,
)
from rag.store.source_store import (
    SourceStore,
)

from rag.pipeline.ingesters._common import ProgressCallback

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class PipelineController:
    """パイプライン制御のメインコントローラ."""

    def __init__(
        self,
        source_store: SourceStore,
        converted_store_dir: Path,
        converter: ConverterProtocol,
        indexer: IndexerProtocol,
    ) -> None:
        self._source_store = source_store
        self._converted_store_dir = converted_store_dir
        self._converter = converter
        self._indexer = indexer
        self._git = GitOperations(source_store.root_dir)
        self._change_detector = RealChangeDetector(
            source_store=source_store,
            git=self._git,
        )
        self._metadata_builder = RealMetadataBuilder(
            source_store=source_store,
            db=source_store.db,
        )
        self._change_handler = RealChangeHandler(
            source_store=source_store,
            converted_store_dir=converted_store_dir,
            converter=converter,
            indexer=indexer,
            metadata_builder=self._metadata_builder,
            db=source_store.db,
        )

    @property
    def db(self) -> MetadataDB:
        """metadata.db への直接アクセス."""
        return self._source_store.db

    @property
    def source_store(self) -> SourceStore:
        """source_store への直接アクセス."""
        return self._source_store

    @property
    def indexer(self) -> IndexerProtocol:
        """インデクサーへの直接アクセス."""
        return self._indexer

    # --- git 操作 ---

    def init_repo(self) -> None:
        """source_store の git リポジトリを初期化する."""
        self._git.init_repo()

    def commit(self, message: str) -> str | None:
        """source_store の変更をコミットする.

        Args:
            message: コミットメッセージ

        Returns:
            コミット ID。変更なしの場合は None。
        """
        self._git.init_repo()
        return self._git.commit(message)

    async def ingest_and_index(
        self,
        message: str,
        progress_callback: ProgressCallback | None = None,
        concurrency: int = 1,
    ) -> PipelineSummary:
        """インジェスター実行後の後処理を一括実行する.

        source_store の変更を git commit し、差分更新を実行する。

        Args:
            message: コミットメッセージ
            progress_callback: 進捗コールバック
            concurrency: 同時実行数

        Returns:
            パイプライン処理結果サマリ
        """
        logger.info("Ingest post-processing started: %s", message)
        self.commit(message)
        return await self.run_incremental(
            progress_callback=progress_callback,
            concurrency=concurrency,
        )

    # --- パイプライン実行 ---

    async def run_incremental(
        self,
        progress_callback: ProgressCallback | None = None,
        concurrency: int = 1,
    ) -> PipelineSummary:
        """差分更新を実行する.

        source_store に未コミットの変更がある場合は自動コミットし、
        last_commit_id と HEAD の差分を検知して変更ファイルのみをパイプライン処理する。
        """
        logger.info("Incremental pipeline started")
        self._git.init_repo()

        # 未コミット変更の自動コミット
        if self._git.has_uncommitted_changes():
            self._auto_commit_for_incremental()

        if not self._git.has_commits():
            return PipelineSummary(
                mode=PipelineMode.INCREMENTAL,
                total_files=0,
                processed=0,
            )

        last_commit_id = self.db.get_last_commit_id()
        head_commit = self._git.get_head_commit()
        logger.info(
            "Commit range: %s..%s", last_commit_id[:8], head_commit[:8],
        )

        # last_commit_id == HEAD → 差分なし
        if last_commit_id == head_commit:
            logger.info("No changes (last_commit_id == HEAD)")
            return PipelineSummary(
                mode=PipelineMode.INCREMENTAL,
                total_files=0,
                processed=0,
                from_commit_id=last_commit_id,
                to_commit_id=head_commit,
            )

        # 変更ファイルの特定
        if last_commit_id == NULL_COMMIT_HASH:
            changes = self._scan_all_as_added()
        elif not self._git.is_commit_valid(last_commit_id):
            logger.warning(
                "last_commit_id が git 履歴に存在しません: %s"
                "（全ファイルを対象とします）",
                last_commit_id,
            )
            changes = self._scan_all_as_added()
        else:
            raw_diff = self._git.get_diff(last_commit_id)
            raw_diff = self._supplement_hidden_changes(
                raw_diff, last_commit_id,
            )
            changes = self._classify_changes(raw_diff)

        if not changes:
            logger.info("No changed files")
            return PipelineSummary(
                mode=PipelineMode.INCREMENTAL,
                total_files=0,
                processed=0,
                from_commit_id=last_commit_id,
                to_commit_id=head_commit,
            )

        status_counts = Counter(e.status.value for e in changes)
        logger.info(
            "Changes detected: %d files (%s)",
            len(changes),
            ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items())),
        )

        summary = await self._process_changes(
            changes, PipelineMode.INCREMENTAL, last_commit_id, head_commit,
            progress_callback=progress_callback,
            concurrency=concurrency,
        )
        logger.info(
            "Incremental pipeline completed: total=%d, processed=%d, errors=%d, warnings=%d",
            summary.total_files,
            summary.processed,
            len(summary.errors),
            len(summary.warnings),
        )

        # 正常完了時のみ pipeline_history に記録
        if not summary.errors:
            self.db.add_pipeline_history(
                from_commit_id=last_commit_id,
                to_commit_id=head_commit,
                processed_at=datetime.now(timezone.utc).isoformat(),
                mode="incremental",
            )

        return summary

    async def run_full_rebuild(
        self,
        source_type: SourceType | None = None,
        *,
        path: str | None = None,
        progress_callback: ProgressCallback | None = None,
        concurrency: int = 1,
    ) -> FullRebuildResult:
        """全再構築を実行する.

        全 convert → 全 index の2フェーズで処理する。
        convert 失敗分は index から除外される。

        Args:
            source_type: 対象媒体フィルタ（None で全媒体）
            path: パスフィルタ（source_type と排他）。指定時は配下の
                ソースのみを対象にする
            progress_callback: 進捗コールバック
            concurrency: convert / index フェーズの同時実行数

        Raises:
            RuntimeError: source_store に未コミットの変更がある場合
            ValueError: source_type と path が同時指定された場合
        """
        _validate_filter_exclusivity(source_type, path)
        logger.info(
            "Full rebuild started (source_type=%s, path=%s)",
            source_type or "all", path or "-",
        )
        self._git.init_repo()
        self._check_uncommitted_changes(source_type, path=path)

        # 1. metadata.db 再構築
        self._source_store.rebuild_db(source_type, path=path)

        # 2. active レコードを取得（rebuild_db が list_files 経由で登録するため、
        # is_source_file=False のファイルは DB に登録されない）
        records = list(self.db.search_sources(
            source_type=source_type,
            status=SourceStatus.ACTIVE,
            path_prefix=path,
        ))

        empty_convert = PipelineSummary(
            mode=PipelineMode.CONVERT_ONLY,
            total_files=0,
            processed=0,
        )
        empty_index = PipelineSummary(
            mode=PipelineMode.INDEX_ONLY,
            total_files=0,
            processed=0,
        )

        logger.info("Target files: %d", len(records))

        if not records:
            return FullRebuildResult(convert=empty_convert, index=empty_index)

        to_commit = ""
        if self._git.has_commits():
            to_commit = self._git.get_head_commit()

        # --- Phase 1: Convert ---
        logger.info("Phase 1: Convert started (%d files)", len(records))
        self._converter.clear(self._converted_store_dir, source_type, path=path)

        def _convert_single(record: SourceRecord) -> None:
            self._converter.convert(
                record.source_id,
                self._source_store.root_dir,
                self._converted_store_dir,
            )

        convert_summary = await self._run_processing_loop(
            records,
            process_fn=_convert_single,
            get_file_path=lambda r: r.source_id,
            phase=PipelinePhase.CONVERT,
            mode=PipelineMode.CONVERT_ONLY,
            log_prefix="全再構築(Convert)中に",
            progress_callback=progress_callback,
            concurrency=concurrency,
        )
        logger.info(
            "Phase 1: Convert completed: processed=%d, errors=%d, warnings=%d",
            convert_summary.processed,
            len(convert_summary.errors),
            len(convert_summary.warnings),
        )

        # --- Phase 2: Index (convert 成功分のみ) ---
        await self._indexer.clear(source_type, path=path)

        convert_failed = convert_summary.failed_paths()
        convert_warned = convert_summary.warned_paths()
        convert_excluded = convert_failed | convert_warned
        if convert_excluded:
            logger.info(
                "Convert excluded: failed=%d, warned=%d",
                len(convert_failed),
                len(convert_warned),
            )
        index_records = [r for r in records if r.source_id not in convert_excluded]

        if not index_records:
            return FullRebuildResult(
                convert=convert_summary,
                index=PipelineSummary(
                    mode=PipelineMode.INDEX_ONLY,
                    total_files=0,
                    processed=0,
                    from_commit_id=NULL_COMMIT_HASH,
                    to_commit_id=to_commit,
                ),
            )

        async def _index_single(record: SourceRecord) -> None:
            converted_rel = get_converted_rel_path(record.source_id)
            converted_path = self._converted_store_dir / converted_rel
            if not converted_path.exists():
                raise ConversionSkippedError(
                    f"converted file not found: {converted_path}",
                )
            metadata = self._build_metadata_from_record(record)
            await self._indexer.add(record.source_id, converted_path, metadata)

        logger.info("Phase 2: Index started (%d files)", len(index_records))
        with self._indexer.batch_writes():
            index_summary = await self._run_processing_loop(
                index_records,
                process_fn=_index_single,
                get_file_path=lambda r: r.source_id,
                phase=PipelinePhase.INDEX,
                mode=PipelineMode.INDEX_ONLY,
                log_prefix="全再構築(Index)中に",
                progress_callback=progress_callback,
                from_commit_id=NULL_COMMIT_HASH,
                to_commit_id=to_commit,
                concurrency=concurrency,
            )
        logger.info(
            "Phase 2: Index completed: processed=%d, errors=%d, warnings=%d",
            index_summary.processed,
            len(index_summary.errors),
            len(index_summary.warnings),
        )

        # pipeline_history に記録（両フェーズともエラーなしの場合のみ）
        if not convert_summary.errors and not index_summary.errors and to_commit:
            self.db.add_pipeline_history(
                from_commit_id=NULL_COMMIT_HASH,
                to_commit_id=to_commit,
                processed_at=datetime.now(timezone.utc).isoformat(),
                mode="full",
                filter_source_type=source_type or "",
                filter_path=path or "",
            )

        return FullRebuildResult(convert=convert_summary, index=index_summary)

    def _check_uncommitted_changes(
        self,
        source_type: SourceType | None = None,
        *,
        path: str | None = None,
    ) -> None:
        """source_store に未コミットの変更がないか確認する.

        Args:
            source_type: チェック対象の媒体ディレクトリ（None で全体）
            path: チェック対象のディレクトリパス（source_type と排他）

        Raises:
            RuntimeError: 未コミットの変更がある場合
            ValueError: source_type と path が同時指定された場合
        """
        _validate_filter_exclusivity(source_type, path)
        check_path: str | None
        if path is not None:
            from rag.store.path_filter import normalize_path_prefix

            check_path = normalize_path_prefix(path)
        else:
            check_path = source_type
        if self._git.has_uncommitted_changes(
            path=check_path,
        ):
            msg = (
                "source_store に未コミットの変更があります。"
                "rebuild 前に変更をコミットしてください。"
            )
            raise RuntimeError(msg)

    def _auto_commit_for_incremental(self) -> None:
        """差分更新時に未コミット変更を自動コミットする."""
        try:
            commit_id = self._git.commit("auto-commit: incremental")
        except subprocess.CalledProcessError as e:
            msg = f"自動コミットに失敗しました: {e.stderr or e}"
            raise RuntimeError(msg) from e
        if commit_id:
            logger.info("自動コミット完了: %s", commit_id)

    async def run_convert_only(
        self,
        source_type: SourceType | None = None,
        *,
        path: str | None = None,
        progress_callback: ProgressCallback | None = None,
        concurrency: int = 1,
    ) -> PipelineSummary:
        """コンバートのみ再実行する.

        converted_store をクリアし、source_store 全ファイルを
        コンバーターで再処理する。インデクサーは実行しない。

        Args:
            source_type: 対象媒体フィルタ（None で全媒体）
            path: パスフィルタ（source_type と排他）
            progress_callback: 進捗コールバック
            concurrency: 同時実行数

        Raises:
            RuntimeError: source_store に未コミットの変更がある場合
            ValueError: source_type と path が同時指定された場合
        """
        _validate_filter_exclusivity(source_type, path)
        logger.info(
            "Convert-only rebuild started (source_type=%s, path=%s)",
            source_type or "all", path or "-",
        )
        self._git.init_repo()
        self._check_uncommitted_changes(source_type, path=path)

        # 1. converted_store クリア
        self._converter.clear(self._converted_store_dir, source_type, path=path)

        # 2. active レコードを取得（is_source_file=False のファイルは rebuild_db で
        # 登録されないため、DB 側フィルタは不要）
        records = list(self.db.search_sources(
            source_type=source_type,
            status=SourceStatus.ACTIVE,
            path_prefix=path,
        ))

        logger.info("Target files: %d", len(records))

        if not records:
            return PipelineSummary(
                mode=PipelineMode.CONVERT_ONLY,
                total_files=0,
                processed=0,
            )

        # 3. 全ファイルをコンバート
        def _convert_single(record: SourceRecord) -> None:
            self._converter.convert(
                record.source_id,
                self._source_store.root_dir,
                self._converted_store_dir,
            )

        summary = await self._run_processing_loop(
            records,
            process_fn=_convert_single,
            get_file_path=lambda r: r.source_id,
            phase=PipelinePhase.CONVERT,
            mode=PipelineMode.CONVERT_ONLY,
            log_prefix="コンバート再実行中に",
            progress_callback=progress_callback,
            concurrency=concurrency,
        )
        logger.info(
            "Convert-only rebuild completed: processed=%d, errors=%d, warnings=%d",
            summary.processed,
            len(summary.errors),
            len(summary.warnings),
        )
        return summary

    async def run_index_only(
        self,
        source_type: SourceType | None = None,
        *,
        path: str | None = None,
        progress_callback: ProgressCallback | None = None,
        concurrency: int = 1,
    ) -> PipelineSummary:
        """インデックスのみ再構築する.

        ChromaDB + BM25 をクリアし、
        converted_store 全ファイルからインデックスを再構築する。

        Args:
            source_type: 対象媒体フィルタ（None で全媒体）
            path: パスフィルタ（source_type と排他）

        Raises:
            RuntimeError: source_store に未コミットの変更がある場合
            ValueError: source_type と path が同時指定された場合
        """
        _validate_filter_exclusivity(source_type, path)
        logger.info(
            "Index-only rebuild started (source_type=%s, path=%s)",
            source_type or "all", path or "-",
        )
        self._git.init_repo()
        self._check_uncommitted_changes(source_type, path=path)

        # 1. インデックスクリア
        await self._indexer.clear(source_type, path=path)

        # 2. active なレコードを取得（is_source_file=False のファイルは DB に登録
        # されないため、フィルタは不要）
        records = list(self.db.search_sources(
            source_type=source_type,
            status=SourceStatus.ACTIVE,
            path_prefix=path,
        ))

        logger.info("Target files: %d", len(records))

        if not records:
            return PipelineSummary(
                mode=PipelineMode.INDEX_ONLY,
                total_files=0,
                processed=0,
            )

        # 3. converted_store からインデックス再構築
        async def _index_single(record: SourceRecord) -> None:
            converted_rel = get_converted_rel_path(record.source_id)
            converted_path = self._converted_store_dir / converted_rel
            if not converted_path.exists():
                raise ConversionSkippedError(
                    f"converted file not found: {converted_path}",
                )
            metadata = self._build_metadata_from_record(record)
            await self._indexer.add(record.source_id, converted_path, metadata)

        to_commit = ""
        if self._git.has_commits():
            to_commit = self._git.get_head_commit()

        with self._indexer.batch_writes():
            summary = await self._run_processing_loop(
                records,
                process_fn=_index_single,
                get_file_path=lambda r: r.source_id,
                phase=PipelinePhase.INDEX,
                mode=PipelineMode.INDEX_ONLY,
                log_prefix="インデックス再構築中に",
                progress_callback=progress_callback,
                concurrency=concurrency,
            )

        logger.info(
            "Index-only rebuild completed: processed=%d, errors=%d, warnings=%d",
            summary.processed,
            len(summary.errors),
            len(summary.warnings),
        )

        # pipeline_history に記録（正常完了時のみ）
        if not summary.errors and to_commit:
            self.db.add_pipeline_history(
                from_commit_id=NULL_COMMIT_HASH,
                to_commit_id=to_commit,
                processed_at=datetime.now(timezone.utc).isoformat(),
                mode="index",
                filter_source_type=source_type or "",
                filter_path=path or "",
            )

        return summary

    # --- 変更ファイルの特定（ChangeDetector への委譲） ---

    def _scan_all_as_added(self) -> list[ChangeEntry]:
        """全追跡ファイルを「追加」として返す（ChangeDetector 委譲）."""
        return self._change_detector.scan_all_as_added()

    def _supplement_hidden_changes(
        self,
        raw_diff: list[tuple[str, str, str]],
        from_commit_id: str,
    ) -> list[tuple[str, str, str]]:
        """ネット差分で検出されない中間変更を補完する（ChangeDetector 委譲）."""
        return self._change_detector.supplement_hidden_changes(
            raw_diff, from_commit_id,
        )

    def _classify_changes(
        self,
        raw_diff: list[tuple[str, str, str]],
    ) -> list[ChangeEntry]:
        """git diff の生出力を ChangeEntry に分類する（ChangeDetector 委譲）."""
        return self._change_detector.classify_changes(raw_diff)

    # --- 共通処理ループ ---

    async def _run_processing_loop(
        self,
        items: Sequence[_T],
        *,
        process_fn: Callable[[_T], Awaitable[None]] | Callable[[_T], None],
        get_file_path: Callable[[_T], str],
        phase: PipelinePhase,
        mode: PipelineMode,
        log_prefix: str,
        progress_callback: ProgressCallback | None = None,
        from_commit_id: str = "",
        to_commit_id: str = "",
        concurrency: int = 1,
    ) -> PipelineSummary:
        """共通の処理ループ.

        各パイプラインモードで共通する
        ループ + try/except + progress + PipelineSummary 組み立てを一元化する。
        process_fn は sync / async どちらも受け付ける。
        sync の場合は asyncio.to_thread でスレッドプールへオフロードし、
        イベントループをブロックしない。

        asyncio.Semaphore で同時実行数を制限し、asyncio.gather で並列処理する。
        concurrency=1 の場合は実質直列動作となる。
        """
        if concurrency < 1:
            msg = f"concurrency must be >= 1, got {concurrency}"
            raise ValueError(msg)

        sem = asyncio.Semaphore(concurrency)
        processed = 0
        errors: list[PipelineErrorEntry] = []
        warnings: list[PipelineWarningEntry] = []
        lock = asyncio.Lock()
        is_async = inspect.iscoroutinefunction(process_fn)

        async def _process_one(item: _T) -> None:
            nonlocal processed
            file_path = get_file_path(item)
            async with sem:
                try:
                    if is_async:
                        await process_fn(item)  # type: ignore[misc]
                    else:
                        await asyncio.to_thread(process_fn, item)
                    async with lock:
                        processed += 1
                except asyncio.CancelledError:
                    raise
                except ConversionSkippedError as e:
                    logger.warning(
                        "%sスキップ: %s (%s)", log_prefix, file_path, e,
                    )
                    async with lock:
                        warnings.append(PipelineWarningEntry(
                            path=file_path,
                            message=str(e),
                            phase=phase.value,
                        ))
                except ConversionFailedError as e:
                    logger.error(
                        "%s変換失敗: %s (%s)", log_prefix, file_path, e,
                    )
                    entry = self._build_error_entry(
                        file_path, str(e), phase,
                    )
                    async with lock:
                        errors.append(entry)
                except Exception as e:
                    logger.exception(
                        "%sエラー: %s", log_prefix, file_path,
                    )
                    entry = self._build_error_entry(
                        file_path, str(e), phase,
                    )
                    async with lock:
                        errors.append(entry)
                if progress_callback is not None:
                    async with lock:
                        completed = processed + len(errors) + len(warnings)
                    try:
                        progress_callback(
                            completed, len(items),
                            f"[{phase.display}] {file_path}",
                        )
                    except Exception:
                        logger.debug(
                            "progress_callback エラー: %s", file_path,
                            exc_info=True,
                        )

        await asyncio.gather(*[_process_one(item) for item in items])

        return PipelineSummary(
            mode=mode,
            total_files=len(items),
            processed=processed,
            errors=errors,
            warnings=warnings,
            from_commit_id=from_commit_id,
            to_commit_id=to_commit_id,
        )

    def _build_error_entry(
        self,
        file_path: str,
        message: str,
        phase: PipelinePhase,
    ) -> PipelineErrorEntry:
        """PipelineSummary.errors に追加する構造化エントリを生成する.

        仕様: docs/specs/pipeline-controller.md の `PipelineSummary.errors`

        size_bytes の取得元は phase に応じて切り替える:
        - CONVERT / CONVERT_AND_INDEX: source_store 側のファイルサイズ
          （壊れファイル検出時に元データのサイズを残す）
        - INDEX: converted_store 側のファイルサイズ
          （index 失敗時は変換済みテキストのサイズが診断に有用）
        - FETCH / その他: source_store 側（デフォルト）

        取得失敗時（権限エラー・未配置等）は `size_bytes=None` とする。
        """
        if phase is PipelinePhase.INDEX:
            converted_rel = get_converted_rel_path(file_path)
            target_path = self._converted_store_dir / converted_rel
        else:
            target_path = self._source_store.root_dir / file_path
        try:
            size_bytes: int | None = target_path.stat().st_size
        except OSError:
            size_bytes = None
        return PipelineErrorEntry(
            path=file_path,
            size_bytes=size_bytes,
            message=message,
            phase=phase.error_key,
        )

    # --- 変更処理 ---

    async def _process_changes(
        self,
        changes: list[ChangeEntry],
        mode: PipelineMode,
        from_commit_id: str,
        to_commit_id: str,
        progress_callback: ProgressCallback | None = None,
        concurrency: int = 1,
    ) -> PipelineSummary:
        """変更エントリを処理する."""
        with self._indexer.batch_writes():
            summary = await self._run_processing_loop(
                changes,
                process_fn=self._process_single_change,
                get_file_path=lambda e: e.file_path,
                phase=PipelinePhase.CONVERT_AND_INDEX,
                mode=mode,
                log_prefix="パイプライン処理中に",
                progress_callback=progress_callback,
                from_commit_id=from_commit_id,
                to_commit_id=to_commit_id,
                concurrency=concurrency,
            )
        return summary

    async def _process_single_change(self, entry: ChangeEntry) -> None:
        """1ファイルの変更を処理する（ChangeHandler 委譲）."""
        await self._change_handler.process_change(entry)

    async def _handle_added(self, entry: ChangeEntry) -> None:
        """追加ファイルを処理する（ChangeHandler 委譲）."""
        await self._change_handler._handle_added(entry)  # noqa: SLF001

    async def _handle_modified(self, entry: ChangeEntry) -> None:
        """変更ファイルを処理する（ChangeHandler 委譲）."""
        await self._change_handler._handle_modified(entry)  # noqa: SLF001

    async def _handle_deleted(self, entry: ChangeEntry) -> None:
        """削除ファイルを処理する（ChangeHandler 委譲）."""
        await self._change_handler._handle_deleted(entry)  # noqa: SLF001

    async def _handle_renamed(self, entry: ChangeEntry) -> None:
        """リネームファイルを処理する（ChangeHandler 委譲）."""
        await self._change_handler._handle_renamed(entry)  # noqa: SLF001

    async def _handle_meta_only(self, entry: ChangeEntry) -> None:
        """.meta のみ変更を処理する（ChangeHandler 委譲）."""
        await self._change_handler._handle_meta_only(entry)  # noqa: SLF001

    # --- metadata 操作（MetadataBuilder への委譲） ---

    def _register_in_db(self, file_path: str) -> None:
        """ファイルを metadata.db に登録する（MetadataBuilder 委譲）."""
        self._metadata_builder.register_in_db(file_path)

    def _update_in_db(self, file_path: str) -> None:
        """ファイルの metadata.db を更新する（MetadataBuilder 委譲）."""
        self._metadata_builder.update_in_db(file_path)

    def _resolve_source_id(self, file_path: str) -> str:
        """file_path から source_id を解決する（MetadataBuilder 委譲）."""
        return self._metadata_builder.resolve_source_id(file_path)

    def _build_metadata(self, file_path: str) -> SourceMetadata:
        """ファイルから SourceMetadata を構築する（MetadataBuilder 委譲）."""
        return self._metadata_builder.build_metadata(file_path)

    def _build_metadata_from_record(
        self,
        record: SourceRecord,
    ) -> SourceMetadata:
        """SourceRecord から SourceMetadata を構築する（MetadataBuilder 委譲）."""
        return self._metadata_builder.build_metadata_from_record(record)


# --- モジュールレベルユーティリティ ---
# resolve_title / resolve_published_at は rag.store.resolve に一元化
# source_id は file_path をそのまま使用（resolve 不要）


def _validate_filter_exclusivity(
    source_type: SourceType | None,
    path: str | None,
) -> None:
    """source_type と path の排他性を検証する."""
    if source_type is not None and path is not None:
        msg = "source_type と path は同時に指定できません"
        raise ValueError(msg)

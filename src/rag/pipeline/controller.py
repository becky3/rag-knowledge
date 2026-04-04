"""パイプライン制御 — メインコントローラ.

仕様: docs/specs/pipeline-controller.md

3段パイプライン（インジェスター → コンバーター → インデクサー）の
ステージ間連携を担当するオーケストレーション層。
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from rag.converter.converter import ConversionSkippedError, get_converted_rel_path
from rag.infrastructure.file_lock import INGEST_LOCK_FILENAME, REBUILD_LOCK_FILENAME
from rag.pipeline.git_ops import GitOperations
from rag.pipeline.models import (
    PHASE_CONVERT,
    PHASE_CONVERT_AND_INDEX,
    PHASE_INDEX,
    ChangeEntry,
    ChangeStatus,
    PipelineMode,
    PipelineSummary,
    detect_source_type,
)
from rag.pipeline.protocols import ConverterProtocol, IndexerProtocol
from rag.store.meta import meta_path_for, read_meta
from rag.store.metadata_db import MetadataDB
from rag.store.models import (
    NULL_COMMIT_HASH,
    SourceMetadata,
    SourceRecord,
    SourceType,
)
from rag.store.resolve import resolve_published_at, resolve_source_id, resolve_title
from rag.store.source_store import SourceStore

from rag.pipeline.ingesters._common import ProgressCallback

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# .meta を持たない媒体
_NO_META_TYPES: frozenset[SourceType] = frozenset({"local"})

# パイプライン処理対象外のファイル（git メタデータ等）
_PIPELINE_EXCLUDE_FILES: frozenset[str] = frozenset({
    ".gitignore",
    INGEST_LOCK_FILENAME,
    REBUILD_LOCK_FILENAME,
    "aozora/catalog.csv",
    "aozora/catalog.csv.meta",
})


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

    def ingest_and_index(
        self,
        message: str,
        progress_callback: ProgressCallback | None = None,
    ) -> PipelineSummary:
        """インジェスター実行後の後処理を一括実行する.

        source_store の変更を git commit し、差分更新を実行する。

        Args:
            message: コミットメッセージ
            progress_callback: 進捗コールバック

        Returns:
            パイプライン処理結果サマリ
        """
        self.commit(message)
        return self.run_incremental(progress_callback=progress_callback)

    # --- パイプライン実行 ---

    def run_incremental(self, progress_callback: ProgressCallback | None = None) -> PipelineSummary:
        """差分更新を実行する.

        source_store に未コミットの変更がある場合は自動コミットし、
        last_commit_id と HEAD の差分を検知して変更ファイルのみをパイプライン処理する。
        """
        self._git.init_repo()

        # 未コミット変更の自動コミット
        if self._git.has_uncommitted_changes():
            self._auto_commit_for_incremental()

        if not self._git.has_commits():
            return PipelineSummary(
                mode=PipelineMode.INCREMENTAL,
                total_files=0,
                processed=0,
                skipped=0,
            )

        last_commit_id = self.db.get_last_commit_id()
        head_commit = self._git.get_head_commit()

        # last_commit_id == HEAD → 差分なし
        if last_commit_id == head_commit:
            return PipelineSummary(
                mode=PipelineMode.INCREMENTAL,
                total_files=0,
                processed=0,
                skipped=0,
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
            changes = self._classify_changes(raw_diff)

        if not changes:
            return PipelineSummary(
                mode=PipelineMode.INCREMENTAL,
                total_files=0,
                processed=0,
                skipped=0,
                from_commit_id=last_commit_id,
                to_commit_id=head_commit,
            )

        summary = self._process_changes(
            changes, PipelineMode.INCREMENTAL, last_commit_id, head_commit,
            progress_callback=progress_callback,
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

    def run_full_rebuild(
        self,
        source_type: SourceType | None = None,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> PipelineSummary:
        """全再構築を実行する.

        converted_store とインデックスをクリアし、
        source_store 全ファイルをパイプライン処理する。

        Args:
            source_type: 対象媒体フィルタ（None で全媒体）

        Raises:
            RuntimeError: source_store に未コミットの変更がある場合
        """
        self._git.init_repo()
        self._check_uncommitted_changes(source_type)

        # 1. metadata.db 再構築
        self._source_store.rebuild_db()

        # 2. converted_store クリア
        self._converter.clear(self._converted_store_dir, source_type)

        # 3. インデックスクリア
        self._indexer.clear(source_type)

        # 4. active ファイルをスキャン（パイプライン対象外ファイルを除外）
        records = [
            r for r in self.db.search_sources(
                source_type=source_type,
                status="active",
            )
            if r.file_path not in _PIPELINE_EXCLUDE_FILES
        ]

        if not records:
            return PipelineSummary(
                mode=PipelineMode.FULL_REBUILD,
                total_files=0,
                processed=0,
                skipped=0,
            )

        # 5-6. コンバート + インデックス
        def _convert_and_index(record: SourceRecord) -> None:
            converted_path = self._converter.convert(
                record.file_path,
                self._source_store.root_dir,
                self._converted_store_dir,
            )
            metadata = self._build_metadata_from_record(record)
            self._indexer.add(record.source_id, converted_path, metadata)

        to_commit = ""
        if self._git.has_commits():
            to_commit = self._git.get_head_commit()

        summary = self._run_processing_loop(
            records,
            process_fn=_convert_and_index,
            get_file_path=lambda r: r.file_path,
            phase=PHASE_CONVERT_AND_INDEX,
            mode=PipelineMode.FULL_REBUILD,
            log_prefix="全再構築中に",
            progress_callback=progress_callback,
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id=to_commit,
        )

        # pipeline_history に記録（正常完了時のみ）
        if not summary.errors and to_commit:
            self.db.add_pipeline_history(
                from_commit_id=NULL_COMMIT_HASH,
                to_commit_id=to_commit,
                processed_at=datetime.now(timezone.utc).isoformat(),
                mode="full",
            )

        return summary

    def _check_uncommitted_changes(
        self,
        source_type: SourceType | None = None,
    ) -> None:
        """source_store に未コミットの変更がないか確認する.

        Args:
            source_type: チェック対象の媒体ディレクトリ（None で全体）

        Raises:
            RuntimeError: 未コミットの変更がある場合
        """
        if self._git.has_uncommitted_changes(
            path=source_type,
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

    def run_convert_only(
        self,
        source_type: SourceType | None = None,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> PipelineSummary:
        """コンバートのみ再実行する.

        converted_store をクリアし、source_store 全ファイルを
        コンバーターで再処理する。インデクサーは実行しない。

        Args:
            source_type: 対象媒体フィルタ（None で全媒体）

        Raises:
            RuntimeError: source_store に未コミットの変更がある場合
        """
        self._git.init_repo()
        self._check_uncommitted_changes(source_type)

        # 1. converted_store クリア
        self._converter.clear(self._converted_store_dir, source_type)

        # 2. active ファイルをスキャン（パイプライン対象外ファイルを除外）
        records = [
            r for r in self.db.search_sources(
                source_type=source_type,
                status="active",
            )
            if r.file_path not in _PIPELINE_EXCLUDE_FILES
        ]

        if not records:
            return PipelineSummary(
                mode=PipelineMode.CONVERT_ONLY,
                total_files=0,
                processed=0,
                skipped=0,
            )

        # 3. 全ファイルをコンバート
        def _convert_single(record: SourceRecord) -> None:
            self._converter.convert(
                record.file_path,
                self._source_store.root_dir,
                self._converted_store_dir,
            )

        return self._run_processing_loop(
            records,
            process_fn=_convert_single,
            get_file_path=lambda r: r.file_path,
            phase=PHASE_CONVERT,
            mode=PipelineMode.CONVERT_ONLY,
            log_prefix="コンバート再実行中に",
            progress_callback=progress_callback,
        )

    def run_index_only(
        self,
        source_type: SourceType | None = None,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> PipelineSummary:
        """インデックスのみ再構築する.

        ChromaDB + BM25 をクリアし、
        converted_store 全ファイルからインデックスを再構築する。

        Args:
            source_type: 対象媒体フィルタ（None で全媒体）

        Raises:
            RuntimeError: source_store に未コミットの変更がある場合
        """
        self._git.init_repo()
        self._check_uncommitted_changes(source_type)

        # 1. インデックスクリア
        self._indexer.clear(source_type)

        # 2. active なレコードを取得（パイプライン対象外ファイルを除外）
        records = [
            r for r in self.db.search_sources(
                source_type=source_type,
                status="active",
            )
            if r.file_path not in _PIPELINE_EXCLUDE_FILES
        ]

        if not records:
            return PipelineSummary(
                mode=PipelineMode.INDEX_ONLY,
                total_files=0,
                processed=0,
                skipped=0,
            )

        # 3. converted_store からインデックス再構築
        def _index_single(record: SourceRecord) -> None:
            converted_rel = get_converted_rel_path(record.file_path)
            converted_path = self._converted_store_dir / converted_rel
            if not converted_path.exists():
                raise ConversionSkippedError(
                    f"converted file not found: {converted_path}",
                )
            metadata = self._build_metadata_from_record(record)
            self._indexer.add(record.source_id, converted_path, metadata)

        to_commit = ""
        if self._git.has_commits():
            to_commit = self._git.get_head_commit()

        summary = self._run_processing_loop(
            records,
            process_fn=_index_single,
            get_file_path=lambda r: r.file_path,
            phase=PHASE_INDEX,
            mode=PipelineMode.INDEX_ONLY,
            log_prefix="インデックス再構築中に",
            progress_callback=progress_callback,
        )

        # pipeline_history に記録（正常完了時のみ）
        if not summary.errors and to_commit:
            self.db.add_pipeline_history(
                from_commit_id=NULL_COMMIT_HASH,
                to_commit_id=to_commit,
                processed_at=datetime.now(timezone.utc).isoformat(),
                mode="index",
            )

        return summary

    # --- 変更ファイルの特定 ---

    def _scan_all_as_added(self) -> list[ChangeEntry]:
        """全追跡ファイルを「追加」として返す."""
        all_files = self._git.list_all_files()
        entries: list[ChangeEntry] = []
        for f in all_files:
            if f in _PIPELINE_EXCLUDE_FILES:
                continue
            if f.endswith(".meta"):
                continue
            entries.append(ChangeEntry(
                status=ChangeStatus.ADDED,
                file_path=f,
            ))
        return entries

    def _classify_changes(
        self,
        raw_diff: list[tuple[str, str, str]],
    ) -> list[ChangeEntry]:
        """git diff の生出力を ChangeEntry に分類する.

        .meta ファイルのみの変更を meta_only として検出する。
        """
        data_entries: dict[str, ChangeEntry] = {}
        meta_files: list[tuple[str, str, str]] = []

        for status_char, file_path, old_path in raw_diff:
            if file_path in _PIPELINE_EXCLUDE_FILES:
                continue
            if file_path.endswith(".meta"):
                meta_files.append((status_char, file_path, old_path))
            else:
                entry = self._map_status(status_char, file_path, old_path)
                data_entries[file_path] = entry

        # .meta のみの変更を検出
        for _status_char, meta_path, _old_path in meta_files:
            data_path = meta_path.removesuffix(".meta")
            if data_path not in data_entries:
                data_entries[data_path] = ChangeEntry(
                    status=ChangeStatus.META_ONLY,
                    file_path=data_path,
                )

        return list(data_entries.values())

    @staticmethod
    def _map_status(
        status_char: str,
        file_path: str,
        old_path: str,
    ) -> ChangeEntry:
        """git status 文字を ChangeStatus にマッピングする."""
        mapping = {
            "A": ChangeStatus.ADDED,
            "M": ChangeStatus.MODIFIED,
            "D": ChangeStatus.DELETED,
            "R": ChangeStatus.RENAMED,
        }
        status = mapping.get(status_char, ChangeStatus.MODIFIED)
        return ChangeEntry(status=status, file_path=file_path, old_path=old_path)

    # --- 共通処理ループ ---

    def _run_processing_loop(
        self,
        items: Sequence[_T],
        *,
        process_fn: Callable[[_T], None],
        get_file_path: Callable[[_T], str],
        phase: str,
        mode: PipelineMode,
        log_prefix: str,
        progress_callback: ProgressCallback | None = None,
        from_commit_id: str = "",
        to_commit_id: str = "",
    ) -> PipelineSummary:
        """共通の処理ループ.

        各パイプラインモードで共通する
        ループ + try/except + progress + PipelineSummary 組み立てを一元化する。
        """
        processed = 0
        skipped = 0
        errors: list[str] = []
        warnings: list[str] = []

        for item in items:
            file_path = get_file_path(item)
            try:
                process_fn(item)
                processed += 1
            except ConversionSkippedError as e:
                logger.warning(
                    "%sスキップ: %s (%s)", log_prefix, file_path, e,
                )
                warnings.append(f"{file_path}: {e}")
                skipped += 1
            except Exception:
                logger.exception(
                    "%sエラー: %s", log_prefix, file_path,
                )
                errors.append(file_path)
                skipped += 1
            if progress_callback is not None:
                progress_callback(
                    processed + skipped, len(items),
                    f"[{phase}] {file_path}",
                )

        return PipelineSummary(
            mode=mode,
            total_files=len(items),
            processed=processed,
            skipped=skipped,
            errors=errors,
            warnings=warnings,
            from_commit_id=from_commit_id,
            to_commit_id=to_commit_id,
        )

    # --- 変更処理 ---

    def _process_changes(
        self,
        changes: list[ChangeEntry],
        mode: PipelineMode,
        from_commit_id: str,
        to_commit_id: str,
        progress_callback: ProgressCallback | None = None,
    ) -> PipelineSummary:
        """変更エントリを処理する."""
        return self._run_processing_loop(
            changes,
            process_fn=self._process_single_change,
            get_file_path=lambda e: e.file_path,
            phase=PHASE_CONVERT_AND_INDEX,
            mode=mode,
            log_prefix="パイプライン処理中に",
            progress_callback=progress_callback,
            from_commit_id=from_commit_id,
            to_commit_id=to_commit_id,
        )

    def _process_single_change(self, entry: ChangeEntry) -> None:
        """1ファイルの変更を処理する."""
        handler = {
            ChangeStatus.ADDED: self._handle_added,
            ChangeStatus.MODIFIED: self._handle_modified,
            ChangeStatus.DELETED: self._handle_deleted,
            ChangeStatus.RENAMED: self._handle_renamed,
            ChangeStatus.META_ONLY: self._handle_meta_only,
        }
        handler[entry.status](entry)

    def _handle_added(self, entry: ChangeEntry) -> None:
        """追加ファイルを処理する."""
        # 論理削除済みファイルは処理をスキップ
        # （DB=active / インデックス未登録の不整合を防止）
        source_id = self._resolve_source_id(entry.file_path)
        existing = self.db.get_source(source_id)
        if existing is not None and existing.status == "deleted":
            return

        self._register_in_db(entry.file_path)
        converted_path = self._converter.convert(
            entry.file_path,
            self._source_store.root_dir,
            self._converted_store_dir,
        )
        metadata = self._build_metadata(entry.file_path)
        self._indexer.add(source_id, converted_path, metadata)

    def _handle_modified(self, entry: ChangeEntry) -> None:
        """変更ファイルを処理する."""
        self._update_in_db(entry.file_path)
        converted_path = self._converter.convert(
            entry.file_path,
            self._source_store.root_dir,
            self._converted_store_dir,
        )
        source_id = self._resolve_source_id(entry.file_path)
        metadata = self._build_metadata(entry.file_path)
        self._indexer.update(source_id, converted_path, metadata)

    def _handle_deleted(self, entry: ChangeEntry) -> None:
        """削除ファイルを処理する."""
        source_id = self._resolve_source_id(entry.file_path)
        self._converter.delete(entry.file_path, self._converted_store_dir)
        self._indexer.delete(source_id)
        try:
            self.db.set_status(source_id, "deleted")
        except KeyError:
            logger.warning(
                "削除対象が metadata.db に存在しません: %s", source_id,
            )

    def _handle_renamed(self, entry: ChangeEntry) -> None:
        """リネームファイルを処理する."""
        old_source_id = self._resolve_source_id(entry.old_path)
        source_type = detect_source_type(entry.file_path)

        # コンバーター: 新パスで変換
        converted_path = self._converter.convert(
            entry.file_path,
            self._source_store.root_dir,
            self._converted_store_dir,
        )

        # 旧パスの converted を削除
        self._converter.delete(entry.old_path, self._converted_store_dir)

        # インデクサー: 旧パス削除 + 新パス追加
        self._indexer.delete(old_source_id)
        new_source_id = self._resolve_source_id(entry.file_path)
        metadata = self._build_metadata(entry.file_path)
        self._indexer.add(new_source_id, converted_path, metadata)

        # metadata.db 更新
        if source_type == "local":
            # local: source_id が変わるため DELETE + INSERT
            try:
                self.db.set_status(old_source_id, "deleted")
            except KeyError:
                pass
            self._register_in_db(entry.file_path)
        else:
            now = datetime.now(timezone.utc).isoformat()
            full_path = self._source_store.root_dir / entry.file_path
            data = full_path.read_bytes()
            content_hash = hashlib.sha256(data).hexdigest()
            try:
                self.db.update_source(
                    old_source_id,
                    file_path=entry.file_path,
                    content_hash=content_hash,
                    file_size=len(data),
                    updated_at=now,
                )
            except KeyError:
                self._register_in_db(entry.file_path)

    def _handle_meta_only(self, entry: ChangeEntry) -> None:
        """.meta のみ変更を処理する."""
        source_id = self._resolve_source_id(entry.file_path)
        source_type = detect_source_type(entry.file_path)

        # metadata.db 更新
        if source_type not in _NO_META_TYPES:
            full_path = self._source_store.root_dir / entry.file_path
            meta_file = meta_path_for(full_path)
            if meta_file.exists():
                meta_data = read_meta(full_path)
                title = str(meta_data.get("title", ""))
                if title:
                    now = datetime.now(timezone.utc).isoformat()
                    try:
                        self.db.update_source(
                            source_id,
                            title=title,
                            updated_at=now,
                        )
                    except KeyError:
                        logger.warning(
                            "meta_only 更新対象が metadata.db にありません: %s",
                            source_id,
                        )
                        return

        # インデクサー: メタデータのみ更新
        metadata = self._build_metadata(entry.file_path)
        self._indexer.upsert_metadata(source_id, metadata)

    # --- metadata 操作ヘルパー ---

    def _register_in_db(self, file_path: str) -> None:
        """ファイルを metadata.db に登録する."""
        full_path = self._source_store.root_dir / file_path
        data = full_path.read_bytes()
        content_hash = hashlib.sha256(data).hexdigest()
        now = datetime.now(timezone.utc).isoformat()

        source_type = detect_source_type(file_path)
        meta_dict = self._read_meta_dict(file_path)

        source_id = resolve_source_id(
            source_type, file_path, meta_dict,
        )
        title = resolve_title(
            source_type, file_path, meta_dict,
        )

        existing = self.db.get_source(source_id)
        if existing:
            collected_at = existing.collected_at
        elif meta_dict and "collected_at" in meta_dict:
            collected_at = str(meta_dict["collected_at"])
        else:
            collected_at = now

        published_at = resolve_published_at(
            source_type, meta_dict, collected_at,
        )

        self.db.register_source(
            source_id=source_id,
            source_type=source_type,
            file_path=file_path,
            title=title,
            content_hash=content_hash,
            file_size=len(data),
            collected_at=collected_at,
            updated_at=now,
            published_at=published_at,
        )

    def _update_in_db(self, file_path: str) -> None:
        """ファイルの metadata.db を更新する."""
        full_path = self._source_store.root_dir / file_path
        data = full_path.read_bytes()
        content_hash = hashlib.sha256(data).hexdigest()
        now = datetime.now(timezone.utc).isoformat()

        source_id = self._resolve_source_id(file_path)
        try:
            self.db.update_source(
                source_id,
                content_hash=content_hash,
                file_size=len(data),
                updated_at=now,
            )
        except KeyError:
            self._register_in_db(file_path)

    def _resolve_source_id(self, file_path: str) -> str:
        """file_path から source_id を解決する.

        1. metadata.db にレコードがあればそちらを使用
        2. ファイル + .meta から取得
        """
        record = self.db.get_source_by_path(file_path)
        if record:
            return record.source_id
        source_type = detect_source_type(file_path)
        meta_dict = self._read_meta_dict(file_path)
        return resolve_source_id(source_type, file_path, meta_dict)

    def _read_meta_dict(self, file_path: str) -> dict[str, str] | None:
        """ファイルの .meta を読み込む."""
        source_type = detect_source_type(file_path)
        if source_type in _NO_META_TYPES:
            return None
        full_path = self._source_store.root_dir / file_path
        meta_file = meta_path_for(full_path)
        if not meta_file.exists():
            return None
        try:
            return read_meta(full_path)
        except Exception:
            logger.warning(".meta の読み込みに失敗: %s", meta_file)
            return None

    def _build_metadata(self, file_path: str) -> SourceMetadata:
        """ファイルから SourceMetadata を構築する."""
        source_type = detect_source_type(file_path)
        meta_dict = self._read_meta_dict(file_path) or {}

        source_id = resolve_source_id(
            source_type, file_path, meta_dict,
        )
        title = resolve_title(
            source_type, file_path, meta_dict,
        )
        collected_at = str(meta_dict.get(
            "collected_at",
            datetime.now(timezone.utc).isoformat(),
        ))

        extra = dict(meta_dict)
        for key in ("source_id", "source_type", "title", "collected_at"):
            extra.pop(key, None)

        return SourceMetadata(
            source_id=source_id,
            source_type=source_type,
            title=title,
            collected_at=collected_at,
            extra=extra,
        )

    def _build_metadata_from_record(
        self,
        record: SourceRecord,
    ) -> SourceMetadata:
        """SourceRecord から SourceMetadata を構築する."""
        file_path = record.file_path
        source_id = record.source_id
        source_type = record.source_type
        title = record.title
        collected_at_db = record.collected_at

        meta_dict = self._read_meta_dict(file_path) or {}
        collected_at = str(meta_dict.get("collected_at", collected_at_db))

        extra = dict(meta_dict)
        for key in ("source_id", "source_type", "title", "collected_at"):
            extra.pop(key, None)

        return SourceMetadata(
            source_id=source_id,
            source_type=source_type,
            title=title,
            collected_at=collected_at,
            extra=extra,
        )


# --- モジュールレベルユーティリティ ---
# resolve_source_id / resolve_title は rag.store.resolve に一元化

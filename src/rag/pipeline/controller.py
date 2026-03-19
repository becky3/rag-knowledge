"""パイプライン制御 — メインコントローラ.

仕様: docs/specs/pipeline-controller.md

3段パイプライン（インジェスター → コンバーター → インデクサー）の
ステージ間連携を担当するオーケストレーション層。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from rag.pipeline.git_ops import GitOperations
from rag.pipeline.models import (
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
from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)

# .meta を持たない媒体
_NO_META_TYPES: frozenset[SourceType] = frozenset({"local"})

# パイプライン処理対象外のファイル（git メタデータ等）
_PIPELINE_EXCLUDE_FILES: frozenset[str] = frozenset({".gitignore"})


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

    # --- パイプライン実行 ---

    def run_incremental(self) -> PipelineSummary:
        """差分更新を実行する.

        last_commit_id と HEAD の差分を検知し、
        変更ファイルのみをパイプライン処理する。
        """
        self._git.init_repo()

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
        )

        # 正常完了時のみ pipeline_history に記録
        if not summary.errors:
            self.db.add_pipeline_history(
                from_commit_id=last_commit_id,
                to_commit_id=head_commit,
                processed_at=datetime.now(timezone.utc).isoformat(),
            )

        return summary

    def run_full_rebuild(
        self,
        source_type: SourceType | None = None,
    ) -> PipelineSummary:
        """全再構築を実行する.

        converted_store とインデックスをクリアし、
        source_store 全ファイルをパイプライン処理する。
        """
        self._git.init_repo()

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
        processed = 0
        skipped = 0
        errors: list[str] = []

        for record in records:
            try:
                converted_path = self._converter.convert(
                    record.file_path,
                    self._source_store.root_dir,
                    self._converted_store_dir,
                )
                metadata = self._build_metadata_from_record(record)
                self._indexer.add(record.source_id, converted_path, metadata)
                processed += 1
            except Exception:
                logger.exception("全再構築中にエラー: %s", record.file_path)
                errors.append(record.file_path)
                skipped += 1

        # pipeline_history に記録（正常完了時のみ）
        to_commit = ""
        if self._git.has_commits():
            to_commit = self._git.get_head_commit()

        if not errors and to_commit:
            self.db.add_pipeline_history(
                from_commit_id=NULL_COMMIT_HASH,
                to_commit_id=to_commit,
                processed_at=datetime.now(timezone.utc).isoformat(),
            )

        return PipelineSummary(
            mode=PipelineMode.FULL_REBUILD,
            total_files=len(records),
            processed=processed,
            skipped=skipped,
            errors=errors,
            from_commit_id=NULL_COMMIT_HASH,
            to_commit_id=to_commit,
        )

    def run_convert_only(
        self,
        source_type: SourceType | None = None,
    ) -> PipelineSummary:
        """コンバートのみ再実行する.

        converted_store をクリアし、source_store 全ファイルを
        コンバーターで再処理する。インデクサーは実行しない。
        """
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
        processed = 0
        skipped = 0
        errors: list[str] = []

        for record in records:
            try:
                self._converter.convert(
                    record.file_path,
                    self._source_store.root_dir,
                    self._converted_store_dir,
                )
                processed += 1
            except Exception:
                logger.exception(
                    "コンバート再実行中にエラー: %s", record.file_path,
                )
                errors.append(record.file_path)
                skipped += 1

        return PipelineSummary(
            mode=PipelineMode.CONVERT_ONLY,
            total_files=len(records),
            processed=processed,
            skipped=skipped,
            errors=errors,
        )

    def run_index_only(
        self,
        source_type: SourceType | None = None,
    ) -> PipelineSummary:
        """インデックスのみ再構築する.

        ChromaDB + BM25 をクリアし、
        converted_store 全ファイルからインデックスを再構築する。
        """
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
        processed = 0
        skipped = 0
        errors: list[str] = []

        for record in records:
            try:
                converted_path = self._converted_store_dir / record.file_path
                if not converted_path.exists():
                    logger.warning(
                        "converted_store にファイルがありません: %s",
                        converted_path,
                    )
                    skipped += 1
                    continue

                metadata = self._build_metadata_from_record(record)
                self._indexer.add(record.source_id, converted_path, metadata)
                processed += 1
            except Exception:
                logger.exception(
                    "インデックス再構築中にエラー: %s", record.file_path,
                )
                errors.append(record.file_path)
                skipped += 1

        return PipelineSummary(
            mode=PipelineMode.INDEX_ONLY,
            total_files=len(records),
            processed=processed,
            skipped=skipped,
            errors=errors,
        )

    # --- 変更ファイルの特定 ---

    def _scan_all_as_added(self) -> list[ChangeEntry]:
        """全追跡ファイルを「追加」として返す."""
        all_files = self._git.list_all_files()
        entries: list[ChangeEntry] = []
        for f in all_files:
            if f == ".gitignore":
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
            if file_path == ".gitignore":
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

    # --- 変更処理 ---

    def _process_changes(
        self,
        changes: list[ChangeEntry],
        mode: PipelineMode,
        from_commit_id: str,
        to_commit_id: str,
    ) -> PipelineSummary:
        """変更エントリを処理する."""
        processed = 0
        skipped = 0
        errors: list[str] = []

        for entry in changes:
            try:
                self._process_single_change(entry)
                processed += 1
            except Exception:
                logger.exception(
                    "パイプライン処理中にエラー: %s", entry.file_path,
                )
                errors.append(entry.file_path)
                skipped += 1

        return PipelineSummary(
            mode=mode,
            total_files=len(changes),
            processed=processed,
            skipped=skipped,
            errors=errors,
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
        # deleted ステータスのファイルはインデックスに追加しない
        # （register_source が status=active に上書きする前にチェック）
        source_id = self._resolve_source_id(entry.file_path)
        existing = self.db.get_source(source_id)
        skip_index = existing is not None and existing.status == "deleted"

        self._register_in_db(entry.file_path)
        converted_path = self._converter.convert(
            entry.file_path,
            self._source_store.root_dir,
            self._converted_store_dir,
        )

        if skip_index:
            return
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
            try:
                self.db.update_source(
                    old_source_id,
                    file_path=entry.file_path,
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

        source_id = _resolve_source_id_from_meta(
            source_type, file_path, meta_dict,
        )
        title = _resolve_title_from_meta(
            source_type, file_path, meta_dict,
        )

        existing = self.db.get_source(source_id)
        if existing:
            created_at = existing.created_at
        elif meta_dict and "collected_at" in meta_dict:
            created_at = str(meta_dict["collected_at"])
        else:
            created_at = now

        self.db.register_source(
            source_id=source_id,
            source_type=source_type,
            file_path=file_path,
            title=title,
            content_hash=content_hash,
            file_size=len(data),
            created_at=created_at,
            updated_at=now,
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
        return _resolve_source_id_from_meta(source_type, file_path, meta_dict)

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

        source_id = _resolve_source_id_from_meta(
            source_type, file_path, meta_dict,
        )
        title = _resolve_title_from_meta(
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
        created_at = record.created_at

        meta_dict = self._read_meta_dict(file_path) or {}
        collected_at = str(meta_dict.get("collected_at", created_at))

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


def _resolve_source_id_from_meta(
    source_type: SourceType,
    rel_path: str,
    meta: dict[str, str] | None,
) -> str:
    """source_id を決定する.

    .meta に source_id があればそれを使用し、
    なければ相対パスをフォールバックとして使用する。
    SourceStore._resolve_source_id と同一ロジック。
    """
    if meta and "source_id" in meta:
        return str(meta["source_id"])
    return rel_path


def _resolve_title_from_meta(
    source_type: SourceType,
    rel_path: str,
    meta: dict[str, str] | None,
) -> str:
    """タイトルを決定する."""
    if meta and "title" in meta:
        return str(meta["title"])
    return Path(rel_path).stem

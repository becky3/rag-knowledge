"""パイプライン制御 — 変更ハンドラ Port + Adapter.

仕様: docs/specs/pipeline-controller.md

ChangeEntry の status に応じた処理（追加・変更・削除・リネーム・meta_only）を
抽象化する。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from rag.pipeline.models import ChangeEntry, ChangeStatus
from rag.store.meta import meta_path_for, read_meta
from rag.store.models import SourceStatus
from rag.store.resolve import resolve_published_at
from rag.store.source_store import NO_META_TYPES, detect_source_type

if TYPE_CHECKING:
    from rag.pipeline.metadata_builder import RealMetadataBuilder
    from rag.pipeline.protocols import ConverterProtocol, IndexerProtocol
    from rag.store.metadata_db import MetadataDB
    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)


class ChangeHandler(Protocol):
    """変更ハンドラ Port.

    ChangeEntry 1 件分の処理（converter / indexer / metadata.db への反映）を
    抽象化する。
    """

    async def process_change(self, entry: ChangeEntry) -> None:
        """1 ファイルの変更を処理する."""
        ...


class RealChangeHandler:
    """ChangeHandler の本番実装."""

    def __init__(
        self,
        source_store: SourceStore,
        converted_store_dir: Path,
        converter: ConverterProtocol,
        indexer: IndexerProtocol,
        metadata_builder: RealMetadataBuilder,
        db: MetadataDB,
    ) -> None:
        self._source_store = source_store
        self._converted_store_dir = converted_store_dir
        self._converter = converter
        self._indexer = indexer
        self._metadata_builder = metadata_builder
        self._db = db
        self._dispatch: dict[ChangeStatus, Callable[[ChangeEntry], Awaitable[None]]] = {
            ChangeStatus.ADDED: self._handle_added,
            ChangeStatus.MODIFIED: self._handle_modified,
            ChangeStatus.DELETED: self._handle_deleted,
            ChangeStatus.RENAMED: self._handle_renamed,
            ChangeStatus.META_ONLY: self._handle_meta_only,
        }

    async def process_change(self, entry: ChangeEntry) -> None:
        """1 ファイルの変更を処理する."""
        await self._dispatch[entry.status](entry)

    async def _handle_added(self, entry: ChangeEntry) -> None:
        """追加ファイルを処理する."""
        source_id = self._metadata_builder.resolve_source_id(entry.file_path)
        existing = self._db.get_source(source_id)
        if existing is not None and existing.status is SourceStatus.DELETED:
            return

        self._metadata_builder.register_in_db(entry.file_path)
        converted_path = await asyncio.to_thread(
            self._converter.convert,
            entry.file_path,
            self._source_store.root_dir,
            self._converted_store_dir,
        )
        metadata = self._metadata_builder.build_metadata(entry.file_path)
        await self._indexer.add(source_id, converted_path, metadata)

    async def _handle_modified(self, entry: ChangeEntry) -> None:
        """変更ファイルを処理する."""
        self._metadata_builder.update_in_db(entry.file_path)
        converted_path = await asyncio.to_thread(
            self._converter.convert,
            entry.file_path,
            self._source_store.root_dir,
            self._converted_store_dir,
        )
        source_id = self._metadata_builder.resolve_source_id(entry.file_path)
        metadata = self._metadata_builder.build_metadata(entry.file_path)
        await self._indexer.update(source_id, converted_path, metadata)

    async def _handle_deleted(self, entry: ChangeEntry) -> None:
        """削除ファイルを処理する."""
        source_id = self._metadata_builder.resolve_source_id(entry.file_path)
        self._converter.delete(entry.file_path, self._converted_store_dir)
        await self._indexer.delete(source_id)
        try:
            self._db.set_status(source_id, SourceStatus.DELETED)
        except KeyError:
            logger.warning(
                "削除対象が metadata.db に存在しません: %s", source_id,
            )

    async def _handle_renamed(self, entry: ChangeEntry) -> None:
        """リネームファイルを処理する."""
        old_source_id = self._metadata_builder.resolve_source_id(entry.old_path)

        converted_path = await asyncio.to_thread(
            self._converter.convert,
            entry.file_path,
            self._source_store.root_dir,
            self._converted_store_dir,
        )

        self._converter.delete(entry.old_path, self._converted_store_dir)

        await self._indexer.delete(old_source_id)
        new_source_id = self._metadata_builder.resolve_source_id(entry.file_path)
        metadata = self._metadata_builder.build_metadata(entry.file_path)
        await self._indexer.add(new_source_id, converted_path, metadata)

        try:
            self._db.set_status(old_source_id, SourceStatus.DELETED)
        except KeyError:
            pass
        self._metadata_builder.register_in_db(entry.file_path)

    async def _handle_meta_only(self, entry: ChangeEntry) -> None:
        """.meta のみ変更を処理する.

        title・meta JSON に加え、`.meta` に `collected_at` が含まれていれば
        `collected_at` / `published_at` も DB に反映する。
        `published_at` は resolve_published_at で source_type ごとの
        フィールド優先順位に従って導出する。
        `.meta` に `collected_at` が無い場合は kwargs から除外して既存 DB 値を温存する
        （`.meta` 手動編集等で一時的に欠落した場合に DB 上の正本を破壊しないため）。

        `NO_META_TYPES`（local 等、`.meta` を持たない source_type）では DB 更新を
        スキップし、インデクサーのメタデータ更新のみ実行する。
        """
        source_id = self._metadata_builder.resolve_source_id(entry.file_path)
        source_type = detect_source_type(entry.file_path)

        # metadata.db 更新
        if source_type not in NO_META_TYPES:
            full_path = self._source_store.root_dir / entry.file_path
            meta_file = meta_path_for(full_path)
            if meta_file.exists():
                meta_data = read_meta(full_path)
                title = str(meta_data.get("title", ""))
                if title:
                    now = datetime.now(timezone.utc).isoformat()
                    meta_json = json.dumps(
                        meta_data, ensure_ascii=False, default=str,
                    )
                    update_kwargs: dict[str, str] = {
                        "title": title,
                        "updated_at": now,
                        "meta": meta_json,
                    }
                    if "collected_at" in meta_data:
                        collected_at = str(meta_data["collected_at"])
                        update_kwargs["collected_at"] = collected_at
                        update_kwargs["published_at"] = resolve_published_at(
                            source_type, meta_data, collected_at,
                        )
                    try:
                        self._db.update_source(source_id, **update_kwargs)
                    except KeyError:
                        logger.warning(
                            "meta_only 更新対象が metadata.db にありません: %s",
                            source_id,
                        )
                        return

        # インデクサー: メタデータのみ更新
        metadata = self._metadata_builder.build_metadata(entry.file_path)
        await self._indexer.upsert_metadata(source_id, metadata)

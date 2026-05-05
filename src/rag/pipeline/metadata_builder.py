"""パイプライン制御 — メタデータ構築 Port + Adapter.

仕様: docs/specs/pipeline-controller.md

ファイルからのメタデータ構築・metadata.db への登録/更新を抽象化する。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from rag.store.meta import meta_path_for, read_meta
from rag.store.models import SourceMetadata, SourceRecord
from rag.store.resolve import resolve_published_at, resolve_title
from rag.store.source_store import NO_META_TYPES, detect_source_type

if TYPE_CHECKING:
    from rag.store.metadata_db import MetadataDB
    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)


class MetadataBuilder(Protocol):
    """メタデータ構築 Port."""

    def register_in_db(self, file_path: str) -> None:
        """ファイルを metadata.db に登録する."""
        ...

    def update_in_db(self, file_path: str) -> None:
        """ファイルの metadata.db を更新する."""
        ...

    def resolve_source_id(self, file_path: str) -> str:
        """file_path から source_id を解決する."""
        ...

    def build_metadata(self, file_path: str) -> SourceMetadata:
        """ファイルから SourceMetadata を構築する."""
        ...

    def build_metadata_from_record(
        self,
        record: SourceRecord,
    ) -> SourceMetadata:
        """SourceRecord から SourceMetadata を構築する."""
        ...


class RealMetadataBuilder:
    """MetadataBuilder の本番実装."""

    def __init__(
        self,
        source_store: SourceStore,
        db: MetadataDB,
    ) -> None:
        self._source_store = source_store
        self._db = db

    def register_in_db(self, file_path: str) -> None:
        """ファイルを metadata.db に登録する."""
        full_path = self._source_store.root_dir / file_path
        data = full_path.read_bytes()
        content_hash = hashlib.sha256(data).hexdigest()
        now = datetime.now(timezone.utc).isoformat()

        source_type = detect_source_type(file_path)
        meta_dict = self._read_meta_dict(file_path)

        source_id = file_path
        title = resolve_title(
            source_type, file_path, meta_dict,
        )

        existing = self._db.get_source(source_id)
        if existing:
            collected_at = existing.collected_at
        elif meta_dict and "collected_at" in meta_dict:
            collected_at = str(meta_dict["collected_at"])
        else:
            collected_at = now

        published_at = resolve_published_at(
            source_type, meta_dict, collected_at,
        )

        meta_json = (
            json.dumps(meta_dict, ensure_ascii=False, default=str)
            if meta_dict
            else "{}"
        )

        self._db.register_source(
            source_id=source_id,
            source_type=source_type,
            title=title,
            content_hash=content_hash,
            file_size=len(data),
            collected_at=collected_at,
            updated_at=now,
            published_at=published_at,
            meta=meta_json,
        )

    def update_in_db(self, file_path: str) -> None:
        """ファイルの metadata.db を更新する."""
        full_path = self._source_store.root_dir / file_path
        data = full_path.read_bytes()
        content_hash = hashlib.sha256(data).hexdigest()
        now = datetime.now(timezone.utc).isoformat()

        source_id = self.resolve_source_id(file_path)
        try:
            self._db.update_source(
                source_id,
                content_hash=content_hash,
                file_size=len(data),
                updated_at=now,
            )
        except KeyError:
            self.register_in_db(file_path)

    def resolve_source_id(self, file_path: str) -> str:
        """file_path から source_id を解決する.

        source_id = file_path（source_store 内の相対パス）。
        """
        return file_path

    def _read_meta_dict(self, file_path: str) -> dict[str, Any] | None:
        """ファイルの .meta を読み込む."""
        source_type = detect_source_type(file_path)
        if source_type in NO_META_TYPES:
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

    def build_metadata(self, file_path: str) -> SourceMetadata:
        """ファイルから SourceMetadata を構築する."""
        source_type = detect_source_type(file_path)
        meta_dict = self._read_meta_dict(file_path) or {}

        source_id = file_path
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

    def build_metadata_from_record(
        self,
        record: SourceRecord,
    ) -> SourceMetadata:
        """SourceRecord から SourceMetadata を構築する."""
        source_id = record.source_id
        source_type = record.source_type
        title = record.title
        collected_at_db = record.collected_at

        meta_dict = self._read_meta_dict(source_id) or {}
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

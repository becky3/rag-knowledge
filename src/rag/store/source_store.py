"""SourceStore — source_store の統合管理.

仕様: docs/specs/source-store.md

ファイル配置、.meta 読み書き、metadata.db 操作、URL↔パス変換を統合する。
git 操作はパイプライン制御層の責務であり、このモジュールでは行わない。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rag.infrastructure.file_lock import INGEST_LOCK_FILENAME, REBUILD_LOCK_FILENAME
from rag.store.meta import meta_path_for, read_meta, write_meta
from rag.store.metadata_db import MetadataDB
from rag.store.models import (
    FileData,
    SourceMetadata,
    SourceRecord,
    SourceType,
)
from rag.store.path_converter import url_to_path
from rag.store.resolve import resolve_published_at, resolve_source_id, resolve_title

logger = logging.getLogger(__name__)

# .meta を持たない媒体
_NO_META_TYPES: frozenset[SourceType] = frozenset({"local"})


class SourceStore:
    """source_store の統合管理クラス."""

    def __init__(self, root_dir: Path) -> None:
        """初期化.

        Args:
            root_dir: source_store のルートディレクトリパス
        """
        self._root = root_dir
        self._db = MetadataDB(root_dir / "metadata.db")

    @property
    def root_dir(self) -> Path:
        """source_store のルートディレクトリ."""
        return self._root

    @property
    def db(self) -> MetadataDB:
        """metadata.db への直接アクセス."""
        return self._db

    def initialize(self) -> None:
        """source_store を初期化する.

        ルートディレクトリと metadata.db スキーマを作成する。
        """
        self._root.mkdir(parents=True, exist_ok=True)
        self._db.initialize()

    def close(self) -> None:
        """リソースを解放する."""
        self._db.close()

    # --- ファイル配置 ---

    def place_file(
        self,
        *,
        source_type: SourceType,
        data: bytes,
        rel_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> Path:
        """ファイルを source_store に配置する.

        Args:
            source_type: 媒体種別
            data: ファイルデータ
            rel_path: source_store 内の相対パス
            metadata: .meta に書き込むメタデータ（local 以外で必須）

        Returns:
            配置先のフルパス

        Raises:
            ValueError: rel_path が不正（絶対パス、パストラバーサル等）、
                        または非 local 媒体で metadata が未指定の場合
        """
        # パストラバーサル防止
        self._validate_rel_path(rel_path)

        # source_type と rel_path プレフィックスの整合性チェック
        expected_prefix = f"{source_type}/"
        if not rel_path.startswith(expected_prefix):
            msg = (
                f"source_type '{source_type}' と rel_path '{rel_path}' の"
                f"プレフィックスが一致しません（期待: '{expected_prefix}'）"
            )
            raise ValueError(msg)

        # 非 local 媒体は metadata 必須
        if source_type not in _NO_META_TYPES and metadata is None:
            msg = f"metadata は {source_type} 媒体で必須です (rel_path={rel_path})"
            raise ValueError(msg)

        dest = self._root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

        # .meta 生成（local 以外）
        if source_type not in _NO_META_TYPES and metadata is not None:
            write_meta(dest, metadata)

        # metadata.db 登録
        content_hash = hashlib.sha256(data).hexdigest()
        now = datetime.now(timezone.utc).isoformat()

        source_id = self._resolve_source_id(source_type, rel_path, metadata)
        title = self._resolve_title(source_type, rel_path, metadata)

        # collected_at: 既存レコード > metadata['collected_at'] > now の優先順
        existing = self._db.get_source(source_id)
        if existing:
            collected_at = existing.collected_at
        elif metadata and "collected_at" in metadata:
            collected_at = str(metadata["collected_at"])
        else:
            collected_at = now

        published_at = resolve_published_at(
            source_type, metadata, collected_at,
        )

        self._db.register_source(
            source_id=source_id,
            source_type=source_type,
            file_path=rel_path,
            title=title,
            content_hash=content_hash,
            file_size=len(data),
            collected_at=collected_at,
            updated_at=now,
            published_at=published_at,
        )

        return dest

    def place_file_from_url(
        self,
        *,
        url: str,
        data: bytes,
        metadata: dict[str, Any],
        extension: str = "",
    ) -> Path:
        """URL ベースでファイルを配置する（web 媒体用）.

        Args:
            url: 元の URL
            data: ファイルデータ
            metadata: .meta に書き込むメタデータ
            extension: ファイル拡張子（例: ``.html``）

        Returns:
            配置先のフルパス
        """
        rel_path = url_to_path(url)
        if extension and not rel_path.endswith(extension):
            rel_path += extension
        return self.place_file(
            source_type="web",
            data=data,
            rel_path=rel_path,
            metadata=metadata,
        )

    # --- ファイル取得 ---

    def _build_metadata(self, record: SourceRecord) -> SourceMetadata:
        """SourceRecord と .meta ファイルから SourceMetadata を構築する.

        .meta の collected_at を優先し、なければ DB の collected_at を使用する。
        共通フィールド（source_id, source_type, title）を除いた残りを extra に格納する。

        Args:
            record: DB レコード

        Returns:
            SourceMetadata
        """
        extra: dict[str, Any] = {}
        collected_at = record.collected_at
        if record.source_type not in _NO_META_TYPES:
            file_path = self._root / record.file_path
            meta_file = meta_path_for(file_path)
            if meta_file.exists():
                meta_data = read_meta(file_path)
                collected_at = str(meta_data.pop("collected_at", collected_at))
                for key in ("source_id", "source_type", "title"):
                    meta_data.pop(key, None)
                extra = meta_data

        return SourceMetadata(
            source_id=record.source_id,
            source_type=record.source_type,
            title=record.title,
            collected_at=collected_at,
            extra=extra,
        )

    def get_file(self, source_id: str) -> FileData | None:
        """source_id でファイルを取得する.

        論理削除済みのファイルも取得可能。

        Args:
            source_id: ソース識別子

        Returns:
            ファイルデータとメタデータ。存在しない場合は None。
        """
        record = self._db.get_source(source_id)
        if record is None:
            return None

        file_path = self._root / record.file_path
        if not file_path.exists():
            logger.warning("DB にレコードがあるがファイルが見つかりません: %s", file_path)
            return None

        content = file_path.read_bytes()

        return FileData(
            content=content,
            file_path=record.file_path,
            metadata=self._build_metadata(record),
        )

    def get_metadata(
        self,
        source_id: str,
        *,
        record: SourceRecord | None = None,
    ) -> SourceMetadata | None:
        """source_id でメタデータのみ取得する（ファイル内容は読まない）.

        Args:
            source_id: ソース識別子
            record: 既に取得済みの SourceRecord（省略時は DB から取得）

        Returns:
            SourceMetadata。存在しない場合は None。
        """
        if record is None:
            record = self._db.get_source(source_id)
        elif record.source_id != source_id:
            raise ValueError(
                f"source_id mismatch: {source_id!r} != {record.source_id!r}"
            )
        if record is None:
            return None

        return self._build_metadata(record)

    # --- ファイル一覧 ---

    def list_files(
        self,
        *,
        source_type: SourceType | None = None,
    ) -> list[Path]:
        """source_store 内のファイルを列挙する.

        .meta ファイル、metadata.db、.git 配下、.gitignore、ロックファイルは除外する。

        Args:
            source_type: 指定時はそのディレクトリのみ

        Returns:
            ファイルパスのリスト（source_store ルートからの相対パス）
        """
        if source_type:
            search_dir = self._root / source_type
        else:
            search_dir = self._root

        if not search_dir.exists():
            return []

        import os

        result: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(search_dir):
            # .git ディレクトリを走査段階で除外（性能最適化）
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fname in filenames:
                full = Path(dirpath) / fname
                rel = full.relative_to(self._root)
                rel_str = rel.as_posix()
                # 除外: .meta, metadata.db 関連, .gitignore, .lock
                if rel_str.endswith(".meta"):
                    continue
                if rel_str == "metadata.db" or rel_str.startswith("metadata.db"):
                    continue
                if fname == ".gitignore":
                    continue
                if fname in (INGEST_LOCK_FILENAME, REBUILD_LOCK_FILENAME):
                    continue
                result.append(rel)

        return sorted(result)

    # --- 削除 ---

    def remove_file(self, source_id: str) -> None:
        """ソースファイルと .meta サイドカーをディスクから削除する.

        metadata.db の更新は行わない（パイプライン制御が git diff 経由で処理する）。
        呼び出し後に controller.ingest_and_index() を実行すること。

        Args:
            source_id: 削除対象のソース識別子

        Raises:
            KeyError: source_id が metadata.db に存在しない場合
        """
        record = self._db.get_source(source_id)
        if record is None:
            msg = f"source_id が存在しません: {source_id}"
            raise KeyError(msg)

        self._validate_rel_path(record.file_path)
        file_path = self._root / record.file_path
        if file_path.exists():
            file_path.unlink()

        meta_file = meta_path_for(file_path)
        if meta_file.exists():
            meta_file.unlink()

    def soft_delete(self, source_id: str) -> None:
        """ソースを論理削除する.

        Raises:
            KeyError: source_id が存在しない場合
        """
        self._db.set_status(source_id, "deleted")

    def restore(self, source_id: str) -> None:
        """論理削除を解除する.

        Raises:
            KeyError: source_id が存在しない場合
        """
        self._db.set_status(source_id, "active")

    # --- DB 再構築 ---

    def rebuild_db(self) -> int:
        """source_store のファイルと .meta から metadata.db を再構築する.

        Returns:
            登録されたソース数
        """
        self._db.delete_all_sources()

        files = self.list_files()
        count = 0
        now = datetime.now(timezone.utc).isoformat()

        for rel_path in files:
            full_path = self._root / rel_path
            rel_str = rel_path.as_posix()
            data = full_path.read_bytes()
            content_hash = hashlib.sha256(data).hexdigest()

            source_type = self._detect_source_type(rel_str)
            meta_dict: dict[str, Any] = {}

            if source_type not in _NO_META_TYPES:
                meta_file = meta_path_for(full_path)
                if meta_file.exists():
                    meta_dict = read_meta(full_path)
                else:
                    logger.warning(
                        ".meta ファイルが欠落しています: %s", full_path
                    )

            source_id = self._resolve_source_id(source_type, rel_str, meta_dict or None)
            title = self._resolve_title(source_type, rel_str, meta_dict or None)
            collected_at = meta_dict.get("collected_at", now)
            published_at = resolve_published_at(
                source_type, meta_dict or None, collected_at,
            )

            self._db.register_source(
                source_id=source_id,
                source_type=source_type,
                file_path=rel_str,
                title=title,
                content_hash=content_hash,
                file_size=len(data),
                collected_at=collected_at,
                updated_at=now,
                published_at=published_at,
            )
            count += 1

        logger.info("metadata.db 再構築完了: %d 件", count)
        return count

    # --- 内部ユーティリティ ---

    def _validate_rel_path(self, rel_path: str) -> None:
        """相対パスの安全性を検証する.

        Raises:
            ValueError: 絶対パス、パストラバーサル、source_store 外への脱出の場合
        """
        # バックスラッシュを拒否（Windows パストラバーサル防止）
        if "\\" in rel_path:
            msg = f"バックスラッシュは許可されていません: {rel_path}"
            raise ValueError(msg)

        from pathlib import PurePosixPath

        pure = PurePosixPath(rel_path)
        if pure.is_absolute():
            msg = f"絶対パスは許可されていません: {rel_path}"
            raise ValueError(msg)
        if ".." in pure.parts:
            msg = f"パストラバーサルは許可されていません: {rel_path}"
            raise ValueError(msg)
        # resolve 後に root_dir 配下に収まることを確認
        resolved = (self._root / rel_path).resolve()
        root_resolved = self._root.resolve()
        if not resolved.is_relative_to(root_resolved):
            msg = f"source_store 外へのパスは許可されていません: {rel_path}"
            raise ValueError(msg)

    @staticmethod
    def _detect_source_type(rel_path: str) -> SourceType:
        """相対パスから source_type を判定する."""
        if rel_path.startswith("web/"):
            return "web"
        if rel_path.startswith("bluesky/"):
            return "bluesky"
        if rel_path.startswith("zenn/"):
            return "zenn"
        if rel_path.startswith("youtube/"):
            return "youtube"
        if rel_path.startswith("aozora/"):
            return "aozora"
        if rel_path.startswith("journal/"):
            return "journal"
        return "local"

    @staticmethod
    def _resolve_source_id(
        source_type: SourceType,
        rel_path: str,
        metadata: dict[str, Any] | None,
    ) -> str:
        """source_id を決定する."""
        return resolve_source_id(source_type, rel_path, metadata)

    @staticmethod
    def _resolve_title(
        source_type: SourceType,
        rel_path: str,
        metadata: dict[str, Any] | None,
    ) -> str:
        """タイトルを決定する."""
        return resolve_title(source_type, rel_path, metadata)

    # --- コンテキストマネージャ ---

    def __enter__(self) -> SourceStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

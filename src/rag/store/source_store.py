"""SourceStore — source_store の統合管理.

仕様: docs/specs/source-store.md

ファイル配置、.meta 読み書き、metadata.db 操作、URL↔パス変換を統合する。
git 操作はパイプライン制御層の責務であり、このモジュールでは行わない。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pathspec

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
from rag.store.resolve import resolve_published_at, resolve_title

logger = logging.getLogger(__name__)

# .meta を持たない媒体（source_store が SSoT）
NO_META_TYPES: frozenset[SourceType] = frozenset({"local"})

# --- ソース判定 ---
#
# is_source_file / resolve_attachment_parent / find_existing_parent /
# detect_source_type は source_store 層を SSoT とするソース判定 API。
# 仕様は docs/specs/source-store.md「ソース判定」セクションを参照。

# 除外パターン（gitignore 形式、pathspec で判定）
_EXCLUDE_PATTERNS: tuple[str, ...] = (
    # .meta サイドカー（任意階層）
    "*.meta",
    # SQLite 管理ファイル（ルート直下のみ、-wal / -shm も含む）
    "/metadata.db*",
    # Git・ロックファイル（ルート直下のみ）
    "/.gitignore",
    f"/{INGEST_LOCK_FILENAME}",
    f"/{REBUILD_LOCK_FILENAME}",
    ".git/",
    # 青空文庫カタログ（aozora インジェスターの内部参照ファイル、sidecar 扱い）
    "/aozora/catalog.csv",
    "/aozora/catalog.csv.meta",
    # OS 生成ファイル（任意階層）
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
)

_EXCLUDE_SPEC: pathspec.PathSpec = pathspec.PathSpec.from_lines(
    "gitignore", _EXCLUDE_PATTERNS,
)

# source_type 値の集合（_schema/enums.yml が SSoT）
_SOURCE_TYPE_VALUES: frozenset[SourceType] = frozenset(
    {"web", "bluesky", "zenn", "youtube", "aozora", "local", "journal"},
)


def detect_source_type(rel_path: str) -> SourceType:
    """相対パスから source_type を判定する.

    先頭ディレクトリが _schema/enums.yml の source_type 値に一致しない場合は
    ValueError を送出する。

    Args:
        rel_path: source_store ルート基準の相対パス

    Returns:
        SourceType

    Raises:
        ValueError: 先頭ディレクトリが既知の source_type でない場合
    """
    normalized = rel_path.replace("\\", "/")
    first = normalized.split("/", 1)[0]
    if first in _SOURCE_TYPE_VALUES:
        # Literal への narrowing
        return first  # type: ignore[return-value]
    msg = f"未知の source_type プレフィックス: {rel_path!r}"
    raise ValueError(msg)


def _resolve_bluesky_attachment_parent(rel_path: str) -> str | None:
    """BlueSky の attachment パスから親 JSON パスを導出する.

    パス構造: bluesky/{escaped_did}/{year}/{month}/media/{rkey}/{filename}
    親 JSON:  bluesky/{escaped_did}/{year}/{month}/{rkey}.json
    """
    if not rel_path.startswith("bluesky/"):
        return None
    parts = rel_path.split("/")
    try:
        media_idx = parts.index("media")
    except ValueError:
        return None
    if media_idx + 1 >= len(parts):
        return None
    rkey = parts[media_idx + 1]
    parent_parts = parts[:media_idx]
    return "/".join(parent_parts) + f"/{rkey}.json"


# source_type ごとの attachment → 親ソース resolver を静的に組み込む
# （仕様: ingesters/common.md「複合ソースの attachment 配置ルール」）。
# 実行時の動的登録は行わず、純粋関数として固定リストに保持する。
_ATTACHMENT_RESOLVERS: tuple[Callable[[str], str | None], ...] = (
    _resolve_bluesky_attachment_parent,
)


def resolve_attachment_parent(rel_path: str) -> str | None:
    """attachment パスから親ソース相対パスを計算する（純粋関数・IO なし）.

    attachment パターンに該当しない場合は None を返す。

    Args:
        rel_path: source_store ルート基準の相対パス

    Returns:
        親ソースの相対パス。attachment でない場合は None。
    """
    normalized = rel_path.replace("\\", "/")
    for resolver in _ATTACHMENT_RESOLVERS:
        parent = resolver(normalized)
        if parent is not None:
            return parent
    return None


def is_source_file(rel_path: str) -> bool:
    """相対パスが独立ソースに該当するかを判定する.

    除外対象（sidecar・ロック・OS 生成ファイル・attachment・未知の source_type
    プレフィックス等）は False、独立ソースに該当する場合は True を返す。

    invariant として、`is_source_file(rel_path) == True` のとき
    `detect_source_type(rel_path)` は必ず `SourceType` を返す（ValueError を
    送出しない）。この保証のため、先頭ディレクトリが既知の source_type で
    あることを検証する。

    Args:
        rel_path: source_store ルート基準の相対パス

    Returns:
        独立ソースなら True、除外対象なら False
    """
    normalized = rel_path.replace("\\", "/")
    if _EXCLUDE_SPEC.match_file(normalized):
        return False
    if resolve_attachment_parent(normalized) is not None:
        return False
    # invariant 担保: detect_source_type が ValueError を送出するパスを False にする
    first = normalized.split("/", 1)[0]
    if first not in _SOURCE_TYPE_VALUES:
        return False
    return True


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

        # 独立ソース以外（attachment, .DS_Store, sidecar 等）の配置を拒否。
        # 複合ソースの attachment は各媒体のインジェスターが place_file を経由せず
        # 独自のパス規則で配置する（仕様: source-store.md ストア操作セクション）。
        if not is_source_file(rel_path):
            msg = (
                f"独立ソースではないパスは place_file で配置できません: {rel_path}"
            )
            raise ValueError(msg)

        # 非 local 媒体は metadata 必須
        if source_type not in NO_META_TYPES and metadata is None:
            msg = f"metadata は {source_type} 媒体で必須です (rel_path={rel_path})"
            raise ValueError(msg)

        dest = self._root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

        # .meta 生成（local 以外）
        if source_type not in NO_META_TYPES and metadata is not None:
            write_meta(dest, metadata)

        # metadata.db 登録
        content_hash = hashlib.sha256(data).hexdigest()
        now = datetime.now(timezone.utc).isoformat()

        source_id = rel_path
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

        meta_json = (
            json.dumps(metadata, ensure_ascii=False, default=str)
            if metadata
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
        if record.source_type not in NO_META_TYPES:
            file_path = self._root / record.source_id
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

        file_path = self._root / record.source_id
        if not file_path.exists():
            logger.warning("DB にレコードがあるがファイルが見つかりません: %s", file_path)
            return None

        content = file_path.read_bytes()

        return FileData(
            content=content,
            file_path=record.source_id,
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
        """source_store 内の独立ソースを列挙する.

        `is_source_file` で独立ソース判定を行い、除外対象（sidecar、ロックファイル、
        attachment 等）はスキップする。除外対象の全リストは
        docs/specs/source-store.md「ソース判定 > 除外対象」を参照。

        Args:
            source_type: 指定時はそのディレクトリのみ

        Returns:
            独立ソースのパスリスト（source_store ルートからの相対パス）
        """
        if source_type:
            search_dir = self._root / source_type
        else:
            search_dir = self._root

        if not search_dir.exists():
            return []

        result: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(search_dir):
            # 走査段階での早期カット（性能最適化）。
            # - .git: 任意階層の git 管理ディレクトリ
            # - bluesky の media/: 複合ソースの attachment ディレクトリ
            #   （投稿数 × attachment 数分の I/O を削減。is_source_file でも
            #    最終的に除外されるが、os.walk 自体の I/O は発生するため）
            rel_dir = Path(dirpath).relative_to(self._root)
            in_bluesky_tree = rel_dir.parts[:1] == ("bluesky",)
            dirnames[:] = [
                d
                for d in dirnames
                if d != ".git" and not (in_bluesky_tree and d == "media")
            ]
            for fname in filenames:
                full = Path(dirpath) / fname
                rel = full.relative_to(self._root)
                if not is_source_file(rel.as_posix()):
                    continue
                result.append(rel)

        return sorted(result)

    def find_existing_parent(self, rel_path: str) -> str | None:
        """attachment パスから親ソース相対パスを返す（親が実在する場合のみ）.

        `resolve_attachment_parent` で導出した親ソースが source_store 上に
        実在するときのみパスを返す。親が存在しない（孤児 attachment）場合は
        None を返す。

        Args:
            rel_path: source_store ルート基準の相対パス

        Returns:
            実在する親ソースの相対パス。attachment でない・親が存在しない場合は None。
        """
        parent = resolve_attachment_parent(rel_path)
        if parent is None:
            return None
        parent_path = self._root / parent
        if not parent_path.exists():
            return None
        return parent

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

        self._validate_rel_path(record.source_id)
        file_path = self._root / record.source_id
        if file_path.exists():
            file_path.unlink()

        meta_file = meta_path_for(file_path)
        if meta_file.exists():
            meta_file.unlink()

        # BlueSky 投稿の media サブディレクトリを削除
        # JSON: bluesky/{did}/{year}/{month}/{rkey}.json
        # Media: bluesky/{did}/{year}/{month}/media/{rkey}/
        self._remove_media_dir(file_path)

    def _remove_media_dir(self, file_path: Path) -> None:
        """BlueSky 投稿の JSON に対応する media/{rkey}/ サブディレクトリを削除する."""
        if file_path.suffix != ".json":
            return
        rel = file_path.relative_to(self._root)
        if rel.parts[0] != "bluesky":
            return
        media_dir = file_path.parent / "media" / file_path.stem
        if media_dir.is_dir():
            shutil.rmtree(media_dir)

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

    def rebuild_db(self, source_type: SourceType | None = None) -> int:
        """source_store のファイルと .meta から metadata.db を再構築する.

        Args:
            source_type: 対象媒体フィルタ。指定時は対象 type のみ
                DELETE → INSERT する。None で全件再構築。

        Returns:
            登録されたソース数
        """
        if source_type is not None:
            self._db.delete_sources_by_type(source_type)
        else:
            self._db.delete_all_sources()

        files = self.list_files(source_type=source_type)
        count = 0
        now = datetime.now(timezone.utc).isoformat()

        for rel_path in files:
            full_path = self._root / rel_path
            rel_str = rel_path.as_posix()
            data = full_path.read_bytes()
            content_hash = hashlib.sha256(data).hexdigest()

            detected_type = detect_source_type(rel_str)
            meta_dict: dict[str, Any] = {}

            if detected_type not in NO_META_TYPES:
                meta_file = meta_path_for(full_path)
                if meta_file.exists():
                    meta_dict = read_meta(full_path)
                else:
                    logger.warning(
                        ".meta ファイルが欠落しています: %s", full_path
                    )

            source_id = rel_str
            title = self._resolve_title(detected_type, rel_str, meta_dict or None)
            collected_at = meta_dict.get("collected_at", now)
            published_at = resolve_published_at(
                detected_type, meta_dict or None, collected_at,
            )

            meta_json = (
                json.dumps(meta_dict, ensure_ascii=False, default=str)
                if meta_dict
                else "{}"
            )

            self._db.register_source(
                source_id=source_id,
                source_type=detected_type,
                title=title,
                content_hash=content_hash,
                file_size=len(data),
                collected_at=collected_at,
                updated_at=now,
                published_at=published_at,
                meta=meta_json,
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

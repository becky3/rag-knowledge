"""metadata.db — ソースメタデータの SQLite 索引.

仕様: docs/specs/source-store.md

sources テーブル + pipeline_history テーブルを管理する。
WAL モードで運用し、source_store のファイルと .meta から再構築可能。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from rag.store.models import (
    NULL_COMMIT_HASH,
    PipelineHistoryRecord,
    SourceRecord,
    SourceStatus,
    SourceType,
)

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS sources (
    source_id    TEXT PRIMARY KEY,
    source_type  TEXT NOT NULL,
    file_path    TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',
    content_hash TEXT NOT NULL,
    file_size    INTEGER NOT NULL,
    collected_at TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pipeline_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    from_commit_id TEXT NOT NULL,
    to_commit_id   TEXT NOT NULL,
    processed_at   TEXT NOT NULL,
    mode           TEXT NOT NULL DEFAULT 'incremental'
);
"""


class MetadataDB:
    """metadata.db の操作クラス."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    @property
    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path), check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def initialize(self) -> None:
        """スキーマを初期化する."""
        self._connection.executescript(_SCHEMA_SQL)
        self._migrate()

    def close(self) -> None:
        """接続を閉じる."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _migrate(self) -> None:
        """既存テーブルのスキーマをマイグレーションする."""
        # pipeline_history に mode 列を追加（既存レコードは incremental 扱い）
        cursor = self._connection.execute("PRAGMA table_info(pipeline_history)")
        ph_columns = {row["name"] for row in cursor.fetchall()}
        if "mode" not in ph_columns:
            self._connection.execute(
                "ALTER TABLE pipeline_history"
                " ADD COLUMN mode TEXT NOT NULL DEFAULT 'incremental'"
            )
            self._connection.commit()
            logger.info("pipeline_history に mode 列を追加しました")

        # sources に published_at 列を追加（既存レコードは collected_at で埋める）
        cursor = self._connection.execute("PRAGMA table_info(sources)")
        src_columns = {row["name"] for row in cursor.fetchall()}
        if "published_at" not in src_columns:
            self._connection.execute(
                "ALTER TABLE sources"
                " ADD COLUMN published_at TEXT NOT NULL DEFAULT ''"
            )
            self._connection.execute(
                "UPDATE sources SET published_at = collected_at"
                " WHERE published_at = ''"
            )
            self._connection.commit()
            logger.info("sources に published_at 列を追加しました")

    def __enter__(self) -> MetadataDB:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # --- sources CRUD ---

    def register_source(
        self,
        *,
        source_id: str,
        source_type: SourceType,
        file_path: str,
        title: str,
        content_hash: str,
        file_size: int,
        collected_at: str,
        updated_at: str,
        published_at: str = "",
    ) -> None:
        """ソースを登録する.

        同一 source_id が既に存在する場合は上書きする
        （再取り込み時の更新動作。deleted → active への復帰を含む）。
        異なる source_id で同一 file_path のレコードが存在する場合は
        旧レコードを削除してから登録する。

        Note:
            collected_at は新規 INSERT 時のみ使用される。既存レコードの
            更新時は元の collected_at が保持される（ON CONFLICT で更新対象外）。
            published_at も同様に INSERT 時のみ使用される。
            local 媒体の git 由来時刻への補正は、パイプライン制御層が
            update_source() で後から実施する。
        """
        # published_at 未指定時は collected_at を使用
        if not published_at:
            published_at = collected_at

        # file_path UNIQUE 競合の防止: 異なる source_id で同じ file_path を持つ旧レコードを削除
        self._connection.execute(
            "DELETE FROM sources WHERE file_path = ? AND source_id != ?",
            (file_path, source_id),
        )
        self._connection.execute(
            """\
            INSERT INTO sources
                (source_id, source_type, file_path, title, status,
                 content_hash, file_size, collected_at, updated_at, published_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                source_type = excluded.source_type,
                file_path = excluded.file_path,
                title = excluded.title,
                status = 'active',
                content_hash = excluded.content_hash,
                file_size = excluded.file_size,
                updated_at = excluded.updated_at
            """,
            (
                source_id,
                source_type,
                file_path,
                title,
                content_hash,
                file_size,
                collected_at,
                updated_at,
                published_at,
            ),
        )
        self._connection.commit()

    def update_source(self, source_id: str, **fields: Any) -> None:
        """ソースの指定フィールドを更新する.

        Args:
            source_id: 対象ソースの識別子
            **fields: 更新するフィールド名と値

        Raises:
            ValueError: 更新フィールドが空の場合
            KeyError: source_id が存在しない場合
        """
        if not fields:
            msg = "更新フィールドが指定されていません"
            raise ValueError(msg)

        allowed = {
            "source_type",
            "file_path",
            "title",
            "status",
            "content_hash",
            "file_size",
            "collected_at",
            "updated_at",
            "published_at",
        }
        invalid = set(fields.keys()) - allowed
        if invalid:
            msg = f"不正なフィールド: {invalid}"
            raise ValueError(msg)

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values())
        values.append(source_id)

        cursor = self._connection.execute(
            f"UPDATE sources SET {set_clause} WHERE source_id = ?",  # noqa: S608
            values,
        )
        if cursor.rowcount == 0:
            msg = f"source_id が見つかりません: {source_id}"
            raise KeyError(msg)
        self._connection.commit()

    def get_source(self, source_id: str) -> SourceRecord | None:
        """source_id でソースを取得する."""
        row = self._connection.execute(
            "SELECT * FROM sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_source_record(row)

    def get_source_by_path(self, file_path: str) -> SourceRecord | None:
        """file_path でソースを取得する."""
        row = self._connection.execute(
            "SELECT * FROM sources WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_source_record(row)

    def search_sources(
        self,
        *,
        source_type: SourceType | None = None,
        status: SourceStatus | None = None,
    ) -> list[SourceRecord]:
        """条件に合致するソースを検索する."""
        conditions: list[str] = []
        params: list[Any] = []

        if source_type is not None:
            conditions.append("source_type = ?")
            params.append(source_type)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)

        where = " AND ".join(conditions) if conditions else "1=1"
        rows = self._connection.execute(
            f"SELECT * FROM sources WHERE {where} ORDER BY collected_at",  # noqa: S608
            params,
        ).fetchall()
        return [_row_to_source_record(r) for r in rows]

    def set_status(self, source_id: str, status: SourceStatus) -> None:
        """ソースのステータスを変更する.

        Raises:
            KeyError: source_id が存在しない場合
        """
        cursor = self._connection.execute(
            "UPDATE sources SET status = ? WHERE source_id = ?",
            (status, source_id),
        )
        if cursor.rowcount == 0:
            msg = f"source_id が見つかりません: {source_id}"
            raise KeyError(msg)
        self._connection.commit()

    def delete_all_sources(self) -> None:
        """全ソースレコードを削除する（DB 再構築用）."""
        self._connection.execute("DELETE FROM sources")
        self._connection.commit()

    # --- pipeline_history ---

    def add_pipeline_history(
        self,
        *,
        from_commit_id: str,
        to_commit_id: str,
        processed_at: str,
        mode: str = "incremental",
    ) -> None:
        """パイプライン実行履歴を追加する."""
        self._connection.execute(
            """\
            INSERT INTO pipeline_history
                (from_commit_id, to_commit_id, processed_at, mode)
            VALUES (?, ?, ?, ?)
            """,
            (from_commit_id, to_commit_id, processed_at, mode),
        )
        self._connection.commit()

    def get_last_commit_id(self) -> str:
        """最終コミット ID を取得する.

        Returns:
            最新の to_commit_id。履歴がない場合は null commit hash。
        """
        row = self._connection.execute(
            "SELECT to_commit_id FROM pipeline_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return NULL_COMMIT_HASH
        return str(row["to_commit_id"])

    def get_pipeline_history(self) -> list[PipelineHistoryRecord]:
        """パイプライン実行履歴を取得する."""
        rows = self._connection.execute(
            "SELECT * FROM pipeline_history ORDER BY id"
        ).fetchall()
        return [
            PipelineHistoryRecord(
                id=row["id"],
                from_commit_id=row["from_commit_id"],
                to_commit_id=row["to_commit_id"],
                processed_at=row["processed_at"],
                mode=row["mode"],
            )
            for row in rows
        ]

    def needs_index_rebuild(self) -> bool:
        """前回の index/full rebuild 以降に更新があったかを判定する.

        Returns:
            True: rebuild が必要（更新あり or 履歴なし）
            False: rebuild 不要（更新なし）
        """
        # 最後の index/full rebuild を取得
        row = self._connection.execute(
            "SELECT id FROM pipeline_history"
            " WHERE mode IN ('index', 'full')"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()

        if row is None:
            # index/full rebuild の履歴なし → 必要
            return True

        last_id = row["id"]

        # それ以降のレコードがあるか確認
        newer = self._connection.execute(
            "SELECT EXISTS("
            "  SELECT 1 FROM pipeline_history WHERE id > ?"
            ") as has_newer",
            (last_id,),
        ).fetchone()

        return bool(newer["has_newer"])

    def checkpoint(self) -> None:
        """WAL をフラッシュする（バックアップ前に実行）."""
        self._connection.commit()
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def source_count(self, *, status: SourceStatus | None = None) -> int:
        """ソース件数を返す."""
        if status is not None:
            row = self._connection.execute(
                "SELECT COUNT(*) as cnt FROM sources WHERE status = ?",
                (status,),
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) as cnt FROM sources"
            ).fetchone()
        return int(row["cnt"]) if row else 0

    def list_sources(
        self,
        *,
        source_type: SourceType,
        limit: int,
        ascending: bool = False,
    ) -> list[SourceRecord]:
        """指定 source_type の active ソースを published_at でソートして取得する."""
        direction = "ASC" if ascending else "DESC"
        rows = self._connection.execute(
            "SELECT * FROM sources"
            " WHERE source_type = ? AND status = 'active'"
            f" ORDER BY published_at {direction}"
            " LIMIT ?",
            (source_type, limit),
        ).fetchall()
        return [_row_to_source_record(r) for r in rows]

    def count_sources_by_type(self, *, source_type: SourceType) -> int:
        """指定 source_type の active ソース件数を返す."""
        row = self._connection.execute(
            "SELECT COUNT(*) as cnt FROM sources"
            " WHERE source_type = ? AND status = 'active'",
            (source_type,),
        ).fetchone()
        return int(row["cnt"]) if row else 0


def _row_to_source_record(row: sqlite3.Row) -> SourceRecord:
    """sqlite3.Row を SourceRecord に変換する."""
    return SourceRecord(
        source_id=row["source_id"],
        source_type=row["source_type"],
        file_path=row["file_path"],
        title=row["title"],
        status=row["status"],
        content_hash=row["content_hash"],
        file_size=row["file_size"],
        collected_at=row["collected_at"],
        updated_at=row["updated_at"],
        published_at=row["published_at"],
    )

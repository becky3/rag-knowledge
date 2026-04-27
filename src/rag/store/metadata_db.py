"""metadata.db — ソースメタデータの SQLite 索引.

仕様: docs/specs/source-store.md

sources テーブル + pipeline_history テーブルを管理する。
WAL モードで運用し、source_store のファイルと .meta から再構築可能。
"""

from __future__ import annotations

import json
import logging
import re
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
from rag.store.path_filter import escape_like, normalize_path_prefix

logger = logging.getLogger(__name__)

# json_extract の JSON パスに埋め込むキー名の許容パターン
_VALID_FILTER_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS sources (
    source_id    TEXT PRIMARY KEY,
    source_type  TEXT NOT NULL,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',
    content_hash TEXT NOT NULL,
    file_size    INTEGER NOT NULL,
    collected_at TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT '',
    meta         TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS pipeline_history (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    from_commit_id     TEXT NOT NULL,
    to_commit_id       TEXT NOT NULL,
    processed_at       TEXT NOT NULL,
    mode               TEXT NOT NULL DEFAULT 'incremental',
    filter_source_type TEXT NOT NULL DEFAULT '',
    filter_path        TEXT NOT NULL DEFAULT ''
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
        """スキーマを初期化する.

        CREATE TABLE IF NOT EXISTS のみ実行する。
        スキーマ変更は migrate() で明示的に実行すること。
        """
        self._connection.executescript(_SCHEMA_SQL)

    def close(self) -> None:
        """接続を閉じる."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def migrate(
        self, *, source_store_dir: Path | None = None,
    ) -> list[str]:
        """既存テーブルのスキーマをマイグレーションする.

        CLI の migrate コマンドから明示的に呼び出す。
        initialize() からは呼び出されない。
        各種機能は最新スキーマを前提とし、旧スキーマへのフォールバックは行わない。

        Args:
            source_store_dir: source_store のルートディレクトリ。
                meta カラムマイグレーションで .meta ファイルを読み取るために必要。

        Returns:
            適用されたマイグレーションの説明リスト（適用なしなら空リスト）
        """
        applied: list[str] = []

        # pipeline_history に mode 列を追加（既存レコードは incremental 扱い）
        cursor = self._connection.execute("PRAGMA table_info(pipeline_history)")
        ph_columns = {row["name"] for row in cursor.fetchall()}
        if "mode" not in ph_columns:
            self._connection.execute(
                "ALTER TABLE pipeline_history"
                " ADD COLUMN mode TEXT NOT NULL DEFAULT 'incremental'"
            )
            self._connection.commit()
            applied.append("pipeline_history に mode 列を追加")
            logger.info("pipeline_history に mode 列を追加しました")

        # pipeline_history に filter_source_type / filter_path 列を追加
        # （既存レコードは「未フィルタ」扱いで空文字列がデフォルト）
        if "filter_source_type" not in ph_columns:
            self._connection.execute(
                "ALTER TABLE pipeline_history"
                " ADD COLUMN filter_source_type TEXT NOT NULL DEFAULT ''"
            )
            self._connection.commit()
            applied.append("pipeline_history に filter_source_type 列を追加")
            logger.info(
                "pipeline_history に filter_source_type 列を追加しました",
            )
        if "filter_path" not in ph_columns:
            self._connection.execute(
                "ALTER TABLE pipeline_history"
                " ADD COLUMN filter_path TEXT NOT NULL DEFAULT ''"
            )
            self._connection.commit()
            applied.append("pipeline_history に filter_path 列を追加")
            logger.info(
                "pipeline_history に filter_path 列を追加しました",
            )

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
            applied.append("sources に published_at 列を追加")
            logger.info("sources に published_at 列を追加しました")

        # source_id = file_path 統合: file_path カラムが残っている旧スキーマを移行
        if "file_path" in src_columns:
            # file_path を新 source_id として直接 INSERT（UPDATE での PK 衝突を回避）
            self._connection.executescript("""\
                DROP TABLE IF EXISTS sources_new;
                CREATE TABLE sources_new (
                    source_id    TEXT PRIMARY KEY,
                    source_type  TEXT NOT NULL,
                    title        TEXT NOT NULL,
                    status       TEXT NOT NULL DEFAULT 'active',
                    content_hash TEXT NOT NULL,
                    file_size    INTEGER NOT NULL,
                    collected_at TEXT NOT NULL,
                    updated_at   TEXT NOT NULL,
                    published_at TEXT NOT NULL DEFAULT ''
                );
                INSERT OR REPLACE INTO sources_new
                    (source_id, source_type, title, status,
                     content_hash, file_size, collected_at, updated_at, published_at)
                    SELECT file_path, source_type, title, status,
                           content_hash, file_size, collected_at, updated_at, published_at
                    FROM sources;
                DROP TABLE sources;
                ALTER TABLE sources_new RENAME TO sources;
            """)
            self._connection.commit()
            applied.append("sources の source_id を file_path ベースに移行")
            logger.info(
                "sources テーブルから file_path カラムを削除し"
                " source_id を file_path ベースに移行しました"
            )
            # 再取得（テーブル再作成後のカラム情報を反映）
            cursor = self._connection.execute("PRAGMA table_info(sources)")
            src_columns = {row["name"] for row in cursor.fetchall()}

        # sources に meta 列を追加し、.meta ファイルからデータを充填
        if "meta" not in src_columns:
            self._connection.execute(
                "ALTER TABLE sources"
                " ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'"
            )
            self._connection.commit()

            filled = self._fill_meta_from_files(source_store_dir)
            applied.append(
                f"sources に meta 列を追加（{filled} 件の .meta データを充填）"
            )
            logger.info(
                "sources に meta 列を追加しました（%d 件充填）", filled,
            )

        return applied

    def _fill_meta_from_files(
        self, source_store_dir: Path | None,
    ) -> int:
        """source_store の .meta ファイルを読み取り meta カラムに充填する.

        Args:
            source_store_dir: source_store のルートディレクトリ。
                None の場合は充填をスキップする。

        Returns:
            充填した件数
        """
        if source_store_dir is None:
            logger.warning(
                "source_store_dir が未指定のため"
                " meta カラムのデータ充填をスキップします"
            )
            return 0

        from rag.store.meta import meta_path_for, read_meta

        rows = self._connection.execute(
            "SELECT source_id FROM sources WHERE meta = '{}'"
        ).fetchall()

        filled = 0
        for row in rows:
            source_id = row["source_id"]
            file_path = source_store_dir / source_id
            meta_file = meta_path_for(file_path)
            if meta_file.exists():
                try:
                    meta_dict = read_meta(file_path)
                    meta_json = json.dumps(
                        meta_dict, ensure_ascii=False, default=str,
                    )
                    self._connection.execute(
                        "UPDATE sources SET meta = ? WHERE source_id = ?",
                        (meta_json, source_id),
                    )
                    filled += 1
                except Exception:
                    logger.warning(
                        ".meta ファイルの読み取りに失敗: %s", source_id,
                        exc_info=True,
                    )

        self._connection.commit()
        return filled

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
        title: str,
        content_hash: str,
        file_size: int,
        collected_at: str,
        updated_at: str,
        published_at: str = "",
        meta: str = "{}",
    ) -> None:
        """ソースを登録する.

        同一 source_id が既に存在する場合は上書きする
        （再取り込み時の更新動作。deleted → active への復帰を含む）。

        Note:
            source_id は source_store 内の相対パス（file_path）と同一の値。
            collected_at は新規 INSERT 時のみ使用される。既存レコードの
            更新時は元の collected_at が保持される（ON CONFLICT で更新対象外）。
            published_at も同様に INSERT 時のみ使用される。
            local 媒体の git 由来時刻への補正は、パイプライン制御層が
            update_source() で後から実施する。
            meta は .meta ファイルの内容を JSON 文字列として格納する。
        """
        # published_at 未指定時は collected_at を使用
        if not published_at:
            published_at = collected_at

        self._connection.execute(
            """\
            INSERT INTO sources
                (source_id, source_type, title, status,
                 content_hash, file_size, collected_at, updated_at,
                 published_at, meta)
            VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                source_type = excluded.source_type,
                title = excluded.title,
                status = 'active',
                content_hash = excluded.content_hash,
                file_size = excluded.file_size,
                updated_at = excluded.updated_at,
                meta = excluded.meta
            """,
            (
                source_id,
                source_type,
                title,
                content_hash,
                file_size,
                collected_at,
                updated_at,
                published_at,
                meta,
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
            "title",
            "status",
            "content_hash",
            "file_size",
            "collected_at",
            "updated_at",
            "published_at",
            "meta",
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

    def search_sources(
        self,
        *,
        source_type: SourceType | None = None,
        status: SourceStatus | None = None,
        path_prefix: str | None = None,
    ) -> list[SourceRecord]:
        """条件に合致するソースを検索する.

        Args:
            source_type: 媒体フィルタ
            status: ステータスフィルタ
            path_prefix: source_id（= source_store 内 rel_path）の prefix
                一致フィルタ。指定パス配下（再帰的）のソースのみを返す。
                LIKE のメタ文字（%, _, バックスラッシュ）は ESCAPE される
        """
        conditions: list[str] = []
        params: list[Any] = []

        if source_type is not None:
            conditions.append("source_type = ?")
            params.append(source_type)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if path_prefix is not None:
            normalized = normalize_path_prefix(path_prefix)
            escaped = escape_like(normalized)
            conditions.append(r"source_id LIKE ? ESCAPE '\'")
            params.append(f"{escaped}/%")

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

    def delete_sources_by_type(self, source_type: SourceType) -> None:
        """指定 source_type のソースレコードを削除する（DB 部分再構築用）."""
        self._connection.execute(
            "DELETE FROM sources WHERE source_type = ?",
            (source_type,),
        )
        self._connection.commit()

    def delete_sources_by_path(self, path_prefix: str) -> None:
        """指定パス配下のソースレコードを削除する（DB 部分再構築用）.

        Args:
            path_prefix: source_id の prefix。指定パス配下（再帰的）の
                レコードを削除する
        """
        normalized = normalize_path_prefix(path_prefix)
        escaped = escape_like(normalized)
        self._connection.execute(
            r"DELETE FROM sources WHERE source_id LIKE ? ESCAPE '\'",
            (f"{escaped}/%",),
        )
        self._connection.commit()

    # --- pipeline_history ---

    def add_pipeline_history(
        self,
        *,
        from_commit_id: str,
        to_commit_id: str,
        processed_at: str,
        mode: str = "incremental",
        filter_source_type: str = "",
        filter_path: str = "",
    ) -> None:
        """パイプライン実行履歴を追加する.

        Args:
            filter_source_type: source_type フィルタ付き rebuild の場合に
                媒体名を記録（filter なしは空文字列）
            filter_path: path フィルタ付き rebuild の場合にパスを記録
                （filter なしは空文字列）
        """
        self._connection.execute(
            """\
            INSERT INTO pipeline_history
                (from_commit_id, to_commit_id, processed_at, mode,
                 filter_source_type, filter_path)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                from_commit_id, to_commit_id, processed_at, mode,
                filter_source_type, filter_path,
            ),
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
                filter_source_type=row["filter_source_type"],
                filter_path=row["filter_path"],
            )
            for row in rows
        ]

    def needs_index_rebuild(self) -> bool:
        """前回の「未フィルタの index/full rebuild」以降に更新があったかを判定する.

        filter 付き rebuild（source_type / path フィルタ）は subset しか
        触っていないため、判定基準には含めない（filter 付きを「全体 rebuild
        完了」と誤認するとスキップ漏れが発生する）。

        Returns:
            True: rebuild が必要（更新あり or 全体 rebuild 履歴なし）
            False: rebuild 不要（前回の全体 rebuild 以降に更新なし）
        """
        # 最後の「未フィルタの」index/full rebuild を取得
        row = self._connection.execute(
            "SELECT id FROM pipeline_history"
            " WHERE mode IN ('index', 'full')"
            " AND filter_source_type = ''"
            " AND filter_path = ''"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()

        if row is None:
            # 全体 index/full rebuild の履歴なし → 必要
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

    @staticmethod
    def _build_meta_filter_conditions(
        filters: dict[str, str],
        conditions: list[str],
        params: list[Any],
    ) -> None:
        """filters dict から meta カラム用の WHERE 条件を構築する.

        キー名は英数字・アンダースコアのみ許可し、
        SQL インジェクションを防止する。

        Raises:
            ValueError: キー名に不正な文字が含まれる場合
        """
        for key, value in filters.items():
            if not _VALID_FILTER_KEY_RE.match(key):
                msg = (
                    f"フィルタキー名に不正な文字が含まれています: {key!r}"
                    "（英数字・アンダースコアのみ許可）"
                )
                raise ValueError(msg)
            conditions.append(f"json_extract(meta, '$.{key}') = ?")
            params.append(value)

    def list_sources(
        self,
        *,
        source_type: SourceType,
        limit: int,
        ascending: bool = False,
        filters: dict[str, str] | None = None,
    ) -> list[SourceRecord]:
        """指定 source_type の active ソースを published_at でソートして取得する.

        Args:
            source_type: ソース種別
            limit: 取得件数
            ascending: True で古い順、False で新しい順
            filters: メタデータフィルタ（key=value 形式）。
                meta JSON カラムの json_extract でフィルタする。

        Raises:
            ValueError: filters のキー名に不正な文字が含まれる場合
        """
        direction = "ASC" if ascending else "DESC"
        conditions = ["source_type = ?", "status = 'active'"]
        params: list[Any] = [source_type]

        if filters:
            self._build_meta_filter_conditions(filters, conditions, params)

        where = " AND ".join(conditions)
        params.append(limit)

        rows = self._connection.execute(
            f"SELECT * FROM sources WHERE {where}"  # noqa: S608
            f" ORDER BY published_at {direction}"
            " LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_source_record(r) for r in rows]

    def count_sources_by_type(
        self,
        *,
        source_type: SourceType,
        filters: dict[str, str] | None = None,
    ) -> int:
        """指定 source_type の active ソース件数を返す.

        Args:
            source_type: ソース種別
            filters: メタデータフィルタ（key=value 形式）。
                meta JSON カラムの json_extract でフィルタする。

        Raises:
            ValueError: filters のキー名に不正な文字が含まれる場合
        """
        conditions = ["source_type = ?", "status = 'active'"]
        params: list[Any] = [source_type]

        if filters:
            self._build_meta_filter_conditions(filters, conditions, params)

        where = " AND ".join(conditions)
        row = self._connection.execute(
            f"SELECT COUNT(*) as cnt FROM sources WHERE {where}",  # noqa: S608
            params,
        ).fetchone()
        return int(row["cnt"]) if row else 0


def _row_to_source_record(row: sqlite3.Row) -> SourceRecord:
    """sqlite3.Row を SourceRecord に変換する."""
    return SourceRecord(
        source_id=row["source_id"],
        source_type=row["source_type"],
        title=row["title"],
        status=row["status"],
        content_hash=row["content_hash"],
        file_size=row["file_size"],
        collected_at=row["collected_at"],
        updated_at=row["updated_at"],
        published_at=row["published_at"],
        meta=row["meta"],
    )

"""metadata.db — ソースメタデータの SQLite 索引.

仕様: docs/specs/source-store.md

sources テーブル + pipeline_history テーブルを管理する。
WAL モードで運用し、source_store のファイルと .meta から再構築可能。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
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

# naive datetime（TZ 情報なし）の published_at は JST として解釈する。
# YouTube の upload_date 由来など、TZ 不在の値が混在することを想定。
_JST_TZ = timezone(timedelta(hours=9))


def normalize_published_at(value: str) -> str:
    """published_at を UTC マイクロ秒 6 桁固定の ISO 8601 文字列に正規化する.

    範囲フィルタを文字列比較で実装するため、表記揺れを吸収する。
    冪等: 正規化済み出力（UTC マイクロ秒 6 桁固定）を再入力しても同じ値を返す。

    入力例 → 出力例:
        ""                                  → ""（空文字はそのまま）
        "2026-01-15T00:00:00+09:00"         → "2026-01-14T15:00:00.000000+00:00"
        "2026-01-15T00:00:00Z"              → "2026-01-15T00:00:00.000000+00:00"
        "2026-01-15T00:00:00.123Z"          → "2026-01-15T00:00:00.123000+00:00"
        "2026-01-15T00:00:00"               → "2026-01-14T15:00:00.000000+00:00"
                                              （naive は JST 解釈）
        "2026-01-14T15:00:00.000000+00:00"  → "2026-01-14T15:00:00.000000+00:00"
                                              （正規化済み入力の冪等性）

    Raises:
        ValueError: ISO 8601 として解析できない場合
    """
    if not value:
        return value
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_JST_TZ)
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


# journal の YYYYMMDD-HHMMSS プレフィックスを検出する正規表現。
_JOURNAL_ENTRY_TS_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})")


def _fix_journal_collected_at(
    source_store_dir: Path,
) -> tuple[int, int, int]:
    """journal の .meta の collected_at を JST→UTC に補正する.

    旧 ``migrate-journal`` CLI が ``datetime.strptime`` の naive 値に
    ``replace(tzinfo=timezone.utc)`` でタグ付けしていたため、JST 値が
    ``+00:00`` で保存されていた行を再計算する。

    検出条件（誤検出を排除するため厳密にマッチさせる）:

    - ファイル名プレフィックス ``YYYYMMDD-HHMMSS`` と ``collected_at`` の
      先頭 19 文字（``YYYY-MM-DDTHH:MM:SS``）が完全一致
    - ``collected_at`` がマイクロ秒部を含まない（小数点なし）
    - ``collected_at`` の末尾が ``+00:00``

    ``add-journal`` 経路の正規データは ``datetime.now(timezone.utc).isoformat()``
    でマイクロ秒を含むため、この条件で誤検出しない。

    Args:
        source_store_dir: source_store のルートディレクトリ

    Returns:
        (補正件数, 既に正しい/対象外でスキップした件数, エラー件数)
    """
    from rag.store.meta import read_meta, write_meta

    journal_dir = source_store_dir / "journal"
    if not journal_dir.is_dir():
        return (0, 0, 0)

    fixed = 0
    skipped = 0
    errors = 0
    # rglob の結果順序は OS 依存のためソートしてログ・テストの再現性を担保する
    for meta_path in sorted(journal_dir.rglob("*.md.meta")):
        # `.md.meta` → `.md` の対応データファイルパスを得る
        data_path = meta_path.with_name(meta_path.name[: -len(".meta")])
        entry_id = data_path.stem
        m = _JOURNAL_ENTRY_TS_RE.match(entry_id)
        if m is None:
            skipped += 1
            continue
        # 1 件の .meta 異常で全件停止しないよう broad に捕捉してログ警告 + 件数集計に倒す。
        # 想定例外は OSError / yaml.YAMLError / ValueError 系だが、想定外の例外も
        # 握り潰さずスタックトレースを残すため exc_info=True とする。
        try:
            meta_dict = read_meta(data_path)
        except Exception:  # noqa: BLE001
            logger.warning(
                ".meta ファイルの読み取りに失敗: %s", meta_path,
                exc_info=True,
            )
            errors += 1
            continue
        raw = meta_dict.get("collected_at")
        if not isinstance(raw, str):
            skipped += 1
            continue
        # 検出条件: 19 文字数値一致 + マイクロ秒なし + +00:00 終端
        y, mo, d, h, mi, s = m.groups()
        expected_prefix = f"{y}-{mo}-{d}T{h}:{mi}:{s}"
        if (
            len(raw) != 25
            or not raw.startswith(expected_prefix)
            or "." in raw
            or not raw.endswith("+00:00")
        ):
            skipped += 1
            continue
        # JST→UTC 変換: 数値部を JST として解釈し UTC に変換
        try:
            dt = datetime.strptime(  # noqa: DTZ007
                expected_prefix, "%Y-%m-%dT%H:%M:%S",
            ).replace(tzinfo=_JST_TZ).astimezone(UTC)
        except ValueError:
            logger.warning(
                "collected_at のパースに失敗: %s value=%r", meta_path, raw,
            )
            errors += 1
            continue
        meta_dict["collected_at"] = dt.isoformat()
        # write_meta の失敗も読み取り側と同様に broad 捕捉（理由は read_meta 側を参照）。
        try:
            write_meta(data_path, meta_dict)
        except Exception:  # noqa: BLE001
            logger.warning(
                ".meta ファイルの書き込みに失敗: %s", meta_path,
                exc_info=True,
            )
            errors += 1
            continue
        fixed += 1
    if fixed > 0:
        logger.info(
            "journal の collected_at を JST→UTC 補正: 補正=%d, スキップ=%d, エラー=%d",
            fixed, skipped, errors,
        )
    return (fixed, skipped, errors)


# SQL 内の status リテラルは SourceStatus Enum の value から導出する。
# 値定義の SSoT は _schema/enums.yml の source_status カテゴリ。
_STATUS_ACTIVE = SourceStatus.ACTIVE.value

_SCHEMA_SQL = f"""\
CREATE TABLE IF NOT EXISTS sources (
    source_id    TEXT PRIMARY KEY,
    source_type  TEXT NOT NULL,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT '{_STATUS_ACTIVE}',
    content_hash TEXT NOT NULL,
    file_size    INTEGER NOT NULL,
    collected_at TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT '',
    meta         TEXT NOT NULL DEFAULT '{{}}'
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
        """source_store のデータ補正を実行する.

        CLI の migrate コマンドから明示的に呼び出す。initialize() からは呼び出されない。
        DB スキーマは ``_SCHEMA_SQL`` で常に最新形に初期化される前提で、
        旧スキーマからの移行コードは保持しない。

        本実装は journal の `.meta` の `collected_at` を JST→UTC に補正する。
        旧 `migrate-journal` CLI が naive datetime を UTC タグ付けで保存していた
        バグデータを再計算する。DB 更新は本関数では行わず、後続の
        `rag rebuild --mode incremental` が `.meta` 変更を検知して反映する。

        Args:
            source_store_dir: source_store のルートディレクトリ。
                journal の .meta を補正するために必要。None の場合は補正をスキップする。

        Returns:
            適用された処理の説明リスト（適用なしなら空リスト）
        """
        applied: list[str] = []

        if source_store_dir is None:
            logger.warning(
                "source_store_dir が未指定のため migrate の処理をスキップします"
            )
            return applied

        fixed, skipped, errors = _fix_journal_collected_at(source_store_dir)
        if fixed > 0:
            applied.append(
                f"journal の .meta の collected_at を JST→UTC に補正"
                f"（{fixed} 件補正, {skipped} 件スキップ, {errors} 件エラー）"
            )
        elif errors > 0:
            applied.append(
                f"journal の .meta 補正中にエラー {errors} 件"
                "（詳細はログ参照、対象ファイルは元値のまま）"
            )

        return applied

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

        # published_at を UTC マイクロ秒 6 桁固定 ISO 8601 に正規化する。
        # 範囲フィルタ（list_sources_by_date_range）の文字列比較で表記揺れを吸収するため。
        # 不正値は migrate 側と挙動を揃えてログ警告のみ（元値そのまま保持）とし、
        # rebuild ループの途中で 1 件の不正値が残り全ソースの処理を止めないようにする。
        try:
            published_at = normalize_published_at(published_at)
        except ValueError as e:
            logger.warning(
                "register_source: published_at の正規化に失敗（元値のまま保存）"
                ": source_id=%s, value=%r, error=%s",
                source_id, published_at, e,
            )

        self._connection.execute(
            """\
            INSERT INTO sources
                (source_id, source_type, title, status,
                 content_hash, file_size, collected_at, updated_at,
                 published_at, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                source_type = excluded.source_type,
                title = excluded.title,
                status = excluded.status,
                content_hash = excluded.content_hash,
                file_size = excluded.file_size,
                updated_at = excluded.updated_at,
                meta = excluded.meta
            """,
            (
                source_id,
                source_type,
                title,
                _STATUS_ACTIVE,
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

        # published_at は書き込み入口で UTC マイクロ秒 6 桁固定 ISO 8601 に正規化する
        # （範囲フィルタの文字列比較で表記揺れを吸収するため）。不正値は migrate 側と
        # 挙動を揃えてログ警告のみ（元値そのまま保持）とし、書き込み入口の fail-fast
        # で連鎖停止しないようにする。
        if "published_at" in fields:
            try:
                fields["published_at"] = normalize_published_at(fields["published_at"])
            except ValueError as e:
                logger.warning(
                    "update_source: published_at の正規化に失敗（元値のまま保存）"
                    ": source_id=%s, value=%r, error=%s",
                    source_id, fields["published_at"], e,
                )

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
            params.append(status.value)
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
            (status.value, source_id),
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
                (status.value,),
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
        conditions = ["source_type = ?", "status = ?"]
        params: list[Any] = [source_type, _STATUS_ACTIVE]

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
        conditions = ["source_type = ?", "status = ?"]
        params: list[Any] = [source_type, _STATUS_ACTIVE]

        if filters:
            self._build_meta_filter_conditions(filters, conditions, params)

        where = " AND ".join(conditions)
        row = self._connection.execute(
            f"SELECT COUNT(*) as cnt FROM sources WHERE {where}",  # noqa: S608
            params,
        ).fetchone()
        return int(row["cnt"]) if row else 0

    def _build_date_range_conditions(
        self,
        *,
        date_from_iso: str,
        date_to_iso: str,
        source_type: SourceType | None,
        filters: dict[str, str] | None,
    ) -> tuple[list[str], list[Any]]:
        """published_at 範囲 + 任意の source_type + filters の WHERE 条件を構築する."""
        conditions = ["status = ?", "published_at >= ?", "published_at <= ?"]
        params: list[Any] = [_STATUS_ACTIVE, date_from_iso, date_to_iso]
        if source_type is not None:
            conditions.append("source_type = ?")
            params.append(source_type)
        if filters:
            self._build_meta_filter_conditions(filters, conditions, params)
        return conditions, params

    def list_sources_by_date_range(
        self,
        *,
        date_from_iso: str,
        date_to_iso: str,
        source_type: SourceType | None = None,
        limit: int,
        ascending: bool = False,
        filters: dict[str, str] | None = None,
    ) -> list[SourceRecord]:
        """published_at 範囲で active ソースを取得する.

        Args:
            date_from_iso: 範囲下端の ISO 8601 文字列（inclusive）
            date_to_iso: 範囲上端の ISO 8601 文字列（inclusive）
            source_type: ソース種別（None で全種別横断）
            limit: 取得件数
            ascending: True で古い順、False で新しい順
            filters: メタデータフィルタ（key=value 形式）

        Raises:
            ValueError: filters のキー名に不正な文字が含まれる場合
        """
        direction = "ASC" if ascending else "DESC"
        conditions, params = self._build_date_range_conditions(
            date_from_iso=date_from_iso,
            date_to_iso=date_to_iso,
            source_type=source_type,
            filters=filters,
        )
        where = " AND ".join(conditions)
        params.append(limit)
        rows = self._connection.execute(
            f"SELECT * FROM sources WHERE {where}"  # noqa: S608
            f" ORDER BY published_at {direction}"
            " LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_source_record(r) for r in rows]

    def count_sources_by_date_range(
        self,
        *,
        date_from_iso: str,
        date_to_iso: str,
        source_type: SourceType | None = None,
        filters: dict[str, str] | None = None,
    ) -> int:
        """published_at 範囲の active ソース件数を返す.

        Raises:
            ValueError: filters のキー名に不正な文字が含まれる場合
        """
        conditions, params = self._build_date_range_conditions(
            date_from_iso=date_from_iso,
            date_to_iso=date_to_iso,
            source_type=source_type,
            filters=filters,
        )
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
        status=SourceStatus(row["status"]),
        content_hash=row["content_hash"],
        file_size=row["file_size"],
        collected_at=row["collected_at"],
        updated_at=row["updated_at"],
        published_at=row["published_at"],
        meta=row["meta"],
    )

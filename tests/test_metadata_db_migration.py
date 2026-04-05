"""metadata.db マイグレーションのテスト.

テスト方針:
- migrate() を明示的に呼び出してスキーマ変更を適用
- initialize() ではマイグレーションが走らないことを確認
- published_at 列の追加、file_path → source_id 移行を検証
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rag.store.metadata_db import MetadataDB


class TestMigrateExplicit:
    """明示的な migrate() 呼び出しのテスト."""

    def test_initialize_does_not_run_migration(self, tmp_path: Path) -> None:
        """initialize() だけではマイグレーションが走らない."""
        db_path = tmp_path / "metadata.db"

        # published_at なし + file_path ありの旧スキーマで DB を手動作成
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""\
            CREATE TABLE sources (
                source_id    TEXT PRIMARY KEY,
                source_type  TEXT NOT NULL,
                file_path    TEXT NOT NULL UNIQUE,
                title        TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'active',
                content_hash TEXT NOT NULL,
                file_size    INTEGER NOT NULL,
                collected_at TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );
            CREATE TABLE pipeline_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                from_commit_id TEXT NOT NULL,
                to_commit_id   TEXT NOT NULL,
                processed_at   TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("s1", "web", "web/s1.html", "Title", "active",
             "hash", 100, "2026-01-15T10:00:00Z", "2026-01-15T10:00:00Z"),
        )
        conn.commit()
        conn.close()

        db = MetadataDB(db_path)
        db.initialize()

        # file_path カラムがまだ残っている（マイグレーション未適用）
        cursor = db._connection.execute("PRAGMA table_info(sources)")
        columns = {row["name"] for row in cursor.fetchall()}
        assert "file_path" in columns

        # source_id は旧値のまま（SQL で直接確認。get_source は旧スキーマでは動作しない）
        row = db._connection.execute(
            "SELECT source_id FROM sources WHERE source_id = ?", ("s1",)
        ).fetchone()
        assert row is not None

        db.close()

    def test_migrate_applies_all_migrations(self, tmp_path: Path) -> None:
        """migrate() で全マイグレーションが適用される."""
        db_path = tmp_path / "metadata.db"

        # published_at なし + mode なし + file_path ありの最古スキーマ
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""\
            CREATE TABLE sources (
                source_id    TEXT PRIMARY KEY,
                source_type  TEXT NOT NULL,
                file_path    TEXT NOT NULL UNIQUE,
                title        TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'active',
                content_hash TEXT NOT NULL,
                file_size    INTEGER NOT NULL,
                collected_at TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );
            CREATE TABLE pipeline_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                from_commit_id TEXT NOT NULL,
                to_commit_id   TEXT NOT NULL,
                processed_at   TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("s1", "web", "web/s1.html", "Title", "active",
             "hash", 100, "2026-01-15T10:00:00Z", "2026-01-15T10:00:00Z"),
        )
        conn.commit()
        conn.close()

        db = MetadataDB(db_path)
        db.initialize()
        applied = db.migrate()

        # 3件のマイグレーションが適用される
        assert len(applied) == 3

        # file_path カラムが削除されている
        cursor = db._connection.execute("PRAGMA table_info(sources)")
        columns = {row["name"] for row in cursor.fetchall()}
        assert "file_path" not in columns
        assert "published_at" in columns

        # source_id が file_path ベースに移行されている
        record = db.get_source("web/s1.html")
        assert record is not None
        assert record.published_at == "2026-01-15T10:00:00Z"

        # pipeline_history に mode 列が追加されている
        cursor = db._connection.execute("PRAGMA table_info(pipeline_history)")
        ph_columns = {row["name"] for row in cursor.fetchall()}
        assert "mode" in ph_columns

        db.close()

    def test_migrate_is_idempotent(self, tmp_path: Path) -> None:
        """migrate() を2回呼んでも2回目は何もしない."""
        db_path = tmp_path / "metadata.db"

        conn = sqlite3.connect(str(db_path))
        conn.executescript("""\
            CREATE TABLE sources (
                source_id    TEXT PRIMARY KEY,
                source_type  TEXT NOT NULL,
                file_path    TEXT NOT NULL UNIQUE,
                title        TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'active',
                content_hash TEXT NOT NULL,
                file_size    INTEGER NOT NULL,
                collected_at TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );
            CREATE TABLE pipeline_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                from_commit_id TEXT NOT NULL,
                to_commit_id   TEXT NOT NULL,
                processed_at   TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("s1", "web", "web/s1.html", "Title", "active",
             "hash", 100, "2026-01-15T10:00:00Z", "2026-01-15T10:00:00Z"),
        )
        conn.commit()
        conn.close()

        db = MetadataDB(db_path)
        db.initialize()

        first = db.migrate()
        assert len(first) == 3

        second = db.migrate()
        assert len(second) == 0

        db.close()

    def test_migrate_on_latest_schema(self, tmp_path: Path) -> None:
        """最新スキーマの DB に対して migrate() は何もしない."""
        db_path = tmp_path / "metadata.db"

        db = MetadataDB(db_path)
        db.initialize()

        applied = db.migrate()
        assert len(applied) == 0

        db.close()

    def test_migrate_multiple_records(self, tmp_path: Path) -> None:
        """複数レコードの published_at が collected_at で埋まる."""
        db_path = tmp_path / "metadata.db"

        conn = sqlite3.connect(str(db_path))
        conn.executescript("""\
            CREATE TABLE sources (
                source_id    TEXT PRIMARY KEY,
                source_type  TEXT NOT NULL,
                file_path    TEXT NOT NULL UNIQUE,
                title        TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'active',
                content_hash TEXT NOT NULL,
                file_size    INTEGER NOT NULL,
                collected_at TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );
            CREATE TABLE pipeline_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                from_commit_id TEXT NOT NULL,
                to_commit_id   TEXT NOT NULL,
                processed_at   TEXT NOT NULL,
                mode           TEXT NOT NULL DEFAULT 'incremental'
            );
        """)
        for i in range(3):
            conn.execute(
                "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"s{i}", "web", f"web/s{i}.html", f"Title {i}", "active",
                 "hash", 100, f"2026-01-{i+1:02d}T00:00:00Z",
                 f"2026-01-{i+1:02d}T00:00:00Z"),
            )
        conn.commit()
        conn.close()

        db = MetadataDB(db_path)
        db.initialize()
        applied = db.migrate()

        # published_at + file_path 移行の 2 件
        assert len(applied) == 2

        for i in range(3):
            record = db.get_source(f"web/s{i}.html")
            assert record is not None
            assert record.published_at == f"2026-01-{i+1:02d}T00:00:00Z"

        db.close()

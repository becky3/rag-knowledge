"""metadata.db マイグレーションのテスト.

テスト方針:
- migrate() を明示的に呼び出してスキーマ変更を適用
- initialize() ではマイグレーションが走らないことを確認
- published_at 列の追加、file_path → source_id 移行、meta 列追加を検証
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

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

        # 7件のマイグレーションが適用される
        # (mode + filter_source_type + filter_path + published_at 列追加 + file_path 移行 + meta
        #  + published_at の UTC 正規化)
        assert len(applied) == 7

        # file_path カラムが削除されている
        cursor = db._connection.execute("PRAGMA table_info(sources)")
        columns = {row["name"] for row in cursor.fetchall()}
        assert "file_path" not in columns
        assert "published_at" in columns

        # source_id が file_path ベースに移行されている
        record = db.get_source("web/s1.html")
        assert record is not None
        # published_at が UTC マイクロ秒 6 桁固定 ISO 8601 に正規化されている
        assert record.published_at == "2026-01-15T10:00:00.000000+00:00"

        # pipeline_history に mode / filter_source_type / filter_path 列が追加されている
        cursor = db._connection.execute("PRAGMA table_info(pipeline_history)")
        ph_columns = {row["name"] for row in cursor.fetchall()}
        assert "mode" in ph_columns
        assert "filter_source_type" in ph_columns
        assert "filter_path" in ph_columns

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
        assert len(first) == 7

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

        # filter_source_type + filter_path + published_at 列追加 + file_path 移行 + meta
        # + published_at UTC 正規化 = 6 件
        assert len(applied) == 6

        for i in range(3):
            record = db.get_source(f"web/s{i}.html")
            assert record is not None
            # published_at が UTC マイクロ秒 6 桁固定 ISO 8601 に正規化されている
            assert record.published_at == f"2026-01-{i+1:02d}T00:00:00.000000+00:00"

        db.close()


class TestMetaMigration:
    """meta カラムマイグレーションのテスト."""

    def _create_pre_meta_db(self, db_path: Path) -> None:
        """meta カラムなしの DB を作成する."""
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""\
            CREATE TABLE sources (
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
            CREATE TABLE pipeline_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                from_commit_id TEXT NOT NULL,
                to_commit_id   TEXT NOT NULL,
                processed_at   TEXT NOT NULL,
                mode           TEXT NOT NULL DEFAULT 'incremental'
            );
        """)
        conn.close()

    def test_migrate_adds_meta_column(self, tmp_path: Path) -> None:
        """migrate() で meta カラムが追加される."""
        db_path = tmp_path / "metadata.db"
        self._create_pre_meta_db(db_path)

        db = MetadataDB(db_path)
        db.initialize()
        applied = db.migrate()

        # filter_source_type + filter_path + meta の 3 件
        assert len(applied) == 3
        assert any("meta" in m for m in applied)

        cursor = db._connection.execute("PRAGMA table_info(sources)")
        columns = {row["name"] for row in cursor.fetchall()}
        assert "meta" in columns

        db.close()

    def test_migrate_fills_meta_from_files(self, tmp_path: Path) -> None:
        """migrate() が .meta ファイルからデータを充填する."""
        source_store = tmp_path / "source_store"
        source_store.mkdir()
        db_path = source_store / "metadata.db"
        self._create_pre_meta_db(db_path)

        # ソースレコードを事前登録
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("journal/repo-a/entry.md", "journal", "Entry", "active",
             "hash", 100, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
             "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        # .meta ファイルを作成
        journal_dir = source_store / "journal" / "repo-a"
        journal_dir.mkdir(parents=True)
        (journal_dir / "entry.md").write_text("content")
        meta_content = {
            "title": "Entry",
            "repository": "repo-a",
            "collected_at": "2026-01-01T00:00:00Z",
        }
        with open(journal_dir / "entry.md.meta", "w", encoding="utf-8") as f:
            yaml.safe_dump(meta_content, f)

        db = MetadataDB(db_path)
        db.initialize()
        applied = db.migrate(source_store_dir=source_store)

        # filter_source_type + filter_path + meta + published_at UTC 正規化 の 4 件
        assert len(applied) == 4
        assert any("1 件" in m for m in applied)

        record = db.get_source("journal/repo-a/entry.md")
        assert record is not None
        meta = json.loads(record.meta)
        assert meta["repository"] == "repo-a"

        db.close()

    def test_migrate_without_source_store_dir(self, tmp_path: Path) -> None:
        """source_store_dir なしでもカラム追加は行われる（データ充填はスキップ）."""
        db_path = tmp_path / "metadata.db"
        self._create_pre_meta_db(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("local/test.md", "local", "Test", "active",
             "hash", 10, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
             "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        db = MetadataDB(db_path)
        db.initialize()
        applied = db.migrate()

        # filter_source_type + filter_path + meta + published_at UTC 正規化 の 4 件
        assert len(applied) == 4
        assert any("0 件" in m for m in applied)

        record = db.get_source("local/test.md")
        assert record is not None
        assert record.meta == "{}"

        db.close()

"""metadata.db マイグレーションのテスト.

テスト方針:
- published_at 列がない既存 DB に対するマイグレーション
- マイグレーション後に published_at が collected_at で埋められること
- file_path カラムがある旧スキーマの source_id 移行
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rag.store.metadata_db import MetadataDB


class TestPublishedAtMigration:
    """published_at 列のマイグレーションテスト."""

    def test_migration_adds_published_at_column(self, tmp_path: Path) -> None:
        """published_at 列がない DB で initialize すると列が追加される."""
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
                processed_at   TEXT NOT NULL,
                mode           TEXT NOT NULL DEFAULT 'incremental'
            );
        """)
        conn.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("s1", "web", "web/s1.html", "Title", "active",
             "hash", 100, "2026-01-15T10:00:00Z", "2026-01-15T10:00:00Z"),
        )
        conn.commit()
        conn.close()

        # MetadataDB で初期化（マイグレーション実行: published_at 追加 + source_id 移行）
        db = MetadataDB(db_path)
        db.initialize()

        # マイグレーションで source_id が file_path の値に更新される
        record = db.get_source("web/s1.html")
        assert record is not None
        assert record.published_at == "2026-01-15T10:00:00Z"
        db.close()

    def test_migration_fills_published_at_from_collected_at(self, tmp_path: Path) -> None:
        """マイグレーションで複数レコードの published_at が collected_at で埋まる."""
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

        # マイグレーションで source_id が file_path の値に更新される
        for i in range(3):
            record = db.get_source(f"web/s{i}.html")
            assert record is not None
            assert record.published_at == f"2026-01-{i+1:02d}T00:00:00Z"

        db.close()

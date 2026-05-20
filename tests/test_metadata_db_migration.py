"""metadata.db マイグレーションのテスト.

テスト方針:
- migrate() の新ステップ「journal の .meta の collected_at JST→UTC 補正」を検証
- 旧バグデータ（マイクロ秒なし + 数値完全一致 + +00:00 終端）のみ補正対象
- マイクロ秒あり・数値不一致・他 TZ オフセット・非 journal は補正されない
- 冪等: 再実行で 0 件
- source_store_dir 未指定時は処理スキップ（applied 空）

前提:
- ヘルパー `_write_meta` は `yaml.safe_dump` で書き込み、`read_meta` 側の
  `_normalize_timestamps` 経由で PyYAML が timestamp タグへ自動変換した datetime/date
  を ISO 8601 文字列に再正規化する経路に依存する。テストはこの再正規化を前提に
  collected_at の値を文字列として検証する。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from rag.store.metadata_db import MetadataDB


def _write_meta(meta_path: Path, content: dict) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        yaml.safe_dump(content, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _read_collected_at(meta_path: Path) -> str:
    return yaml.safe_load(meta_path.read_text(encoding="utf-8"))["collected_at"]


class TestMigrateNoOp:
    """無処理ケースのテスト."""

    def test_migrate_without_source_store_dir_returns_empty(
        self, tmp_path: Path,
    ) -> None:
        """source_store_dir なしの場合は applied 空."""
        db = MetadataDB(tmp_path / "metadata.db")
        db.initialize()
        applied = db.migrate()
        assert applied == []
        db.close()

    def test_migrate_with_no_journal_dir_returns_empty(
        self, tmp_path: Path,
    ) -> None:
        """journal ディレクトリが存在しない場合は applied 空."""
        source_store = tmp_path / "source_store"
        source_store.mkdir()
        db = MetadataDB(source_store / "metadata.db")
        db.initialize()
        applied = db.migrate(source_store_dir=source_store)
        assert applied == []
        db.close()

    def test_migrate_on_clean_journal_returns_empty(
        self, tmp_path: Path,
    ) -> None:
        """補正対象がない（全て正常データ）の場合は applied 空."""
        source_store = tmp_path / "source_store"
        journal = source_store / "journal" / "repo-a"
        journal.mkdir(parents=True)
        (journal / "20260101-000000-entry.md").write_text("content")
        # マイクロ秒あり = 正規データ → 補正対象外
        _write_meta(
            journal / "20260101-000000-entry.md.meta",
            {
                "title": "Entry",
                "collected_at": "2026-01-01T00:00:00.123456+00:00",
                "repository": "repo-a",
            },
        )

        db = MetadataDB(source_store / "metadata.db")
        db.initialize()
        applied = db.migrate(source_store_dir=source_store)
        assert applied == []
        db.close()


class TestMigrateJSTtoUTC:
    """JST→UTC 補正の本処理テスト."""

    def test_buggy_meta_is_fixed(self, tmp_path: Path) -> None:
        """検出条件マッチの .meta は JST→UTC に補正される."""
        source_store = tmp_path / "source_store"
        journal = source_store / "journal" / "repo-a"
        journal.mkdir(parents=True)
        (journal / "20260209-160629-retro.md").write_text("content")
        # JST 16:06:29 を UTC タグで保存（旧バグ）
        _write_meta(
            journal / "20260209-160629-retro.md.meta",
            {
                "title": "retro",
                "collected_at": "2026-02-09T16:06:29+00:00",
                "repository": "repo-a",
            },
        )

        db = MetadataDB(source_store / "metadata.db")
        db.initialize()
        applied = db.migrate(source_store_dir=source_store)

        assert len(applied) == 1
        assert "1 件補正" in applied[0]

        # JST 16:06:29 → UTC 07:06:29
        fixed = _read_collected_at(journal / "20260209-160629-retro.md.meta")
        assert fixed == "2026-02-09T07:06:29+00:00"
        db.close()

    def test_with_microseconds_is_not_fixed(self, tmp_path: Path) -> None:
        """マイクロ秒ありは正規データ扱いで補正されない."""
        source_store = tmp_path / "source_store"
        journal = source_store / "journal" / "repo-a"
        journal.mkdir(parents=True)
        (journal / "20260513-202146-entry.md").write_text("content")
        _write_meta(
            journal / "20260513-202146-entry.md.meta",
            {
                "title": "entry",
                "collected_at": "2026-05-13T11:22:58.466305+00:00",
                "repository": "repo-a",
            },
        )

        db = MetadataDB(source_store / "metadata.db")
        db.initialize()
        applied = db.migrate(source_store_dir=source_store)

        assert applied == []
        original = _read_collected_at(journal / "20260513-202146-entry.md.meta")
        assert original == "2026-05-13T11:22:58.466305+00:00"
        db.close()

    def test_numeric_mismatch_is_not_fixed(self, tmp_path: Path) -> None:
        """ファイル名と collected_at の数値が一致しないものは補正されない."""
        source_store = tmp_path / "source_store"
        journal = source_store / "journal" / "repo-a"
        journal.mkdir(parents=True)
        (journal / "20260219-115900session2-late-import.md").write_text("x")
        _write_meta(
            journal / "20260219-115900session2-late-import.md.meta",
            {
                "title": "late",
                "collected_at": "2026-04-15T22:36:42+00:00",
                "repository": "repo-a",
            },
        )

        db = MetadataDB(source_store / "metadata.db")
        db.initialize()
        applied = db.migrate(source_store_dir=source_store)

        assert applied == []
        db.close()

    def test_non_utc_offset_is_not_fixed(self, tmp_path: Path) -> None:
        """+00:00 以外の TZ オフセットは補正されない."""
        source_store = tmp_path / "source_store"
        journal = source_store / "journal" / "repo-a"
        journal.mkdir(parents=True)
        (journal / "20260209-160629-entry.md").write_text("x")
        # 数値一致だが既に JST タグなら補正されない（既に正しい）
        _write_meta(
            journal / "20260209-160629-entry.md.meta",
            {
                "title": "e",
                "collected_at": "2026-02-09T16:06:29+09:00",
                "repository": "repo-a",
            },
        )

        db = MetadataDB(source_store / "metadata.db")
        db.initialize()
        applied = db.migrate(source_store_dir=source_store)

        assert applied == []
        db.close()

    def test_migrate_is_idempotent(self, tmp_path: Path) -> None:
        """補正後に再実行すると 0 件（冪等性）."""
        source_store = tmp_path / "source_store"
        journal = source_store / "journal" / "repo-a"
        journal.mkdir(parents=True)
        (journal / "20260101-000000-entry.md").write_text("x")
        _write_meta(
            journal / "20260101-000000-entry.md.meta",
            {
                "title": "e",
                "collected_at": "2026-01-01T00:00:00+00:00",
                "repository": "repo-a",
            },
        )

        db = MetadataDB(source_store / "metadata.db")
        db.initialize()

        first = db.migrate(source_store_dir=source_store)
        assert len(first) == 1

        second = db.migrate(source_store_dir=source_store)
        assert second == []
        db.close()

    def test_non_journal_dir_is_not_touched(self, tmp_path: Path) -> None:
        """非 journal ディレクトリの .meta は走査対象外で補正されない."""
        source_store = tmp_path / "source_store"
        # journal ディレクトリには補正対象を 1 件配置
        journal = source_store / "journal" / "repo-a"
        journal.mkdir(parents=True)
        (journal / "20260101-000000-entry.md").write_text("x")
        _write_meta(
            journal / "20260101-000000-entry.md.meta",
            {
                "title": "e",
                "collected_at": "2026-01-01T00:00:00+00:00",
                "repository": "repo-a",
            },
        )
        # web ディレクトリには検出条件にマッチする「ように見える」値を配置（補正されないこと）
        web = source_store / "web" / "example.com"
        web.mkdir(parents=True)
        (web / "20260101-000000.html").write_text("<html/>")
        _write_meta(
            web / "20260101-000000.html.meta",
            {
                "title": "page",
                "collected_at": "2026-01-01T00:00:00+00:00",
            },
        )

        db = MetadataDB(source_store / "metadata.db")
        db.initialize()
        applied = db.migrate(source_store_dir=source_store)

        assert len(applied) == 1
        assert "1 件補正" in applied[0]
        # journal 配下は補正される
        assert _read_collected_at(
            journal / "20260101-000000-entry.md.meta"
        ) == "2025-12-31T15:00:00+00:00"
        # web 配下は不変
        assert _read_collected_at(
            web / "20260101-000000.html.meta"
        ) == "2026-01-01T00:00:00+00:00"
        db.close()

    def test_invalid_date_value_counts_as_error(self, tmp_path: Path) -> None:
        """ファイル名は数値形式だが datetime としてパース不能な値はエラーに計上される."""
        source_store = tmp_path / "source_store"
        journal = source_store / "journal" / "repo-a"
        journal.mkdir(parents=True)
        # 2026 年は閏年ではないが 02-29 のファイル名で配置
        (journal / "20260229-000000-leap.md").write_text("x")
        _write_meta(
            journal / "20260229-000000-leap.md.meta",
            {
                "title": "leap",
                # 検出条件を満たすが datetime としては不正な値（2026-02-29 は存在しない）
                "collected_at": "2026-02-29T00:00:00+00:00",
                "repository": "repo-a",
            },
        )

        db = MetadataDB(source_store / "metadata.db")
        db.initialize()
        applied = db.migrate(source_store_dir=source_store)

        # 補正 0 + エラー 1 → applied にエラーメッセージのみ
        assert len(applied) == 1
        assert "エラー 1 件" in applied[0]
        # 元値は保持される
        assert _read_collected_at(
            journal / "20260229-000000-leap.md.meta"
        ) == "2026-02-29T00:00:00+00:00"
        db.close()

    def test_multiple_files_mixed(self, tmp_path: Path) -> None:
        """複数ファイルの混在（補正対象 + 対象外）が正しく分類される."""
        source_store = tmp_path / "source_store"
        journal = source_store / "journal" / "repo-a"
        journal.mkdir(parents=True)

        # 補正対象 2 件
        for stem, ca in [
            ("20260101-000000-a", "2026-01-01T00:00:00+00:00"),
            ("20260202-120000-b", "2026-02-02T12:00:00+00:00"),
        ]:
            (journal / f"{stem}.md").write_text("x")
            _write_meta(
                journal / f"{stem}.md.meta",
                {"title": stem, "collected_at": ca, "repository": "repo-a"},
            )
        # 対象外 1 件（マイクロ秒あり）
        (journal / "20260303-090000-c.md").write_text("x")
        _write_meta(
            journal / "20260303-090000-c.md.meta",
            {
                "title": "c",
                "collected_at": "2026-03-03T00:00:00.000000+00:00",
                "repository": "repo-a",
            },
        )

        db = MetadataDB(source_store / "metadata.db")
        db.initialize()
        applied = db.migrate(source_store_dir=source_store)

        assert len(applied) == 1
        assert "2 件補正" in applied[0]

        # 補正対象は JST→UTC 変換
        assert _read_collected_at(
            journal / "20260101-000000-a.md.meta"
        ) == "2025-12-31T15:00:00+00:00"
        assert _read_collected_at(
            journal / "20260202-120000-b.md.meta"
        ) == "2026-02-02T03:00:00+00:00"
        # 対象外は不変
        assert _read_collected_at(
            journal / "20260303-090000-c.md.meta"
        ) == "2026-03-03T00:00:00.000000+00:00"
        db.close()

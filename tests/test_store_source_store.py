"""source_store 統合テスト.

仕様: docs/specs/source-store.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.store.source_store import SourceStore


@pytest.fixture()
def store(tmp_path: Path) -> SourceStore:
    """テスト用 SourceStore インスタンス."""
    ss = SourceStore(tmp_path / "source_store")
    ss.initialize()
    return ss


class TestPlaceFile:
    """ファイル配置のテスト."""

    def test_place_local_file(self, store: SourceStore) -> None:
        data = b"# Test Document\n\nHello World"
        dest = store.place_file(
            source_type="local",
            data=data,
            rel_path="local/test.md",
        )

        assert dest.exists()
        assert dest.read_bytes() == data
        # .meta は local では生成されない
        assert not dest.with_name(dest.name + ".meta").exists()

        # DB に登録されている
        record = store.db.get_source("local/test.md")
        assert record is not None
        assert record.source_type == "local"
        assert record.title == "test"
        assert record.status == "active"
        assert record.file_size == len(data)

    def test_place_web_file_with_meta(self, store: SourceStore) -> None:
        data = b"<html><body>Test</body></html>"
        metadata = {
            "source_id": "https://example.com/page",
            "source_type": "web",
            "title": "Test Page",
            "collected_at": "2026-01-15T10:30:00+09:00",
            "url": "https://example.com/page",
        }

        dest = store.place_file(
            source_type="web",
            data=data,
            rel_path="web/https/example.com/page.html",
            metadata=metadata,
        )

        assert dest.exists()
        # .meta が生成される
        meta_file = dest.with_name(dest.name + ".meta")
        assert meta_file.exists()

        record = store.db.get_source("https://example.com/page")
        assert record is not None
        assert record.title == "Test Page"

    def test_place_rejects_absolute_path(self, store: SourceStore) -> None:
        """絶対パスは拒否される."""
        with pytest.raises(ValueError, match="絶対パス"):
            store.place_file(
                source_type="local", data=b"x", rel_path="/etc/passwd"
            )

    def test_place_rejects_path_traversal(self, store: SourceStore) -> None:
        """パストラバーサルは拒否される."""
        with pytest.raises(ValueError, match="パストラバーサル"):
            store.place_file(
                source_type="local", data=b"x", rel_path="local/../../etc/passwd"
            )

    def test_place_rejects_backslash(self, store: SourceStore) -> None:
        """バックスラッシュは拒否される."""
        with pytest.raises(ValueError, match="バックスラッシュ"):
            store.place_file(
                source_type="local", data=b"x", rel_path="local\\..\\..\\etc\\passwd"
            )

    def test_place_web_without_metadata_raises(self, store: SourceStore) -> None:
        """非 local 媒体で metadata=None は ValueError."""
        with pytest.raises(ValueError, match="metadata は web 媒体で必須"):
            store.place_file(
                source_type="web",
                data=b"<html></html>",
                rel_path="web/https/example.com/page.html",
            )

    def test_place_overwrites_existing(self, store: SourceStore) -> None:
        """既存ファイルを上書きする."""
        store.place_file(
            source_type="local",
            data=b"old content",
            rel_path="local/test.md",
        )
        store.place_file(
            source_type="local",
            data=b"new content",
            rel_path="local/test.md",
        )

        dest = store.root_dir / "local" / "test.md"
        assert dest.read_bytes() == b"new content"


class TestPlaceFileFromUrl:
    """URL ベースのファイル配置テスト."""

    def test_place_from_url(self, store: SourceStore) -> None:
        data = b"<html>page</html>"
        metadata = {
            "source_id": "https://example.com/docs/guide",
            "source_type": "web",
            "title": "Guide",
            "collected_at": "2026-01-15T10:30:00+09:00",
            "url": "https://example.com/docs/guide",
        }

        dest = store.place_file_from_url(
            url="https://example.com/docs/guide",
            data=data,
            metadata=metadata,
            extension=".html",
        )

        assert dest.exists()
        assert "web" in dest.as_posix()
        assert "https" in dest.as_posix()
        assert "example.com" in dest.as_posix()


class TestGetFile:
    """ファイル取得テスト."""

    def test_get_existing_file(self, store: SourceStore) -> None:
        metadata = {
            "source_id": "https://example.com/page",
            "source_type": "web",
            "title": "Test Page",
            "collected_at": "2026-01-15T10:30:00+09:00",
            "url": "https://example.com/page",
        }
        store.place_file(
            source_type="web",
            data=b"<html>test</html>",
            rel_path="web/https/example.com/page.html",
            metadata=metadata,
        )

        result = store.get_file("https://example.com/page")
        assert result is not None
        assert result.content == b"<html>test</html>"
        assert result.metadata.source_id == "https://example.com/page"
        assert result.metadata.title == "Test Page"
        assert result.metadata.extra.get("url") == "https://example.com/page"

    def test_get_local_file(self, store: SourceStore) -> None:
        store.place_file(
            source_type="local",
            data=b"hello",
            rel_path="local/test.md",
        )

        result = store.get_file("local/test.md")
        assert result is not None
        assert result.content == b"hello"
        assert result.metadata.source_type == "local"

    def test_get_nonexistent(self, store: SourceStore) -> None:
        assert store.get_file("nonexistent") is None

    def test_get_file_with_missing_physical_file(self, store: SourceStore) -> None:
        """DB レコードがあるが物理ファイルが削除されている場合 None を返す."""
        store.place_file(source_type="local", data=b"data", rel_path="local/gone.md")
        # 物理ファイルを手動削除
        (store.root_dir / "local" / "gone.md").unlink()

        result = store.get_file("local/gone.md")
        assert result is None

    def test_get_deleted_file(self, store: SourceStore) -> None:
        """論理削除済みでも取得可能."""
        store.place_file(
            source_type="local",
            data=b"content",
            rel_path="local/test.md",
        )
        store.soft_delete("local/test.md")

        result = store.get_file("local/test.md")
        assert result is not None
        assert result.content == b"content"


class TestListFiles:
    """ファイル一覧テスト."""

    def test_list_all(self, store: SourceStore) -> None:
        store.place_file(source_type="local", data=b"a", rel_path="local/a.md")
        store.place_file(source_type="local", data=b"b", rel_path="local/b.md")
        store.place_file(
            source_type="web",
            data=b"c",
            rel_path="web/https/example.com/c.html",
            metadata={
                "source_id": "https://example.com/c",
                "source_type": "web",
                "title": "C",
                "collected_at": "2026-01-01T00:00:00Z",
            },
        )

        files = store.list_files()
        paths = [f.as_posix() for f in files]

        assert "local/a.md" in paths
        assert "local/b.md" in paths
        assert "web/https/example.com/c.html" in paths
        # .meta は除外
        assert not any(p.endswith(".meta") for p in paths)

    def test_list_by_type(self, store: SourceStore) -> None:
        store.place_file(source_type="local", data=b"a", rel_path="local/a.md")
        store.place_file(
            source_type="web",
            data=b"b",
            rel_path="web/https/example.com/b.html",
            metadata={
                "source_id": "https://example.com/b",
                "source_type": "web",
                "title": "B",
                "collected_at": "2026-01-01T00:00:00Z",
            },
        )

        local_files = store.list_files(source_type="local")
        assert len(local_files) == 1
        assert local_files[0].as_posix() == "local/a.md"

    def test_list_empty(self, store: SourceStore) -> None:
        assert store.list_files() == []

    def test_excludes_metadata_db(self, store: SourceStore) -> None:
        """metadata.db はリストに含まれない."""
        store.place_file(source_type="local", data=b"a", rel_path="local/a.md")
        files = store.list_files()
        paths = [f.as_posix() for f in files]
        assert "metadata.db" not in paths

    def test_excludes_git_directory(self, store: SourceStore) -> None:
        """.git/ 配下のファイルはリストに含まれない."""
        git_dir = store.root_dir / ".git" / "objects"
        git_dir.mkdir(parents=True)
        (git_dir / "dummy").write_bytes(b"git object")
        store.place_file(source_type="local", data=b"a", rel_path="local/a.md")

        files = store.list_files()
        paths = [f.as_posix() for f in files]
        assert not any(p.startswith(".git") for p in paths)
        assert "local/a.md" in paths


class TestSoftDelete:
    """論理削除テスト."""

    def test_soft_delete_and_restore(self, store: SourceStore) -> None:
        store.place_file(source_type="local", data=b"data", rel_path="local/test.md")

        store.soft_delete("local/test.md")
        record = store.db.get_source("local/test.md")
        assert record is not None
        assert record.status == "deleted"

        # ファイルはまだ存在する
        assert (store.root_dir / "local" / "test.md").exists()

        store.restore("local/test.md")
        record = store.db.get_source("local/test.md")
        assert record is not None
        assert record.status == "active"

    def test_soft_delete_nonexistent(self, store: SourceStore) -> None:
        with pytest.raises(KeyError):
            store.soft_delete("nonexistent")


class TestRebuildDb:
    """DB 再構築テスト."""

    def test_rebuild(self, store: SourceStore) -> None:
        """ファイルと .meta から DB を再構築する."""
        # ファイルを配置
        store.place_file(source_type="local", data=b"local data", rel_path="local/doc.md")
        store.place_file(
            source_type="web",
            data=b"<html>page</html>",
            rel_path="web/https/example.com/page.html",
            metadata={
                "source_id": "https://example.com/page",
                "source_type": "web",
                "title": "Test Page",
                "collected_at": "2026-01-15T10:30:00+09:00",
            },
        )

        # DB をクリアして再構築
        count = store.rebuild_db()
        assert count == 2

        # local ファイル
        local_record = store.db.get_source("local/doc.md")
        assert local_record is not None
        assert local_record.source_type == "local"
        assert local_record.title == "doc"

        # web ファイル（.meta から source_id とタイトルを取得）
        web_record = store.db.get_source("https://example.com/page")
        assert web_record is not None
        assert web_record.source_type == "web"
        assert web_record.title == "Test Page"

    def test_rebuild_with_missing_meta(self, store: SourceStore) -> None:
        """非 local 媒体で .meta が欠落している場合、パスベースのフォールバックで登録される."""
        # .meta なしで直接ファイルを配置
        web_dir = store.root_dir / "web" / "https" / "example.com"
        web_dir.mkdir(parents=True)
        (web_dir / "page.html").write_bytes(b"<html>test</html>")

        count = store.rebuild_db()
        assert count == 1

        # パスベースのフォールバックで登録
        record = store.db.get_source("web/https/example.com/page.html")
        assert record is not None
        assert record.source_type == "web"
        assert record.title == "page"  # ファイル名から導出


class TestPlaceFileFromUrlExtension:
    """place_file_from_url の拡張子処理テスト."""

    def test_no_double_extension(self, store: SourceStore) -> None:
        """URL パスが既に拡張子で終わる場合、二重付加しない."""
        metadata = {
            "source_id": "https://example.com/page.html",
            "source_type": "web",
            "title": "Page",
            "collected_at": "2026-01-01T00:00:00Z",
        }
        dest = store.place_file_from_url(
            url="https://example.com/page.html",
            data=b"<html></html>",
            metadata=metadata,
            extension=".html",
        )
        assert dest.name == "page.html"
        assert ".html.html" not in dest.name


class TestContextManager:
    """コンテキストマネージャテスト."""

    def test_context_manager(self, tmp_path: Path) -> None:
        with SourceStore(tmp_path / "store") as store:
            store.initialize()
            store.place_file(source_type="local", data=b"test", rel_path="local/t.md")
            record = store.db.get_source("local/t.md")
            assert record is not None

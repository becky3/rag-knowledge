"""source_store 統合テスト.

仕様: docs/specs/source-store.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.store.models import SourceStatus
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
        assert record.status is SourceStatus.ACTIVE
        assert record.file_size == len(data)

    def test_place_web_file_with_meta(self, store: SourceStore) -> None:
        data = b"<html><body>Test</body></html>"
        metadata = {
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

        record = store.db.get_source("web/https/example.com/page.html")
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

        result = store.get_file("web/https/example.com/page.html")
        assert result is not None
        assert result.content == b"<html>test</html>"
        assert result.metadata.source_id == "web/https/example.com/page.html"
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
                "source_type": "web",
                "title": "C",
                "collected_at": "2026-01-01T00:00:00Z",
                "url": "https://example.com/c",
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
                "source_type": "web",
                "title": "B",
                "collected_at": "2026-01-01T00:00:00Z",
                "url": "https://example.com/b",
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

    def test_excludes_lock_files(self, store: SourceStore) -> None:
        """ロックファイルはリストに含まれない."""
        (store.root_dir / ".write.lock").write_bytes(b"")
        (store.root_dir / ".rebuild.lock").write_bytes(b"")
        store.place_file(source_type="local", data=b"a", rel_path="local/a.md")

        files = store.list_files()
        paths = [f.as_posix() for f in files]
        assert ".write.lock" not in paths
        assert ".rebuild.lock" not in paths
        assert "local/a.md" in paths

    def test_excludes_bluesky_media(self, store: SourceStore) -> None:
        """BlueSky の media/ 配下はリストに含まれない."""
        store.place_file(
            source_type="bluesky",
            data=b"post",
            rel_path="bluesky/did/2026/04/rkey1.json",
            metadata={
                "source_type": "bluesky",
                "title": "post",
                "collected_at": "2026-04-01T00:00:00Z",
                "at_uri": "at://did/app.bsky.feed.post/rkey1",
            },
        )
        # media/ 配下に画像を直接配置（インジェスターの挙動を模倣）
        media_dir = store.root_dir / "bluesky" / "did" / "2026" / "04" / "media" / "rkey1"
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / "image_0.webp").write_bytes(b"img0")
        (media_dir / "image_1.jpg").write_bytes(b"img1")

        # source_type 指定なし
        all_files = store.list_files()
        all_paths = [f.as_posix() for f in all_files]
        assert "bluesky/did/2026/04/rkey1.json" in all_paths
        assert not any("media/" in p for p in all_paths)

        # source_type=bluesky 指定
        bs_files = store.list_files(source_type="bluesky")
        bs_paths = [f.as_posix() for f in bs_files]
        assert "bluesky/did/2026/04/rkey1.json" in bs_paths
        assert not any("media/" in p for p in bs_paths)

    def test_local_media_dir_not_excluded(self, store: SourceStore) -> None:
        """local の media/ ディレクトリは除外されない."""
        store.place_file(
            source_type="local", data=b"img", rel_path="local/media/photo.jpg",
        )
        files = store.list_files()
        paths = [f.as_posix() for f in files]
        assert "local/media/photo.jpg" in paths

    def test_excludes_aozora_catalog(self, store: SourceStore) -> None:
        """aozora/catalog.csv は独立ソースと共存しても sidecar として除外される.

        独立ソース（aozora 作品 XHTML）と catalog.csv が両方存在する場合に、
        catalog.csv と .meta のみが除外され、独立ソースは残ることを検証する
        （#599 問題 6 回帰防止）。
        """
        catalog = store.root_dir / "aozora" / "catalog.csv"
        catalog.parent.mkdir(parents=True, exist_ok=True)
        catalog.write_text("book_id,author\n001,Alice\n", encoding="utf-8")
        (catalog.parent / "catalog.csv.meta").write_text(
            "source_type: aozora\ntitle: catalog\n",
            encoding="utf-8",
        )
        store.place_file(
            source_type="aozora",
            data=b"<html></html>",
            rel_path="aozora/000035/001.html",
            metadata={
                "source_type": "aozora",
                "title": "Sample",
                "collected_at": "2026-01-01T00:00:00Z",
                "book_id": "001",
                "person_id": "000035",
                "author": "Alice",
                "author_kana": "a",
                "copyright_expired": True,
                "url": "https://example.com",
            },
        )
        files = store.list_files()
        paths = [f.as_posix() for f in files]
        assert "aozora/catalog.csv" not in paths
        assert "aozora/catalog.csv.meta" not in paths
        assert "aozora/000035/001.html" in paths

    def test_stats_count_consistency(self, store: SourceStore) -> None:
        """list_files と is_source_file は整合する（#599 問題 6 回帰防止）.

        ロック・catalog・attachment・.meta がすべて除外され、
        残りの独立ソースのみがカウントされること。
        """
        from rag.store.source_store import detect_source_type

        # 独立ソース
        store.place_file(
            source_type="local", data=b"a", rel_path="local/a.md",
        )
        store.place_file(
            source_type="bluesky",
            data=b"{}",
            rel_path="bluesky/did/2026/04/p.json",
            metadata={
                "source_type": "bluesky",
                "title": "p",
                "collected_at": "2026-04-01T00:00:00Z",
                "at_uri": "at://did/app.bsky.feed.post/p",
            },
        )
        # 除外されるべきファイル群
        (store.root_dir / ".write.lock").write_bytes(b"")
        (store.root_dir / ".rebuild.lock").write_bytes(b"")
        catalog = store.root_dir / "aozora" / "catalog.csv"
        catalog.parent.mkdir(parents=True, exist_ok=True)
        catalog.write_bytes(b"x")
        (catalog.parent / "catalog.csv.meta").write_bytes(b"x")
        # bluesky attachment
        media = (
            store.root_dir / "bluesky" / "did" / "2026" / "04"
            / "media" / "p"
        )
        media.mkdir(parents=True, exist_ok=True)
        (media / "image_0.webp").write_bytes(b"img")

        files = store.list_files()
        paths = [f.as_posix() for f in files]
        # 独立ソースの 2 件のみ列挙される
        assert set(paths) == {
            "local/a.md",
            "bluesky/did/2026/04/p.json",
        }

        # 媒体別の内訳を作成（run_stats と同じロジック）
        by_type: dict[str, int] = {}
        for f in files:
            st = detect_source_type(f.as_posix())  # ValueError 送出なし
            by_type[st] = by_type.get(st, 0) + 1
        assert by_type == {"local": 1, "bluesky": 1}


class TestSoftDelete:
    """論理削除テスト."""

    def test_soft_delete_and_restore(self, store: SourceStore) -> None:
        store.place_file(source_type="local", data=b"data", rel_path="local/test.md")

        store.soft_delete("local/test.md")
        record = store.db.get_source("local/test.md")
        assert record is not None
        assert record.status is SourceStatus.DELETED

        # ファイルはまだ存在する
        assert (store.root_dir / "local" / "test.md").exists()

        store.restore("local/test.md")
        record = store.db.get_source("local/test.md")
        assert record is not None
        assert record.status is SourceStatus.ACTIVE

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
                "source_type": "web",
                "title": "Test Page",
                "collected_at": "2026-01-15T10:30:00+09:00",
                "url": "https://example.com/page",
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

        # web ファイル（.meta からタイトルを取得）
        web_record = store.db.get_source("web/https/example.com/page.html")
        assert web_record is not None
        assert web_record.source_type == "web"
        assert web_record.title == "Test Page"

    def test_rebuild_with_source_type_filter(self, store: SourceStore) -> None:
        """source_type フィルタ指定時は対象 type のみ再構築し、他は保持する."""
        # local と web の 2 種類を配置
        store.place_file(source_type="local", data=b"local data", rel_path="local/doc.md")
        store.place_file(
            source_type="web",
            data=b"<html>page</html>",
            rel_path="web/https/example.com/page.html",
            metadata={
                "source_type": "web",
                "title": "Test Page",
                "collected_at": "2026-01-15T10:30:00+09:00",
                "url": "https://example.com/page",
            },
        )

        # 初回は全件再構築
        count = store.rebuild_db()
        assert count == 2

        # web のみ再構築
        count = store.rebuild_db(source_type="web")
        assert count == 1

        # local レコードは保持されている
        local_record = store.db.get_source("local/doc.md")
        assert local_record is not None
        assert local_record.source_type == "local"

        # web レコードも再登録されている
        web_record = store.db.get_source("web/https/example.com/page.html")
        assert web_record is not None
        assert web_record.source_type == "web"

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
            "source_type": "web",
            "title": "Page",
            "collected_at": "2026-01-01T00:00:00Z",
            "url": "https://example.com/page.html",
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


class TestIsSourceFile:
    """is_source_file 判定テスト（仕様: source-store.md ソース判定セクション）."""

    def test_independent_source_returns_true(self) -> None:
        """独立ソースは True を返す."""
        from rag.store.source_store import is_source_file

        assert is_source_file("local/my-notes/memo.md") is True
        assert is_source_file("web/https/example.com/docs/guide.html") is True
        assert is_source_file("zenn/alice/articles/sample.json") is True
        assert is_source_file("bluesky/did-plc-xxx/2026/03/rkey.json") is True
        assert is_source_file("youtube/UC123/video.json") is True
        assert is_source_file("aozora/000035/001567.html") is True
        assert is_source_file("journal/rag-knowledge/entry.md") is True

    def test_meta_sidecar_excluded(self) -> None:
        """.meta サイドカーは任意階層で除外される."""
        from rag.store.source_store import is_source_file

        assert is_source_file("web/https/example.com/page.html.meta") is False
        assert is_source_file("local/notes.md.meta") is False
        assert is_source_file("aozora/catalog.csv.meta") is False

    def test_root_lock_files_excluded(self) -> None:
        """ルート直下のロック・管理ファイルは除外される."""
        from rag.store.source_store import is_source_file

        assert is_source_file("metadata.db") is False
        assert is_source_file("metadata.db-wal") is False
        assert is_source_file("metadata.db-shm") is False
        assert is_source_file(".gitignore") is False
        assert is_source_file(".write.lock") is False
        assert is_source_file(".rebuild.lock") is False

    def test_user_files_with_same_name_not_excluded(self) -> None:
        """ユーザー文書内の同名ファイル（サブディレクトリ）は除外されない.

        pathspec の anchoring により、ルート直下以外の同名ファイルは残る。
        """
        from rag.store.source_store import is_source_file

        assert is_source_file("local/myproject/.gitignore") is True
        assert is_source_file("local/foo/.write.lock") is True
        assert is_source_file("local/bar/metadata.db") is True

    def test_git_directory_excluded_any_depth(self) -> None:
        """.git/ は任意階層で除外される."""
        from rag.store.source_store import is_source_file

        assert is_source_file(".git/HEAD") is False
        assert is_source_file(".git/objects/abc") is False

    def test_aozora_catalog_excluded(self) -> None:
        """aozora の catalog.csv は sidecar として除外される."""
        from rag.store.source_store import is_source_file

        assert is_source_file("aozora/catalog.csv") is False
        # 通常の aozora 作品は独立ソースとして扱う
        assert is_source_file("aozora/000035/001567.html") is True

    def test_os_generated_files_excluded_any_depth(self) -> None:
        """OS 生成ファイルは任意階層で除外される."""
        from rag.store.source_store import is_source_file

        assert is_source_file(".DS_Store") is False
        assert is_source_file("local/foo/.DS_Store") is False
        assert is_source_file("Thumbs.db") is False
        assert is_source_file("web/https/example.com/Thumbs.db") is False
        assert is_source_file("desktop.ini") is False

    def test_docdata_directory_excluded_any_depth(self) -> None:
        """docdata/ 配下はドキュメント生成ツールのインフラファイルとして除外される."""
        from rag.store.source_store import is_source_file

        assert is_source_file("local/Unity/Docs/Manual/docdata/index.js") is False
        assert is_source_file("local/Unity/Docs/Manual/docdata/toc.js") is False
        assert is_source_file("local/Unity/Docs/Manual/docdata/index.json") is False
        assert is_source_file("local/Unity/Docs/Manual/docdata/toc.json") is False
        assert is_source_file("local/other-docs/docdata/search.js") is False

    def test_xrefmap_excluded_any_depth(self) -> None:
        """xrefmap.yml はドキュメント生成ツールのインフラファイルとして除外される."""
        from rag.store.source_store import is_source_file

        assert is_source_file("local/Unity/Docs/Manual/xrefmap.yml") is False
        assert is_source_file("local/other-docs/xrefmap.yml") is False

    def test_docdata_name_in_non_directory_context_not_excluded(self) -> None:
        """docdata がディレクトリではなくファイル名の一部の場合は除外しない."""
        from rag.store.source_store import is_source_file

        assert is_source_file("local/my-notes/docdata-notes.md") is True

    def test_bluesky_attachment_excluded(self) -> None:
        """BlueSky の attachment（media/ 配下）は除外される."""
        from rag.store.source_store import is_source_file

        assert is_source_file("bluesky/did/2026/01/media/rkey/image_0.webp") is False
        assert is_source_file("bluesky/did/2026/01/media/rkey/video_0.ts") is False

    def test_unknown_prefix_excluded(self) -> None:
        """未知 source_type プレフィックスは False を返す（invariant 担保）.

        `detect_source_type` が ValueError を送出するパスは `is_source_file` が
        False を返すことで、以下の invariant が成立する:
            is_source_file(p) == True  →  detect_source_type(p) は成功する
        """
        from rag.store.source_store import is_source_file

        assert is_source_file("unknown/foo.md") is False
        assert is_source_file("random/path/file.txt") is False
        # ルート直下の単独ファイル（プレフィックスが source_type でない）も除外
        assert is_source_file("orphan.md") is False


class TestResolveAttachmentParent:
    """resolve_attachment_parent（純粋関数）のテスト."""

    def test_bluesky_media_image_parent(self) -> None:
        """bluesky media image → 親 JSON に解決される."""
        from rag.store.source_store import resolve_attachment_parent

        parent = resolve_attachment_parent(
            "bluesky/did-plc-xxx/2026/03/media/rkey/image_0.webp",
        )
        assert parent == "bluesky/did-plc-xxx/2026/03/rkey.json"

    def test_bluesky_media_video_parent(self) -> None:
        """bluesky media video → 親 JSON に解決される."""
        from rag.store.source_store import resolve_attachment_parent

        parent = resolve_attachment_parent(
            "bluesky/did/2026/03/media/abc/video_0.ts",
        )
        assert parent == "bluesky/did/2026/03/abc.json"

    def test_non_attachment_returns_none(self) -> None:
        """attachment パターンに該当しないパスは None."""
        from rag.store.source_store import resolve_attachment_parent

        assert resolve_attachment_parent("local/foo.md") is None
        assert resolve_attachment_parent("bluesky/did/2026/03/rkey.json") is None
        assert resolve_attachment_parent("zenn/alice/articles/a.json") is None

    def test_is_pure_function_no_io(self, tmp_path: Path) -> None:
        """ファイル存在確認を行わない（純粋関数）.

        親ファイルの存在/不存在で結果が変わらないことで純粋性を検証する。
        """
        from rag.store.source_store import resolve_attachment_parent

        path = "bluesky/did/2026/03/media/ghost/image_0.webp"
        # 親が存在しない状態
        result_before = resolve_attachment_parent(path)
        # 親ファイルを作成
        parent = tmp_path / "bluesky" / "did" / "2026" / "03" / "ghost.json"
        parent.parent.mkdir(parents=True, exist_ok=True)
        parent.write_text("{}", encoding="utf-8")
        # 親が存在する状態
        result_after = resolve_attachment_parent(path)
        # 親実在性に依らず同じ結果
        assert result_before == result_after == "bluesky/did/2026/03/ghost.json"


class TestFindExistingParent:
    """find_existing_parent（IO 付き関数）のテスト."""

    def test_existing_parent_returned(self, store: SourceStore) -> None:
        """親 JSON が実在する場合はパスを返す."""
        store.place_file(
            source_type="bluesky",
            data=b"{}",
            rel_path="bluesky/did/2026/03/rkey.json",
            metadata={
                "source_type": "bluesky",
                "title": "p",
                "collected_at": "2026-03-01T00:00:00Z",
                "at_uri": "at://did/app.bsky.feed.post/rkey",
            },
        )
        result = store.find_existing_parent(
            "bluesky/did/2026/03/media/rkey/image_0.webp",
        )
        assert result == "bluesky/did/2026/03/rkey.json"

    def test_orphan_attachment_returns_none(self, store: SourceStore) -> None:
        """親 JSON が存在しない孤児 attachment は None."""
        result = store.find_existing_parent(
            "bluesky/did/2026/03/media/ghost/image_0.webp",
        )
        assert result is None

    def test_non_attachment_returns_none(self, store: SourceStore) -> None:
        """attachment パターンでないパスは常に None."""
        result = store.find_existing_parent("local/foo.md")
        assert result is None


class TestDetectSourceType:
    """detect_source_type のテスト."""

    def test_known_prefixes(self) -> None:
        """既知のプレフィックスは対応する SourceType を返す."""
        from rag.store.source_store import detect_source_type

        assert detect_source_type("web/foo.html") == "web"
        assert detect_source_type("bluesky/did/a.json") == "bluesky"
        assert detect_source_type("zenn/user/articles/a.json") == "zenn"
        assert detect_source_type("youtube/UC/v.json") == "youtube"
        assert detect_source_type("aozora/000035/001.html") == "aozora"
        assert detect_source_type("local/foo.md") == "local"
        assert detect_source_type("journal/repo/e.md") == "journal"

    def test_unknown_prefix_raises_value_error(self) -> None:
        """未知プレフィックスは ValueError を送出する."""
        from rag.store.source_store import detect_source_type

        with pytest.raises(ValueError, match="未知の source_type"):
            detect_source_type(".write.lock")
        with pytest.raises(ValueError, match="未知の source_type"):
            detect_source_type("unknown/foo.md")
        with pytest.raises(ValueError, match="未知の source_type"):
            detect_source_type(".gitignore")

    def test_invariant_is_source_file_implies_success(self) -> None:
        """invariant: is_source_file=True のパスでは detect_source_type が成功する."""
        from rag.store.source_store import detect_source_type, is_source_file

        # 代表的な独立ソースパスを is_source_file を通して検証
        candidates = [
            "local/my-notes/memo.md",
            "web/https/example.com/docs/guide.html",
            "zenn/alice/articles/sample.json",
            "bluesky/did/2026/03/rkey.json",
            "youtube/UC/video.json",
            "aozora/000035/001567.html",
            "journal/repo/e.md",
        ]
        for path in candidates:
            assert is_source_file(path) is True, path
            # ValueError が送出されないこと
            detect_source_type(path)


class TestPlaceFileRejectsNonSource:
    """place_file の is_source_file 事前チェック."""

    def test_rejects_ds_store(self, store: SourceStore) -> None:
        """.DS_Store の配置は拒否される."""
        with pytest.raises(ValueError, match="独立ソースではない"):
            store.place_file(
                source_type="local",
                data=b"",
                rel_path="local/.DS_Store",
            )

    def test_rejects_thumbs_db(self, store: SourceStore) -> None:
        """Thumbs.db の配置は拒否される."""
        with pytest.raises(ValueError, match="独立ソースではない"):
            store.place_file(
                source_type="local",
                data=b"",
                rel_path="local/Thumbs.db",
            )

    def test_rejects_meta_file(self, store: SourceStore) -> None:
        """.meta 直接配置は拒否される（サイドカーは write_meta 経由）."""
        with pytest.raises(ValueError, match="独立ソースではない"):
            store.place_file(
                source_type="local",
                data=b"",
                rel_path="local/foo.md.meta",
            )

    def test_rejects_attachment_path(self, store: SourceStore) -> None:
        """attachment パス（bluesky media/）の place_file 経由配置は拒否される."""
        with pytest.raises(ValueError, match="独立ソースではない"):
            store.place_file(
                source_type="bluesky",
                data=b"img",
                rel_path="bluesky/did/2026/03/media/rkey/image_0.webp",
                metadata={
                    "source_type": "bluesky",
                    "title": "x",
                    "collected_at": "2026-03-01T00:00:00Z",
                    "at_uri": "at://did/app.bsky.feed.post/rkey",
                },
            )

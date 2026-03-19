"""meta モジュールのテスト.

仕様: docs/specs/source-store.md — .meta サイドカーファイル
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.store.meta import meta_path_for, read_meta, write_meta


class TestMetaPathFor:
    """meta_path_for のテスト."""

    def test_html_file(self) -> None:
        result = meta_path_for(Path("guide.html"))
        assert result == Path("guide.html.meta")

    def test_json_file(self) -> None:
        result = meta_path_for(Path("post_xyz.json"))
        assert result == Path("post_xyz.json.meta")

    def test_nested_path(self) -> None:
        result = meta_path_for(Path("web/https/example.com/page.html"))
        assert result == Path("web/https/example.com/page.html.meta")


class TestWriteAndReadMeta:
    """write_meta / read_meta のテスト."""

    def test_write_and_read(self, tmp_path: Path) -> None:
        data_file = tmp_path / "test.html"
        data_file.write_text("<html></html>")

        metadata = {
            "source_id": "https://example.com/test",
            "source_type": "web",
            "title": "Test Page",
            "collected_at": "2026-01-15T10:30:00+09:00",
            "url": "https://example.com/test",
        }

        write_meta(data_file, metadata)
        result = read_meta(data_file)

        assert result["source_id"] == "https://example.com/test"
        assert result["source_type"] == "web"
        assert result["title"] == "Test Page"
        assert result["url"] == "https://example.com/test"

    def test_unicode_content(self, tmp_path: Path) -> None:
        """日本語を含むメタデータの読み書き."""
        data_file = tmp_path / "article.html"
        data_file.write_text("<html></html>")

        metadata = {
            "source_id": "https://example.com/article",
            "title": "テスト記事タイトル",
            "source_type": "web",
            "collected_at": "2026-01-15T10:30:00+09:00",
        }

        write_meta(data_file, metadata)
        result = read_meta(data_file)

        assert result["title"] == "テスト記事タイトル"

    def test_bluesky_meta(self, tmp_path: Path) -> None:
        """BlueSky の .meta フィールド."""
        data_file = tmp_path / "rkey.json"
        data_file.write_text("{}")

        metadata = {
            "source_id": "at://did:plc:abc123/app.bsky.feed.post/xyz789",
            "source_type": "bluesky",
            "title": "Sample post text",
            "collected_at": "2026-01-15T10:30:00+09:00",
            "handle": "alice.bsky.social",
            "did": "did:plc:abc123",
            "rkey": "xyz789",
            "url": "https://bsky.app/profile/alice.bsky.social/post/xyz789",
            "created_at": "2026-01-15T09:00:00Z",
            "has_images": False,
            "has_video": False,
            "has_external_link": True,
            "is_reply": False,
            "is_repost": False,
        }

        write_meta(data_file, metadata)
        result = read_meta(data_file)

        assert result["handle"] == "alice.bsky.social"
        assert result["has_external_link"] is True
        assert result["is_reply"] is False

    def test_zenn_meta_with_topics(self, tmp_path: Path) -> None:
        """Zenn の topics リストの読み書き."""
        data_file = tmp_path / "slug.html"
        data_file.write_text("<html></html>")

        metadata = {
            "source_id": "https://zenn.dev/alice/articles/sample",
            "source_type": "zenn",
            "title": "Sample Article",
            "collected_at": "2026-01-15T10:30:00+09:00",
            "slug": "sample",
            "content_type": "article",
            "article_type": "tech",
            "published_at": "2026-01-10T12:00:00+09:00",
            "liked_count": 42,
            "topics": ["Python", "FastAPI"],
            "comments_count": 0,
            "closed": False,
            "username": "alice",
        }

        write_meta(data_file, metadata)
        result = read_meta(data_file)

        assert result["topics"] == ["Python", "FastAPI"]
        assert result["liked_count"] == 42

    def test_read_missing_meta(self, tmp_path: Path) -> None:
        """.meta ファイルが存在しない場合 FileNotFoundError."""
        data_file = tmp_path / "no_meta.html"
        data_file.write_text("<html></html>")

        with pytest.raises(FileNotFoundError):
            read_meta(data_file)

    def test_meta_file_is_yaml(self, tmp_path: Path) -> None:
        """.meta ファイルが YAML 形式で書かれていることを確認."""
        data_file = tmp_path / "test.html"
        data_file.write_text("<html></html>")

        write_meta(data_file, {"key": "value"})

        meta_file = tmp_path / "test.html.meta"
        content = meta_file.read_text(encoding="utf-8")
        assert "key: value" in content

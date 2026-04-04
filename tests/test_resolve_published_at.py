"""resolve_published_at のテスト.

テスト方針:
- source_type ごとの公開日時フィールド抽出
- フォールバック（フィールド欠損時に collected_at を使用）
- youtube の YYYYMMDD → ISO 8601 変換
"""

from __future__ import annotations

from rag.store.resolve import resolve_published_at


class TestResolvePublishedAt:
    """resolve_published_at のテスト."""

    def test_bluesky_uses_created_at(self) -> None:
        meta = {"created_at": "2026-03-15T12:00:00+00:00"}
        result = resolve_published_at("bluesky", meta, "2026-04-01T00:00:00Z")
        assert result == "2026-03-15T12:00:00+00:00"

    def test_zenn_uses_published_at(self) -> None:
        meta = {"published_at": "2026-02-20T09:30:00+09:00"}
        result = resolve_published_at("zenn", meta, "2026-04-01T00:00:00Z")
        assert result == "2026-02-20T09:30:00+09:00"

    def test_youtube_converts_upload_date(self) -> None:
        meta = {"upload_date": "20260115"}
        result = resolve_published_at("youtube", meta, "2026-04-01T00:00:00Z")
        assert result == "2026-01-15T00:00:00+00:00"

    def test_youtube_non_8digit_falls_back(self) -> None:
        """upload_date が 8桁数値でない場合はそのまま返す."""
        meta = {"upload_date": "2026-01-15"}
        result = resolve_published_at("youtube", meta, "2026-04-01T00:00:00Z")
        assert result == "2026-01-15"

    def test_aozora_falls_back_to_collected_at(self) -> None:
        meta = {"title": "some title"}
        result = resolve_published_at("aozora", meta, "2026-04-01T00:00:00Z")
        assert result == "2026-04-01T00:00:00Z"

    def test_journal_falls_back_to_collected_at(self) -> None:
        result = resolve_published_at("journal", {}, "2026-04-01T00:00:00Z")
        assert result == "2026-04-01T00:00:00Z"

    def test_local_falls_back_to_collected_at(self) -> None:
        result = resolve_published_at("local", None, "2026-04-01T00:00:00Z")
        assert result == "2026-04-01T00:00:00Z"

    def test_web_falls_back_to_collected_at(self) -> None:
        meta = {"url": "https://example.com"}
        result = resolve_published_at("web", meta, "2026-04-01T00:00:00Z")
        assert result == "2026-04-01T00:00:00Z"

    def test_none_metadata_falls_back(self) -> None:
        result = resolve_published_at("bluesky", None, "2026-04-01T00:00:00Z")
        assert result == "2026-04-01T00:00:00Z"

    def test_empty_field_value_falls_back(self) -> None:
        """フィールドが存在するが空文字の場合はフォールバック."""
        meta = {"created_at": ""}
        result = resolve_published_at("bluesky", meta, "2026-04-01T00:00:00Z")
        assert result == "2026-04-01T00:00:00Z"

    def test_missing_field_falls_back(self) -> None:
        """対象フィールドが .meta にない場合はフォールバック."""
        meta = {"title": "test"}
        result = resolve_published_at("bluesky", meta, "2026-04-01T00:00:00Z")
        assert result == "2026-04-01T00:00:00Z"

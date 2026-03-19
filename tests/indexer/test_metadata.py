"""チャンクメタデータ構築のテスト.

仕様: docs/specs/indexer.md
"""

from __future__ import annotations

from rag.indexer.metadata import build_chunk_metadata
from rag.store.models import SourceMetadata


def _make_metadata(**overrides: object) -> SourceMetadata:
    """テスト用の SourceMetadata を生成する."""
    defaults: dict[str, object] = {
        "source_id": "https://example.com/page",
        "source_type": "web",
        "title": "Test Page",
        "collected_at": "2025-01-01T00:00:00Z",
        "extra": {},
    }
    defaults.update(overrides)
    return SourceMetadata(**defaults)  # type: ignore[arg-type]


class TestBuildChunkMetadata:
    """build_chunk_metadata のテスト."""

    def test_common_fields(self) -> None:
        meta = _make_metadata()
        result = build_chunk_metadata(meta, chunk_index=0, total_chunks=5)

        assert result["source_id"] == "https://example.com/page"
        assert result["source_type"] == "web"
        assert result["title"] == "Test Page"
        assert result["chunk_index"] == 0
        assert result["total_chunks"] == 5
        assert result["collected_at"] == "2025-01-01T00:00:00Z"

    def test_chunk_index_varies(self) -> None:
        meta = _make_metadata()
        r0 = build_chunk_metadata(meta, chunk_index=0, total_chunks=3)
        r2 = build_chunk_metadata(meta, chunk_index=2, total_chunks=3)
        assert r0["chunk_index"] == 0
        assert r2["chunk_index"] == 2
        assert r0["total_chunks"] == r2["total_chunks"] == 3

    def test_custom_fields_with_prefix(self) -> None:
        meta = _make_metadata(extra={"author": "Alice", "category": "tech"})
        result = build_chunk_metadata(meta, chunk_index=0, total_chunks=1)

        assert result["custom:author"] == "Alice"
        assert result["custom:category"] == "tech"

    def test_list_value_converted_to_str(self) -> None:
        meta = _make_metadata(extra={"tags": ["Python", "AI"]})
        result = build_chunk_metadata(meta, chunk_index=0, total_chunks=1)

        assert result["custom:tags"] == "['Python', 'AI']"

    def test_bool_value_preserved(self) -> None:
        meta = _make_metadata(extra={"is_featured": True})
        result = build_chunk_metadata(meta, chunk_index=0, total_chunks=1)

        assert result["custom:is_featured"] is True

    def test_int_value_preserved(self) -> None:
        meta = _make_metadata(extra={"view_count": 42})
        result = build_chunk_metadata(meta, chunk_index=0, total_chunks=1)

        assert result["custom:view_count"] == 42

    def test_float_value_preserved(self) -> None:
        meta = _make_metadata(extra={"score": 0.95})
        result = build_chunk_metadata(meta, chunk_index=0, total_chunks=1)

        assert result["custom:score"] == 0.95

    def test_no_custom_fields_when_extra_empty(self) -> None:
        meta = _make_metadata(extra={})
        result = build_chunk_metadata(meta, chunk_index=0, total_chunks=1)

        custom_keys = [k for k in result if k.startswith("custom:")]
        assert custom_keys == []

    def test_source_type_bluesky(self) -> None:
        meta = _make_metadata(source_type="bluesky")
        result = build_chunk_metadata(meta, chunk_index=0, total_chunks=1)
        assert result["source_type"] == "bluesky"

    def test_source_type_local(self) -> None:
        meta = _make_metadata(source_type="local")
        result = build_chunk_metadata(meta, chunk_index=0, total_chunks=1)
        assert result["source_type"] == "local"

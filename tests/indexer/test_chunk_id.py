"""チャンク ID 生成のテスト.

仕様: docs/specs/indexer.md
"""

from __future__ import annotations

import hashlib

import pytest

from rag.indexer.chunk_id import generate_chunk_id, parse_chunk_index, source_id_prefix


class TestSourceIdPrefix:
    """source_id_prefix のテスト."""

    def test_returns_16_char_hex(self) -> None:
        result = source_id_prefix("https://example.com/docs/guide")
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    def test_matches_sha256_prefix(self) -> None:
        source_id = "https://example.com/docs/guide"
        expected = hashlib.sha256(source_id.encode()).hexdigest()[:16]
        assert source_id_prefix(source_id) == expected

    def test_same_source_id_returns_same_prefix(self) -> None:
        sid = "https://example.com/page"
        assert source_id_prefix(sid) == source_id_prefix(sid)

    def test_different_source_ids_return_different_prefixes(self) -> None:
        assert source_id_prefix("a") != source_id_prefix("b")


class TestGenerateChunkId:
    """generate_chunk_id のテスト."""

    def test_format(self) -> None:
        result = generate_chunk_id("https://example.com/docs/guide", 0)
        prefix = source_id_prefix("https://example.com/docs/guide")
        assert result == f"{prefix}_0"

    def test_chunk_index_in_id(self) -> None:
        sid = "https://example.com"
        assert generate_chunk_id(sid, 0).endswith("_0")
        assert generate_chunk_id(sid, 1).endswith("_1")
        assert generate_chunk_id(sid, 99).endswith("_99")

    def test_same_source_same_index_is_deterministic(self) -> None:
        sid = "test-source"
        assert generate_chunk_id(sid, 5) == generate_chunk_id(sid, 5)

    def test_same_source_different_index_differs(self) -> None:
        sid = "test-source"
        assert generate_chunk_id(sid, 0) != generate_chunk_id(sid, 1)

    def test_different_source_same_index_differs(self) -> None:
        assert generate_chunk_id("source-a", 0) != generate_chunk_id("source-b", 0)


class TestParseChunkIndex:
    """parse_chunk_index のテスト."""

    def test_parses_index_zero(self) -> None:
        chunk_id = generate_chunk_id("test-source", 0)
        assert parse_chunk_index(chunk_id) == 0

    def test_parses_index_nonzero(self) -> None:
        chunk_id = generate_chunk_id("test-source", 42)
        assert parse_chunk_index(chunk_id) == 42

    def test_roundtrip(self) -> None:
        for i in range(10):
            chunk_id = generate_chunk_id("source", i)
            assert parse_chunk_index(chunk_id) == i

    def test_invalid_format_no_underscore(self) -> None:
        with pytest.raises(ValueError, match="不正なチャンク ID"):
            parse_chunk_index("nounderscore")

    def test_invalid_format_non_numeric_index(self) -> None:
        with pytest.raises(ValueError, match="インデックスを解析"):
            parse_chunk_index("prefix_abc")

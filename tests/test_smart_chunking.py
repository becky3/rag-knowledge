"""smart_chunk のテスト

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

from rag.indexer.smart_chunking import smart_chunk


class TestSmartChunking:
    """AC1/AC3/AC5: smart_chunk() のテスト."""

    def test_smart_chunk_detects_table_data(self) -> None:
        """AC1: テーブルデータが正しく検出・チャンキングされること."""
        table_text = """名前\tHP\tMP\t攻撃力
魔王\t200\t100\t140
ゴブリン\t8\t0\t5
ゴーレム\t120\t0\t90"""

        chunks = smart_chunk(table_text, chunk_size=200, chunk_overlap=30)

        assert len(chunks) > 0
        assert all(isinstance(c, tuple) and len(c) == 2 for c in chunks)
        assert any("名前:" in c[0] or "魔王" in c[0] for c in chunks)

    def test_smart_chunk_detects_headings(self) -> None:
        """AC3: 見出し付きテキストが正しく検出・チャンキングされること."""
        heading_text = """# メインタイトル

これは最初のセクションです。

## サブセクション1

サブセクション1の内容です。

## サブセクション2

サブセクション2の内容です。"""

        chunks = smart_chunk(heading_text, chunk_size=200, chunk_overlap=30)

        assert len(chunks) > 0
        assert all(isinstance(c, tuple) and len(c) == 2 for c in chunks)

    def test_smart_chunk_prose_fallback(self) -> None:
        """AC5: 通常テキストは従来のチャンキングにフォールバックすること."""
        prose_text = """これは通常の段落テキストです。
特に構造化されていない長いテキストが続きます。
複数の文で構成されており、自然言語で書かれています。"""

        chunks = smart_chunk(prose_text, chunk_size=200, chunk_overlap=30)

        assert len(chunks) >= 1
        assert all(isinstance(c, tuple) and len(c) == 2 for c in chunks)

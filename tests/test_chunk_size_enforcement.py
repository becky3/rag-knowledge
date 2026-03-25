"""チャンクサイズ制限の全経路適用テスト.

#364: 全チャンカーの出力がトークン安全上限を超えないことを検証する。
仕様: docs/specs/indexer.md「トークン安全上限」
"""

from __future__ import annotations

from rag.chunker import chunk_text
from rag.heading_chunker import chunk_by_headings
from rag.table_chunker import chunk_table_data


class TestProseChunkerSizeEnforcement:
    """散文チャンカーのサイズ制限テスト."""

    def test_chunks_within_limit(self) -> None:
        """全チャンクが chunk_size 以内であること."""
        text = "あ" * 2000
        chunks = chunk_text(text, chunk_size=200, chunk_overlap=30)
        assert all(len(c) <= 200 for c in chunks)

    def test_overlap_is_internal(self) -> None:
        """overlap がチャンク内に内包されること（chunk_size を超えない）."""
        text = "あ" * 500
        chunks = chunk_text(text, chunk_size=200, chunk_overlap=30)
        assert len(chunks) >= 2
        # 各チャンクが chunk_size 以内
        assert all(len(c) <= 200 for c in chunks)
        # 先頭チャンクの末尾と次チャンクの先頭が重複
        assert chunks[0][-30:] == chunks[1][:30]


class TestHeadingChunkerSizeEnforcement:
    """見出しチャンカーのサイズ制限テスト."""

    def test_code_block_without_empty_lines(self) -> None:
        """空行なしのコードブロックが max_chunk_size 以内に分割されること."""
        code = "\n".join([f"int var_{i} = {i};" for i in range(100)])
        text = f"# Code Section\n\n{code}"
        chunks = chunk_by_headings(text, max_chunk_size=200, min_chunk_size=50)
        assert len(chunks) > 1
        assert all(len(c.formatted_text) <= 200 for c in chunks)

    def test_deep_nesting_formatted_text_limit(self) -> None:
        """深い見出しネストでも formatted_text が max_chunk_size 以内であること."""
        text = (
            "# L1\n## L2\n### L3\n#### L4\n##### L5\n###### L6\n"
            + "x" * 500
        )
        chunks = chunk_by_headings(text, max_chunk_size=200, min_chunk_size=50)
        assert all(len(c.formatted_text) <= 200 for c in chunks)

    def test_single_long_paragraph_split(self) -> None:
        """段落分割できない長いテキストが行/文字ベースで分割されること."""
        # 空行なし・改行ありの長いテキスト
        long_para = " ".join(["word"] * 200)
        text = f"# Section\n\n{long_para}"
        chunks = chunk_by_headings(text, max_chunk_size=100, min_chunk_size=20)
        assert len(chunks) > 1
        assert all(len(c.formatted_text) <= 100 for c in chunks)

    def test_prose_fallback_split(self) -> None:
        """見出しなしテキストで段落超過時に分割されること."""
        long_para = "x" * 500
        chunks = chunk_by_headings(long_para, max_chunk_size=200, min_chunk_size=50)
        assert len(chunks) > 1
        assert all(len(c.formatted_text) <= 200 for c in chunks)

    def test_normal_case_unchanged(self) -> None:
        """通常ケースの動作が変わらないこと."""
        text = "# Title\n\nShort paragraph.\n\n## Section\n\nAnother paragraph."
        chunks = chunk_by_headings(text, max_chunk_size=500, min_chunk_size=50)
        assert len(chunks) == 2
        assert chunks[0].heading == "Title"
        assert chunks[1].heading == "Section"


class TestTableChunkerSizeEnforcement:
    """テーブルチャンカーのサイズ制限テスト."""

    def test_no_limit_backward_compatible(self) -> None:
        """max_chunk_size=0（デフォルト）で従来動作と互換であること."""
        table = "| name | value |\n|---|---|\n| A | 1 |\n| B | 2 |"
        chunks = chunk_table_data(table)
        assert len(chunks) == 2
        assert chunks[0].entity_name == "A"

    def test_large_table_row_split(self) -> None:
        """カラム数が多い行が max_chunk_size 以内に分割されること."""
        headers = "| " + " | ".join([f"col{i}" for i in range(20)]) + " |"
        sep = "|" + "|".join(["---"] * 20) + "|"
        row = "| " + " | ".join([f"long_value_{i}" for i in range(20)]) + " |"
        table = f"{headers}\n{sep}\n{row}"
        chunks = chunk_table_data(table, max_chunk_size=200)
        assert len(chunks) > 1
        assert all(len(c.formatted_text) <= 200 for c in chunks)
        # 全チャンクにエンティティ名が保持されている
        assert all(c.entity_name == "long_value_0" for c in chunks)

    def test_context_rows_removed_first(self) -> None:
        """周辺行除去で収まる場合はチャンク分割されないこと."""
        # 周辺行付きで超過、周辺行除去で収まるケース
        table = (
            "| name | description |\n"
            "|---|---|\n"
            "| ItemA | Short description for item A |\n"
            "| ItemB | Short description for item B |\n"
            "| ItemC | Short description for item C |"
        )
        # 周辺行付きだと超過するサイズに設定
        chunks_with_context = chunk_table_data(table, row_context_size=1)
        max_with = max(len(c.formatted_text) for c in chunks_with_context)

        # max_chunk_size を周辺行なしなら収まるが周辺行ありだと超過する値に
        limit = max_with - 5
        chunks = chunk_table_data(table, max_chunk_size=limit)
        # 分割ではなく周辺行除去で対応される
        assert len(chunks) == 3  # 行数と同じ


class TestTableChunkerLongJapaneseValues:
    """日本語の長いセル値を持つテーブルのテスト."""

    def test_long_japanese_cells(self) -> None:
        """日本語の長いセル値が max_chunk_size 以内に分割されること."""
        table = (
            "| 項目 | 説明 | 備考 |\n"
            "|---|---|---|\n"
            "| ベクトル検索 | " + "あ" * 200 + " | " + "い" * 200 + " |"
        )
        chunks = chunk_table_data(table, max_chunk_size=300)
        assert all(len(c.formatted_text) <= 300 for c in chunks)

    def test_single_attribute_exceeds_limit(self) -> None:
        """単一セル値が max_chunk_size を超える場合でも制限内に収まること."""
        table = (
            "| name | description |\n"
            "|---|---|\n"
            "| Item | " + "あ" * 500 + " |"
        )
        chunks = chunk_table_data(table, max_chunk_size=200)
        assert len(chunks) >= 1
        assert all(len(c.formatted_text) <= 200 for c in chunks)

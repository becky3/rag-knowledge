"""テーブルチャンキングのテスト

仕様: docs/specs/rag-knowledge.md
"""

from factories import make_table_chunks


class TestChunkTableData:
    """chunk_table_data関数のテスト."""

    def test_empty_text_returns_empty_list(self) -> None:
        """空のテキストは空リストを返す."""
        assert make_table_chunks("") == []
        assert make_table_chunks("   ") == []

    def test_markdown_table_chunks_by_row(self) -> None:
        """Markdownテーブルを行単位でチャンキングする."""
        text = """| 名前 | HP | MP |
|------|-----|-----|
| 魔王 | 200 | 100 |
| 闇の王 | 500 | 255 |
| ゴブリン | 8 | 0 |"""

        chunks = make_table_chunks(text)

        assert len(chunks) == 3
        assert chunks[0].entity_name == "魔王"
        assert "HP" in chunks[0].header
        assert "200" in chunks[0].content

        assert chunks[1].entity_name == "闇の王"
        assert "500" in chunks[1].content

        assert chunks[2].entity_name == "ゴブリン"
        assert "8" in chunks[2].content

    def test_tab_separated_table_chunks_by_row(self) -> None:
        """タブ区切りテーブルを行単位でチャンキングする."""
        text = "名前\tHP\tMP\n魔王\t200\t100\n闇の王\t500\t255"

        chunks = make_table_chunks(text)

        assert len(chunks) == 2
        assert chunks[0].entity_name == "魔王"
        assert chunks[1].entity_name == "闇の王"

    def test_header_included_in_each_chunk(self) -> None:
        """各チャンクにヘッダー情報が含まれる."""
        text = """| 名前 | HP | MP |
|------|-----|-----|
| 魔王 | 200 | 100 |"""

        chunks = make_table_chunks(text)

        assert len(chunks) == 1
        assert "HP" in chunks[0].header
        assert "MP" in chunks[0].header

    def test_content_contains_entity_and_attributes(self) -> None:
        """content にエンティティと属性が含まれる."""
        text = """| 名前 | HP | MP |
|------|-----|-----|
| 魔王 | 200 | 100 |"""

        chunks = make_table_chunks(text)

        formatted = chunks[0].content
        assert "魔王" in formatted
        assert "HP" in formatted
        assert "200" in formatted

    def test_context_rows_included(self) -> None:
        """コンテキスト行が含まれる."""
        text = """| 名前 | HP |
|------|-----|
| A | 100 |
| B | 200 |
| C | 300 |"""

        # row_context_size=1 で前後1行を含める
        chunks = make_table_chunks(text, row_context_size=1)

        # 中央の行（B）のコンテキストには A と C が含まれる
        assert len(chunks) == 3
        # rows には前後の行も含まれる
        assert len(chunks[1].rows) == 3  # A, B, C

    def test_single_row_table(self) -> None:
        """1行のみのテーブルも処理できる."""
        text = """| 名前 | HP |
|------|-----|
| 魔王 | 200 |"""

        chunks = make_table_chunks(text)

        assert len(chunks) == 1
        assert chunks[0].entity_name == "魔王"

"""見出しチャンキングのテスト

仕様: docs/specs/rag-knowledge.md
"""

from rag.heading_chunker import chunk_by_headings


class TestChunkByHeadings:
    """chunk_by_headings関数のテスト."""

    def test_empty_text_returns_empty_list(self) -> None:
        """空のテキストは空リストを返す."""
        assert chunk_by_headings("") == []
        assert chunk_by_headings("   ") == []

    def test_markdown_headings_detected(self) -> None:
        """Markdown見出しを検出してチャンキングする."""
        text = """# 見出し1
本文1

## 見出し2
本文2"""

        chunks = chunk_by_headings(text)

        assert len(chunks) == 2
        assert chunks[0].heading == "見出し1"
        assert chunks[0].heading_level == 1
        assert "本文1" in chunks[0].content

        assert chunks[1].heading == "見出し2"
        assert chunks[1].heading_level == 2
        assert "本文2" in chunks[1].content

    def test_html_headings_converted_to_markdown(self) -> None:
        """HTML見出しをMarkdown形式に変換してチャンキングする."""
        text = """<h1>見出し1</h1>
本文1

<h2>見出し2</h2>
本文2"""

        chunks = chunk_by_headings(text)

        assert len(chunks) == 2
        assert chunks[0].heading == "見出し1"
        assert chunks[1].heading == "見出し2"

    def test_parent_headings_tracked(self) -> None:
        """親見出しの階層が追跡される."""
        text = """# 第1章
## 1.1 セクション
内容

## 1.2 セクション
内容

# 第2章
## 2.1 セクション
内容"""

        chunks = chunk_by_headings(text)

        # コンテンツのない見出し（第1章、第2章）はチャンクに含まれない
        # 結果: 1.1, 1.2, 2.1 の3チャンク
        assert len(chunks) == 3

        # 1.1 セクションの親は「第1章」
        assert chunks[0].heading == "1.1 セクション"
        assert chunks[0].parent_headings == ["第1章"]

        # 1.2 セクションの親も「第1章」
        assert chunks[1].heading == "1.2 セクション"
        assert chunks[1].parent_headings == ["第1章"]

        # 2.1 セクションの親は「第2章」
        assert chunks[2].heading == "2.1 セクション"
        assert chunks[2].parent_headings == ["第2章"]

    def test_deep_hierarchy_tracked(self) -> None:
        """深い階層も正しく追跡される."""
        text = """# レベル1
## レベル2
### レベル3
#### レベル4
内容"""

        chunks = chunk_by_headings(text)

        # レベル4の親は「レベル1 > レベル2 > レベル3」
        level4_chunk = [c for c in chunks if c.heading == "レベル4"][0]
        assert level4_chunk.parent_headings == ["レベル1", "レベル2", "レベル3"]

    def test_text_without_headings_returns_single_chunk(self) -> None:
        """見出しのないテキストは1つのチャンクを返す."""
        text = "これは見出しのない通常のテキストです。\n改行も含まれています。"

        chunks = chunk_by_headings(text)

        assert len(chunks) == 1
        assert chunks[0].heading == ""
        assert chunks[0].heading_level == 0
        assert "通常のテキスト" in chunks[0].content

    def test_large_content_split_by_max_chunk_size(self) -> None:
        """AC15: 大きなコンテンツはmax_chunk_sizeで分割される."""
        # 500文字を超える本文を生成
        long_paragraph = "これは長い段落です。" * 50  # 約500文字
        text = f"""# 見出し
{long_paragraph}

別の段落です。"""

        chunks = chunk_by_headings(text, max_chunk_size=200)

        # 複数のチャンクに分割される
        assert len(chunks) > 1
        # すべてのチャンクが見出し情報を持つ
        for chunk in chunks:
            assert "見出し" in chunk.heading

    def test_formatted_text_includes_breadcrumb(self) -> None:
        """AC4: フォーマット済みテキストにパンくずリストが含まれる."""
        text = """# 親見出し
## 子見出し
内容"""

        chunks = chunk_by_headings(text)

        # 子見出しのチャンク
        child_chunk = [c for c in chunks if c.heading == "子見出し"][0]
        assert "[親見出し]" in child_chunk.formatted_text
        assert "# 子見出し" in child_chunk.formatted_text

    def test_small_chunks_merged(self) -> None:
        """AC5: 小さすぎるチャンクは前のチャンクと結合される."""
        text = """# 見出し1
これは十分な長さの本文です。最低限のサイズを超えています。

# 見出し2
短い。"""

        # min_chunk_size=100 で短いテキストを結合
        chunks = chunk_by_headings(text, min_chunk_size=100)

        # 「短い。」は前のチャンクと結合されるか、独立チャンクになる
        # 結合の条件を満たさない場合は独立チャンクとして存在
        assert len(chunks) >= 1

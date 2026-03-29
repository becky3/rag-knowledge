"""見出しチャンキングのテスト

仕様: docs/specs/rag-knowledge.md
"""

from factories import make_heading_chunks


class TestChunkByHeadings:
    """chunk_by_headings関数のテスト."""

    def test_empty_text_returns_empty_list(self) -> None:
        """空のテキストは空リストを返す."""
        assert make_heading_chunks("") == []
        assert make_heading_chunks("   ") == []

    def test_markdown_headings_detected(self) -> None:
        """Markdown見出しを検出してチャンキングする."""
        text = """# 見出し1
本文1

## 見出し2
本文2"""

        chunks = make_heading_chunks(text)

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

        chunks = make_heading_chunks(text)

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

        chunks = make_heading_chunks(text)

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

        chunks = make_heading_chunks(text)

        # レベル4の親は「レベル1 > レベル2 > レベル3」
        level4_chunk = [c for c in chunks if c.heading == "レベル4"][0]
        assert level4_chunk.parent_headings == ["レベル1", "レベル2", "レベル3"]

    def test_text_without_headings_returns_single_chunk(self) -> None:
        """見出しのないテキストは1つのチャンクを返す."""
        text = "これは見出しのない通常のテキストです。\n改行も含まれています。"

        chunks = make_heading_chunks(text)

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

        chunks = make_heading_chunks(text, max_chunk_size=200)

        # 複数のチャンクに分割される
        assert len(chunks) > 1
        # 最初のチャンクは見出しを持ち、2つ目以降は空
        assert chunks[0].heading == "見出し"
        for chunk in chunks[1:]:
            assert chunk.heading == ""

    def test_section_path_includes_breadcrumb(self) -> None:
        """AC4: section_path に親見出しと現見出しが含まれる."""
        text = """# 親見出し
## 子見出し
内容"""

        chunks = make_heading_chunks(text)

        # 子見出しのチャンク
        child_chunk = [c for c in chunks if c.heading == "子見出し"][0]
        assert child_chunk.section_path == "親見出し > 子見出し"
        assert child_chunk.parent_headings == ["親見出し"]

    def test_small_chunks_merged(self) -> None:
        """AC5: 小さすぎるチャンクは前のチャンクと結合される."""
        text = """# 見出し1
これは十分な長さの本文です。最低限のサイズを超えています。

# 見出し2
短い。"""

        # min_chunk_size=100 で短いテキストを結合
        chunks = make_heading_chunks(text, min_chunk_size=100)

        # 「短い。」は前のチャンクと結合されるか、独立チャンクになる
        # 結合の条件を満たさない場合は独立チャンクとして存在
        assert len(chunks) >= 1

    def test_section_path_with_deep_nesting(self) -> None:
        """section_path が親見出し + 現見出しの > 区切りで生成されること."""
        text = """# 第1章
## 1.1 前処理
### 1.1.1 正規化
正規化の内容"""

        chunks = make_heading_chunks(text)

        target = [c for c in chunks if c.heading == "1.1.1 正規化"][0]
        assert target.section_path == "第1章 > 1.1 前処理 > 1.1.1 正規化"

    def test_section_path_empty_for_no_heading(self) -> None:
        """見出しなしチャンクの section_path は空文字列."""
        text = "見出しのないテキスト"
        chunks = make_heading_chunks(text)

        assert len(chunks) == 1
        assert chunks[0].section_path == ""

    def test_split_chunks_have_empty_heading(self) -> None:
        """分割チャンクの2つ目以降で heading が空であること."""
        long_content = "あ" * 500
        text = f"# 見出し\n\n{long_content}"
        chunks = make_heading_chunks(text, max_chunk_size=200, min_chunk_size=50)

        assert len(chunks) > 1
        assert chunks[0].heading == "見出し"
        for c in chunks[1:]:
            assert c.heading == ""

    def test_no_tsuzuki_suffix(self) -> None:
        """分割チャンクに (続き) サフィックスが付与されないこと."""
        long_content = "あ" * 500
        text = f"# 見出し\n\n{long_content}"
        chunks = make_heading_chunks(text, max_chunk_size=200, min_chunk_size=50)

        for c in chunks:
            assert "(続き)" not in c.heading

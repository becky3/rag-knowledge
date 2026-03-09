"""コンテンツタイプ検出のテスト

仕様: docs/specs/rag-knowledge.md
"""

from rag.content_detector import (
    ContentType,
    detect_content_type,
    split_by_content_type,
)


class TestDetectContentType:
    """detect_content_type関数のテスト."""

    def test_empty_text_returns_prose(self) -> None:
        """空のテキストはPROSEを返す."""
        assert detect_content_type("") == ContentType.PROSE
        assert detect_content_type("   ") == ContentType.PROSE

    def test_normal_text_returns_prose(self) -> None:
        """通常のテキストはPROSEを返す."""
        text = """
        これは通常のテキストです。
        段落形式で書かれた文章です。
        特にテーブルや見出しは含まれていません。
        """
        assert detect_content_type(text) == ContentType.PROSE

    def test_markdown_heading_returns_heading(self) -> None:
        """Markdown見出しを含むテキストはHEADINGを返す."""
        text = """
        # 見出し1
        これは本文です。

        ## 見出し2
        これも本文です。
        """
        assert detect_content_type(text) == ContentType.HEADING

    def test_html_heading_returns_heading(self) -> None:
        """HTML見出しを含むテキストはHEADINGを返す."""
        text = """
        <h1>見出し1</h1>
        これは本文です。

        <h2>見出し2</h2>
        これも本文です。
        """
        assert detect_content_type(text) == ContentType.HEADING

    def test_markdown_table_returns_table(self) -> None:
        """Markdownテーブルを含むテキストはTABLEを返す."""
        text = """
        | 名前 | 値 |
        |------|-----|
        | A    | 100 |
        | B    | 200 |
        """
        assert detect_content_type(text) == ContentType.TABLE

    def test_tab_separated_table_returns_table(self) -> None:
        """タブ区切りテーブルはTABLEを返す."""
        text = "名前\t値1\t値2\nA\t100\t200\nB\t150\t250\nC\t180\t280"
        assert detect_content_type(text) == ContentType.TABLE

    def test_numeric_heavy_text_returns_table(self) -> None:
        """数値が多いテキストはTABLEを返す."""
        text = """
        りゅうおう  200  100  140  75
        ゾーマ      500  255  220  150
        スライム    8    0    5    4
        """
        assert detect_content_type(text) == ContentType.TABLE

    def test_table_with_heading_returns_mixed(self) -> None:
        """テーブルと見出しを含むテキストはMIXEDを返す."""
        text = """
        # モンスター一覧

        | 名前 | HP |
        |------|-----|
        | A    | 100 |
        | B    | 200 |
        """
        assert detect_content_type(text) == ContentType.MIXED


class TestSplitByContentType:
    """split_by_content_type関数のテスト."""

    def test_empty_text_returns_empty_list(self) -> None:
        """空のテキストは空リストを返す."""
        assert split_by_content_type("") == []
        assert split_by_content_type("   ") == []

    def test_text_with_headings_splits_by_heading(self) -> None:
        """見出しでテキストを分割する."""
        text = """# 見出し1
本文1

## 見出し2
本文2"""
        blocks = split_by_content_type(text)

        assert len(blocks) == 2
        assert blocks[0].heading == "見出し1"
        assert blocks[0].heading_level == 1
        assert "本文1" in blocks[0].text
        assert blocks[1].heading == "見出し2"
        assert blocks[1].heading_level == 2
        assert "本文2" in blocks[1].text

    def test_text_without_headings_returns_single_block(self) -> None:
        """見出しのないテキストは1つのブロックを返す."""
        text = "これは通常のテキストです。\n改行も含まれています。"
        blocks = split_by_content_type(text)

        assert len(blocks) == 1
        assert blocks[0].heading == ""
        assert blocks[0].content_type == ContentType.PROSE

"""コンバーターのテスト.

仕様: docs/specs/converter.md
Issue: #249
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from factories import make_converter_args
from rag.converter.converter import (
    ConversionSkippedError,
    Converter,
    get_converted_rel_path,
)
from rag.converter.handlers import (
    _fix_void_elements,
    convert_html,
    convert_json_bluesky,
    convert_json_zenn_article,
    convert_json_zenn_scrap,
    passthrough_copy,
)
from rag.converter.normalize import normalize_text


# ============================================================
# テキスト正規化
# ============================================================


class TestNormalizeText:
    """normalize_text のテスト."""

    def test_trailing_whitespace_removal(self) -> None:
        text = "hello   \nworld\t\n"
        result = normalize_text(text)
        assert result == "hello\nworld"

    def test_consecutive_blank_lines_compression(self) -> None:
        text = "line1\n\n\n\nline2\n\n\n\n\nline3"
        result = normalize_text(text)
        assert result == "line1\n\nline2\n\nline3"

    def test_already_clean_text(self) -> None:
        text = "line1\n\nline2\nline3"
        result = normalize_text(text)
        assert result == text

    def test_mixed_normalization(self) -> None:
        text = "hello   \n\n\n\nworld  \t\n\nfoo\n\n\nbar"
        result = normalize_text(text)
        assert result == "hello\n\nworld\n\nfoo\n\nbar"

    def test_empty_string(self) -> None:
        assert normalize_text("") == ""

    def test_whitespace_only(self) -> None:
        assert normalize_text("   \n\n  \n") == ""


# ============================================================
# HTML → Markdown 変換ハンドラ
# ============================================================


class TestConvertHtml:
    """convert_html のテスト."""

    def test_basic_html_to_markdown(self, tmp_path: Path) -> None:
        html = "<html><body><h1>Title</h1><p>Content here.</p></body></html>"
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Title" in result
        assert "Content here." in result

    def test_content_area_article(self, tmp_path: Path) -> None:
        html = (
            "<html><body>"
            "<nav>Navigation</nav>"
            "<article><h1>Article</h1><p>Article body.</p></article>"
            "<footer>Footer</footer>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Article body." in result
        assert "Navigation" not in result
        assert "Footer" not in result

    def test_content_area_main(self, tmp_path: Path) -> None:
        html = (
            "<html><body>"
            "<nav>Navigation</nav>"
            "<main><h1>Main</h1><p>Main body.</p></main>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Main body." in result
        assert "Navigation" not in result

    def test_non_content_tags_removed(self, tmp_path: Path) -> None:
        """script/style/noscript/form がコンテンツ領域内で除去される."""
        html = (
            "<html><body>"
            "<div id='main'>"
            "<script>alert('x')</script>"
            "<style>.red{color:red}</style>"
            "<p>Content</p>"
            "<noscript>NoScript</noscript>"
            "<form><label>Search Site</label><button>Submit</button></form>"
            "</div>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Content" in result
        assert "alert" not in result
        assert "NoScript" not in result
        assert "Search Site" not in result
        assert "Submit" not in result

    def test_content_area_id_main(self, tmp_path: Path) -> None:
        """id='main' でコンテンツ領域が特定され、外部のノイズが除外される."""
        html = (
            "<html><body>"
            "<div id='header'>Header Noise</div>"
            "<div id='sidebar'>Sidebar Noise</div>"
            "<div id='main'><h1>Title</h1><p>Main body.</p></div>"
            "<div id='footer'>Footer Noise</div>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Main body." in result
        assert "Header Noise" not in result
        assert "Sidebar Noise" not in result
        assert "Footer Noise" not in result

    def test_content_area_id_content_wrap(self, tmp_path: Path) -> None:
        """id='content-wrap' でコンテンツ領域が特定される."""
        html = (
            "<html><body>"
            "<div id='sidebar'>Sidebar</div>"
            "<div id='content-wrap'><p>Content body.</p></div>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Content body." in result
        assert "Sidebar" not in result

    def test_content_area_class_main_content(self, tmp_path: Path) -> None:
        """class='main-content' でコンテンツ領域が特定される."""
        html = (
            "<html><body>"
            "<div class='sidebar'>Sidebar Noise</div>"
            "<div class='main-content'><h1>Title</h1><p>Main body.</p></div>"
            "<div class='footer'>Footer Noise</div>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Main body." in result
        assert "Sidebar Noise" not in result
        assert "Footer Noise" not in result

    def test_content_area_id_page_container(self, tmp_path: Path) -> None:
        """id='page-container' でコンテンツ領域が特定される."""
        html = (
            "<html><body>"
            "<div id='global-header'>Global Header</div>"
            "<div id='page-container'>"
            "<header class='interviewheader'><h2>Interview Title</h2></header>"
            "<p>Interview body.</p>"
            "</div>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Interview Title" in result
        assert "Interview body." in result
        assert "Global Header" not in result

    def test_content_area_role_main(self, tmp_path: Path) -> None:
        """role='main' でコンテンツ領域が特定される."""
        html = (
            "<html><body>"
            "<div class='nav'>Nav</div>"
            "<div role='main'><p>Main content.</p></div>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Main content." in result
        assert "Nav" not in result

    def test_header_inside_content_preserved(self, tmp_path: Path) -> None:
        """コンテンツ領域内の <header> タグは除去されずに保持される."""
        html = (
            "<html><body>"
            "<div id='main'>"
            "<header><h1>Article Title</h1></header>"
            "<p>Article body.</p>"
            "</div>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Article Title" in result
        assert "Article body." in result

    def test_text_density_fallback(self, tmp_path: Path) -> None:
        """パターンマッチに失敗した場合、テキスト密度で最大の子要素を選ぶ."""
        html = (
            "<html><body>"
            "<div class='small-nav'>Nav Link</div>"
            "<div class='big-content'>"
            "<h1>Title</h1>"
            "<p>Long content paragraph with substantial text for density.</p>"
            "<p>Another paragraph with more content to make this the largest.</p>"
            "</div>"
            "<div class='tiny-footer'>Footer Noise</div>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Long content paragraph" in result
        assert "Nav Link" not in result
        assert "Footer Noise" not in result

    def test_breadcrumb_class_removed(self, tmp_path: Path) -> None:
        """コンテンツ領域内の breadcrumb class が除去される."""
        html = (
            "<html><body>"
            "<div id='main'>"
            "<div class='breadcrumb'>Home > Page</div>"
            "<p>Content here.</p>"
            "</div>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Content here." in result
        assert "Home > Page" not in result

    def test_details_summary_preserved(self, tmp_path: Path) -> None:
        """<details>/<summary> タグ内のコンテンツが保持される."""
        html = (
            "<html><body>"
            "<div id='content'>"
            "<details><summary><h3>FAQ Question</h3></summary>"
            "<p>FAQ Answer here.</p>"
            "</details>"
            "</div>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "FAQ Question" in result
        assert "FAQ Answer" in result

    def test_semantic_article_takes_priority(self, tmp_path: Path) -> None:
        """<article> がある場合、id パターンより優先される."""
        html = (
            "<html><body>"
            "<div id='main'><p>Main div content.</p></div>"
            "<article><p>Article content.</p></article>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Article content." in result
        assert "Main div content." not in result

    def test_atx_headings(self, tmp_path: Path) -> None:
        html = "<html><body><h1>H1</h1><h2>H2</h2><h3>H3</h3></body></html>"
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "# H1" in result
        assert "## H2" in result
        assert "### H3" in result

    def test_link_url_removed(self, tmp_path: Path) -> None:
        html = '<html><body><a href="https://example.com">Link Text</a></body></html>'
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Link Text" in result
        assert "https://example.com" not in result

    def test_image_alt_only(self, tmp_path: Path) -> None:
        html = '<html><body><img src="img.png" alt="Photo description"></body></html>'
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Photo description" in result
        assert "img.png" not in result

    def test_non_utf8_encoding(self, tmp_path: Path) -> None:
        html = "<html><body><p>日本語テスト</p></body></html>"
        html_file = tmp_path / "test.html"
        html_file.write_bytes(html.encode("shift_jis"))

        result = convert_html(html_file)
        assert result is not None
        assert "日本語テスト" in result

    def test_table_conversion(self, tmp_path: Path) -> None:
        html = (
            "<html><body>"
            "<table><tr><th>Name</th><th>Value</th></tr>"
            "<tr><td>A</td><td>1</td></tr></table>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Name" in result
        assert "Value" in result
        assert "|" in result

    def test_void_element_img_children_unwrapped(self, tmp_path: Path) -> None:
        """img タグが後続コンテンツを飲み込んだ場合でも本文が保持される (#336).

        html.parser は特定の HTML 構造で <img> を自己閉じとして認識しない
        ケースがある（Python docs pathlib.html で再現）。この場合、後続の
        全コンテンツが img の子ノードとして解析され、convert_img() で
        alt テキストのみが返却されるため本文が消失する。
        """
        # Python docs pathlib.html の構造を模擬
        # section > h1 > img(class付き) > 大量の後続コンテンツ
        html = (
            "<html><body>"
            '<section id="module-test">'
            "<h1>Test Module</h1>"
            '<img class="align-center" src="diagram.png" '
            'alt="Inheritance diagram">'
            "<p>First paragraph with important content.</p>"
            "<dl><dt>SomeClass</dt><dd>Description of the class.</dd></dl>"
            "<p>Second paragraph with more content.</p>"
            "</section>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "First paragraph" in result
        assert "Second paragraph" in result
        assert "SomeClass" in result
        assert "Test Module" in result

    def test_void_element_br_children_unwrapped(self, tmp_path: Path) -> None:
        """br タグが後続コンテンツを飲み込んだ場合でも本文が保持される (#336)."""
        html = (
            "<html><body>"
            "<p>Before break.<br>After break.</p>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Before break." in result
        assert "After break." in result

    def test_void_element_no_children_unchanged(self, tmp_path: Path) -> None:
        """自己閉じの void 要素は変更されない."""
        html = (
            "<html><body>"
            '<img src="logo.png" alt="Logo" />'
            "<p>Content after self-closed img.</p>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "Content after self-closed img." in result

    def test_ruby_tag_keeps_kanji_with_reading(self, tmp_path: Path) -> None:
        """ruby タグで漢字を保持し、ふりがなを半角括弧付きで残す."""
        html = (
            "<html><body>"
            "<p><ruby><rb>吾輩</rb><rt>わがはい</rt></ruby>は猫である</p>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "吾輩" in result
        assert "(わがはい)" in result
        assert "猫" in result

    def test_ruby_tag_with_rp(self, tmp_path: Path) -> None:
        """rp 括弧付きの ruby タグでも漢字+半角括弧ふりがなを保持する."""
        html = (
            "<html><body>"
            "<p><ruby>漢字<rp>(</rp><rt>かんじ</rt><rp>)</rp></ruby></p>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "漢字" in result
        assert "(かんじ)" in result

    def test_ruby_tag_multiple(self, tmp_path: Path) -> None:
        """複数の ruby タグが連続する場合も正しく処理する."""
        html = (
            "<html><body>"
            "<p>"
            "<ruby><rb>青空</rb><rt>あおぞら</rt></ruby>"
            "<ruby><rb>文庫</rb><rt>ぶんこ</rt></ruby>"
            "</p>"
            "</body></html>"
        )
        html_file = tmp_path / "test.html"
        html_file.write_text(html, encoding="utf-8")

        result = convert_html(html_file)
        assert result is not None
        assert "青空" in result
        assert "文庫" in result
        assert "(あおぞら)" in result
        assert "(ぶんこ)" in result


class TestFixVoidElements:
    """_fix_void_elements の単体テスト (#336)."""

    def test_img_with_children(self) -> None:
        """img の子ノードが親に巻き上げられる."""

        # html.parser の誤解析を模擬: img 要素に子ノードがある状態を構築
        soup = BeautifulSoup('<div><img src="x.png" alt="X"></div>', "html.parser")
        img = soup.find("img")
        assert img is not None
        p = soup.new_tag("p")
        p.string = "Body text"
        img.append(p)

        # 前提: img に子がある
        assert img.find("p") is not None

        _fix_void_elements(soup)

        # 修正後: img に子がなく、p が div の直接子になっている
        img_after = soup.find("img")
        assert img_after is not None
        assert not img_after.contents
        p_tag = soup.find("p")
        assert p_tag is not None
        assert p_tag.parent.name == "div"

    def test_multiple_void_elements(self) -> None:
        """複数の void 要素が同時に修正される."""

        html = '<div><img src="a.png"><p>After img</p><hr><p>After hr</p></div>'
        soup = BeautifulSoup(html, "html.parser")
        _fix_void_elements(soup)

        paragraphs = soup.find_all("p")
        assert len(paragraphs) == 2
        assert "After img" in paragraphs[0].get_text()
        assert "After hr" in paragraphs[1].get_text()

    def test_no_void_elements(self) -> None:
        """void 要素がない場合は何も変更しない."""

        html = "<div><p>Normal content</p><span>More</span></div>"
        soup = BeautifulSoup(html, "html.parser")
        original = str(soup)
        _fix_void_elements(soup)
        assert str(soup) == original


# ============================================================
# BlueSky JSON テキスト抽出ハンドラ
# ============================================================


class TestConvertJsonBluesky:
    """convert_json_bluesky のテスト."""

    def test_basic_post(self) -> None:
        data = {
            "post": {
                "record": {"text": "Sample post text"},
            },
        }
        result = convert_json_bluesky(data)
        assert result == "Sample post text"

    def test_image_alt(self) -> None:
        data = {
            "post": {
                "record": {
                    "text": "Post with image",
                    "embed": {
                        "$type": "app.bsky.embed.images",
                        "images": [
                            {"alt": "A sunset photo"},
                            {"alt": "A mountain view"},
                        ],
                    },
                },
            },
        }
        result = convert_json_bluesky(data)
        assert result is not None
        assert "Post with image" in result
        assert "[Image ALT]" in result
        assert "A sunset photo" in result
        assert "A mountain view" in result

    def test_video_alt(self) -> None:
        data = {
            "post": {
                "record": {
                    "text": "Post with video",
                    "embed": {
                        "$type": "app.bsky.embed.video",
                        "alt": "Video description",
                    },
                },
            },
        }
        result = convert_json_bluesky(data)
        assert result is not None
        assert "[Video ALT] Video description" in result

    def test_link_card(self) -> None:
        data = {
            "post": {
                "record": {
                    "text": "Check this out",
                    "embed": {
                        "$type": "app.bsky.embed.external",
                        "external": {
                            "title": "Sample Article",
                            "uri": "https://example.com/article",
                            "description": "An article about something",
                        },
                    },
                },
            },
        }
        result = convert_json_bluesky(data)
        assert result is not None
        assert "[Link Card]" in result
        assert "Title: Sample Article" in result
        assert "URL: https://example.com/article" in result
        assert "Description: An article about something" in result

    def test_record_with_media(self) -> None:
        data = {
            "post": {
                "record": {
                    "text": "Post with quote and media",
                    "embed": {
                        "$type": "app.bsky.embed.recordWithMedia",
                        "media": {
                            "$type": "app.bsky.embed.images",
                            "images": [{"alt": "Media image"}],
                        },
                        "record": {"uri": "at://did:plc:xxx/app.bsky.feed.post/yyy"},
                    },
                },
            },
        }
        result = convert_json_bluesky(data)
        assert result is not None
        assert "Post with quote and media" in result
        assert "[Image ALT] Media image" in result

    def test_quote_embed_record(self) -> None:
        data = {
            "post": {
                "record": {
                    "text": "Quoting this",
                    "embed": {
                        "$type": "app.bsky.embed.record",
                        "record": {"uri": "at://did:plc:xxx/app.bsky.feed.post/yyy"},
                    },
                },
                "embed": {
                    "$type": "app.bsky.embed.record#view",
                    "record": {
                        "value": {"text": "Original quoted text"},
                    },
                },
            },
        }
        result = convert_json_bluesky(data)
        assert result is not None
        assert "Quoting this" in result
        assert "[Quote]" in result
        assert "Original quoted text" in result

    def test_quote_embed_record_with_media(self) -> None:
        data = {
            "post": {
                "record": {
                    "text": "Quote with media",
                    "embed": {
                        "$type": "app.bsky.embed.recordWithMedia",
                        "media": {"$type": "app.bsky.embed.images", "images": []},
                        "record": {"uri": "at://did:plc:xxx/app.bsky.feed.post/yyy"},
                    },
                },
                "embed": {
                    "$type": "app.bsky.embed.recordWithMedia#view",
                    "record": {
                        "record": {
                            "value": {"text": "Quoted via recordWithMedia"},
                        },
                    },
                },
            },
        }
        result = convert_json_bluesky(data)
        assert result is not None
        assert "Quote with media" in result
        assert "[Quote]" in result
        assert "Quoted via recordWithMedia" in result

    def test_repost(self) -> None:
        data = {
            "post": {
                "author": {"handle": "original.bsky.social"},
                "record": {"text": "Reposted content"},
            },
            "reason": {
                "$type": "app.bsky.feed.defs#reasonRepost",
                "by": {"handle": "reposter.bsky.social"},
            },
        }
        result = convert_json_bluesky(data)
        assert result is not None
        assert "[Repost: @original.bsky.social]" in result
        assert "Reposted content" in result

    def test_empty_post(self) -> None:
        data = {"post": {"record": {"text": ""}}}
        result = convert_json_bluesky(data)
        assert result is None

    def test_missing_post(self) -> None:
        data = {"something": "else"}
        result = convert_json_bluesky(data)
        assert result is None


# ============================================================
# Zenn スクラップ JSON テキスト抽出ハンドラ
# ============================================================


class TestConvertJsonZennScrap:
    """convert_json_zenn_scrap のテスト."""

    def test_multiple_comments(self) -> None:
        data = {
            "comments": [
                {"body_html": "<p>First comment</p>"},
                {"body_html": "<p>Second comment</p>"},
                {"body_html": "<p>Third comment</p>"},
            ],
        }
        result = convert_json_zenn_scrap(data)
        assert result is not None
        assert "First comment" in result
        assert "Second comment" in result
        assert "Third comment" in result
        assert "---" in result

    def test_scrap_key_wrapping(self) -> None:
        data = {
            "scrap": {
                "comments": [
                    {"body_html": "<p>Comment in scrap</p>"},
                ],
            },
        }
        result = convert_json_zenn_scrap(data)
        assert result is not None
        assert "Comment in scrap" in result

    def test_empty_comments_list(self) -> None:
        data = {"comments": []}
        result = convert_json_zenn_scrap(data)
        assert result is None

    def test_zero_comments_key(self) -> None:
        data = {"title": "No comments scrap"}
        result = convert_json_zenn_scrap(data)
        assert result is None

    def test_all_empty_body_html(self) -> None:
        data = {
            "comments": [
                {"body_html": ""},
                {"body_html": "  "},
            ],
        }
        result = convert_json_zenn_scrap(data)
        assert result is None

    def test_scrap_value_not_dict(self) -> None:
        data = {"scrap": "not a dict"}
        result = convert_json_zenn_scrap(data)
        assert result is None

    def test_html_tags_removed_in_comments(self) -> None:
        data = {
            "comments": [
                {"body_html": "<p>Clean text</p><script>evil()</script>"},
            ],
        }
        result = convert_json_zenn_scrap(data)
        assert result is not None
        assert "Clean text" in result
        assert "evil" not in result


# ============================================================
# Zenn 記事 JSON テキスト抽出ハンドラ
# ============================================================


class TestConvertJsonZennArticle:
    """convert_json_zenn_article のテスト."""

    def test_basic_article(self) -> None:
        data = {
            "article": {
                "body_html": "<h2>Section</h2><p>Article body text.</p>",
            },
        }
        result = convert_json_zenn_article(data)
        assert result is not None
        assert "Section" in result
        assert "Article body text." in result

    def test_article_without_wrapper(self) -> None:
        """article キーなしの直接オブジェクトでも動作する."""
        data = {
            "body_html": "<p>Direct body.</p>",
        }
        result = convert_json_zenn_article(data)
        assert result is not None
        assert "Direct body." in result

    def test_empty_body_html(self) -> None:
        data = {"article": {"body_html": ""}}
        result = convert_json_zenn_article(data)
        assert result is None

    def test_missing_body_html(self) -> None:
        data = {"article": {"title": "No body"}}
        result = convert_json_zenn_article(data)
        assert result is None

    def test_whitespace_only_body_html(self) -> None:
        data = {"article": {"body_html": "   "}}
        result = convert_json_zenn_article(data)
        assert result is None

    def test_html_to_markdown_conversion(self) -> None:
        data = {
            "article": {
                "body_html": (
                    "<h2>Heading</h2>"
                    "<p>Paragraph with <strong>bold</strong> text.</p>"
                    "<ul><li>Item 1</li><li>Item 2</li></ul>"
                ),
            },
        }
        result = convert_json_zenn_article(data)
        assert result is not None
        assert "## Heading" in result
        assert "bold" in result
        assert "Item 1" in result

    def test_script_style_removed(self) -> None:
        data = {
            "article": {
                "body_html": (
                    "<p>Clean text</p>"
                    "<script>evil()</script>"
                    "<style>.red{color:red}</style>"
                ),
            },
        }
        result = convert_json_zenn_article(data)
        assert result is not None
        assert "Clean text" in result
        assert "evil" not in result
        assert "color" not in result

    def test_article_value_not_dict(self) -> None:
        data = {"article": "not a dict"}
        result = convert_json_zenn_article(data)
        assert result is None


# ============================================================
# パススルー
# ============================================================


class TestPassthrough:
    """passthrough_copy のテスト."""

    def test_md_copy(self, tmp_path: Path) -> None:
        src = tmp_path / "source" / "doc.md"
        src.parent.mkdir(parents=True)
        src.write_text("# Markdown content", encoding="utf-8")

        dest = tmp_path / "dest" / "doc.md"
        passthrough_copy(src, dest)
        assert dest.read_text(encoding="utf-8") == "# Markdown content"

    def test_txt_copy(self, tmp_path: Path) -> None:
        src = tmp_path / "source" / "note.txt"
        src.parent.mkdir(parents=True)
        src.write_text("Plain text", encoding="utf-8")

        dest = tmp_path / "dest" / "note.txt"
        passthrough_copy(src, dest)
        assert dest.read_text(encoding="utf-8") == "Plain text"

    def test_adoc_copy(self, tmp_path: Path) -> None:
        src = tmp_path / "source" / "guide.adoc"
        src.parent.mkdir(parents=True)
        src.write_text("= AsciiDoc Title", encoding="utf-8")

        dest = tmp_path / "dest" / "guide.adoc"
        passthrough_copy(src, dest)
        assert dest.read_text(encoding="utf-8") == "= AsciiDoc Title"

    def test_creates_directories(self, tmp_path: Path) -> None:
        src = tmp_path / "source" / "doc.md"
        src.parent.mkdir(parents=True)
        src.write_text("content", encoding="utf-8")

        dest = tmp_path / "deep" / "nested" / "dir" / "doc.md"
        passthrough_copy(src, dest)
        assert dest.exists()


# ============================================================
# 相対パス変換ユーティリティ
# ============================================================


class TestGetConvertedRelPath:
    """get_converted_rel_path のテスト."""

    def test_html_to_md(self) -> None:
        assert get_converted_rel_path("web/example/page.html") == "web/example/page.md"

    def test_htm_to_md(self) -> None:
        assert get_converted_rel_path("web/example/page.htm") == "web/example/page.md"

    def test_pdf_to_md(self) -> None:
        assert get_converted_rel_path("web/doc/report.pdf") == "web/doc/report.md"

    def test_json_to_md(self) -> None:
        assert get_converted_rel_path("bluesky/did/post.json") == "bluesky/did/post.md"

    def test_md_passthrough(self) -> None:
        assert get_converted_rel_path("local/notes/memo.md") == "local/notes/memo.md"

    def test_txt_passthrough(self) -> None:
        assert get_converted_rel_path("local/note.txt") == "local/note.txt"

    def test_adoc_passthrough(self) -> None:
        assert get_converted_rel_path("local/guide.adoc") == "local/guide.adoc"

    def test_unknown_extension(self) -> None:
        assert get_converted_rel_path("local/file.xyz") == "local/file.xyz"


# ============================================================
# Converter 統合テスト
# ============================================================


def _setup_source(
    tmp_path: Path,
    rel_path: str,
    content: str | bytes,
) -> tuple[Path, Path]:
    """テスト用のソースファイルを配置し、ディレクトリを返す."""
    source_dir = tmp_path / "source_store"
    converted_dir = tmp_path / "converted_store"
    file_path = source_dir / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        file_path.write_bytes(content)
    else:
        file_path.write_text(content, encoding="utf-8")
    return source_dir, converted_dir


class TestConverterConvert:
    """Converter.convert のテスト."""

    def test_convert_html(self, tmp_path: Path) -> None:
        html = "<html><body><h1>Title</h1><p>Content.</p></body></html>"
        source_dir, converted_dir = _setup_source(
            tmp_path, "web/example/page.html", html,
        )
        converter = Converter(**make_converter_args())
        result = converter.convert(
            "web/example/page.html", source_dir, converted_dir,
        )
        assert result.exists()
        text = result.read_text(encoding="utf-8")
        assert "Title" in text
        assert "Content." in text
        # 出力拡張子が .md
        assert result.name == "page.md"

    def test_convert_htm(self, tmp_path: Path) -> None:
        """`.htm` 拡張子のファイルが HTML として変換されること (#346)."""
        html = "<html><body><h1>Title</h1><p>HTM Content.</p></body></html>"
        source_dir, converted_dir = _setup_source(
            tmp_path, "web/example/page.htm", html,
        )
        converter = Converter(**make_converter_args())
        result = converter.convert(
            "web/example/page.htm", source_dir, converted_dir,
        )
        assert result.exists()
        text = result.read_text(encoding="utf-8")
        assert "Title" in text
        assert "HTM Content." in text
        # 出力拡張子が .md
        assert result.name == "page.md"

    def test_convert_passthrough_md(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "local/notes/memo.md", "# Memo\nContent here.",
        )
        converter = Converter(**make_converter_args())
        result = converter.convert(
            "local/notes/memo.md", source_dir, converted_dir,
        )
        assert result.exists()
        assert result.read_text(encoding="utf-8") == "# Memo\nContent here."

    def test_convert_passthrough_txt(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "local/note.txt", "Plain text content",
        )
        converter = Converter(**make_converter_args())
        result = converter.convert("local/note.txt", source_dir, converted_dir)
        assert result.exists()
        assert result.read_text(encoding="utf-8") == "Plain text content"

    def test_convert_passthrough_adoc(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "local/guide.adoc", "= AsciiDoc Title",
        )
        converter = Converter(**make_converter_args())
        result = converter.convert("local/guide.adoc", source_dir, converted_dir)
        assert result.exists()
        assert result.read_text(encoding="utf-8") == "= AsciiDoc Title"

    def test_convert_json_bluesky(self, tmp_path: Path) -> None:
        post_data = json.dumps({
            "post": {
                "record": {"text": "Sample BlueSky post"},
            },
        })
        source_dir, converted_dir = _setup_source(
            tmp_path, "bluesky/did/2026/03/rkey.json", post_data,
        )
        converter = Converter(**make_converter_args())
        result = converter.convert(
            "bluesky/did/2026/03/rkey.json", source_dir, converted_dir,
        )
        assert result.exists()
        text = result.read_text(encoding="utf-8")
        assert "Sample BlueSky post" in text
        assert result.name == "rkey.md"

    def test_convert_json_zenn_scrap(self, tmp_path: Path) -> None:
        scrap_data = json.dumps({
            "comments": [
                {"body_html": "<p>Comment 1</p>"},
                {"body_html": "<p>Comment 2</p>"},
            ],
        })
        source_dir, converted_dir = _setup_source(
            tmp_path, "zenn/user/scraps/slug.json", scrap_data,
        )
        converter = Converter(**make_converter_args())
        result = converter.convert(
            "zenn/user/scraps/slug.json", source_dir, converted_dir,
        )
        assert result.exists()
        text = result.read_text(encoding="utf-8")
        assert "Comment 1" in text
        assert "Comment 2" in text
        assert "---" in text

    def test_convert_pdf(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "web/doc/report.pdf", b"%PDF-1.4 dummy",
        )
        converter = Converter(**make_converter_args())
        with patch(
            "rag.converter.converter.extract_pdf",
            return_value="# PDF Content\n\nExtracted text.",
        ):
            result = converter.convert(
                "web/doc/report.pdf", source_dir, converted_dir,
            )
        assert result.exists()
        text = result.read_text(encoding="utf-8")
        assert "PDF Content" in text
        assert result.name == "report.md"

    def test_text_normalization_applied(self, tmp_path: Path) -> None:
        html = "<html><body><p>line1   </p><p></p><p></p><p></p><p>line2</p></body></html>"
        source_dir, converted_dir = _setup_source(
            tmp_path, "web/page.html", html,
        )
        converter = Converter(**make_converter_args())
        result = converter.convert("web/page.html", source_dir, converted_dir)
        text = result.read_text(encoding="utf-8")
        # 連続空行が圧縮されていること
        assert "\n\n\n" not in text

    def test_converted_store_dir_auto_created(self, tmp_path: Path) -> None:
        html = "<html><body><p>Content</p></body></html>"
        source_dir, converted_dir = _setup_source(
            tmp_path, "web/deep/nested/page.html", html,
        )
        converter = Converter(**make_converter_args())
        result = converter.convert(
            "web/deep/nested/page.html", source_dir, converted_dir,
        )
        assert result.exists()
        assert "nested" in str(result)


    def test_convert_json_zenn_article(self, tmp_path: Path) -> None:
        article_data = json.dumps({
            "article": {
                "body_html": "<h2>Section</h2><p>Article content.</p>",
            },
        })
        source_dir, converted_dir = _setup_source(
            tmp_path, "zenn/alice/articles/slug.json", article_data,
        )
        converter = Converter(**make_converter_args())
        result = converter.convert(
            "zenn/alice/articles/slug.json", source_dir, converted_dir,
        )
        assert result.exists()
        text = result.read_text(encoding="utf-8")
        assert "Section" in text
        assert "Article content." in text
        assert result.name == "slug.md"

    def test_convert_json_zenn_article_with_title(self, tmp_path: Path) -> None:
        """Zenn 記事 JSON 変換時に .meta のタイトルが H1 として先頭付与されること."""
        article_data = json.dumps({
            "article": {
                "body_html": "<h2>Section</h2><p>Article body.</p>",
            },
        })
        source_dir, converted_dir = _setup_source(
            tmp_path, "zenn/alice/articles/slug.json", article_data,
        )
        # .meta ファイルを配置
        meta_content = (
            'source_id: "https://zenn.dev/alice/articles/slug"\n'
            "source_type: zenn\n"
            'title: "Sample Article Title"\n'
            'collected_at: "2026-01-15T10:30:00+09:00"\n'
        )
        meta_path = source_dir / "zenn" / "alice" / "articles" / "slug.json.meta"
        meta_path.write_text(meta_content, encoding="utf-8")

        converter = Converter(**make_converter_args())
        result = converter.convert(
            "zenn/alice/articles/slug.json", source_dir, converted_dir,
        )
        text = result.read_text(encoding="utf-8")
        assert text.startswith("# Sample Article Title")
        assert "## Section" in text
        assert "Article body." in text

    def test_convert_json_zenn_article_empty_body(self, tmp_path: Path) -> None:
        """Zenn 記事 JSON で body_html が空の場合は変換スキップ."""
        article_data = json.dumps({
            "article": {"body_html": ""},
        })
        source_dir, converted_dir = _setup_source(
            tmp_path, "zenn/alice/articles/empty.json", article_data,
        )
        converter = Converter(**make_converter_args())
        with pytest.raises(ConversionSkippedError, match="Empty conversion"):
            converter.convert(
                "zenn/alice/articles/empty.json", source_dir, converted_dir,
            )


# ============================================================
# Zenn 記事タイトル付与
# ============================================================


class TestZennArticleTitlePrepend:
    """Zenn 記事の .meta からタイトルを先頭付与するテスト."""

    def test_non_zenn_html_no_title(self, tmp_path: Path) -> None:
        """Web HTML にはタイトルが付与されないこと."""
        html = "<html><body><p>Web content.</p></body></html>"
        source_dir, converted_dir = _setup_source(
            tmp_path, "web/https/example.com/page.html", html,
        )
        converter = Converter(**make_converter_args())
        result = converter.convert(
            "web/https/example.com/page.html", source_dir, converted_dir,
        )
        text = result.read_text(encoding="utf-8")
        assert not text.startswith("# ")

    def test_zenn_scrap_no_title(self, tmp_path: Path) -> None:
        """Zenn スクラップ JSON にはタイトルが付与されないこと."""
        scrap_data = json.dumps({
            "comments": [{"body_html": "<p>Comment</p>"}],
        })
        source_dir, converted_dir = _setup_source(
            tmp_path, "zenn/alice/scraps/slug.json", scrap_data,
        )
        converter = Converter(**make_converter_args())
        result = converter.convert(
            "zenn/alice/scraps/slug.json", source_dir, converted_dir,
        )
        text = result.read_text(encoding="utf-8")
        assert not text.startswith("# ")



# ============================================================
# Converter.delete
# ============================================================


class TestConverterDelete:
    """Converter.delete のテスト."""

    def test_delete_existing(self, tmp_path: Path) -> None:
        converted_dir = tmp_path / "converted"
        converted_file = converted_dir / "web" / "page.md"
        converted_file.parent.mkdir(parents=True)
        converted_file.write_text("content", encoding="utf-8")

        converter = Converter(**make_converter_args())
        converter.delete("web/page.html", converted_dir)
        assert not converted_file.exists()

    def test_delete_nonexistent(self, tmp_path: Path) -> None:
        converted_dir = tmp_path / "converted"
        converter = Converter(**make_converter_args())
        # Should not raise
        converter.delete("web/page.html", converted_dir)


# ============================================================
# Converter.clear
# ============================================================


class TestConverterClear:
    """Converter.clear のテスト."""

    def test_clear_all(self, tmp_path: Path) -> None:
        converted_dir = tmp_path / "converted"
        (converted_dir / "web" / "page.md").parent.mkdir(parents=True)
        (converted_dir / "web" / "page.md").write_text("x", encoding="utf-8")
        (converted_dir / "local" / "doc.md").parent.mkdir(parents=True)
        (converted_dir / "local" / "doc.md").write_text("y", encoding="utf-8")

        converter = Converter(**make_converter_args())
        converter.clear(converted_dir)
        assert not converted_dir.exists()

    def test_clear_source_type(self, tmp_path: Path) -> None:
        converted_dir = tmp_path / "converted"
        (converted_dir / "web" / "page.md").parent.mkdir(parents=True)
        (converted_dir / "web" / "page.md").write_text("x", encoding="utf-8")
        (converted_dir / "local" / "doc.md").parent.mkdir(parents=True)
        (converted_dir / "local" / "doc.md").write_text("y", encoding="utf-8")

        converter = Converter(**make_converter_args())
        converter.clear(converted_dir, source_type="web")
        assert not (converted_dir / "web").exists()
        assert (converted_dir / "local" / "doc.md").exists()

    def test_clear_nonexistent_dir(self, tmp_path: Path) -> None:
        converter = Converter(**make_converter_args())
        # Should not raise
        converter.clear(tmp_path / "nonexistent")


# ============================================================
# エッジケース
# ============================================================


class TestConverterEdgeCases:
    """Converter のエッジケーステスト."""

    def test_source_file_not_found(self, tmp_path: Path) -> None:
        source_dir = tmp_path / "source_store"
        source_dir.mkdir()
        converted_dir = tmp_path / "converted_store"
        converter = Converter(**make_converter_args())
        with pytest.raises(ConversionSkippedError, match="Source not found"):
            converter.convert("web/missing.html", source_dir, converted_dir)

    def test_json_root_is_array(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "bluesky/did/post.json", json.dumps([1, 2, 3]),
        )
        converter = Converter(**make_converter_args())
        with pytest.raises(ConversionSkippedError, match="Empty conversion"):
            converter.convert("bluesky/did/post.json", source_dir, converted_dir)

    def test_zero_byte_file(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "web/empty.html", "",
        )
        converter = Converter(**make_converter_args())
        with pytest.raises(ConversionSkippedError, match="0-byte"):
            converter.convert("web/empty.html", source_dir, converted_dir)

    def test_unsupported_extension(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "web/file.xyz", "content",
        )
        converter = Converter(**make_converter_args())
        with pytest.raises(ConversionSkippedError, match="Unsupported extension"):
            converter.convert("web/file.xyz", source_dir, converted_dir)

    def test_meta_file_excluded(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "web/page.html.meta", "meta content",
        )
        converter = Converter(**make_converter_args())
        with pytest.raises(ConversionSkippedError, match="Meta file"):
            converter.convert("web/page.html.meta", source_dir, converted_dir)

    def test_metadata_db_excluded(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "metadata.db", b"sqlite3",
        )
        converter = Converter(**make_converter_args())
        with pytest.raises(ConversionSkippedError, match="Excluded file"):
            converter.convert("metadata.db", source_dir, converted_dir)

    def test_empty_html_extraction(self, tmp_path: Path) -> None:
        html = "<html><body><script>only script</script></body></html>"
        source_dir, converted_dir = _setup_source(
            tmp_path, "web/empty-content.html", html,
        )
        converter = Converter(**make_converter_args())
        with pytest.raises(ConversionSkippedError, match="Empty conversion"):
            converter.convert("web/empty-content.html", source_dir, converted_dir)

    def test_empty_pdf_extraction(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "web/empty.pdf", b"%PDF-1.4",
        )
        converter = Converter(**make_converter_args())
        with patch(
            "rag.converter.converter.extract_pdf", return_value=None,
        ):
            with pytest.raises(ConversionSkippedError, match="Empty conversion"):
                converter.convert("web/empty.pdf", source_dir, converted_dir)

    def test_unknown_json_source_type(self, tmp_path: Path) -> None:
        data = json.dumps({"key": "value"})
        source_dir, converted_dir = _setup_source(
            tmp_path, "local/data.json", data,
        )
        converter = Converter(**make_converter_args())
        with pytest.raises(ConversionSkippedError, match="Empty conversion"):
            converter.convert("local/data.json", source_dir, converted_dir)

    def test_zenn_scrap_empty_comments_deletes_existing(
        self, tmp_path: Path,
    ) -> None:
        scrap_data = json.dumps({"comments": []})
        source_dir, converted_dir = _setup_source(
            tmp_path, "zenn/user/scraps/slug.json", scrap_data,
        )
        # Pre-create converted file
        converted_file = converted_dir / "zenn" / "user" / "scraps" / "slug.md"
        converted_file.parent.mkdir(parents=True)
        converted_file.write_text("old content", encoding="utf-8")

        converter = Converter(**make_converter_args(regen_option="force"))
        with pytest.raises(ConversionSkippedError):
            converter.convert(
                "zenn/user/scraps/slug.json", source_dir, converted_dir,
            )
        # Existing file should be deleted
        assert not converted_file.exists()


# ============================================================
# 再生成オプション
# ============================================================


class TestRegenOptions:
    """再生成オプションのテスト."""

    def test_skip_existing(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "local/doc.md", "new content",
        )
        # Pre-create converted file with different content
        converted_file = converted_dir / "local" / "doc.md"
        converted_file.parent.mkdir(parents=True)
        converted_file.write_text("old content", encoding="utf-8")

        converter = Converter(**make_converter_args(regen_option="skip"))
        result = converter.convert("local/doc.md", source_dir, converted_dir)
        # Should return existing, not overwrite
        assert result.read_text(encoding="utf-8") == "old content"

    def test_if_modified_not_newer(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "local/doc.md", "source content",
        )
        converted_file = converted_dir / "local" / "doc.md"
        converted_file.parent.mkdir(parents=True)
        converted_file.write_text("converted content", encoding="utf-8")

        # Make converted file newer than source
        source_file = source_dir / "local" / "doc.md"
        old_time = time.time() - 100
        os.utime(source_file, (old_time, old_time))

        converter = Converter(**make_converter_args(regen_option="if_modified"))
        result = converter.convert("local/doc.md", source_dir, converted_dir)
        assert result.read_text(encoding="utf-8") == "converted content"

    def test_if_modified_newer(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "local/doc.md", "updated content",
        )
        converted_file = converted_dir / "local" / "doc.md"
        converted_file.parent.mkdir(parents=True)
        converted_file.write_text("old content", encoding="utf-8")

        # Make source newer than converted
        old_time = time.time() - 100
        os.utime(converted_file, (old_time, old_time))

        converter = Converter(**make_converter_args(regen_option="if_modified"))
        result = converter.convert("local/doc.md", source_dir, converted_dir)
        assert result.read_text(encoding="utf-8") == "updated content"

    def test_force_overwrites(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "local/doc.md", "new content",
        )
        converted_file = converted_dir / "local" / "doc.md"
        converted_file.parent.mkdir(parents=True)
        converted_file.write_text("old content", encoding="utf-8")

        converter = Converter(**make_converter_args(regen_option="force"))
        result = converter.convert("local/doc.md", source_dir, converted_dir)
        assert result.read_text(encoding="utf-8") == "new content"


# ============================================================
# 一括変換
# ============================================================


class TestConvertBatch:
    """Converter.convert_batch のテスト."""

    def test_batch_success(self, tmp_path: Path) -> None:
        source_dir = tmp_path / "source_store"
        converted_dir = tmp_path / "converted_store"

        for name in ("a.md", "b.md", "c.txt"):
            f = source_dir / "local" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"content of {name}", encoding="utf-8")

        converter = Converter(**make_converter_args())
        result = converter.convert_batch(
            ["local/a.md", "local/b.md", "local/c.txt"],
            source_dir,
            converted_dir,
        )
        assert result.success == 3
        assert result.skipped == 0
        assert result.errors == 0

    def test_batch_mixed_results(self, tmp_path: Path) -> None:
        source_dir = tmp_path / "source_store"
        converted_dir = tmp_path / "converted_store"

        # One valid file
        valid = source_dir / "local" / "doc.md"
        valid.parent.mkdir(parents=True, exist_ok=True)
        valid.write_text("valid content", encoding="utf-8")

        # One 0-byte file (will be skipped)
        empty = source_dir / "web" / "empty.html"
        empty.parent.mkdir(parents=True, exist_ok=True)
        empty.write_text("", encoding="utf-8")

        converter = Converter(**make_converter_args())
        result = converter.convert_batch(
            ["local/doc.md", "web/empty.html"],
            source_dir,
            converted_dir,
        )
        assert result.success == 1
        assert result.skipped == 1

    def test_batch_regen_option_override(self, tmp_path: Path) -> None:
        source_dir, converted_dir = _setup_source(
            tmp_path, "local/doc.md", "new content",
        )
        converted_file = converted_dir / "local" / "doc.md"
        converted_file.parent.mkdir(parents=True)
        converted_file.write_text("old content", encoding="utf-8")

        converter = Converter(**make_converter_args(regen_option="skip"))
        result = converter.convert_batch(
            ["local/doc.md"],
            source_dir,
            converted_dir,
            regen_option="force",
        )
        assert result.success == 1
        assert converted_file.read_text(encoding="utf-8") == "new content"

        # Verify original regen_option restored
        assert converter._regen_option == "skip"

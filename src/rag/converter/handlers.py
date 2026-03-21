"""コンバーター変換ハンドラ.

仕様: docs/specs/converter.md

各ファイル形式に対応する変換ハンドラをモジュールレベル関数として提供する。
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag
from charset_normalizer import from_bytes

from rag.ingesters.bluesky_ingester import (
    REASON_REPOST,
    _extract_quote_text_from_view_embed,
    _extract_text_from_post,
)
from rag.ingesters.document_ingester import DocumentIngester
from rag.markdown import RagMarkdownConverter

logger = logging.getLogger(__name__)

# --- HTML コンテンツ領域特定 ---

# セマンティックタグ（優先順）
_SEMANTIC_CONTENT_TAGS = ("article", "main")

# id パターン（長いものから試行 = 具体的なパターン優先）
_CONTENT_ID_PATTERNS = (
    "main-content",
    "main_content",
    "content-wrap",
    "content_wrap",
    "page-container",
    "page_container",
    "main-body",
    "main_body",
    "content",
    "main",
)

# class パターン
_CONTENT_CLASS_PATTERNS = (
    "main-content",
    "main_content",
    "content-wrap",
    "content_wrap",
    "page-container",
    "page_container",
)

# --- コンテンツ領域内の非コンテンツ除去 ---

# タグ名ベースの除去
_REMOVE_TAGS = ("script", "style", "noscript", "form")

# class パターンベースの除去（部分一致）
_REMOVE_CLASS_PATTERNS = (
    "breadcrumb",
    "topic-path",
    "nextprev",
    "pagination",
    "toolbar",
)

# テキスト密度フォールバックで除外するタグ
_TEXT_DENSITY_SKIP_TAGS = frozenset(
    ("script", "style", "nav", "noscript", "link"),
)

# 事前コンパイル済み正規表現
_COMPILED_ID_PATTERNS = tuple(
    re.compile(re.escape(p), re.IGNORECASE) for p in _CONTENT_ID_PATTERNS
)
_COMPILED_CLASS_PATTERNS = tuple(
    re.compile(re.escape(p), re.IGNORECASE) for p in _CONTENT_CLASS_PATTERNS
)
_COMPILED_REMOVE_CLASS_RE = re.compile(
    "|".join(re.escape(p) for p in _REMOVE_CLASS_PATTERNS), re.IGNORECASE
)


def _create_md_converter() -> RagMarkdownConverter:
    """共通の Markdown コンバーターを生成する."""
    return RagMarkdownConverter(
        heading_style="ATX",
        table_infer_header=True,
        escape_underscores=False,
        escape_asterisks=False,
    )


def _find_content_area(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    """HTML からコンテンツ領域を特定する.

    仕様: docs/specs/converter.md「コンテンツ領域の特定」

    優先順（仕様書と同一）:
    1. <article> タグ
    2. <main> タグ
    3. role="main" 属性
    4. id パターンマッチ
    5. class パターンマッチ
    6. テキスト密度フォールバック（body 直下で最大テキスト量のコンテナ要素）
    7. <body> タグ（最終フォールバック。body もなければ soup を返す）
    """
    # 1. セマンティックタグ
    for tag_name in _SEMANTIC_CONTENT_TAGS:
        found = soup.find(tag_name)
        if isinstance(found, Tag):
            return found

    # 2. role="main"
    found = soup.find(attrs={"role": "main"})
    if isinstance(found, Tag):
        return found

    # 3. id パターンマッチ（長いパターンから = 具体的なパターン優先）
    for regex in _COMPILED_ID_PATTERNS:
        found = soup.find(id=regex)
        if isinstance(found, Tag):
            return found

    # 4. class パターンマッチ
    for regex in _COMPILED_CLASS_PATTERNS:
        found = soup.find(class_=regex)
        if isinstance(found, Tag):
            return found

    # 5-6. テキスト密度フォールバック → body 最終フォールバック
    body = soup.find("body")
    if isinstance(body, Tag):
        # body 直下の子要素のうち、子 Tag を持つコンテナ要素に限定して
        # テキスト量が最大のものを選ぶ。<p> や <h1> 等の末端要素は
        # コンテンツラッパーではないため候補から除外する。
        best: Tag | None = None
        best_len = 0
        for child in body.children:
            if not isinstance(child, Tag):
                continue
            if child.name in _TEXT_DENSITY_SKIP_TAGS:
                continue
            # 子 Tag を持たない末端要素はラッパーではない
            if not any(isinstance(c, Tag) for c in child.children):
                continue
            text_len = len(child.get_text(strip=True))
            if text_len > best_len:
                best_len = text_len
                best = child
        # テキスト密度で候補が見つかればそれを、なければ body を返す
        return best if best is not None else body

    return soup


def _clean_content_area(content: Tag | BeautifulSoup) -> None:
    """コンテンツ領域内の非コンテンツ要素を除去する.

    仕様: docs/specs/converter.md「コンテンツ領域内の非コンテンツ除去」
    """
    # タグ名ベースの除去
    for tag_name in _REMOVE_TAGS:
        for tag in content.find_all(tag_name):
            tag.decompose()

    # class パターンベースの除去（1つの結合済み正規表現で1パス走査）
    for tag in content.find_all(class_=_COMPILED_REMOVE_CLASS_RE):
        tag.decompose()


def convert_html(source_path: Path) -> str | None:
    """HTML ファイルを Markdown に変換する.

    仕様: docs/specs/converter.md「HTML → Markdown 変換」

    - コンテンツ領域の特定（article/main → role="main" → id/class パターン → テキスト密度 → body）
    - コンテンツ領域内の非コンテンツ除去
    - markdownify ベースの変換（ATX見出し、テーブル保持、リンクURL除去）
    - 非 UTF-8 エンコーディング自動推定（charset_normalizer）

    Args:
        source_path: HTML ファイルの絶対パス

    Returns:
        Markdown テキスト、または変換失敗時は None
    """
    try:
        raw_bytes = source_path.read_bytes()
    except OSError:
        logger.exception("Failed to read HTML file: %s", source_path)
        return None

    # エンコーディング自動推定（charset_normalizer）
    detection = from_bytes(raw_bytes).best()
    if detection is None:
        logger.warning("Failed to detect encoding: %s", source_path)
        return None
    html_text = str(detection)

    soup = BeautifulSoup(html_text, "html.parser")

    # コンテンツ領域の特定
    content_area = _find_content_area(soup)

    # コンテンツ領域内の非コンテンツ除去
    _clean_content_area(content_area)

    # HTML → Markdown 変換
    md_converter = _create_md_converter()
    result: str = md_converter.convert_soup(content_area)
    return result


def convert_pdf(
    source_path: Path,
    doc_ingester: DocumentIngester,
) -> str | None:
    """PDF ファイルからテキストを抽出する.

    既存の DocumentIngester の PDF 抽出ロジックを再利用する。

    Args:
        source_path: PDF ファイルの絶対パス
        doc_ingester: DocumentIngester インスタンス（PDF設定を保持）

    Returns:
        Markdown テキスト、または抽出失敗/空の場合は None
    """
    return doc_ingester._extract_pdf(source_path)  # noqa: SLF001


def convert_json_bluesky(data: dict[str, Any]) -> str | None:
    """BlueSky 投稿 JSON からテキストを抽出する.

    仕様: docs/specs/converter.md「BlueSky 投稿」

    AT Protocol の投稿 JSON（getAuthorFeed レスポンスのフィードアイテム）から
    テキストを構造化して抽出する。

    Args:
        data: フィードアイテム JSON（getAuthorFeed レスポンスの1アイテム）

    Returns:
        構造化プレーンテキスト、または抽出不可時は None
    """
    post = data.get("post")
    if not isinstance(post, dict):
        return None

    record = post.get("record")
    if not isinstance(record, dict):
        return None

    # テキスト抽出（post.record = raw record）
    text = _extract_text_from_post(record)

    # 引用元テキストの取得（post.embed = view版）
    view_embed = post.get("embed")
    quote_text = _extract_quote_text_from_view_embed(view_embed)
    if quote_text:
        quote_section = "[Quote]\n" + quote_text
        text = text + "\n\n" + quote_section if text else quote_section

    # リポスト検出
    reason = data.get("reason")
    is_repost = (
        isinstance(reason, dict)
        and reason.get("$type") == REASON_REPOST
    )
    if is_repost:
        author = post.get("author")
        author_handle = (
            author.get("handle", "") if isinstance(author, dict) else ""
        )
        if author_handle:
            text = f"[Repost: @{author_handle}]\n{text}"

    return text if text and text.strip() else None


def convert_json_zenn_scrap(data: dict[str, Any]) -> str | None:
    """Zenn スクラップ JSON からテキストを抽出する.

    仕様: docs/specs/converter.md「Zenn スクラップ」

    comments 配列の各コメントの body_html を順序保持で結合し、
    HTML → Markdown 変換して水平線で区切る。

    Args:
        data: スクラップ JSON

    Returns:
        Markdown テキスト（水平線区切り）、
        またはコメント 0 件/全空の場合は None
    """
    # scrap オブジェクトの取得
    scrap = data.get("scrap", data)
    if not isinstance(scrap, dict):
        return None
    comments = scrap.get("comments")
    if not isinstance(comments, list) or not comments:
        return None

    md_converter = _create_md_converter()
    converted_parts: list[str] = []

    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body_html = comment.get("body_html", "")
        if not isinstance(body_html, str) or not body_html.strip():
            continue

        soup = BeautifulSoup(body_html, "html.parser")
        for tag_name in ("script", "style"):
            for tag in soup.find_all(tag_name):
                tag.decompose()

        md_text: str = md_converter.convert_soup(soup)
        if md_text and md_text.strip():
            converted_parts.append(md_text.strip())

    if not converted_parts:
        return None

    return "\n\n---\n\n".join(converted_parts)


def passthrough_copy(source_path: Path, dest_path: Path) -> None:
    """ファイルをそのままコピーする（パススルー）.

    Args:
        source_path: コピー元ファイルパス
        dest_path: コピー先ファイルパス
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, dest_path)

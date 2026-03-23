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

from rag.markdown import RagMarkdownConverter

logger = logging.getLogger(__name__)

# --- HTML void 要素修正 ---

# HTML void 要素（自己閉じ、子要素を持てない）
# https://html.spec.whatwg.org/multipage/syntax.html#void-elements
_VOID_ELEMENTS = (
    "area", "base", "br", "col", "embed", "hr", "img",
    "input", "link", "meta", "param", "source", "track", "wbr",
)


def _fix_void_elements(soup: BeautifulSoup) -> None:
    """html.parser が void 要素の子として誤解析したノードを親に巻き上げる.

    html.parser は <img> 等の void 要素を自己閉じとして認識しない場合があり、
    後続コンテンツが void 要素の子ノードとして解析される。
    この前処理で void 要素の子を親要素に移動し、DOM ツリーを修正する。
    """
    for el in soup.find_all(_VOID_ELEMENTS):
        if not el.contents:
            continue
        # 子ノードを逆順で void 要素の直後に移動（順序を保持）
        for child in reversed(list(el.contents)):
            el.insert_after(child)


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

    # html.parser が void 要素の子として誤解析したノードを修正
    _fix_void_elements(soup)

    # コンテンツ領域の特定
    content_area = _find_content_area(soup)

    # コンテンツ領域内の非コンテンツ除去
    _clean_content_area(content_area)

    # HTML → Markdown 変換
    md_converter = _create_md_converter()
    result: str = md_converter.convert_soup(content_area)
    return result



# --- BlueSky テキスト抽出ヘルパー ---

REASON_REPOST = "app.bsky.feed.defs#reasonRepost"
"""リポスト理由の $type"""


def _extract_text_from_post(value: dict[str, Any]) -> str:
    """投稿レコードからテキストを構造化して抽出する.

    仕様: docs/specs/ingesters/bluesky.md「テキスト抽出」

    構造:
    1. 投稿テキスト（先頭）
    2. 画像/動画 ALT テキスト（[Image ALT] / [Video ALT] プレフィックス）
    3. リンクカード（[Link Card] セクション）

    引用元テキストは呼び出し元で [Quote] セクションとして追加する。

    Args:
        value: 投稿レコードの value/record オブジェクト

    Returns:
        構造化されたプレーンテキスト
    """
    sections: list[str] = []

    # 1. 投稿テキスト（先頭）
    text = value.get("text", "")
    if text:
        sections.append(text)

    # 2-3. embed からメディア情報を抽出
    embed = value.get("embed")
    if isinstance(embed, dict):
        media_sections = _extract_embed_sections(embed)
        sections.extend(media_sections)

    return "\n\n".join(sections)


def _extract_embed_sections(embed: dict[str, Any]) -> list[str]:
    """embed オブジェクトから構造化セクションを抽出する.

    Args:
        embed: embed オブジェクト

    Returns:
        構造化セクションのリスト
    """
    embed_type = embed.get("$type", "")

    if embed_type == "app.bsky.embed.recordWithMedia":
        media = embed.get("media")
        if isinstance(media, dict):
            return _extract_media_sections(media)
        return []

    return _extract_media_sections(embed)


def _extract_media_sections(media: dict[str, Any]) -> list[str]:
    """メディアオブジェクトから構造化セクションを抽出する.

    Args:
        media: メディアオブジェクト（embed または embed.media）

    Returns:
        構造化セクションのリスト
    """
    sections: list[str] = []

    # 画像 ALT テキスト
    images = media.get("images")
    if isinstance(images, list):
        alt_texts = [
            img.get("alt", "")
            for img in images
            if isinstance(img, dict) and img.get("alt", "")
        ]
        if alt_texts:
            sections.append("[Image ALT] " + "\n".join(alt_texts))

    # 動画 ALT テキスト
    video_alt = media.get("alt", "")
    if video_alt:
        sections.append(f"[Video ALT] {video_alt}")

    # リンクカード
    external = media.get("external")
    if isinstance(external, dict):
        card_parts: list[str] = ["[Link Card]"]
        ext_title = external.get("title", "")
        if ext_title:
            card_parts.append(f"Title: {ext_title}")
        ext_uri = external.get("uri", "")
        if ext_uri:
            card_parts.append(f"URL: {ext_uri}")
        ext_desc = external.get("description", "")
        if ext_desc:
            card_parts.append(f"Description: {ext_desc}")
        if len(card_parts) > 1:
            sections.append("\n".join(card_parts))

    return sections


def _extract_quote_text_from_view_embed(
    view_embed: dict[str, Any] | None,
) -> str | None:
    """view embed（post.embed）から引用元テキストを取得する.

    getAuthorFeed のレスポンスでは引用元テキストが post.embed に展開済み。

    パス:
    - app.bsky.embed.record#view → post.embed.record.value.text
    - app.bsky.embed.recordWithMedia#view → post.embed.record.record.value.text

    Args:
        view_embed: post.embed オブジェクト（view 版）

    Returns:
        引用元テキスト、または引用なし/非投稿引用時は None
    """
    if not isinstance(view_embed, dict):
        return None

    embed_type = view_embed.get("$type", "")

    if embed_type == "app.bsky.embed.record#view":
        record = view_embed.get("record")
        if isinstance(record, dict):
            value = record.get("value")
            if isinstance(value, dict):
                text = value.get("text", "")
                return text if text else None
    elif embed_type == "app.bsky.embed.recordWithMedia#view":
        record = view_embed.get("record")
        if isinstance(record, dict):
            inner_record = record.get("record")
            if isinstance(inner_record, dict):
                value = inner_record.get("value")
                if isinstance(value, dict):
                    text = value.get("text", "")
                    return text if text else None

    return None


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
        _fix_void_elements(soup)
        for tag_name in ("script", "style"):
            for tag in soup.find_all(tag_name):
                tag.decompose()

        md_text: str = md_converter.convert_soup(soup)
        if md_text and md_text.strip():
            converted_parts.append(md_text.strip())

    if not converted_parts:
        return None

    return "\n\n---\n\n".join(converted_parts)


def convert_json_zenn_article(data: dict[str, Any]) -> str | None:
    """Zenn 記事 JSON からテキストを抽出する.

    仕様: docs/specs/converter.md「Zenn 記事」

    article オブジェクトの body_html を HTML → Markdown 変換する。

    Args:
        data: 記事 JSON（article オブジェクト、またはそれを含むラッパー）

    Returns:
        Markdown テキスト、または body_html が空/存在しない場合は None
    """
    article = data.get("article", data)
    if not isinstance(article, dict):
        return None

    body_html = article.get("body_html", "")
    if not isinstance(body_html, str) or not body_html.strip():
        return None

    md_converter = _create_md_converter()
    soup = BeautifulSoup(body_html, "html.parser")
    _fix_void_elements(soup)
    for tag_name in ("script", "style"):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    md_text: str = md_converter.convert_soup(soup)
    return md_text if md_text and md_text.strip() else None


def passthrough_copy(source_path: Path, dest_path: Path) -> None:
    """ファイルをそのままコピーする（パススルー）.

    Args:
        source_path: コピー元ファイルパス
        dest_path: コピー先ファイルパス
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, dest_path)


# --- YouTube JSON 変換 ---


def _format_timestamp_yt(seconds: float, has_hours: bool) -> str:
    """秒数をタイムスタンプ文字列に変換する."""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if has_hours:
        return f"[{h:d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


def _format_upload_date(upload_date: Any) -> str:
    """YYYYMMDD → YYYY-MM-DD に変換する."""
    if upload_date is None:
        return ""
    s = str(upload_date).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def convert_json_youtube(
    data: dict[str, Any],
    *,
    merge_gap_sec: float = 2.0,
    merge_max_chars: int = 300,
) -> str | None:
    """YouTube JSON をスニペット結合した Markdown に変換する.

    仕様: docs/specs/ingesters/youtube.md（コンバーター対応セクション）

    Args:
        data: パース済み JSON dict
        merge_gap_sec: スニペット結合の間隔閾値（秒）
        merge_max_chars: スニペット結合の最大文字数

    Returns:
        Markdown テキスト（スニペットがない場合はヘッダーのみの Markdown）
    """
    video_id = data.get("video_id") or ""
    title = data.get("title") or "(Untitled)"
    uploader = data.get("uploader") or ""
    upload_date_raw = data.get("upload_date") or ""
    upload_date = _format_upload_date(upload_date_raw)
    try:
        duration = int(data.get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0
    snippets: list[dict[str, Any]] = data.get("snippets", [])

    # ヘッダー
    lines: list[str] = [
        f"# {title}",
        "",
        f"投稿者: {uploader}",
        f"公開日: {upload_date}",
        f"動画URL: https://www.youtube.com/watch?v={video_id}",
        "",
        "---",
        "",
    ]

    if not snippets or not isinstance(snippets, list):
        return "\n".join(lines).rstrip() + "\n"

    # 不正な要素をフィルタ（dict 以外をスキップ）
    snippets = [s for s in snippets if isinstance(s, dict)]
    if not snippets:
        return "\n".join(lines).rstrip() + "\n"

    # タイムスタンプ形式の決定
    has_hours = duration >= 3600

    # スニペット結合
    paragraphs: list[tuple[float, str]] = []
    current_text = ""
    current_start = snippets[0].get("start", 0.0)
    prev_end = snippets[0].get("end", 0.0)

    for i, snippet in enumerate(snippets):
        s_start = snippet.get("start", 0.0)
        s_text = snippet.get("text", "")

        if i == 0:
            current_text = s_text
            prev_end = snippet.get("end", 0.0)
            continue

        gap = s_start - prev_end
        if gap >= merge_gap_sec or len(current_text) + len(s_text) > merge_max_chars:
            # 段落を確定
            paragraphs.append((current_start, current_text))
            current_text = s_text
            current_start = s_start
        else:
            current_text += s_text

        # end 時刻の逆転（YouTube 自動生成字幕で発生）に備え max で追跡
        prev_end = max(prev_end, snippet.get("end", 0.0))

    # 最後の段落
    if current_text:
        paragraphs.append((current_start, current_text))

    # 段落をフォーマット
    for start, text in paragraphs:
        ts = _format_timestamp_yt(start, has_hours)
        lines.append(f"{ts} {text}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"

"""コンテンツタイプ検出モジュール

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ContentType(Enum):
    """コンテンツタイプ."""

    PROSE = "prose"  # 通常テキスト（段落形式）
    TABLE = "table"  # テーブルデータ（数値が多い）
    HEADING = "heading"  # 見出し付きテキスト
    MIXED = "mixed"  # 混合（テーブル＋テキスト）


@dataclass
class ContentBlock:
    """コンテンツブロック.

    テキストを構造化された単位に分割した結果。
    """

    content_type: ContentType
    text: str
    heading: str = ""  # 見出しテキスト（ある場合）
    heading_level: int = 0  # 見出しレベル（1-6）


def detect_content_type(text: str) -> ContentType:
    """テキストの内容タイプを検出する.

    Args:
        text: 判定対象のテキスト

    Returns:
        ContentType: 検出されたコンテンツタイプ
    """
    if not text or not text.strip():
        return ContentType.PROSE

    text = text.strip()

    # 見出しの検出
    has_headings = _has_headings(text)

    # テーブルデータの検出
    is_table = _is_table_data(text)

    if is_table and has_headings:
        return ContentType.MIXED
    if is_table:
        return ContentType.TABLE
    if has_headings:
        return ContentType.HEADING

    return ContentType.PROSE


def _has_headings(text: str) -> bool:
    """テキストに見出しが含まれているか判定する.

    Markdown見出し（#）とHTML見出し（<h1>-<h6>）を検出。
    """
    # Markdown見出し: 行頭の # で始まる
    markdown_heading_pattern = r"^#{1,6}\s+.+"
    # HTML見出し: <h1>-<h6>タグ
    html_heading_pattern = r"<h[1-6][^>]*>.*?</h[1-6]>"

    if re.search(markdown_heading_pattern, text, re.MULTILINE):
        return True
    if re.search(html_heading_pattern, text, re.IGNORECASE):
        return True

    return False


def _is_table_data(text: str) -> bool:
    """テキストがテーブルデータかどうか判定する.

    ヒューリスティック:
    - 数値が行の大半を占める（50%以上の行で）
    - タブ/複数スペース区切りの列構造
    - または Markdown テーブル形式
    """
    lines = text.strip().split("\n")
    if len(lines) < 2:
        return False

    # Markdown テーブルのパターン検出
    # | で区切られた行が複数ある
    pipe_lines = [line for line in lines if "|" in line and line.count("|") >= 2]
    if len(pipe_lines) >= 2:
        # セパレータ行（|---|---|）の存在をチェック
        separator_pattern = r"^\|?[\s\-:|]+\|[\s\-:|]+\|?$"
        for line in lines:
            if re.match(separator_pattern, line.strip()):
                return True

    # タブまたは複数スペース区切りのテーブル検出
    numeric_row_count = 0
    structured_row_count = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # タブまたは2つ以上のスペースで分割
        cells = re.split(r"\t|\s{2,}", line)
        if len(cells) >= 3:
            structured_row_count += 1

            # 数値セルの割合を計算
            numeric_cells = sum(1 for cell in cells if _is_numeric_cell(cell))
            if numeric_cells / len(cells) >= 0.5:
                numeric_row_count += 1

    # 構造化された行が半数以上、かつ数値行が一定数以上
    total_lines = len([line for line in lines if line.strip()])
    if total_lines < 2:
        return False

    structured_ratio = structured_row_count / total_lines
    numeric_ratio = numeric_row_count / total_lines  # total_lines >= 2 は保証済み

    # 構造化された行が30%以上、または数値行が20%以上
    return structured_ratio >= 0.3 or numeric_ratio >= 0.2


def _is_numeric_cell(cell: str) -> bool:
    """セルが数値かどうか判定する."""
    cell = cell.strip()
    if not cell:
        return False

    # 通貨記号、カンマ、パーセントを除去
    cell = re.sub(r"[¥$€£,円%]", "", cell)

    # 数値パターン（整数、小数、負数）
    return bool(re.match(r"^-?\d+\.?\d*$", cell))


def split_by_content_type(text: str) -> list[ContentBlock]:
    """テキストをコンテンツタイプごとに分割する.

    見出しやテーブルの境界で分割し、それぞれのブロックを返す。

    Args:
        text: 分割対象のテキスト

    Returns:
        ContentBlockのリスト
    """
    if not text or not text.strip():
        return []

    blocks: list[ContentBlock] = []
    lines = text.split("\n")
    current_block_lines: list[str] = []
    current_heading = ""
    current_heading_level = 0

    for line in lines:
        # 見出し行の検出（Markdown）
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        # HTML見出しの検出
        html_heading_match = re.match(
            r"^<h([1-6])[^>]*>(.*?)</h\1>$", line.strip(), re.IGNORECASE
        )

        detected_heading: str | None = None
        detected_level: int = 0

        if heading_match:
            detected_heading = heading_match.group(2).strip()
            detected_level = len(heading_match.group(1))
        elif html_heading_match:
            detected_heading = html_heading_match.group(2).strip()
            detected_level = int(html_heading_match.group(1))

        if detected_heading is not None:
            # 前のブロックを確定
            if current_block_lines:
                block_text = "\n".join(current_block_lines).strip()
                if block_text:
                    block_type = detect_content_type(block_text)
                    blocks.append(
                        ContentBlock(
                            content_type=block_type,
                            text=block_text,
                            heading=current_heading,
                            heading_level=current_heading_level,
                        )
                    )
                current_block_lines = []

            # 新しい見出しを設定
            current_heading = detected_heading
            current_heading_level = detected_level
            continue

        current_block_lines.append(line)

    # 最後のブロックを確定
    if current_block_lines:
        block_text = "\n".join(current_block_lines).strip()
        if block_text:
            block_type = detect_content_type(block_text)
            blocks.append(
                ContentBlock(
                    content_type=block_type,
                    text=block_text,
                    heading=current_heading,
                    heading_level=current_heading_level,
                )
            )

    return blocks

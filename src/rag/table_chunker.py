"""テーブルデータチャンキングモジュール

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class TableChunk:
    """テーブルチャンク.

    テーブルの各行を独立したチャンクとして扱い、
    ヘッダー情報を各チャンクに付加する。
    """

    header: str  # ヘッダー行（カラム名）
    rows: list[str]  # データ行
    entity_name: str  # 行の識別子（最初のカラムの値）
    formatted_text: str  # 検索用にフォーマットされたテキスト


def chunk_table_data(
    text: str,
    header_row: str | None = None,
    row_context_size: int = 1,
) -> list[TableChunk]:
    """テーブルデータをチャンキングする.

    仕様: docs/specs/rag-knowledge.md

    - ヘッダー行を各チャンクに付加
    - 行単位で分割（意味的な単位を保持）
    - 前後の行をコンテキストとして含める

    Args:
        text: テーブルデータを含むテキスト
        header_row: 明示的なヘッダー行（Noneの場合は自動検出）
        row_context_size: 前後に含めるコンテキスト行数

    Returns:
        TableChunkのリスト
    """
    if not text or not text.strip():
        return []

    lines = text.strip().split("\n")
    lines = [line.strip() for line in lines if line.strip()]

    if len(lines) < 2:
        return []

    # テーブル形式を検出してパース
    parsed = _parse_table(lines, header_row)
    if not parsed:
        return []

    headers, data_rows = parsed

    if not data_rows:
        return []

    chunks: list[TableChunk] = []

    for i, row in enumerate(data_rows):
        # コンテキスト行を取得
        start_idx = max(0, i - row_context_size)
        end_idx = min(len(data_rows), i + row_context_size + 1)
        context_rows = data_rows[start_idx:end_idx]

        # エンティティ名（最初のカラムの値）
        entity_name = row[0] if row else ""

        # フォーマットされたテキストを生成
        # context_rows内でのメイン行のインデックス
        main_row_index_in_context = i - start_idx
        formatted_text = _format_table_chunk(
            headers, row, context_rows, entity_name, main_row_index_in_context
        )

        chunks.append(
            TableChunk(
                header=", ".join(headers),
                rows=[", ".join(r) for r in context_rows],
                entity_name=entity_name,
                formatted_text=formatted_text,
            )
        )

    return chunks


def _parse_table(
    lines: list[str],
    header_row: str | None = None,
) -> tuple[list[str], list[list[str]]] | None:
    """テーブルをパースしてヘッダーとデータ行に分割する.

    Markdown テーブル、タブ区切り、スペース区切りに対応。

    Returns:
        (ヘッダーリスト, データ行リスト) または None
    """
    # Markdown テーブルの検出
    if any("|" in line and line.count("|") >= 2 for line in lines):
        return _parse_markdown_table(lines, header_row)

    # タブ/スペース区切りテーブルの検出
    return _parse_delimited_table(lines, header_row)


def _parse_markdown_table(
    lines: list[str],
    header_row: str | None = None,
) -> tuple[list[str], list[list[str]]] | None:
    """Markdownテーブルをパースする."""
    # パイプ区切りの行を抽出
    table_lines = [line for line in lines if "|" in line and line.count("|") >= 2]
    if len(table_lines) < 2:
        return None

    # セパレータ行を検出して除外
    separator_pattern = r"^\|?[\s\-:|]+\|[\s\-:|]+\|?$"
    separator_idx = -1
    for i, line in enumerate(table_lines):
        if re.match(separator_pattern, line.strip()):
            separator_idx = i
            break

    if separator_idx == -1:
        # セパレータがない場合は最初の行をヘッダーとして扱う
        header_idx = 0
        data_start = 1
    else:
        # セパレータの前がヘッダー
        header_idx = separator_idx - 1 if separator_idx > 0 else 0
        data_start = separator_idx + 1

    # ヘッダーをパース
    if header_row:
        headers = _split_table_row(header_row)
    else:
        headers = _split_table_row(table_lines[header_idx])

    # データ行をパース（セパレータ行を除外）
    data_rows: list[list[str]] = []
    for i, line in enumerate(table_lines):
        if i < data_start:
            continue
        if re.match(separator_pattern, line.strip()):
            continue
        cells = _split_table_row(line)
        if cells:
            data_rows.append(cells)

    return headers, data_rows


def _parse_delimited_table(
    lines: list[str],
    header_row: str | None = None,
) -> tuple[list[str], list[list[str]]] | None:
    """タブ/スペース区切りテーブルをパースする."""
    # 区切り文字を検出
    delimiter = _detect_delimiter(lines)
    if not delimiter:
        return None

    # ヘッダーをパース
    if header_row:
        headers = _split_by_delimiter(header_row, delimiter)
    else:
        headers = _split_by_delimiter(lines[0], delimiter)

    # データ行をパース
    # header_row指定時はデータのみ（1行目からデータ）、指定なしは1行目がヘッダー
    data_rows: list[list[str]] = []
    start_idx = 0 if header_row else 1

    for line in lines[start_idx:]:
        cells = _split_by_delimiter(line, delimiter)
        if cells and len(cells) >= 2:
            data_rows.append(cells)

    if not headers or not data_rows:
        return None

    return headers, data_rows


def _split_table_row(line: str) -> list[str]:
    """テーブル行をセルに分割する（Markdown形式）."""
    # 先頭と末尾のパイプを除去
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]

    cells = [cell.strip() for cell in line.split("|")]
    return cells


def _detect_delimiter(lines: list[str]) -> str | None:
    """テーブルの区切り文字を検出する."""
    # タブ区切りを優先
    tab_counts = [line.count("\t") for line in lines]
    tab_counts_head = [c for c in tab_counts[:3] if c]
    if tab_counts_head and all(c >= 1 for c in tab_counts_head):
        return "\t"

    # 複数スペース区切り
    space_pattern = r"\s{2,}"
    space_matches = [len(re.findall(space_pattern, line)) for line in lines]
    space_matches_head = [m for m in space_matches[:3] if m]
    if space_matches_head and all(m >= 1 for m in space_matches_head):
        return space_pattern

    return None


def _split_by_delimiter(line: str, delimiter: str) -> list[str]:
    """指定された区切り文字で行を分割する."""
    if delimiter == "\t":
        return [cell.strip() for cell in line.split("\t")]
    else:
        # 正規表現パターンの場合
        cells = re.split(delimiter, line)
        return [cell.strip() for cell in cells if cell.strip()]


def _format_table_chunk(
    headers: list[str],
    main_row: list[str],
    context_rows: list[list[str]],
    entity_name: str,
    main_row_index_in_context: int,
) -> str:
    """テーブルチャンクを検索しやすいテキスト形式にフォーマットする.

    例:
        名前: りゅうおう
        HP: 200, MP: 100, 攻撃力: 140, 守備力: 75
    """
    parts: list[str] = []

    # エンティティ名を強調
    if entity_name:
        parts.append(f"名前: {entity_name}")

    # メイン行の属性をフォーマット
    attributes: list[str] = []
    for i, value in enumerate(main_row):
        if i == 0:
            continue  # 最初のカラム（名前）はスキップ
        if i < len(headers):
            header = headers[i]
            attributes.append(f"{header}: {value}")
        else:
            attributes.append(value)

    if attributes:
        parts.append(", ".join(attributes))

    # 周辺行をコンテキストとして追加（メイン行以外をインデックスで除外）
    other_rows = [
        row for idx, row in enumerate(context_rows) if idx != main_row_index_in_context
    ]
    if other_rows:
        context_parts: list[str] = []
        for row in other_rows:
            row_name = row[0] if row else ""
            context_parts.append(row_name)
        if context_parts:
            parts.append(f"周辺行: {', '.join(context_parts)}")

    return "\n".join(parts)

"""見出しベースチャンキングモジュール

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class HeadingChunk:
    """見出し付きチャンク.

    見出しとその配下のテキストをセットでチャンク化。
    親見出しの階層情報も保持する。
    """

    heading: str  # 見出しテキスト
    content: str  # 本文
    heading_level: int  # 見出しレベル（1〜6）
    parent_headings: list[str] = field(default_factory=list)  # 親見出しの階層
    formatted_text: str = ""  # 検索用にフォーマットされたテキスト

    def __post_init__(self) -> None:
        """フォーマット済みテキストを生成する."""
        if not self.formatted_text:
            self.formatted_text = self._format()

    def _format(self) -> str:
        """検索用のフォーマット済みテキストを生成する."""
        parts: list[str] = []

        # 親見出しをパンくずリスト形式で追加
        if self.parent_headings:
            breadcrumb = " > ".join(self.parent_headings)
            parts.append(f"[{breadcrumb}]")

        # 現在の見出し
        if self.heading:
            parts.append(f"# {self.heading}")

        # 本文
        if self.content:
            parts.append(self.content)

        return "\n".join(parts)


def chunk_by_headings(
    text: str,
    max_chunk_size: int = 500,
    min_chunk_size: int = 50,
) -> list[HeadingChunk]:
    """見出し単位でテキストを分割する.

    仕様: docs/specs/rag-knowledge.md

    - Markdown見出し（#, ##, ###）を検出
    - HTML見出し（<h1>〜<h6>）を検出
    - 見出し＋本文をセットでチャンク化
    - 見出しがない場合は従来のチャンキングにフォールバック

    Args:
        text: 分割対象のテキスト
        max_chunk_size: 各チャンクの最大文字数
        min_chunk_size: 各チャンクの最小文字数（これより短い場合は次と結合）

    Returns:
        HeadingChunkのリスト
    """
    if not text or not text.strip():
        return []

    text = text.strip()

    # HTML見出しをMarkdown形式に変換
    text = _convert_html_headings_to_markdown(text)

    # 見出しで分割
    sections = _split_by_headings(text)

    if not sections:
        # 見出しがない場合は全体を1つのチャンクとして返す
        if len(text) <= max_chunk_size:
            return [
                HeadingChunk(
                    heading="",
                    content=text,
                    heading_level=0,
                    parent_headings=[],
                    formatted_text=text,
                )
            ]
        # 大きすぎる場合は段落で分割
        return _split_prose_into_chunks(text, max_chunk_size, min_chunk_size)

    # 親見出しの階層を追跡しながらチャンクを生成
    chunks: list[HeadingChunk] = []
    heading_stack: list[tuple[int, str]] = []  # (level, heading)

    for heading, level, content in sections:
        # 現在のレベル以上の見出しをスタックから削除
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()

        # 親見出しのリストを取得
        parent_headings = [h for _, h in heading_stack]

        # 現在の見出しをスタックに追加
        if heading:
            heading_stack.append((level, heading))

        # コンテンツが大きすぎる場合は分割
        if len(content) > max_chunk_size:
            sub_chunks = _split_content_preserving_heading(
                heading, level, content, parent_headings, max_chunk_size
            )
            chunks.extend(sub_chunks)
        elif content and (len(content) >= min_chunk_size or heading or not chunks):
            # コンテンツが存在し、以下のいずれかを満たす場合にチャンクを作成:
            # 1. 最小サイズ以上
            # 2. 見出しがある
            # 3. まだチャンクがない（最初のコンテンツは常に保持）
            chunks.append(
                HeadingChunk(
                    heading=heading,
                    content=content,
                    heading_level=level,
                    parent_headings=parent_headings.copy(),
                )
            )
        elif chunks:
            # 小さすぎる場合は前のチャンクに結合
            prev = chunks[-1]
            combined_content = f"{prev.content}\n\n{content}".strip()
            if len(combined_content) <= max_chunk_size:
                chunks[-1] = HeadingChunk(
                    heading=prev.heading,
                    content=combined_content,
                    heading_level=prev.heading_level,
                    parent_headings=prev.parent_headings.copy(),
                )
            else:
                # 結合しても大きすぎる場合は別チャンクとして追加
                chunks.append(
                    HeadingChunk(
                        heading=heading,
                        content=content,
                        heading_level=level,
                        parent_headings=parent_headings.copy(),
                    )
                )

    return _enforce_formatted_text_limit(chunks, max_chunk_size)


def _enforce_formatted_text_limit(
    chunks: list[HeadingChunk],
    max_chunk_size: int,
) -> list[HeadingChunk]:
    """formatted_text が max_chunk_size を超えるチャンクの content を縮小する.

    breadcrumb + heading のオーバーヘッドにより formatted_text が
    max_chunk_size を超える場合、content を切り詰めて制限内に収める。
    """
    result: list[HeadingChunk] = []
    for chunk in chunks:
        if len(chunk.formatted_text) <= max_chunk_size:
            result.append(chunk)
            continue

        # formatted_text のオーバーヘッド（breadcrumb + heading + 改行）を算出
        overhead = len(chunk.formatted_text) - len(chunk.content)
        available = max_chunk_size - overhead
        if available <= 0:
            # オーバーヘッドだけで超過する場合は heading/breadcrumb を省略
            result.append(
                HeadingChunk(
                    heading="",
                    content=chunk.content[:max_chunk_size],
                    heading_level=chunk.heading_level,
                    parent_headings=[],
                    formatted_text=chunk.content[:max_chunk_size],
                )
            )
            continue

        # content を縮小して再生成
        trimmed_content = chunk.content[:available]
        result.append(
            HeadingChunk(
                heading=chunk.heading,
                content=trimmed_content,
                heading_level=chunk.heading_level,
                parent_headings=chunk.parent_headings.copy(),
            )
        )
    return result


def _convert_html_headings_to_markdown(text: str) -> str:
    """HTML見出しをMarkdown形式に変換する."""

    def replace_heading(match: re.Match[str]) -> str:
        level = int(match.group(1))
        content = match.group(2).strip()
        return "#" * level + " " + content

    # <h1>text</h1> -> # text
    pattern = r"<h([1-6])[^>]*>(.*?)</h\1>"
    return re.sub(pattern, replace_heading, text, flags=re.IGNORECASE | re.DOTALL)


def _split_by_headings(text: str) -> list[tuple[str, int, str]]:
    """テキストを見出しで分割する.

    Returns:
        (見出しテキスト, レベル, 本文) のリスト
    """
    # Markdown見出しのパターン
    heading_pattern = r"^(#{1,6})\s+(.+?)$"

    lines = text.split("\n")
    sections: list[tuple[str, int, str]] = []
    current_heading = ""
    current_level = 0
    current_content_lines: list[str] = []

    for line in lines:
        match = re.match(heading_pattern, line)
        if match:
            # 前のセクションを保存
            if current_content_lines or current_heading:
                content = "\n".join(current_content_lines).strip()
                sections.append((current_heading, current_level, content))

            # 新しいセクションを開始
            current_heading = match.group(2).strip()
            current_level = len(match.group(1))
            current_content_lines = []
        else:
            current_content_lines.append(line)

    # 最後のセクションを保存
    if current_content_lines or current_heading:
        content = "\n".join(current_content_lines).strip()
        sections.append((current_heading, current_level, content))

    return sections


def _split_content_preserving_heading(
    heading: str,
    level: int,
    content: str,
    parent_headings: list[str],
    max_chunk_size: int,
) -> list[HeadingChunk]:
    """見出し情報を保持しながらコンテンツを分割する."""
    chunks: list[HeadingChunk] = []

    # 段落で分割
    paragraphs = re.split(r"\n\s*\n", content)
    current_content_parts: list[str] = []
    current_size = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_size = len(para)

        # 単一段落が max_chunk_size を超える場合はフォールバック分割
        if para_size > max_chunk_size:
            # 現在の蓄積分を確定
            if current_content_parts:
                chunk_content = "\n\n".join(current_content_parts)
                chunks.append(
                    HeadingChunk(
                        heading=heading if not chunks else f"{heading} (続き)",
                        content=chunk_content,
                        heading_level=level,
                        parent_headings=parent_headings.copy(),
                    )
                )
                current_content_parts = []
                current_size = 0

            # 行ベース → 文字ベースのフォールバックで分割
            for sub in _split_large_block(para, max_chunk_size):
                chunks.append(
                    HeadingChunk(
                        heading=heading if not chunks else f"{heading} (続き)",
                        content=sub,
                        heading_level=level,
                        parent_headings=parent_headings.copy(),
                    )
                )
            continue

        if current_size + para_size + 2 > max_chunk_size and current_content_parts:
            # 現在のチャンクを確定
            chunk_content = "\n\n".join(current_content_parts)
            chunks.append(
                HeadingChunk(
                    heading=heading if not chunks else f"{heading} (続き)",
                    content=chunk_content,
                    heading_level=level,
                    parent_headings=parent_headings.copy(),
                )
            )
            current_content_parts = [para]
            current_size = para_size
        else:
            current_content_parts.append(para)
            current_size += para_size + 2

    # 残りのチャンク
    if current_content_parts:
        chunk_content = "\n\n".join(current_content_parts)
        chunks.append(
            HeadingChunk(
                heading=heading if not chunks else f"{heading} (続き)",
                content=chunk_content,
                heading_level=level,
                parent_headings=parent_headings.copy(),
            )
        )

    return chunks


def _split_prose_into_chunks(
    text: str,
    max_chunk_size: int,
    min_chunk_size: int,
) -> list[HeadingChunk]:
    """見出しのないテキストをチャンクに分割する."""
    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[HeadingChunk] = []
    current_parts: list[str] = []
    current_size = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_size = len(para)

        # 単一段落が max_chunk_size を超える場合はフォールバック分割
        if para_size > max_chunk_size:
            if current_parts:
                chunk_content = "\n\n".join(current_parts)
                chunks.append(
                    HeadingChunk(
                        heading="",
                        content=chunk_content,
                        heading_level=0,
                        parent_headings=[],
                        formatted_text=chunk_content,
                    )
                )
                current_parts = []
                current_size = 0

            for sub in _split_large_block(para, max_chunk_size):
                chunks.append(
                    HeadingChunk(
                        heading="",
                        content=sub,
                        heading_level=0,
                        parent_headings=[],
                        formatted_text=sub,
                    )
                )
            continue

        if current_size + para_size + 2 > max_chunk_size and current_parts:
            chunk_content = "\n\n".join(current_parts)
            chunks.append(
                HeadingChunk(
                    heading="",
                    content=chunk_content,
                    heading_level=0,
                    parent_headings=[],
                    formatted_text=chunk_content,
                )
            )
            current_parts = [para]
            current_size = para_size
        else:
            current_parts.append(para)
            current_size += para_size + 2

    if current_parts:
        chunk_content = "\n\n".join(current_parts)
        chunks.append(
            HeadingChunk(
                heading="",
                content=chunk_content,
                heading_level=0,
                parent_headings=[],
                formatted_text=chunk_content,
            )
        )

    return chunks


def _split_large_block(text: str, max_size: int) -> list[str]:
    """大きなテキストブロックを行ベース → 文字ベースで分割する.

    空行のない長いコンテンツ（コードブロック等）を max_size 以内に分割する。
    まず行単位でまとめ、それでも超える場合は文字数で強制分割する。
    """
    max_size = max(1, max_size)
    lines = text.split("\n")
    chunks: list[str] = []
    current_lines: list[str] = []
    current_size = 0

    for line in lines:
        line_size = len(line)

        # 単一行が max_size を超える場合は文字ベースで強制分割
        if line_size > max_size:
            if current_lines:
                chunks.append("\n".join(current_lines))
                current_lines = []
                current_size = 0
            for i in range(0, line_size, max_size):
                chunks.append(line[i : i + max_size])
            continue

        # 行を追加すると超える場合は現在の蓄積分を確定
        new_size = current_size + line_size + (1 if current_lines else 0)
        if new_size > max_size and current_lines:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_size = line_size
        else:
            current_lines.append(line)
            current_size = new_size

    if current_lines:
        chunks.append("\n".join(current_lines))

    return chunks

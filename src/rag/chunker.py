"""テキストチャンキングモジュール

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import re


def chunk_text(
    text: str,
    chunk_size: int,
    chunk_overlap: int,
) -> list[str]:
    """テキストをオーバーラップ付きチャンクに分割する.

    仕様: docs/specs/rag-knowledge.md

    分割優先順: 段落 → 文 → 文字数
    外部依存なし（LangChain不要）。

    Args:
        text: 分割対象のテキスト
        chunk_size: 各チャンクの最大文字数
        chunk_overlap: チャンク間のオーバーラップ文字数

    Returns:
        分割されたチャンクのリスト

    Raises:
        ValueError: chunk_size <= 0 または chunk_overlap >= chunk_size の場合
    """
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")
    if chunk_overlap < 0:
        raise ValueError(f"chunk_overlap must be non-negative, got {chunk_overlap}")
    if chunk_overlap >= chunk_size:
        raise ValueError(
            f"chunk_overlap ({chunk_overlap}) must be less than chunk_size ({chunk_size})"
        )

    if not text or not text.strip():
        return []

    text = text.strip()

    if len(text) <= chunk_size:
        return [text]

    # 段落で分割を試みる
    paragraphs = _split_by_paragraphs(text)
    chunks = _merge_into_chunks(paragraphs, chunk_size, chunk_overlap)

    return chunks


def _split_by_paragraphs(text: str) -> list[str]:
    """テキストを段落（空行区切り）で分割する."""
    # 空行（2つ以上の連続改行）で分割
    paragraphs = re.split(r"\n\s*\n", text)
    return [p.strip() for p in paragraphs if p.strip()]


def _split_by_sentences(text: str) -> list[str]:
    """テキストを文（句点区切り）で分割する."""
    # 日本語の句点（。）と英語の終端記号（. / ! / ?）で分割
    # 句点後の空白があれば含めて分割、なくても分割する
    sentences = re.split(r"(?<=[。.!?])\s*", text)
    return [s.strip() for s in sentences if s.strip()]


def _split_by_characters(text: str, chunk_size: int) -> list[str]:
    """テキストを指定文字数で分割する."""
    chunks = []
    for i in range(0, len(text), chunk_size):
        chunks.append(text[i : i + chunk_size])
    return chunks


def _merge_into_chunks(
    segments: list[str],
    chunk_size: int,
    chunk_overlap: int,
) -> list[str]:
    """セグメントをチャンクサイズに収まるようにマージする.

    大きすぎるセグメントは文単位で分割し、それでも大きい場合は文字数で分割する。
    """
    chunks: list[str] = []
    current_chunk = ""
    overlap_buffer = ""

    for segment in segments:
        # セグメントがチャンクサイズより大きい場合は分割
        if len(segment) > chunk_size:
            # 現在のチャンクを確定
            if current_chunk:
                chunks.append(current_chunk)
                overlap_buffer = _get_overlap(current_chunk, chunk_overlap)
                current_chunk = ""

            # セグメントを文単位で分割
            sub_segments = _split_by_sentences(segment)
            if len(sub_segments) == 1 and len(sub_segments[0]) > chunk_size:
                # 文単位で分割できない場合は文字数で分割
                sub_segments = _split_by_characters(segment, chunk_size - chunk_overlap)

            # 分割したセグメントを再帰的に処理
            sub_chunks = _merge_into_chunks(sub_segments, chunk_size, chunk_overlap)
            for i, sub_chunk in enumerate(sub_chunks):
                if i == 0 and overlap_buffer:
                    # 最初のサブチャンクにオーバーラップを付与
                    combined = overlap_buffer + " " + sub_chunk
                    if len(combined) <= chunk_size:
                        chunks.append(combined)
                    else:
                        chunks.append(sub_chunk)
                else:
                    chunks.append(sub_chunk)
            if sub_chunks:
                overlap_buffer = _get_overlap(sub_chunks[-1], chunk_overlap)
            continue

        # 現在のチャンクにセグメントを追加
        if current_chunk:
            combined = current_chunk + "\n\n" + segment
        else:
            combined = overlap_buffer + " " + segment if overlap_buffer else segment
            combined = combined.strip()

        if len(combined) <= chunk_size:
            current_chunk = combined
        else:
            # チャンクを確定して新しいチャンクを開始
            if current_chunk:
                chunks.append(current_chunk)
                overlap_buffer = _get_overlap(current_chunk, chunk_overlap)
            current_chunk = overlap_buffer + " " + segment if overlap_buffer else segment
            current_chunk = current_chunk.strip()

            # それでもチャンクサイズを超える場合は強制分割
            # スライディングウィンドウでチャンク間オーバーラップを維持する
            if len(current_chunk) > chunk_size:
                forced_chunks: list[str] = []
                step = chunk_size - chunk_overlap
                start = 0
                while start < len(current_chunk):
                    forced_chunk = current_chunk[start : start + chunk_size]
                    if not forced_chunk:
                        break
                    forced_chunks.append(forced_chunk)
                    # これ以上 chunk_size 分スライスできない場合は終了
                    if start + chunk_size >= len(current_chunk):
                        break
                    start += step

                for forced_chunk in forced_chunks[:-1]:
                    chunks.append(forced_chunk)
                current_chunk = forced_chunks[-1] if forced_chunks else ""
                # 強制分割系列内のオーバーラップは上記スライディングウィンドウで確保済み
                overlap_buffer = ""

    # 残りのチャンクを追加
    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _get_overlap(text: str, overlap_size: int) -> str:
    """テキストの末尾からオーバーラップ用の文字列を取得する."""
    if len(text) <= overlap_size:
        return text
    return text[-overlap_size:]

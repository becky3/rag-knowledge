"""コンテンツタイプに応じた汎用チャンキング.

仕様: docs/specs/rag-knowledge.md

TABLE / HEADING / MIXED / PROSE をコンテンツ判定し、適切なチャンキング手法を選択する。
"""

from __future__ import annotations

import logging

from rag.chunker import chunk_text
from rag.content_detector import ContentType, detect_content_type
from rag.heading_chunker import chunk_by_headings
from rag.table_chunker import chunk_table_data

logger = logging.getLogger(__name__)


def smart_chunk(
    text: str, chunk_size: int, chunk_overlap: int,
) -> list[tuple[str, str]]:
    """コンテンツタイプに応じた適切なチャンキング手法を選択する.

    - TABLE: テーブルデータとして行単位でチャンキング
    - HEADING/MIXED: 見出し単位でチャンキング
    - PROSE: 従来の段落ベースチャンキング

    Args:
        text: チャンキング対象のテキスト
        chunk_size: チャンクの最大文字数
        chunk_overlap: チャンク間のオーバーラップ文字数

    Returns:
        (content, section_path) のタプルリスト
    """
    if not text or not text.strip():
        return []

    content_type = detect_content_type(text)
    logger.debug("Detected content type: %s", content_type.value)

    if content_type == ContentType.TABLE:
        table_chunks = chunk_table_data(
            text, row_context_size=1, max_chunk_size=chunk_size,
        )
        if table_chunks:
            return [(c.content, c.section_path) for c in table_chunks]
        logger.debug("Table chunking returned no results, falling back to prose")

    if content_type in (ContentType.HEADING, ContentType.MIXED):
        heading_chunks = chunk_by_headings(
            text,
            max_chunk_size=chunk_size,
            min_chunk_size=max(1, chunk_size // 4),
        )
        if heading_chunks:
            return [(c.content, c.section_path) for c in heading_chunks]
        logger.debug("Heading chunking returned no results, falling back to prose")

    return [
        (c, "") for c in chunk_text(
            text, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
        )
    ]

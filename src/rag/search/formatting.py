"""検索結果の境界整形ロジック.

CLI / MCP 応答層で共有する format 関数を集約する。
仕様: docs/specs/search-response.md
"""

from __future__ import annotations

from collections.abc import Sequence

from rag.search.models import BM25SearchItem, RawSearchResults, VectorSearchItem


def format_raw_search_results(raw: RawSearchResults) -> str:
    """RawSearchResults をテキスト形式にフォーマットする.

    server.py（MCP）と cli.py（CLI）で共通使用する。

    Returns:
        フォーマット済みテキスト。結果なしの場合は結果なしメッセージ。
    """
    if not raw.vector_results and not raw.bm25_results:
        return "該当する情報が見つかりませんでした"

    sections: list[tuple[str, Sequence[VectorSearchItem | BM25SearchItem]]] = []
    if raw.vector_results:
        sections.append(("## ベクトル検索結果 (意味的類似度)\n", raw.vector_results))
    if raw.bm25_results:
        sections.append(("## BM25 検索結果 (キーワード一致)\n", raw.bm25_results))

    parts: list[str] = []
    for header, items in sections:
        parts.append(header)
        for i, item in enumerate(items, start=1):
            # スコア行: ベクトルは distance、BM25 は score
            if isinstance(item, VectorSearchItem):
                parts.append(f"### Result {i} [distance={item.distance:.3f}]")
            else:
                parts.append(f"### Result {i} [score={item.score:.3f}]")

            chunk_pos = _format_chunk_position(item.chunk_index, item.total_chunks)
            parts.append(f"Source: {item.source_url}")
            if item.url:
                parts.append(f"URL: {item.url}")
            parts.append(f"Title: {item.title}")
            parts.append(f"Chunk: {chunk_pos}")
            parts.append(f"Type: {item.source_type}")
            if item.section_path:
                parts.append(f"Section: {item.section_path}")
            if item.collected_at:
                parts.append(f"Collected: {item.collected_at}")
            parts.append("<<content>>")
            parts.append(item.text)
            parts.append("<</content>>")
            parts.append("")

    return "\n".join(parts).rstrip()


def _format_chunk_position(chunk_index: int, total_chunks: int) -> str:
    """チャンク位置を表示用文字列にフォーマットする."""
    pos = chunk_index + 1
    if total_chunks > 0:
        return f"{pos}/{total_chunks}"
    return f"{pos}/?"

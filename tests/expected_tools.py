"""MCP ツール一覧の期待値定義（SSoT）.

ツール追加・削除時はここだけを更新する。
"""

from __future__ import annotations

EXPECTED_MCP_TOOL_NAMES: frozenset[str] = frozenset({
    "rag_search", "rag_get_document",
    "rag_crawl_zenn", "rag_add_zenn",
    "rag_crawl_bluesky", "rag_add_bluesky",
    "rag_add_youtube", "rag_crawl_youtube",
    "rag_add_document", "rag_add_journal", "rag_crawl_documents",
    "rag_site_ingest",
    "rag_update_aozora_catalog", "rag_search_aozora",
    "rag_add_aozora", "rag_crawl_aozora",
    "rag_delete", "rag_rebuild", "rag_stats",
    "rag_list_recent", "rag_list_by_date_range",
})

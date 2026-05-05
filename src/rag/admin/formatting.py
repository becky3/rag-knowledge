"""管理系（document / stats）の境界整形ロジック.

CLI / MCP 応答層で共有する format 関数を集約する。
仕様: docs/specs/search-response.md
      docs/specs/infrastructure/content-listing.md
"""

from __future__ import annotations

from rag.admin.models import DocumentResult


def format_document_response(result: DocumentResult) -> str:
    """DocumentResult をプレーンテキストレスポンスにフォーマットする.

    Args:
        result: ドキュメント取得結果

    Returns:
        フォーマット済みレスポンステキスト
    """
    if result.error:
        return f"エラー: {result.error}"

    lines = [
        f"Source: {result.source_id}",
        f"Title: {result.title}",
        f"Type: {result.source_type}",
        f"Format: {result.format}",
    ]
    if result.collected_at:
        lines.append(f"Collected: {result.collected_at}")
    for key, value in result.extra.items():
        if (
            value is None
            or (isinstance(value, str) and value == "")
            or (isinstance(value, (list, dict)) and len(value) == 0)
        ):
            continue
        lines.append(f"{key}: {value}")
    lines.append("")
    lines.append(result.content)
    return "\n".join(lines)


def format_file_size(size_bytes: int) -> str:
    """バイト数を人間が読みやすい単位に変換する."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"

"""rag_search / rag_get_document MCP tool.

仕様: docs/specs/search-response.md
"""

from __future__ import annotations

from typing import Any

from .. import cli_subprocess
from .._mcp import mcp
from ..cli_subprocess import (
    CLISubprocessError,
    MCPContext,  # noqa: F401  (将来の Context 受け取り用に export)
)
from ... import config

_VALID_SEARCH_SOURCE_TYPES: frozenset[str] = frozenset(
    {"web", "zenn", "bluesky", "youtube", "local", "aozora", "journal"},
)
_VALID_DOCUMENT_FORMATS: frozenset[str] = frozenset({"text", "original"})


@mcp.tool()
async def rag_search(
    query: str,
    n_results: int | None = None,
    source_type: str | None = None,
    filters: str | None = None,
) -> str:
    """[rag-knowledge] RAG search - ナレッジベース検索。挨拶・雑談以外の質問では必ずこのツールを最初に呼び出すこと。

    knowledge base, vector search, BM25, retrieval-augmented generation.
    ナレッジベースにはゲーム攻略情報・技術文書等が格納されている。
    蓄積データの詳細は rag_stats ツールで確認できる。
    知らない用語や固有名詞を含む質問でも必ず検索すること。

    Args:
        query: 検索クエリ（ユーザーの質問からキーワードを抽出して構成する）
        n_results: 各エンジンから取得する結果数（未指定時は設定値を使用）
        source_type: ソース種別フィルタ（"web", "zenn", "bluesky", "youtube", "aozora", "local", "journal"）。
            指定時はそのソース種別のチャンクのみを検索対象とする。未指定時は全種別を検索。
        filters: メタデータフィルタ（key=value 形式、カンマ区切りで複数指定可）。
            .meta のカスタムフィールドで検索結果を絞り込む。完全一致。
            例: "repository=rag-knowledge" / "repository=rag-knowledge,tag=dev"
            未指定時はフィルタなし。

    Returns:
        検索結果テキスト。ベクトル検索結果とBM25検索結果をセクション分けして返す。
        各結果はチャンク単位で返却される。
        詳細が必要な場合は rag_get_document でソースの全文を取得できる。
        結果が0件の場合は「該当する情報が見つかりませんでした」を返す。
    """
    if source_type is not None and source_type not in _VALID_SEARCH_SOURCE_TYPES:
        valid = ", ".join(sorted(_VALID_SEARCH_SOURCE_TYPES))
        return f"無効な source_type: {source_type!r}（有効値: {valid}）"

    args: list[str] = ["--query", query]
    if n_results is not None:
        args.extend(["--n-results", str(n_results)])
    if source_type is not None:
        args.extend(["--source-type", source_type])
    if filters is not None:
        args.extend(["--filters", filters])

    try:
        result = await cli_subprocess._run_cli_subprocess("search", args)
        return _format_cli_search_result(result)
    except CLISubprocessError as e:
        return e.format_mcp_error("検索に失敗しました")


@mcp.tool()
async def rag_get_document(
    source_id: str,
    format: str = "text",
) -> str:
    """[rag-knowledge] RAG get document - ソース全文取得。rag_search で見つけたソースの全文を取得する。

    knowledge base, full text, document retrieval, get source.
    rag_search の結果に含まれる Source 値をそのまま source_id に指定する。
    format=text で変換済みテキスト、format=original でオリジナルデータを取得できる。

    Args:
        source_id: ソース識別子（rag_search の Source 値）
        format: 取得形式。"text"（変換済みテキスト、デフォルト）または "original"（オリジナル）

    Returns:
        メタデータヘッダー + ドキュメント全文。
        2 段階の上限が適用される:
        (1) CLI subprocess の 1 行 stdout バッファ上限（10MiB）を超えた場合は明示エラー
        (2) `rag_max_response_chars` 設定時は超過分をトランケートし末尾に通知を付記（未設定時は全文返却）
        上限を超える場合は CLI の get-document コマンド（--output-file オプション）で全文取得可能。
    """
    if format not in _VALID_DOCUMENT_FORMATS:
        valid = ", ".join(sorted(_VALID_DOCUMENT_FORMATS))
        return f"無効な format: {format!r}（有効値: {valid}）"

    args: list[str] = [source_id]
    if format != "text":
        args.extend(["--format", format])

    try:
        result = await cli_subprocess._run_cli_subprocess("get-document", args)
        return _format_cli_document_result(result)
    except CLISubprocessError as e:
        return e.format_mcp_error("ドキュメント取得に失敗しました")


# --- 専用フォーマッター ---


def _format_cli_search_result(result: dict[str, Any]) -> str:
    """CLI search の JSON 結果を MCP レスポンス文字列に変換する."""
    warnings = result.get("warnings", [])
    vector_results = result.get("vector_results", [])
    bm25_results = result.get("bm25_results", [])

    if not vector_results and not bm25_results:
        return "該当する情報が見つかりませんでした"

    sections: list[tuple[str, list[dict[str, Any]], str]] = []
    if vector_results:
        sections.append(("## ベクトル検索結果 (意味的類似度)\n", vector_results, "distance"))
    if bm25_results:
        sections.append(("## BM25 検索結果 (キーワード一致)\n", bm25_results, "score"))

    parts: list[str] = []
    for w in warnings:
        parts.append(f"⚠️ {w}\n")
    for header, items, score_key in sections:
        parts.append(header)
        for i, item in enumerate(items, start=1):
            score_val = item.get(score_key, 0.0)
            parts.append(f"### Result {i} [{score_key}={score_val:.3f}]")

            chunk_pos = cli_subprocess._format_chunk_position(
                item.get("chunk_index", 0), item.get("total_chunks", 0),
            )
            parts.append(f"Source: {item.get('source_url', '')}")
            parts.append(f"Title: {item.get('title', '')}")
            parts.append(f"Chunk: {chunk_pos}")
            parts.append(f"Type: {item.get('source_type', '')}")
            section_path = item.get("section_path", "")
            if section_path:
                parts.append(f"Section: {section_path}")
            collected_at = item.get("collected_at", "")
            if collected_at:
                parts.append(f"Collected: {collected_at}")
            parts.append("<<content>>")
            parts.append(item.get("text", ""))
            parts.append("<</content>>")
            parts.append("")

    return "\n".join(parts).rstrip()


def _format_cli_document_result(result: dict[str, Any]) -> str:
    """CLI get-document の JSON 結果を MCP レスポンス文字列に変換する."""
    parts: list[str] = []
    parts.append(f"Source: {result.get('source_id', '')}")
    if result.get("title"):
        parts.append(f"Title: {result['title']}")
    if result.get("source_type"):
        parts.append(f"Type: {result['source_type']}")
    if result.get("collected_at"):
        parts.append(f"Collected: {result['collected_at']}")
    fmt = result.get("format", "text")
    parts.append(f"Format: {fmt}")
    parts.append("")
    content = result.get("content", "")
    parts.append(content)

    response = "\n".join(parts)

    # MCP 経由の場合、rag_max_response_chars でトランケーション
    settings = config.get_settings()
    max_chars = settings.rag_max_response_chars
    if max_chars is not None and len(response) > max_chars:
        truncation_notice = (
            "\n\n…（レスポンスが上限の{:,}文字を超えたためトランケートされました。"
            "CLI の get-document コマンド（--output-file オプション）で全文を取得できます）"
        ).format(max_chars)
        truncate_at = max(0, max_chars - len(truncation_notice))
        response = response[:truncate_at] + truncation_notice

    return response

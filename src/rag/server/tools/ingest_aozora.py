"""rag_update_aozora_catalog / rag_search_aozora / rag_add_aozora / rag_crawl_aozora MCP tool.

仕様: docs/specs/ingesters/aozora.md
"""

from __future__ import annotations

from typing import Any

from .. import cli_subprocess
from .._mcp import mcp
from ..cli_subprocess import CLISubprocessError, MCPContext
from ..fake_labels import _AOZORA_INGEST_FAKE_SOURCES, _fake_mode_labels


@mcp.tool()
async def rag_update_aozora_catalog(
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG update Aozora catalog - 青空文庫の作品カタログを更新.

    knowledge base, Aozora, aozora bunko, catalog, update, CSV.
    青空文庫の作品カタログ CSV をダウンロードし、source_store に配置する。
    前回カタログとの差分から新着・更新作品を検出して結果を返す。

    Returns:
        カタログ更新結果のサマリーテキスト
    """
    label = _fake_mode_labels(_AOZORA_INGEST_FAKE_SOURCES)
    try:
        result = await cli_subprocess._run_cli_subprocess("update-aozora-catalog", ctx=ctx)
        return label + str(result.get("message", "カタログ更新完了"))
    except CLISubprocessError as e:
        return label + e.format_mcp_error("青空文庫カタログの更新に失敗しました")


@mcp.tool()
async def rag_search_aozora(
    author: str | None = None,
    title: str | None = None,
    limit: int = 20,
) -> str:
    """[rag-knowledge] RAG search Aozora catalog - 青空文庫カタログを検索.

    knowledge base, Aozora, aozora bunko, search, catalog, author, title.
    ローカルカタログ CSV を著者名・作品名で部分一致検索する。ネットワークアクセス不要。

    Args:
        author: 著者名（部分一致検索）
        title: 作品タイトル（部分一致検索）
        limit: 最大表示件数（デフォルト: 20、許容範囲: 1〜2000）

    Returns:
        検索結果リスト（作品 ID、タイトル、著者名、著作権フラグ）
    """
    args: list[str] = []
    if author is not None:
        args.extend(["--author", author])
    if title is not None:
        args.extend(["--title", title])
    if limit != 20:
        args.extend(["--limit", str(limit)])

    try:
        result = await cli_subprocess._run_cli_subprocess("search-aozora", args)
        return _format_cli_search_aozora_result(result)
    except CLISubprocessError as e:
        return e.format_mcp_error("青空文庫カタログ検索に失敗しました")


@mcp.tool()
async def rag_add_aozora(
    book_id: str,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG add Aozora - 青空文庫の作品を取り込み.

    knowledge base, Aozora, aozora bunko, ingest, book, work.
    指定作品の XHTML を取得し、ナレッジベースに取り込む。著作権フリーの作品のみ対応。

    Args:
        book_id: 青空文庫の作品 ID（カタログ検索で取得）

    Returns:
        取り込み結果のサマリーテキスト
    """
    label = _fake_mode_labels(_AOZORA_INGEST_FAKE_SOURCES)
    try:
        result = await cli_subprocess._run_cli_subprocess("ingest-aozora", [book_id], ctx=ctx)
        return label + cli_subprocess._format_cli_ingest_result(result, context=f"作品ID: {book_id}")
    except CLISubprocessError as e:
        return label + e.format_mcp_error(f"青空文庫作品の取り込みに失敗しました（作品ID: {book_id}）")


@mcp.tool()
async def rag_crawl_aozora(
    person_id: str,
    max_works: int | None = None,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG crawl Aozora - 青空文庫の著者作品を一括取り込み.

    knowledge base, Aozora, aozora bunko, ingest, crawl, author, works, person.
    指定著者（人物 ID）の著作権フリー作品を一括取り込みする。

    Args:
        person_id: 著者の人物 ID（rag_search_aozora で確認可能）
        max_works: 取得する最大作品数（未指定時は設定値を使用、許容範囲: 1〜500）

    Returns:
        取り込み結果のサマリーテキスト
    """
    args: list[str] = [person_id]
    if max_works is not None:
        args.extend(["--max-works", str(max_works)])

    label = _fake_mode_labels(_AOZORA_INGEST_FAKE_SOURCES)
    try:
        result = await cli_subprocess._run_cli_subprocess("ingest-aozora-author", args, ctx=ctx)
        return label + cli_subprocess._format_cli_ingest_result(result, context=f"人物ID: {person_id}")
    except CLISubprocessError as e:
        return label + e.format_mcp_error(f"青空文庫作品の取り込みに失敗しました（人物ID: {person_id}）")


# --- 専用フォーマッター ---


def _format_cli_search_aozora_result(result: dict[str, Any]) -> str:
    """CLI search-aozora の JSON 結果を MCP レスポンス文字列に変換する."""
    results = result.get("results", [])
    count = result.get("count", len(results))

    if not results:
        return f"検索結果: {count}件"

    lines = [f"検索結果: {count}件", ""]
    for r in results:
        lines.append(
            f"- [{r.get('book_id', '')}] {r.get('title', '')} / {r.get('author', '')} "
            f"({r.get('copyright', '')})"
        )
    return "\n".join(lines)

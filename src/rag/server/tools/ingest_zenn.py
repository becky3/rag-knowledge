"""rag_crawl_zenn / rag_add_zenn MCP tool.

仕様: docs/specs/ingesters/zenn.md
"""

from __future__ import annotations

from .. import cli_subprocess
from .._mcp import mcp
from ..cli_subprocess import CLISubprocessError, MCPContext
from ..fake_labels import _ZENN_INGEST_FAKE_SOURCES, _fake_mode_labels


@mcp.tool()
async def rag_crawl_zenn(
    username: str,
    max_articles: int | None = None,
    content_type: str = "all",
    force: bool = False,
    defer_indexing: bool = False,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG crawl Zenn - Zenn コンテンツを API 経由で取得し一括取り込み.

    knowledge base, Zenn, ingest, articles, scraps, API.
    指定ユーザーの Zenn 記事・スクラップを API 経由で取得し、ナレッジベースに取り込む。
    デフォルトでは既存コンテンツはスキップする。force=True で上書き取り込み。

    Args:
        username: Zenn ユーザー名
        max_articles: 取得する最大コンテンツ数（未指定時は設定値を使用、許容範囲: 1〜100）
        content_type: 取得対象（"articles": 記事のみ、"scraps": スクラップのみ、"all": 両方。デフォルト: "all"）
        force: 既存ファイルを上書きするか（デフォルト: false＝スキップモード）
        defer_indexing: True の場合、後段のパイプライン処理（converter + indexer）を
            スキップする。CLI の `--no-pipeline` と等価

    Returns:
        取り込み結果のサマリーテキスト
    """
    args: list[str] = [username]
    if content_type != "all":
        args.extend(["--content-type", content_type])
    if max_articles is not None:
        args.extend(["--max-articles", str(max_articles)])
    if force:
        args.append("--force")
    if defer_indexing:
        args.append("--no-pipeline")

    label = _fake_mode_labels(_ZENN_INGEST_FAKE_SOURCES)
    try:
        result = await cli_subprocess._run_cli_subprocess("crawl-zenn", args, ctx=ctx)
        return label + cli_subprocess._format_cli_ingest_result(result, context=f"ユーザー: {username}")
    except CLISubprocessError as e:
        return label + e.format_mcp_error(f"Zenn 記事の取り込みに失敗しました（ユーザー: {username}）")


@mcp.tool()
async def rag_add_zenn(
    urls: list[str],
    defer_indexing: bool = False,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG add Zenn - Zenn コンテンツを URL 指定で取り込み.

    knowledge base, Zenn, add, ingest, single article, single scrap, bulk.
    指定 URL の Zenn 記事またはスクラップを取得し、ナレッジベースに取り込む。
    既存コンテンツは上書きする。複数 URL を一括指定可能。

    Args:
        urls: Zenn コンテンツの URL リスト（1 件以上、例: ["https://zenn.dev/alice/articles/my-post"]）
        defer_indexing: True の場合、後段のパイプライン処理（converter + indexer）を
            スキップする。CLI の `--no-pipeline` と等価

    Returns:
        取り込み結果のサマリーテキスト
    """
    label = _fake_mode_labels(_ZENN_INGEST_FAKE_SOURCES)
    if not urls:
        return label + "エラー: urls が空です（1 件以上指定してください）"
    args: list[str] = list(urls)
    if defer_indexing:
        args.append("--no-pipeline")

    try:
        result = await cli_subprocess._run_cli_subprocess("ingest-zenn", args, ctx=ctx)
        return label + cli_subprocess._format_cli_ingest_result(result, context="Zenn ingest")
    except CLISubprocessError as e:
        return label + e.format_mcp_error("Zenn コンテンツの取り込みに失敗しました")

"""rag_crawl_bluesky / rag_add_bluesky MCP tool.

仕様: docs/specs/ingesters/bluesky.md
"""

from __future__ import annotations

from .. import cli_subprocess
from .._mcp import mcp
from ..cli_subprocess import CLISubprocessError, MCPContext
from ..fake_labels import _BLUESKY_INGEST_FAKE_SOURCES, _fake_mode_labels


@mcp.tool()
async def rag_crawl_bluesky(
    handle: str,
    max_posts: int | None = None,
    include_reposts: bool | None = None,
    force: bool = False,
    skip_pipeline: bool = False,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG crawl BlueSky - BlueSky 投稿を AT Protocol API 経由で取得し一括取り込み.

    knowledge base, BlueSky, Bluesky, ingest, posts, AT Protocol.
    指定ユーザーの BlueSky 投稿を AT Protocol API 経由で取得し、ナレッジベースに取り込む。
    通常は既存の投稿をスキップする。force 指定時は全データを上書き再取得する。

    Args:
        handle: BlueSky ハンドル（例: user.bsky.social）。DID 形式は不可
        max_posts: 取得する最大投稿数（タイムライン全体に適用、未指定時は設定値を使用、許容範囲: 1〜1000）
        include_reposts: タイムラインにリポストを含めるか（未指定時は設定値を使用）
        force: 上書き再取得モード。既存ファイルを上書きし、メディアDLと投稿内URL先の再取得も実行する
        skip_pipeline: True の場合、後段のパイプライン処理（converter + indexer）を
            スキップする。CLI の `--skip-pipeline` と等価

    Returns:
        取り込み結果のサマリーテキスト
    """
    args: list[str] = [handle]
    if max_posts is not None:
        args.extend(["--max-posts", str(max_posts)])
    # include_reposts=True/False の両方を CLI に伝播させる（None は設定値を使用）。
    # CLI 側は argparse.BooleanOptionalAction で --include-reposts / --no-include-reposts
    # の双方を受け付ける。
    if include_reposts is True:
        args.append("--include-reposts")
    elif include_reposts is False:
        args.append("--no-include-reposts")
    if force:
        args.append("--force")
    if skip_pipeline:
        args.append("--skip-pipeline")

    label = _fake_mode_labels(_BLUESKY_INGEST_FAKE_SOURCES)
    try:
        result = await cli_subprocess._run_cli_subprocess("crawl-bluesky", args, ctx=ctx)
        return label + cli_subprocess._format_cli_ingest_result(result, context=f"ハンドル: {handle}")
    except CLISubprocessError as e:
        return label + e.format_mcp_error(f"BlueSky 投稿の取り込みに失敗しました（ハンドル: {handle}）")


@mcp.tool()
async def rag_add_bluesky(
    urls: list[str],
    skip_pipeline: bool = False,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG add BlueSky - BlueSky 投稿を URL 指定で取り込み.

    knowledge base, BlueSky, add, ingest, single post, bulk.
    指定 URL の BlueSky 投稿を取得し、ナレッジベースに取り込む。
    メディア（画像・動画）も DL する。既存投稿は上書きする。複数 URL を一括指定可能。

    Args:
        urls: BlueSky 投稿の URL リスト（1 件以上、例: ["https://bsky.app/profile/user.bsky.social/post/abc123"]）
        skip_pipeline: True の場合、後段のパイプライン処理（converter + indexer）を
            スキップする。CLI の `--skip-pipeline` と等価

    Returns:
        取り込み結果のサマリーテキスト
    """
    label = _fake_mode_labels(_BLUESKY_INGEST_FAKE_SOURCES)
    if not urls:
        return label + "エラー: urls が空です（1 件以上指定してください）"
    # オプション注入対策: ユーザー入力の positional 群（urls）は `--` 以降に置く。
    # URL が `-`/`--` で始まる場合に argparse がオプションとして誤解釈するのを防ぐ。
    args: list[str] = []
    if skip_pipeline:
        args.append("--skip-pipeline")
    args.append("--")
    args.extend(urls)

    try:
        result = await cli_subprocess._run_cli_subprocess("ingest-bluesky", args, ctx=ctx)
        return label + cli_subprocess._format_cli_ingest_result(result, context="BlueSky ingest")
    except CLISubprocessError as e:
        return label + e.format_mcp_error("BlueSky 投稿の取り込みに失敗しました")

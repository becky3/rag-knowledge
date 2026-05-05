"""rag_add_youtube / rag_crawl_youtube MCP tool.

仕様: docs/specs/ingesters/youtube.md
"""

from __future__ import annotations

from .. import cli_subprocess
from .._mcp import mcp
from ..cli_subprocess import CLISubprocessError, MCPContext
from ..fake_labels import _YOUTUBE_INGEST_FAKE_SOURCES, _fake_mode_labels


@mcp.tool()
async def rag_add_youtube(
    video_url: str,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG add YouTube - YouTube 動画の字幕/文字起こしを取り込む.

    knowledge base, YouTube, video, transcript, subtitle, ingest.
    YouTube 動画の字幕または音声文字起こしを取得し、ナレッジベースに取り込む。

    Args:
        video_url: YouTube 動画 URL（youtube.com/watch?v= または youtu.be/ 形式）

    Returns:
        取り込み結果のサマリーテキスト
    """
    label = _fake_mode_labels(_YOUTUBE_INGEST_FAKE_SOURCES)
    try:
        result = await cli_subprocess._run_cli_subprocess("ingest-youtube", [video_url], ctx=ctx)
        return label + cli_subprocess._format_cli_ingest_result(result, context=f"動画: {video_url}")
    except CLISubprocessError as e:
        return label + e.format_mcp_error(f"YouTube 動画の取り込みに失敗しました: {video_url}")


@mcp.tool()
async def rag_crawl_youtube(
    playlist_url: str,
    max_videos: int | None = None,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG crawl YouTube playlist - YouTube プレイリストの動画を一括取り込み.

    knowledge base, YouTube, playlist, video, transcript, ingest, crawl.
    YouTube プレイリスト内の動画の字幕/文字起こしを一括取得し、ナレッジベースに取り込む。

    Args:
        playlist_url: YouTube プレイリスト URL（youtube.com/playlist?list= 形式）
        max_videos: 取得する最大動画数（未指定時は設定値を使用、許容範囲: 1〜500）

    Returns:
        取り込み結果のサマリーテキスト
    """
    args: list[str] = [playlist_url]
    if max_videos is not None:
        args.extend(["--max-videos", str(max_videos)])

    label = _fake_mode_labels(_YOUTUBE_INGEST_FAKE_SOURCES)
    try:
        result = await cli_subprocess._run_cli_subprocess("ingest-youtube-playlist", args, ctx=ctx)
        return label + cli_subprocess._format_cli_ingest_result(result, context=f"プレイリスト: {playlist_url}")
    except CLISubprocessError as e:
        return label + e.format_mcp_error(f"YouTube プレイリストの取り込みに失敗しました: {playlist_url}")

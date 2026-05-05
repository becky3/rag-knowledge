"""rag_list_recent / rag_stats MCP tool.

仕様: docs/specs/infrastructure/content-listing.md
     docs/specs/rebuild-stats.md「rag_stats 出力項目」
"""

from __future__ import annotations

from typing import Any

from .. import cli_subprocess
from .._mcp import mcp
from ..cli_subprocess import CLISubprocessError
from ...admin.formatting import format_file_size

_VALID_LISTING_SOURCE_TYPES: frozenset[str] = frozenset(
    {"web", "zenn", "bluesky", "youtube", "local", "aozora", "journal"},
)


@mcp.tool()
async def rag_list_recent(
    source_type: str,
    limit: int | None = None,
    order: str = "desc",
    filters: str | None = None,
) -> str:
    """[rag-knowledge] List recent sources - 指定した source_type のソースを公開日時順で一覧取得する.

    content listing, recent sources, source list, browse.
    ナレッジベースに取り込んだコンテンツを source_type 別に一覧で確認できる。
    最近取り込んだコンテンツの確認や、ナレッジベースの内容把握に使用する。

    Args:
        source_type: ソース種別: "web", "bluesky", "zenn", "youtube", "aozora", "local", "journal"
        limit: 取得件数（1〜100、未指定時は設定値を使用）
        order: ソート順（"desc": 新しい順（デフォルト）, "asc": 古い順）
        filters: メタデータフィルタ（key=value 形式、カンマ区切りで複数指定可）。
            .meta のカスタムフィールドで一覧を絞り込む。完全一致。
            例: "repository=rag-knowledge" / "repository=rag-knowledge,tag=dev"
            未指定時はフィルタなし。

    Returns:
        ソース一覧テキスト（タイトル、source_id、published_at、ファイルサイズ）
    """
    if source_type not in _VALID_LISTING_SOURCE_TYPES:
        valid = ", ".join(sorted(_VALID_LISTING_SOURCE_TYPES))
        return f"無効な source_type: {source_type!r}（有効値: {valid}）"

    if limit is not None and (limit < 1 or limit > 100):
        return "エラー: limit は 1〜100 の範囲で指定してください"

    if order not in ("asc", "desc"):
        return f"エラー: order は 'asc' または 'desc' を指定してください（指定値: {order!r}）"

    args: list[str] = ["--source-type", source_type]
    if limit is not None:
        args.extend(["--limit", str(limit)])
    args.extend(["--order", order])
    if filters is not None:
        args.extend(["--filters", filters])

    try:
        result = await cli_subprocess._run_cli_subprocess("list-recent", args)
        return _format_cli_list_recent_result(result)
    except CLISubprocessError as e:
        return e.format_mcp_error("ソース一覧の取得に失敗しました")


@mcp.tool()
async def rag_stats() -> str:
    """[rag-knowledge] RAG stats - ナレッジベースの統計情報と蓄積データ概要を表示.

    knowledge base, statistics, chunk count, source count, source list.
    蓄積されているナレッジの概要を4セクション
    （source_store / converted_store / インデックス / パイプライン）で返す。
    検索前にこのツールを呼ぶことで、ナレッジベースの内容を把握し
    適切な検索キーワードを構成できる。

    Returns:
        統計情報のテキスト
    """
    try:
        result = await cli_subprocess._run_cli_subprocess("stats")
        return _format_cli_stats_result(result)
    except CLISubprocessError as e:
        return e.format_mcp_error("統計情報の取得に失敗しました")


# --- 専用フォーマッター ---


def _format_cli_list_recent_result(result: dict[str, Any]) -> str:
    """CLI list-recent の JSON 結果を MCP レスポンス文字列に変換する."""
    sources = result.get("sources", [])
    source_type = result.get("source_type", "")
    count = result.get("count", len(sources))
    total = result.get("total", count)

    if not sources:
        return f"{source_type}: 0 件"

    order = result.get("order", "desc")
    order_label = "古い順" if order == "asc" else "新しい順"
    lines = [f"{source_type}: {count} 件（全 {total} 件中, {order_label}）", ""]
    for s in sources:
        title = s.get("title", "(無題)")
        source_id = s.get("source_id", "")
        published_at = s.get("published_at", "")
        file_size = s.get("file_size", 0)
        size_str = format_file_size(file_size) if file_size else ""
        line = f"- {title}"
        if published_at:
            line += f"  [{published_at}]"
        if size_str:
            line += f"  ({size_str})"
        lines.append(line)
        lines.append(f"  {source_id}")

    return "\n".join(lines)


def _format_cli_stats_result(result: dict[str, Any]) -> str:
    """CLI stats の JSON 結果を MCP レスポンス文字列に変換する."""
    parts: list[str] = ["📊 RAG Knowledge 統計"]

    # source_store
    ss = result.get("source_store", {})
    parts.append("")
    parts.append("■ source_store")
    if ss.get("status") in {"unconfigured", "not_found"}:
        parts.append("  未設定" if ss.get("status") == "unconfigured" else "  ディレクトリが存在しません")
    else:
        parts.append(f"  総ファイル数: {ss.get('total_files', 0):,}")
        parts.append(f"  総サイズ: {format_file_size(int(ss.get('total_size', 0)))}")
        by_type = ss.get("by_type")
        if by_type and isinstance(by_type, dict):
            parts.append("  媒体別:")
            for st_key in sorted(by_type.keys()):
                info = by_type[st_key]
                parts.append(
                    f"    {st_key}: {info['files']} files"
                    f" ({format_file_size(info['size'])})"
                )

    # converted_store
    cs = result.get("converted_store", {})
    parts.append("")
    parts.append("■ converted_store")
    if cs.get("status") in {"unconfigured", "not_found"}:
        parts.append("  未設定" if cs.get("status") == "unconfigured" else "  ディレクトリが存在しません")
    else:
        parts.append(f"  総ファイル数: {cs.get('total_files', 0):,}")
        parts.append(f"  総サイズ: {format_file_size(int(cs.get('total_size', 0)))}")

    # インデックス
    idx = result.get("index", {})
    parts.append("")
    parts.append("■ インデックス")
    if "error" in idx:
        parts.append(f"  エラー: {idx['error']}")
    else:
        parts.append(f"  総チャンク数: {idx.get('total_chunks', 0):,}")
        parts.append(f"  ソース数: {idx.get('source_count', 0):,}")

    # パイプライン
    pl = result.get("pipeline", {})
    parts.append("")
    parts.append("■ パイプライン")
    if pl.get("status") == "unconfigured":
        parts.append("  未設定")
    elif pl.get("status") == "uninitialized":
        parts.append("  未初期化")
    else:
        last_at = pl.get("last_processed_at") or "（未実行）"
        parts.append(f"  最終処理: {last_at}")
        parts.append(f"  実行回数: {pl.get('run_count', 0)}")
        lci = pl.get("last_commit_id")
        parts.append(f"  last_commit_id: {lci if lci else '（未実行）'}")
        parts.append(f"  論理削除: {pl.get('deleted_count', 0)} 件")

    return "\n".join(parts)

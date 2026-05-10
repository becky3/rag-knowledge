"""rag_delete MCP tool.

仕様: docs/specs/rag-knowledge.md（rag_delete セクション）
"""

from __future__ import annotations

from .. import cli_subprocess
from .._mcp import mcp
from ..cli_subprocess import CLISubprocessError, MCPContext
from ...pipeline.models import format_pipeline_error as _format_pipeline_error


@mcp.tool()
async def rag_delete(
    source_ids: list[str],
    skip_pipeline: bool = False,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG delete - ソース識別子指定でナレッジから削除（bulk 対応）.

    knowledge base, remove source, delete document, bulk.
    source_store からファイルを物理削除し、パイプライン経由で
    インデックス・metadata.db を更新する。
    git 管理下のため、削除後も git checkout で復旧可能。
    複数 source_id を 1 リクエストで処理可能。末尾 1 回だけパイプラインが走る。

    Args:
        source_ids: 削除するソース識別子のリスト（1 件以上）
        skip_pipeline: True の場合、後段のパイプライン処理（converter + indexer）を
            スキップする。CLI の `--skip-pipeline` と等価。bulk 削除時の高速化用

    Returns:
        削除結果のメッセージ
    """
    if not source_ids:
        return "エラー: source_ids が空です（1 件以上指定してください）"
    # オプション注入対策: ユーザー入力の positional 群（source_ids）は `--` 以降に置く。
    # source_id が `-`/`--` で始まる場合に argparse がオプションとして誤解釈するのを防ぐ。
    cli_args: list[str] = []
    if skip_pipeline:
        cli_args.append("--skip-pipeline")
    cli_args.append("--")
    cli_args.extend(source_ids)
    context = source_ids[0] if len(source_ids) == 1 else f"{len(source_ids)} 件"
    try:
        result = await cli_subprocess._run_cli_subprocess("delete", cli_args, ctx=ctx)

        # 全件 not_found のケース（CLI 側で {"deleted": False, "not_found_ids": [...]} を返す）
        if result.get("deleted") is False:
            not_found_ids = result.get("not_found_ids", [])
            return f"該当するソースが見つかりませんでした: {', '.join(not_found_ids) or context}"

        deleted_count = result.get("deleted_count", 0)
        not_found_ids = result.get("not_found_ids", []) or []

        pipeline_data = result.get("pipeline")
        if pipeline_data:
            pipeline_summary = cli_subprocess._parse_pipeline_summary(pipeline_data)
            if pipeline_summary and pipeline_summary.errors:
                errors_text = "; ".join(
                    _format_pipeline_error(e) for e in pipeline_summary.errors
                )
                return f"削除しましたが、パイプラインでエラーが発生しました: {context} ({errors_text})"

        msg = f"削除しました: {deleted_count}件"
        if not_found_ids:
            msg += f"（見つからなかったソース: {len(not_found_ids)}件）"
        return msg
    except CLISubprocessError as e:
        return e.format_mcp_error(f"削除に失敗しました。{context}")

"""rag_delete MCP tool.

仕様: docs/specs/rag-knowledge.md（rag_delete セクション）
"""

from __future__ import annotations

from .. import cli_subprocess
from .._mcp import mcp
from ..cli_subprocess import CLISubprocessError, MCPContext
from ...pipeline.models import format_pipeline_error as _format_pipeline_error


@mcp.tool()
async def rag_delete(source_id: str, ctx: MCPContext | None = None) -> str:
    """[rag-knowledge] RAG delete - ソース識別子指定でナレッジから削除.

    knowledge base, remove source, delete document.
    source_store からファイルを物理削除し、パイプライン経由で
    インデックス・metadata.db を更新する。
    git 管理下のため、削除後も git checkout で復旧可能。

    Args:
        source_id: 削除するソース識別子（source_id）

    Returns:
        削除結果のメッセージ
    """
    try:
        result = await cli_subprocess._run_cli_subprocess("delete", [source_id], ctx=ctx)

        if result.get("not_found"):
            return f"該当するソースが見つかりませんでした: {source_id}"

        pipeline_data = result.get("pipeline")
        if pipeline_data:
            pipeline_summary = cli_subprocess._parse_pipeline_summary(pipeline_data)
            if pipeline_summary and pipeline_summary.errors:
                errors_text = "; ".join(
                    _format_pipeline_error(e) for e in pipeline_summary.errors
                )
                return f"削除しましたが、パイプラインでエラーが発生しました: {source_id} ({errors_text})"
        return f"削除しました: {source_id}"
    except CLISubprocessError as e:
        return e.format_mcp_error(f"削除に失敗しました。source_id: {source_id}")

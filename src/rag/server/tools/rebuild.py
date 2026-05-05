"""rag_rebuild MCP tool.

仕様: docs/specs/rebuild-stats.md
"""

from __future__ import annotations

import logging

from .. import cli_subprocess
from .._mcp import mcp
from ..cli_subprocess import CLISubprocessError, MCPContext

logger = logging.getLogger("rag.server")


@mcp.tool()
async def rag_rebuild(
    mode: str,
    source_type: str | None = None,
    path: str | None = None,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG rebuild - ナレッジベースの再構築を実行する.

    knowledge base, rebuild, pipeline, reindex, convert.
    パイプラインの再構築を指定モードで実行する。
    注意: full / index モードは Embedding API を呼び出すため、
    データ量に比例したコスト（API 利用料）が発生します。

    Args:
        mode: 再構築モード。
            "full" — 全再構築（データ破損時・大規模設計変更時）
            "convert" — コンバートのみ再実行（変換ロジック改修時）
            "index" — インデックスのみ再構築（Embedding モデル変更時）
            "incremental" — 差分更新（通常運用。未コミット変更は自動コミット）
        source_type: 対象媒体フィルタ: "web", "bluesky", "zenn", "youtube", "aozora", "local", "journal"。
            未指定時は全媒体。incremental モードでは指定不可。path と排他指定。
        path: 対象パスフィルタ。source_store ルート相対のディレクトリパスを指定し、
            配下（再帰的）のソースのみを対象にする。
            未指定時は全パス。incremental モードでは指定不可。source_type と排他指定。
            空文字列・絶対パス・`..`/`.` を含むパスはバリデーションエラー
            （全パス対象は引数を省略する）。

    Returns:
        処理結果サマリ（処理件数、エラー件数、所要時間）
    """
    args: list[str] = ["--mode", mode]
    if source_type is not None:
        args.extend(["--source-type", source_type])
    if path is not None:
        args.extend(["--path", path])

    try:
        result = await cli_subprocess._run_cli_subprocess("rebuild", args, ctx=ctx)
        elapsed = float(result.get("elapsed", 0.0))

        # full モードは2フェーズ結果
        if mode == "full":
            convert_data = result.get("convert")
            index_data = result.get("index")
            if convert_data and index_data:
                convert_summary = cli_subprocess._parse_pipeline_summary(convert_data)
                index_summary = cli_subprocess._parse_pipeline_summary(index_data)
                if convert_summary and index_summary:
                    return cli_subprocess._format_full_rebuild_summary(
                        convert_summary, index_summary, elapsed,
                    )
            missing = []
            if not convert_data:
                missing.append("convert")
            if not index_data:
                missing.append("index")
            logger.error(
                "full rebuild 結果の解析に失敗: 欠落キー=%s, result_keys=%s",
                missing or "parse_error", list(result.keys()),
            )
            return "再構築完了（結果の解析に失敗）"

        # その他のモードは単一 PipelineSummary
        summary = cli_subprocess._parse_pipeline_summary(result)
        if summary is not None:
            return cli_subprocess._format_rebuild_summary(summary, elapsed)
        return "再構築完了（結果の解析に失敗）"
    except CLISubprocessError as e:
        return e.format_mcp_error("再構築中にエラーが発生しました")

"""パイプライン処理のサブプロセスワーカー.

MCP サーバーから subprocess で起動され、パイプライン処理を実行する。
C 拡張（BM25s 等）の SEGFAULT がサーバープロセスを巻き込まないよう、
別プロセスで実行するための薄いエントリポイント。

結果は PipelineSummary 相当の JSON を stdout に出力する。
エラー時はエラー JSON を stdout に出力し、exit code 1 で終了する。

Usage:
    python -m rag.pipeline.worker rebuild \\
        --mode full --source-type web --auto-commit
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from rag.store.models import SourceType


def run_rebuild(args: argparse.Namespace) -> None:
    """rebuild を実行し、結果 JSON を stdout に出力する."""
    from rag.pipeline.factory import build_pipeline_controller

    mode: str = args.mode
    source_type: SourceType | None = args.source_type
    auto_commit: bool = args.auto_commit

    try:
        controller = build_pipeline_controller()

        start = time.monotonic()

        if mode == "full":
            summary = controller.run_full_rebuild(
                source_type=source_type, auto_commit=auto_commit,
            )
        elif mode == "convert":
            summary = controller.run_convert_only(
                source_type=source_type, auto_commit=auto_commit,
            )
        elif mode == "index":
            summary = controller.run_index_only(
                source_type=source_type, auto_commit=auto_commit,
            )
        else:
            summary = controller.run_incremental()

        elapsed = time.monotonic() - start

        result = {
            "mode": summary.mode.value,
            "total_files": summary.total_files,
            "processed": summary.processed,
            "skipped": summary.skipped,
            "errors": summary.errors,
            "elapsed": round(elapsed, 1),
        }
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        error_result = {
            "error": True,
            "message": str(exc),
        }
        print(json.dumps(error_result, ensure_ascii=False))
        sys.exit(1)


def main() -> None:
    """エントリポイント."""
    parser = argparse.ArgumentParser(description="パイプラインワーカー")
    subparsers = parser.add_subparsers(dest="command")

    rebuild_parser = subparsers.add_parser("rebuild")
    rebuild_parser.add_argument(
        "--mode", required=True,
        choices=["full", "convert", "index", "incremental"],
    )
    rebuild_parser.add_argument(
        "--source-type",
        choices=["web", "bluesky", "zenn", "local"],
        default=None,
    )
    rebuild_parser.add_argument(
        "--auto-commit", action="store_true", default=False,
    )

    args = parser.parse_args()

    if args.command == "rebuild":
        run_rebuild(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

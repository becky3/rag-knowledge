"""パイプライン処理のサブプロセスワーカー.

MCP サーバーから subprocess で起動され、パイプライン処理を実行する。
C 拡張（BM25s 等）の SEGFAULT がサーバープロセスを巻き込まないよう、
別プロセスで実行するための薄いエントリポイント。

結果は PipelineSummary 相当の JSON を stdout に出力する。
エラー時はエラー JSON を stdout に出力し、exit code 1 で終了する。

Usage:
    python -m rag.pipeline.worker rebuild \\
        --mode full --source-type web --auto-commit
    python -m rag.pipeline.worker ingest-and-index \\
        --commit-message "add: new document"
    python -m rag.pipeline.worker delete \\
        --source-id "https://example.com/page"
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from rag.pipeline.models import PipelineSummary

from rag.store.models import SourceType


def _summary_to_dict(summary: PipelineSummary, elapsed: float) -> dict[str, object]:
    """PipelineSummary を JSON 出力用 dict に変換する."""
    return {
        "mode": summary.mode.value,
        "total_files": summary.total_files,
        "processed": summary.processed,
        "skipped": summary.skipped,
        "errors": summary.errors,
        "elapsed": round(elapsed, 1),
    }


def _emit_error(exc: Exception) -> NoReturn:
    """エラー JSON を stdout に出力し、exit code 1 で終了する."""
    print(json.dumps({"error": True, "message": str(exc)}, ensure_ascii=False))
    sys.exit(1)


def run_rebuild(args: argparse.Namespace) -> None:
    """rebuild を実行し、結果 JSON を stdout に出力する."""
    from rag.pipeline.factory import build_pipeline_controller

    mode: str = args.mode
    source_type: SourceType | None = args.source_type
    auto_commit: bool = args.auto_commit

    # incremental モードのバリデーション（CLI と同等）
    if mode == "incremental" and source_type is not None:
        print(json.dumps({"error": True, "message": "incremental モードでは source_type を指定できません"}))
        sys.exit(1)
    if mode == "incremental" and auto_commit:
        print(json.dumps({"error": True, "message": "incremental モードでは auto_commit を指定できません"}))
        sys.exit(1)

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

        print(json.dumps(_summary_to_dict(summary, elapsed), ensure_ascii=False))
    except Exception as exc:
        _emit_error(exc)


def run_ingest_and_index(args: argparse.Namespace) -> None:
    """ingest_and_index を実行し、結果 JSON を stdout に出力する."""
    from rag.pipeline.factory import build_pipeline_controller

    try:
        controller = build_pipeline_controller()

        start = time.monotonic()
        summary = controller.ingest_and_index(args.commit_message)
        elapsed = time.monotonic() - start

        print(json.dumps(_summary_to_dict(summary, elapsed), ensure_ascii=False))
    except Exception as exc:
        _emit_error(exc)


def run_delete(args: argparse.Namespace) -> None:
    """source_store からファイルを削除し、パイプラインを再実行する."""
    from rag.pipeline.factory import build_pipeline_controller

    try:
        controller = build_pipeline_controller()
        source_id: str = args.source_id

        try:
            controller.source_store.remove_file(source_id)
        except KeyError:
            print(json.dumps({"not_found": True}))
            return  # exit code 0（not_found は正常系）

        start = time.monotonic()
        summary = controller.ingest_and_index(f"delete: {source_id}")
        elapsed = time.monotonic() - start

        result = {
            "deleted": True,
            "pipeline": _summary_to_dict(summary, elapsed),
        }
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        _emit_error(exc)


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

    ingest_parser = subparsers.add_parser("ingest-and-index")
    ingest_parser.add_argument(
        "--commit-message", required=True,
    )

    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument(
        "--source-id", required=True,
    )

    args = parser.parse_args()

    if args.command == "rebuild":
        run_rebuild(args)
    elif args.command == "ingest-and-index":
        run_ingest_and_index(args)
    elif args.command == "delete":
        run_delete(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

"""パイプライン処理のサブプロセスワーカー.

MCP サーバーから subprocess で起動され、パイプライン処理を実行する。
C 拡張（BM25s 等）の SEGFAULT がサーバープロセスを巻き込まないよう、
別プロセスで実行するための薄いエントリポイント。

結果は PipelineSummary 相当の JSON を stdout に出力する。

Usage:
    python -m rag.pipeline.worker rebuild \\
        --mode full --source-type web --auto-commit
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rag.pipeline.controller import PipelineController

logger = logging.getLogger(__name__)


def _build_pipeline_controller() -> PipelineController:
    """CLI の run_rebuild と同じ初期化ロジックでコントローラを構築する."""
    from rag.bm25_index import BM25Index
    from rag.config import get_settings
    from rag.converter import Converter
    from rag.embedding.factory import get_embedding_provider
    from rag.indexer import Indexer
    from rag.ingesters.document_ingester import PdfBackendConfig
    from rag.pipeline.controller import PipelineController
    from rag.store.source_store import SourceStore
    from rag.vector_store import VectorStore

    settings = get_settings()

    source_store_dir = Path(settings.source_store_dir)
    converted_store_dir = Path(settings.converted_store_dir)
    converted_store_dir.mkdir(parents=True, exist_ok=True)

    source_store = SourceStore(source_store_dir)
    source_store.db.initialize()

    pdf_config = PdfBackendConfig(
        backend=settings.rag_pdf_backend,
        mineru_mfd_conf_thres=settings.rag_pdf_mineru_mfd_conf_thres,
        quality_ufffd_threshold=settings.rag_pdf_quality_ufffd_threshold,
        quality_greek_threshold=settings.rag_pdf_quality_greek_threshold,
        quality_cjk_min_threshold=settings.rag_pdf_quality_cjk_min_threshold,
        quality_min_chars_per_page=settings.rag_pdf_quality_min_chars_per_page,
        quality_sample_pages=settings.rag_pdf_quality_sample_pages,
    )
    converter = Converter(regen_option="force", pdf_config=pdf_config)

    embedding_provider = get_embedding_provider(settings, settings.embedding_provider)

    with contextlib.redirect_stdout(io.StringIO()):
        vector_store = VectorStore(
            embedding_provider=embedding_provider,
            persist_directory=settings.chromadb_persist_dir,
        )
        bm25_index = BM25Index(
            k1=settings.rag_bm25_k1,
            b=settings.rag_bm25_b,
            persist_dir=settings.bm25_persist_dir,
        )

    indexer = Indexer(
        vector_store=vector_store,
        bm25_index=bm25_index,
        metadata_db=source_store.db,
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
    )

    return PipelineController(
        source_store=source_store,
        converted_store_dir=converted_store_dir,
        converter=converter,
        indexer=indexer,
    )


def run_rebuild(args: argparse.Namespace) -> None:
    """rebuild を実行し、結果 JSON を stdout に出力する."""
    from rag.store.models import SourceType

    mode: str = args.mode
    source_type: SourceType | None = args.source_type
    auto_commit: bool = args.auto_commit

    controller = _build_pipeline_controller()

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

"""PipelineController のファクトリ関数.

server.py / cli.py / worker.py で重複していた初期化ロジックを共通化する。
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

from rag.bm25_index import BM25Index
from rag.config import RAGSettings, get_settings
from rag.converter import Converter
from rag.embedding.factory import get_embedding_provider
from rag.indexer import Indexer
from rag.converter.pdf_extractor import PdfBackendConfig
from rag.pipeline.controller import PipelineController
from rag.store.source_store import SourceStore
from rag.vector_store import VectorStore


def build_pipeline_controller(
    settings: RAGSettings | None = None,
) -> PipelineController:
    """設定から PipelineController を構築する.

    Args:
        settings: 設定。None の場合は get_settings() で取得する。

    Returns:
        構築された PipelineController
    """
    if settings is None:
        settings = get_settings()

    source_store_dir = Path(settings.source_store_dir)
    converted_store_dir = Path(settings.converted_store_dir)
    converted_store_dir.mkdir(parents=True, exist_ok=True)

    source_store = SourceStore(source_store_dir)
    source_store.initialize()

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

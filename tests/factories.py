"""テスト用ファクトリ関数.

本番コードのコンストラクタからデフォルト値を除去したことに伴い、
テストコードでは関心のある引数だけオーバーライドできるファクトリを使用する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# 基盤クラス群
# ---------------------------------------------------------------------------


def make_bm25_index(**overrides: Any) -> Any:
    """BM25Index のテスト用ファクトリ."""
    from rag.bm25_index import BM25Index

    defaults: dict[str, Any] = {
        "k1": 1.5,
        "b": 0.75,
        "persist_dir": None,
    }
    defaults.update(overrides)
    return BM25Index(**defaults)


def make_hybrid_search_engine(**overrides: Any) -> Any:
    """HybridSearchEngine のテスト用ファクトリ."""
    from rag.hybrid_search import HybridSearchEngine

    defaults: dict[str, Any] = {
        "vector_store": overrides.pop("vector_store", MagicMock()),
        "bm25_index": overrides.pop("bm25_index", MagicMock()),
        "vector_weight": 0.5,
    }
    defaults.update(overrides)
    return HybridSearchEngine(**defaults)


def make_vector_store_ephemeral(embedding_provider: Any, **overrides: Any) -> Any:
    """VectorStore.create_ephemeral のテスト用ファクトリ."""
    from rag.vector_store import VectorStore

    defaults: dict[str, Any] = {
        "collection_name": "knowledge",
        "hnsw_m": None,
        "hnsw_construction_ef": None,
        "hnsw_search_ef": None,
    }
    defaults.update(overrides)
    collection_name = defaults.pop("collection_name")
    return VectorStore.create_ephemeral(
        embedding_provider=embedding_provider,
        collection_name=collection_name,
        **defaults,
    )


def make_vector_store(embedding_provider: Any, **overrides: Any) -> Any:
    """VectorStore.__init__ のテスト用ファクトリ."""
    from rag.vector_store import VectorStore

    defaults: dict[str, Any] = {
        "persist_directory": "./chroma_db",
        "collection_name": "knowledge",
        "hnsw_m": None,
        "hnsw_construction_ef": None,
        "hnsw_search_ef": None,
    }
    defaults.update(overrides)
    return VectorStore(embedding_provider=embedding_provider, **defaults)


def make_vector_store_http(embedding_provider: Any, **overrides: Any) -> Any:
    """VectorStore.create_http のテスト用ファクトリ."""
    from rag.vector_store import VectorStore

    defaults: dict[str, Any] = {
        "host": "localhost",
        "port": 8000,
        "collection_name": "knowledge",
        "hnsw_m": None,
        "hnsw_construction_ef": None,
        "hnsw_search_ef": None,
    }
    defaults.update(overrides)
    host = defaults.pop("host")
    port = defaults.pop("port")
    collection_name = defaults.pop("collection_name")
    return VectorStore.create_http(
        embedding_provider=embedding_provider,
        host=host,
        port=port,
        collection_name=collection_name,
        **defaults,
    )


def make_rag_knowledge_service(**overrides: Any) -> Any:
    """RAGKnowledgeService のテスト用ファクトリ."""
    from rag.rag_knowledge import RAGKnowledgeService

    defaults: dict[str, Any] = {
        "vector_store": overrides.pop("vector_store", MagicMock()),
        "chunk_size": 200,
        "chunk_overlap": 30,
        "similarity_threshold": None,
        "bm25_index": None,
        "hybrid_search_enabled": False,
        "vector_weight": 1.0,
        "min_combined_score": None,
        "debug_log_enabled": False,
    }
    defaults.update(overrides)
    return RAGKnowledgeService(**defaults)


def make_safe_browsing_client(**overrides: Any) -> Any:
    """SafeBrowsingClient のテスト用ファクトリ."""
    from rag.safe_browsing import SafeBrowsingClient

    defaults: dict[str, Any] = {
        "api_key": "test-api-key",
        "timeout": 10.0,
        "cache_ttl": None,
        "client_id": "rag-knowledge",
        "client_version": "1.0.0",
        "max_cache_size": None,
        "constrained_client_kwargs": None,
    }
    defaults.update(overrides)
    return SafeBrowsingClient(**defaults)


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


def make_lmstudio_embedding_args(**overrides: Any) -> dict[str, Any]:
    """LMStudioEmbedding のデフォルト引数を返す."""
    defaults: dict[str, Any] = {
        "base_url": "http://localhost:1234",
        "model": "nomic-embed-text",
        "prefix_enabled": False,
        "retry_count": 0,
        "retry_base_delay": 0.1,
    }
    defaults.update(overrides)
    return defaults


def make_openai_embedding_args(**overrides: Any) -> dict[str, Any]:
    """OpenAIEmbedding のデフォルト引数を返す."""
    defaults: dict[str, Any] = {
        "api_key": "sk-test",
        "model": "text-embedding-3-small",
    }
    defaults.update(overrides)
    return defaults


def make_chunker_args(**overrides: Any) -> dict[str, Any]:
    """chunk_text のデフォルト引数を返す."""
    defaults: dict[str, Any] = {
        "chunk_size": 500,
        "chunk_overlap": 50,
    }
    defaults.update(overrides)
    return defaults


def make_heading_chunks(text: str, **overrides: Any) -> Any:
    """chunk_by_headings のテスト用ファクトリ."""
    from rag.heading_chunker import chunk_by_headings

    defaults: dict[str, Any] = {
        "max_chunk_size": 500,
        "min_chunk_size": 50,
    }
    defaults.update(overrides)
    return chunk_by_headings(text, **defaults)


def make_table_chunks(text: str, **overrides: Any) -> Any:
    """chunk_table_data のテスト用ファクトリ."""
    from rag.table_chunker import chunk_table_data

    defaults: dict[str, Any] = {
        "row_context_size": 1,
        "max_chunk_size": 0,
    }
    defaults.update(overrides)
    return chunk_table_data(text, **defaults)


def make_chromadb_server_manager_args(**overrides: Any) -> dict[str, Any]:
    """ChromaDBServerManager のデフォルト引数を返す."""
    defaults: dict[str, Any] = {
        "host": "localhost",
        "port": 8000,
        "persist_dir": "./test_chroma_db",
        "auto_start": True,
    }
    defaults.update(overrides)
    return defaults


def make_scrapy_runner_args(**overrides: Any) -> dict[str, Any]:
    """ScrapyRunner のデフォルト引数を返す."""
    defaults: dict[str, Any] = {
        "temp_dir": "/tmp/test_scrapy",
        "delay_sec": 0.1,
        "max_pages": 10000,
        "download_timeout": 30,
        "timeout_sec": 0.0,
        "error_count": 0,
    }
    defaults.update(overrides)
    return defaults


def make_converter_args(**overrides: Any) -> dict[str, Any]:
    """Converter のデフォルト引数を返す."""
    from rag.converter.pdf_extractor import PdfBackendConfig

    defaults: dict[str, Any] = {
        "regen_option": "skip",
        "pdf_config": PdfBackendConfig(),
        "youtube_merge_gap_sec": 2.0,
        "youtube_merge_max_chars": 300,
        "media_analyzer": None,
        "html_remove_class_tokens": [
            "breadcrumb", "breadcrumbs", "topic-path", "nextprev", "pagination",
            "toolbar", "footer-wrapper", "footer", "scrollToFeedback", "suggest",
        ],
    }
    defaults.update(overrides)
    return defaults


def make_indexer_args(**overrides: Any) -> dict[str, Any]:
    """Indexer のデフォルト引数を返す（vector_store, bm25_index, metadata_db は呼び出し元で渡す）."""
    defaults: dict[str, Any] = {
        "chunk_size": 200,
        "chunk_overlap": 30,
        "embedding_prefix_enabled": True,
        "embedding_context_length": 512,
        "worst_token_char_ratio": 0.7,
    }
    defaults.update(overrides)
    return defaults


def make_bluesky_ingester(source_store: Any, **overrides: Any) -> Any:
    """BlueskyIngester のテスト用ファクトリ.

    Fetcher / MediaDownloader / YoutubeClassifier / YoutubeDelegator /
    SiteIngestRunner は省略可。省略時は Fake / Mock を注入する。
    特定の振る舞い検証時は overrides で個別に Fake を渡す。
    """
    from rag.pipeline.ingesters.bluesky import BlueskyIngester

    if "fetcher" not in overrides or "media_downloader" not in overrides:
        from pathlib import Path

        from rag.pipeline.ingesters._fake.bluesky import (
            FakeBlueskyFetcher,
            FakeBlueskyMediaDownloader,
        )

        fixture_dir = (
            Path(__file__).parent.parent
            / "src" / "rag" / "pipeline" / "ingesters" / "_fake" / "bluesky" / "data"
        )
        overrides.setdefault("fetcher", FakeBlueskyFetcher(fixture_dir=fixture_dir))
        overrides.setdefault("media_downloader", FakeBlueskyMediaDownloader())

    if "youtube_classifier" not in overrides:
        from rag.pipeline.ingesters.youtube_protocols import (
            create_youtube_classifier,
        )
        overrides["youtube_classifier"] = create_youtube_classifier()

    if "site_ingest_runner" not in overrides:
        from rag.pipeline.site_ingest_runner import create_site_ingest_runner
        overrides["site_ingest_runner"] = create_site_ingest_runner()

    defaults: dict[str, Any] = {
        "youtube_delegator": None,
        "max_posts": 200,
        "include_reposts": True,
        "force_youtube_reingest": False,
        "youtube_request_interval": 0.0,
    }
    defaults.update(overrides)
    return BlueskyIngester(source_store, **defaults)


class StubZennFetcher:
    """ZennFetcher Protocol のテスト用 stub 実装.

    flat な応答シーケンス（dict / Exception の混在リスト）を順番に消費し、
    list_contents / fetch_content_detail のいずれの呼び出しでも同一シーケンスを
    使う。これは旧 ``_make_mock_client(responses)`` パターンとの互換性を持たせ、
    テストの呼び出し順序ベースの記述を維持するため。
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self._idx = 0

    async def __aenter__(self) -> "StubZennFetcher":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    def _next(self) -> Any:
        if self._idx >= len(self._responses):
            raise IndexError(
                f"StubZennFetcher: 応答シーケンスを使い切りました（idx={self._idx}）",
            )
        item = self._responses[self._idx]
        self._idx += 1
        if isinstance(item, BaseException):
            raise item
        return item

    async def list_contents(
        self,
        kind: str,
        username: str,
        page: int,
    ) -> dict[str, Any]:
        del kind, username, page
        result = self._next()
        assert isinstance(result, dict)  # noqa: S101
        return result

    async def fetch_content_detail(
        self,
        kind: str,
        slug: str,
    ) -> dict[str, Any]:
        del kind, slug
        result = self._next()
        assert isinstance(result, dict)  # noqa: S101
        return result


def make_zenn_ingester(source_store: Any, **overrides: Any) -> Any:
    """ZennIngester のテスト用ファクトリ.

    fetcher は省略可。省略時は空応答の StubZennFetcher を注入する。
    シナリオを使うテストは overrides で `fetcher=...` を渡す。
    """
    from rag.pipeline.ingesters.zenn import ZennIngester

    if "fetcher" not in overrides:
        overrides["fetcher"] = StubZennFetcher([])

    defaults: dict[str, Any] = {
        "max_articles": 50,
    }
    defaults.update(overrides)
    return ZennIngester(source_store, **defaults)


def make_youtube_ingester(source_store: Any, **overrides: Any) -> Any:
    """YoutubeIngester のテスト用ファクトリ.

    fetcher は省略可。省略時は FakeYoutubeFetcher(scenario="happy") を注入する。
    シナリオ・カスタムデータを使う場合は overrides で `fetcher=...` を渡す。
    """
    from rag.pipeline.ingesters.youtube import YoutubeIngester

    if "fetcher" not in overrides:
        from pathlib import Path

        from rag.pipeline.ingesters._fake.youtube import FakeYoutubeFetcher

        fixture_dir = Path(__file__).parent.parent / "src" / "rag" / "pipeline" / "ingesters" / "_fake" / "youtube" / "data"
        overrides["fetcher"] = FakeYoutubeFetcher(fixture_dir=fixture_dir)

    defaults: dict[str, Any] = {
        "max_videos": 100,
        "request_interval": 0.1,
        "request_timeout": 30,
        "whisper_model": "base",
        "whisper_device": "cuda",
        "transcript_languages": None,
        "max_duration": 14400,
    }
    defaults.update(overrides)
    return YoutubeIngester(source_store, **defaults)



def make_local_ingester(source_store: Any, **overrides: Any) -> Any:
    """LocalIngester のテスト用ファクトリ.

    fetcher は省略可。省略時は RealLocalFetcher を注入する（既存 tmp_path テストとの
    互換性維持のため。Fake は filesystem 抽象化のみで実 I/O テストを置き換えない）。
    """
    from rag.pipeline.ingesters.local import LocalIngester, RealLocalFetcher

    if "fetcher" not in overrides:
        overrides["fetcher"] = RealLocalFetcher()

    defaults: dict[str, Any] = {
        "supported_extensions": None,
        "http_mode_enabled": False,
        "allowed_dirs": None,
    }
    defaults.update(overrides)
    return LocalIngester(source_store, **defaults)


class StubAozoraFetcher:
    """AozoraFetcher Protocol のテスト用 stub.

    fetch_catalog_zip / fetch_xhtml に対する応答を辞書 / シーケンスで指定可能。
    旧 ``_mock_client`` のシンプルな差し替えを互換維持する目的で導入。
    """

    def __init__(
        self,
        *,
        catalog_zip: bytes | BaseException | None = None,
        xhtml_responses: list[Any] | None = None,
        xhtml_default: bytes | BaseException = b"<html><body>test</body></html>",
    ) -> None:
        self._catalog_zip = catalog_zip
        self._xhtml_responses = list(xhtml_responses) if xhtml_responses else None
        self._xhtml_default = xhtml_default
        self._xhtml_idx = 0

    async def __aenter__(self) -> "StubAozoraFetcher":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def fetch_catalog_zip(self) -> bytes:
        if isinstance(self._catalog_zip, BaseException):
            raise self._catalog_zip
        if self._catalog_zip is None:
            raise RuntimeError("StubAozoraFetcher: catalog_zip 未設定")
        return self._catalog_zip

    async def fetch_xhtml(self, github_url: str) -> bytes:
        del github_url
        if self._xhtml_responses is None:
            if isinstance(self._xhtml_default, BaseException):
                raise self._xhtml_default
            return self._xhtml_default
        if self._xhtml_idx >= len(self._xhtml_responses):
            raise IndexError(
                f"StubAozoraFetcher: xhtml 応答シーケンスを使い切りました（idx={self._xhtml_idx}）",
            )
        item = self._xhtml_responses[self._xhtml_idx]
        self._xhtml_idx += 1
        if isinstance(item, BaseException):
            raise item
        if not isinstance(item, bytes):
            raise TypeError(
                f"StubAozoraFetcher: xhtml 応答は bytes でなければなりません（{type(item)}）",
            )
        return item


def make_aozora_ingester(source_store: Any, **overrides: Any) -> Any:
    """AozoraIngester のテスト用ファクトリ.

    fetcher は省略可。省略時はデフォルトの StubAozoraFetcher を注入する。
    """
    from rag.pipeline.ingesters.aozora import AozoraIngester

    if "fetcher" not in overrides:
        overrides["fetcher"] = StubAozoraFetcher()

    defaults: dict[str, Any] = {
        "max_works": 200,
    }
    defaults.update(overrides)
    return AozoraIngester(source_store, **defaults)

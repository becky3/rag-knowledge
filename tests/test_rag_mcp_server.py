"""RAG MCPサーバーのテスト.

仕様: docs/specs/rag-knowledge.md
5つのRAGツール（rag_search, rag_add, rag_crawl, rag_delete, rag_stats）が
MCPサーバーとして公開されていることを検証する。
"""

from __future__ import annotations

from importlib import import_module
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rag.rag_knowledge import (
    BM25SearchItem,
    RawSearchResults,
    VectorSearchItem,
)
from rag.server import _configure_and_run, _reset_rag_service


@pytest.fixture(autouse=True)
def _reset_rag_global_state() -> None:
    """各テスト前にRAGサービスのグローバル状態をリセットする."""
    _reset_rag_service()


@pytest.mark.asyncio
async def test_rag_server_exposes_five_tools() -> None:
    """AC20: RAG MCPサーバーが5つのツールを公開すること."""
    mod = import_module("rag.server")
    server = mod.mcp

    tools = await server.list_tools()
    tool_names = {t.name for t in tools}

    expected = {"rag_search", "rag_add", "rag_crawl", "rag_delete", "rag_stats"}
    assert tool_names == expected, f"Expected {expected}, got {tool_names}"


@pytest.mark.asyncio
async def test_rag_server_tool_count() -> None:
    """AC20: RAG MCPサーバーのツール数が正確に5であること."""
    mod = import_module("rag.server")
    server = mod.mcp

    tools = await server.list_tools()
    assert len(tools) == 5


class TestRagSearchOutput:
    """rag_search ツールの出力フォーマットテスト（準Agentic Search, Issue #548）."""

    @pytest.fixture(autouse=True)
    def _patch_rag_service(self) -> None:
        """rag_search のテスト用に RAGKnowledgeService をモックする."""
        self.mock_service = AsyncMock()
        self.mock_service.get_full_page_text = AsyncMock(
            return_value="ページ全文テキスト"
        )
        self.mock_settings = MagicMock()
        self.mock_settings.rag_retrieval_count = 3

    async def test_output_contains_vector_and_bm25_sections(self) -> None:
        """出力にベクトル検索結果とBM25検索結果のセクションが含まれること（#548）."""
        mod = import_module("rag.server")

        self.mock_service.retrieve_raw_results = AsyncMock(
            return_value=RawSearchResults(
                vector_results=[
                    VectorSearchItem(
                        text="ベクトルの結果テキスト",
                        source_url="https://example.com/vec1",
                        distance=0.234,
                        chunk_index=0,
                    ),
                ],
                bm25_results=[
                    BM25SearchItem(
                        text="BM25の結果テキスト",
                        source_url="https://example.com/bm25_1",
                        score=4.521,
                        doc_id="doc1",
                    ),
                ],
            )
        )
        self.mock_service.get_full_page_text = AsyncMock(
            side_effect=lambda url: f"{url} のページ全文"
        )

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_search("テストクエリ")

        assert "## ベクトル検索結果 (意味的類似度)" in result
        assert "## BM25検索結果 (キーワード一致)" in result

    async def test_output_contains_source_urls(self) -> None:
        """出力に Source: URL 行が含まれること（#548）."""
        mod = import_module("rag.server")

        self.mock_service.retrieve_raw_results = AsyncMock(
            return_value=RawSearchResults(
                vector_results=[
                    VectorSearchItem(
                        text="テキスト",
                        source_url="https://example.com/page1",
                        distance=0.1,
                        chunk_index=0,
                    ),
                ],
                bm25_results=[],
            )
        )

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_search("テスト")

        assert "Source: https://example.com/page1" in result

    async def test_output_contains_raw_scores(self) -> None:
        """出力に生スコアが含まれること（#548）."""
        mod = import_module("rag.server")

        self.mock_service.retrieve_raw_results = AsyncMock(
            return_value=RawSearchResults(
                vector_results=[
                    VectorSearchItem(
                        text="テキスト",
                        source_url="https://example.com/v",
                        distance=0.567,
                        chunk_index=0,
                    ),
                ],
                bm25_results=[
                    BM25SearchItem(
                        text="テキスト",
                        source_url="https://example.com/b",
                        score=3.456,
                        doc_id="d1",
                    ),
                ],
            )
        )

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_search("テスト")

        assert "[distance=0.567]" in result
        assert "[score=3.456]" in result

    async def test_empty_results_returns_not_found_message(self) -> None:
        """0件時に「該当する情報が見つかりませんでした」が返ること."""
        mod = import_module("rag.server")

        self.mock_service.retrieve_raw_results = AsyncMock(
            return_value=RawSearchResults(
                vector_results=[],
                bm25_results=[],
            )
        )

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_search("存在しないクエリ")

        assert result == "該当する情報が見つかりませんでした"

    async def test_output_contains_full_page_text(self) -> None:
        """チャンクテキストの代わりにページ全文が返ること（#575）."""
        mod = import_module("rag.server")

        self.mock_service.retrieve_raw_results = AsyncMock(
            return_value=RawSearchResults(
                vector_results=[
                    VectorSearchItem(
                        text="チャンク断片",
                        source_url="https://example.com/page1",
                        distance=0.1,
                        chunk_index=0,
                    ),
                ],
                bm25_results=[],
            )
        )
        self.mock_service.get_full_page_text = AsyncMock(
            return_value="これはページ全文のテキストです。複数チャンクが結合されています。"
        )

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_search("テスト")

        assert "これはページ全文のテキストです。複数チャンクが結合されています。" in result
        assert "チャンク断片" not in result

    async def test_duplicate_url_cross_engine_shows_reference(self) -> None:
        """ベクトル→BM25で同一URLが重複した場合、参照テキストが出ること（#575）."""
        mod = import_module("rag.server")

        same_url = "https://example.com/same-page"
        self.mock_service.retrieve_raw_results = AsyncMock(
            return_value=RawSearchResults(
                vector_results=[
                    VectorSearchItem(
                        text="ベクトルチャンク",
                        source_url=same_url,
                        distance=0.1,
                        chunk_index=0,
                    ),
                ],
                bm25_results=[
                    BM25SearchItem(
                        text="BM25チャンク",
                        source_url=same_url,
                        score=5.0,
                        doc_id="doc1",
                    ),
                ],
            )
        )
        self.mock_service.get_full_page_text = AsyncMock(
            return_value="ページ全文テキスト"
        )

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_search("テスト")

        # ベクトル検索結果にはページ全文が出る
        assert "ページ全文テキスト" in result
        # BM25検索結果には参照テキストが出る
        assert "ベクトル検索結果 Result 1 に掲載済み" in result

    async def test_duplicate_url_within_same_engine(self) -> None:
        """同一エンジン内で同一URLが重複した場合、2回目以降は参照テキストが出ること（#575）."""
        mod = import_module("rag.server")

        same_url = "https://example.com/same-page"
        self.mock_service.retrieve_raw_results = AsyncMock(
            return_value=RawSearchResults(
                vector_results=[
                    VectorSearchItem(
                        text="チャンク1",
                        source_url=same_url,
                        distance=0.1,
                        chunk_index=0,
                    ),
                    VectorSearchItem(
                        text="チャンク2",
                        source_url=same_url,
                        distance=0.2,
                        chunk_index=1,
                    ),
                ],
                bm25_results=[],
            )
        )
        self.mock_service.get_full_page_text = AsyncMock(
            return_value="全文テキスト"
        )

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_search("テスト")

        # Result 1 にはページ全文が出る
        assert "全文テキスト" in result
        # Result 2 には参照テキストが出る
        assert "ベクトル検索結果 Result 1 に掲載済み" in result


class TestConfigureAndRun:
    """_configure_and_run の起動分岐テスト."""

    def test_stdio_mode_calls_run_without_transport(self) -> None:
        """stdio モードでは mcp.run() が引数なしで呼ばれること."""
        mod = import_module("rag.server")
        mock_settings = MagicMock()
        mock_settings.rag_transport = "stdio"

        with (
            patch.object(mod, "get_settings", return_value=mock_settings),
            patch.object(mod.mcp, "run") as mock_run,
        ):
            _configure_and_run()

        mock_run.assert_called_once_with()

    def test_http_mode_calls_run_with_streamable_http(self) -> None:
        """HTTP モードでは mcp.run(transport='streamable-http') が呼ばれること."""
        mod = import_module("rag.server")
        mock_settings = MagicMock()
        mock_settings.rag_transport = "http"
        mock_settings.rag_http_host = "0.0.0.0"
        mock_settings.rag_http_port = 9090
        mock_settings.rag_dns_rebinding_protection = True

        with (
            patch.object(mod, "get_settings", return_value=mock_settings),
            patch.object(mod.mcp, "run") as mock_run,
        ):
            _configure_and_run()

        mock_run.assert_called_once_with(transport="streamable-http")
        assert mod.mcp.settings.host == "0.0.0.0"
        assert mod.mcp.settings.port == 9090

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
from rag.web_crawler import CrawlPreviewPage


@pytest.fixture(autouse=True)
def _reset_rag_global_state() -> None:
    """各テスト前にRAGサービスのグローバル状態をリセットする."""
    _reset_rag_service()


@pytest.mark.asyncio
async def test_rag_server_exposes_six_tools() -> None:
    """RAG MCPサーバーが6つのツールを公開すること."""
    mod = import_module("rag.server")
    server = mod.mcp

    tools = await server.list_tools()
    tool_names = {t.name for t in tools}

    expected = {
        "rag_search", "rag_add", "rag_crawl", "rag_crawl_preview",
        "rag_delete", "rag_stats",
    }
    assert tool_names == expected, f"Expected {expected}, got {tool_names}"


@pytest.mark.asyncio
async def test_rag_server_tool_count() -> None:
    """RAG MCPサーバーのツール数が正確に6であること."""
    mod = import_module("rag.server")
    server = mod.mcp

    tools = await server.list_tools()
    assert len(tools) == 6


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
        self.mock_settings.rag_max_response_chars = None

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

    def test_keyboard_interrupt_graceful_shutdown(self) -> None:
        """Ctrl+C (KeyboardInterrupt) で終了コード130で終了すること (#42)."""
        mod = import_module("rag.server")
        mock_settings = MagicMock()
        mock_settings.rag_transport = "http"
        mock_settings.rag_http_host = "127.0.0.1"
        mock_settings.rag_http_port = 8080
        mock_settings.rag_dns_rebinding_protection = True

        with (
            patch.object(mod, "get_settings", return_value=mock_settings),
            patch.object(
                mod.mcp, "run", side_effect=KeyboardInterrupt
            ),
            patch.object(mod.logger, "info") as mock_log,
            pytest.raises(SystemExit, match="130"),
        ):
            _configure_and_run()

        mock_log.assert_called_once_with("MCP server shut down")

    def test_stdio_keyboard_interrupt_graceful_shutdown(self) -> None:
        """stdio モードでも KeyboardInterrupt で終了コード130で終了すること (#42)."""
        mod = import_module("rag.server")
        mock_settings = MagicMock()
        mock_settings.rag_transport = "stdio"

        with (
            patch.object(mod, "get_settings", return_value=mock_settings),
            patch.object(
                mod.mcp, "run", side_effect=KeyboardInterrupt
            ),
            patch.object(mod.logger, "info") as mock_log,
            pytest.raises(SystemExit, match="130"),
        ):
            _configure_and_run()

        mock_log.assert_called_once_with("MCP server shut down")

    def test_shutdown_log_on_normal_exit(self) -> None:
        """正常終了時もシャットダウンログが出力されること (#42)."""
        mod = import_module("rag.server")
        mock_settings = MagicMock()
        mock_settings.rag_transport = "stdio"

        with (
            patch.object(mod, "get_settings", return_value=mock_settings),
            patch.object(mod.mcp, "run"),
            patch.object(mod.logger, "info") as mock_log,
        ):
            _configure_and_run()

        mock_log.assert_called_once_with("MCP server shut down")


class TestRagSearchResponseTruncation:
    """rag_search レスポンスサイズ上限ガードのテスト（#26）."""

    @pytest.fixture(autouse=True)
    def _patch_rag_service(self) -> None:
        """rag_search のテスト用に RAGKnowledgeService をモックする."""
        self.mock_service = AsyncMock()
        self.mock_settings = MagicMock()
        self.mock_settings.rag_retrieval_count = 3

    async def test_response_not_truncated_when_limit_is_none(self) -> None:
        """上限未設定時はレスポンスがそのまま返ること（#26）."""
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
        self.mock_service.get_full_page_text = AsyncMock(
            return_value="ページ全文テキスト"
        )
        self.mock_settings.rag_max_response_chars = None

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_search("テスト")

        assert "切り詰めました" not in result
        assert "ページ全文テキスト" in result

    async def test_response_not_truncated_when_within_limit(self) -> None:
        """レスポンスが上限以下の場合はそのまま返ること（#26）."""
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
        self.mock_service.get_full_page_text = AsyncMock(
            return_value="短いテキスト"
        )
        self.mock_settings.rag_max_response_chars = 100000

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_search("テスト")

        assert "切り詰めました" not in result
        assert "短いテキスト" in result

    async def test_response_truncated_when_exceeds_limit(self) -> None:
        """レスポンスが上限を超えた場合にトランケートされること（#26）."""
        mod = import_module("rag.server")

        long_text = "あ" * 500
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
        self.mock_service.get_full_page_text = AsyncMock(
            return_value=long_text
        )
        self.mock_settings.rag_max_response_chars = 100

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_search("テスト")

        assert "切り詰めました" in result
        assert "100" in result

    async def test_truncated_response_starts_with_original_content(self) -> None:
        """トランケートされたレスポンスが元の内容の先頭部分を含むこと（#26）."""
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
        self.mock_service.get_full_page_text = AsyncMock(
            return_value="あ" * 1000
        )
        self.mock_settings.rag_max_response_chars = 50

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_search("テスト")

        # トランケート通知の前の部分が正確に50文字であること
        truncation_marker = "\n\n…（レスポンスが上限の"
        marker_pos = result.index(truncation_marker)
        assert marker_pos == 50

    async def test_empty_results_not_affected_by_limit(self) -> None:
        """0件結果は上限設定に影響されないこと（#26）."""
        mod = import_module("rag.server")

        self.mock_service.retrieve_raw_results = AsyncMock(
            return_value=RawSearchResults(
                vector_results=[],
                bm25_results=[],
            )
        )
        self.mock_settings.rag_max_response_chars = 10

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_search("テスト")

        assert result == "該当する情報が見つかりませんでした"


class TestRagCrawlPreviewTool:
    """rag_crawl_preview ツールのテスト（Issue #45）."""

    @pytest.mark.asyncio
    async def test_crawl_preview_returns_page_list(self) -> None:
        """クロール対象ページの一覧テキストが返ること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.crawl_preview = AsyncMock(
            return_value=[
                CrawlPreviewPage(url="https://example.com/page1", title="ページ1"),
                CrawlPreviewPage(url="https://example.com/page2", title="ページ2"),
            ]
        )

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            result = await mod.rag_crawl_preview("https://example.com/index")

        assert "クロール対象: 2ページ" in result
        assert "ページ1" in result
        assert "https://example.com/page1" in result
        assert "ページ2" in result
        assert "https://example.com/page2" in result

    @pytest.mark.asyncio
    async def test_crawl_preview_empty_result(self) -> None:
        """対象ページが見つからない場合のメッセージが返ること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.crawl_preview = AsyncMock(return_value=[])

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            result = await mod.rag_crawl_preview("https://example.com/empty")

        assert result == "対象ページが見つかりませんでした"

    @pytest.mark.asyncio
    async def test_crawl_preview_shows_fallback_title(self) -> None:
        """タイトル取得不可の場合にフォールバックテキストが表示されること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.crawl_preview = AsyncMock(
            return_value=[
                CrawlPreviewPage(url="https://example.com/page1", title=""),
            ]
        )

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            result = await mod.rag_crawl_preview("https://example.com/index")

        assert "(タイトル取得不可)" in result

    @pytest.mark.asyncio
    async def test_crawl_preview_with_pattern(self) -> None:
        """patternパラメータがservice.crawl_previewに渡されること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.crawl_preview = AsyncMock(return_value=[])

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            await mod.rag_crawl_preview(
                "https://example.com/index", pattern=r"\.html$"
            )

        mock_service.crawl_preview.assert_called_once_with(
            "https://example.com/index", url_pattern=r"\.html$"
        )

    @pytest.mark.asyncio
    async def test_crawl_preview_value_error(self) -> None:
        """URL検証エラー時にエラーメッセージが返ること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.crawl_preview = AsyncMock(
            side_effect=ValueError("許可されていないスキームです")
        )

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            result = await mod.rag_crawl_preview("ftp://example.com")

        assert "エラー:" in result
        assert "許可されていないスキームです" in result

    @pytest.mark.asyncio
    async def test_crawl_preview_unexpected_error(self) -> None:
        """予期しないエラー時にエラーメッセージが返ること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.crawl_preview = AsyncMock(
            side_effect=RuntimeError("Unexpected")
        )

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            result = await mod.rag_crawl_preview("https://example.com/index")

        assert "エラー: プレビューに失敗しました" in result


class TestRagStatsOutput:
    """rag_stats ツールの出力フォーマットテスト（Issue #25）."""

    @pytest.fixture(autouse=True)
    def _patch_rag_service(self) -> None:
        """rag_stats のテスト用に RAGKnowledgeService をモックする."""
        self.mock_service = AsyncMock()
        self.mock_settings = MagicMock()
        self.mock_settings.rag_stats_max_sources = 100

    async def test_stats_contains_sources_section(self) -> None:
        """蓄積データ概要セクションが出力に含まれること（#25）."""
        mod = import_module("rag.server")

        self.mock_service.get_stats = AsyncMock(
            return_value={
                "total_chunks": 150,
                "source_count": 3,
                "sources": [
                    {
                        "domain": "example.com",
                        "pages": [
                            {
                                "url": "https://example.com/page1",
                                "title": "テストページ1",
                                "chunks": 5,
                            },
                            {
                                "url": "https://example.com/page2",
                                "title": "テストページ2",
                                "chunks": 3,
                            },
                        ],
                    },
                    {
                        "domain": "other.com",
                        "pages": [
                            {
                                "url": "https://other.com/doc",
                                "title": "ドキュメント",
                                "chunks": 10,
                            },
                        ],
                    },
                ],
            }
        )

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_stats()

        assert "ナレッジベース統計:" in result
        assert "総チャンク数: 150" in result
        assert "ソースURL数: 3" in result
        assert "蓄積データ概要:" in result
        assert "[example.com] (2ページ)" in result
        assert "テストページ1 (https://example.com/page1)" in result
        assert "テストページ2 (https://example.com/page2)" in result
        assert "[other.com] (1ページ)" in result
        assert "ドキュメント (https://other.com/doc)" in result

    async def test_stats_empty_sources(self) -> None:
        """ソースが空の場合は蓄積データ概要セクションが含まれないこと（#25）."""
        mod = import_module("rag.server")

        self.mock_service.get_stats = AsyncMock(
            return_value={
                "total_chunks": 0,
                "source_count": 0,
                "sources": [],
            }
        )

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_stats()

        assert "ナレッジベース統計:" in result
        assert "総チャンク数: 0" in result
        assert "蓄積データ概要:" not in result

    async def test_stats_truncation_when_exceeds_limit(self) -> None:
        """表示上限を超える場合に省略メッセージが出ること（#25）."""
        mod = import_module("rag.server")

        # 3ページ分のデータを用意し、上限を2に設定
        self.mock_settings.rag_stats_max_sources = 2
        self.mock_service.get_stats = AsyncMock(
            return_value={
                "total_chunks": 30,
                "source_count": 3,
                "sources": [
                    {
                        "domain": "example.com",
                        "pages": [
                            {
                                "url": "https://example.com/page1",
                                "title": "ページ1",
                                "chunks": 10,
                            },
                            {
                                "url": "https://example.com/page2",
                                "title": "ページ2",
                                "chunks": 10,
                            },
                            {
                                "url": "https://example.com/page3",
                                "title": "ページ3",
                                "chunks": 10,
                            },
                        ],
                    },
                ],
            }
        )

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_stats()

        # 最初の2ページは表示される
        assert "ページ1" in result
        assert "ページ2" in result
        # 3ページ目は省略される
        assert "ページ3" not in result
        assert "表示上限 2 件に達したため省略されたソースがあります" in result

    async def test_stats_fallback_title(self) -> None:
        """タイトルが空の場合にフォールバックテキストが表示されること（#25）."""
        mod = import_module("rag.server")

        self.mock_service.get_stats = AsyncMock(
            return_value={
                "total_chunks": 5,
                "source_count": 1,
                "sources": [
                    {
                        "domain": "example.com",
                        "pages": [
                            {
                                "url": "https://example.com/page1",
                                "title": "",
                                "chunks": 5,
                            },
                        ],
                    },
                ],
            }
        )

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_stats()

        assert "(タイトル取得不可)" in result

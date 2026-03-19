"""RAG MCPサーバーのテスト.

仕様: docs/specs/rag-knowledge.md, docs/specs/search-response.md
11個のRAGツール（rag_search, rag_get_document, rag_add, rag_crawl, rag_crawl_preview,
rag_crawl_zenn, rag_crawl_bluesky, rag_add_document, rag_crawl_documents, rag_delete, rag_stats）が
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
async def test_rag_server_exposes_eleven_tools() -> None:
    """RAG MCPサーバーが11個のツールを公開すること."""
    mod = import_module("rag.server")
    server = mod.mcp

    tools = await server.list_tools()
    tool_names = {t.name for t in tools}

    expected = {
        "rag_search", "rag_get_document", "rag_add", "rag_crawl",
        "rag_crawl_preview", "rag_crawl_zenn", "rag_crawl_bluesky",
        "rag_add_document", "rag_crawl_documents", "rag_delete", "rag_stats",
    }
    assert tool_names == expected, f"Expected {expected}, got {tool_names}"


@pytest.mark.asyncio
async def test_rag_server_tool_count() -> None:
    """RAG MCPサーバーのツール数が正確に11であること."""
    mod = import_module("rag.server")
    server = mod.mcp

    tools = await server.list_tools()
    assert len(tools) == 11


class TestRagSearchOutput:
    """rag_search ツールのチャンク単位出力フォーマットテスト（#251）."""

    @pytest.fixture(autouse=True)
    def _patch_rag_service(self) -> None:
        """rag_search のテスト用に RAGKnowledgeService をモックする."""
        self.mock_service = AsyncMock()
        self.mock_settings = MagicMock()
        self.mock_settings.rag_retrieval_count = 3

    async def test_output_contains_vector_and_bm25_sections(self) -> None:
        """出力にベクトル検索結果とBM25検索結果のセクションが含まれること."""
        mod = import_module("rag.server")

        self.mock_service.retrieve_raw_results = AsyncMock(
            return_value=RawSearchResults(
                vector_results=[
                    VectorSearchItem(
                        text="ベクトルの結果テキスト",
                        source_url="https://example.com/vec1",
                        distance=0.234,
                        chunk_index=2,
                        title="ガイドページ",
                        source_type="web",
                        total_chunks=15,
                    ),
                ],
                bm25_results=[
                    BM25SearchItem(
                        text="BM25の結果テキスト",
                        source_url="https://example.com/bm25_1",
                        score=4.521,
                        doc_id="doc1",
                        chunk_index=4,
                        title="サンプル記事",
                        source_type="zenn",
                        total_chunks=20,
                    ),
                ],
            )
        )

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_search("テストクエリ")

        assert "## ベクトル検索結果 (意味的類似度)" in result
        assert "## BM25 検索結果 (キーワード一致)" in result

    async def test_output_contains_chunk_metadata(self) -> None:
        """各結果にSource/Title/Chunk/Typeメタデータが含まれること."""
        mod = import_module("rag.server")

        self.mock_service.retrieve_raw_results = AsyncMock(
            return_value=RawSearchResults(
                vector_results=[
                    VectorSearchItem(
                        text="チャンクテキスト",
                        source_url="https://example.com/docs/guide",
                        distance=0.234,
                        chunk_index=2,
                        title="ガイドページ",
                        source_type="web",
                        total_chunks=15,
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

        assert "Source: https://example.com/docs/guide" in result
        assert "Title: ガイドページ" in result
        assert "Chunk: 3/15" in result
        assert "Type: web" in result
        assert "チャンクテキスト" in result

    async def test_chunk_position_with_unknown_total(self) -> None:
        """total_chunks=0（レガシーデータ）のとき Chunk: N/? と表示されること."""
        mod = import_module("rag.server")

        self.mock_service.retrieve_raw_results = AsyncMock(
            return_value=RawSearchResults(
                vector_results=[
                    VectorSearchItem(
                        text="テキスト",
                        source_url="https://example.com/page1",
                        distance=0.1,
                        chunk_index=4,
                        total_chunks=0,
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

        assert "Chunk: 5/?" in result

    async def test_output_contains_raw_scores(self) -> None:
        """出力に生スコアが含まれること."""
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

    async def test_chunk_text_returned_directly(self) -> None:
        """チャンクテキストがそのまま返却されること（ページ全文ではない）."""
        mod = import_module("rag.server")

        self.mock_service.retrieve_raw_results = AsyncMock(
            return_value=RawSearchResults(
                vector_results=[
                    VectorSearchItem(
                        text="これはチャンクテキストです",
                        source_url="https://example.com/page1",
                        distance=0.1,
                        chunk_index=0,
                        title="ページ1",
                        source_type="web",
                        total_chunks=5,
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

        assert "これはチャンクテキストです" in result

    async def test_same_source_different_chunks_shown_individually(self) -> None:
        """同一ソースの異なるチャンクが個別に表示されること."""
        mod = import_module("rag.server")

        self.mock_service.retrieve_raw_results = AsyncMock(
            return_value=RawSearchResults(
                vector_results=[
                    VectorSearchItem(
                        text="チャンク1のテキスト",
                        source_url="https://example.com/page",
                        distance=0.1,
                        chunk_index=0,
                        title="ページ",
                        source_type="web",
                        total_chunks=3,
                    ),
                    VectorSearchItem(
                        text="チャンク2のテキスト",
                        source_url="https://example.com/page",
                        distance=0.2,
                        chunk_index=2,
                        title="ページ",
                        source_type="web",
                        total_chunks=3,
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

        assert "チャンク1のテキスト" in result
        assert "チャンク2のテキスト" in result
        assert "Chunk: 1/3" in result
        assert "Chunk: 3/3" in result


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


class TestRagGetDocumentTool:
    """rag_get_document ツールのテスト（#251）."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self) -> None:
        """テスト用の設定をモックする."""
        self.mock_settings = MagicMock()
        self.mock_settings.source_store_dir = "/tmp/source_store"
        self.mock_settings.converted_store_dir = "/tmp/converted_store"
        self.mock_settings.rag_max_response_chars = None

    async def test_format_text_returns_document(self) -> None:
        """format=text でドキュメントが返ること."""
        from rag.rag_knowledge import DocumentResult

        mod = import_module("rag.server")
        mock_result = DocumentResult(
            source_id="https://example.com/docs/guide",
            title="ガイドページ",
            source_type="web",
            format="text",
            content="これはドキュメント全文です。",
        )

        with (
            patch.object(mod, "get_settings", return_value=self.mock_settings),
            patch.object(mod, "get_document", return_value=mock_result),
        ):
            result = await mod.rag_get_document("https://example.com/docs/guide")

        assert "Source: https://example.com/docs/guide" in result
        assert "Title: ガイドページ" in result
        assert "Type: web" in result
        assert "Format: text" in result
        assert "これはドキュメント全文です。" in result

    async def test_format_original_returns_document(self) -> None:
        """format=original でドキュメントが返ること."""
        from rag.rag_knowledge import DocumentResult

        mod = import_module("rag.server")
        mock_result = DocumentResult(
            source_id="https://example.com/page.html",
            title="HTMLページ",
            source_type="web",
            format="original",
            content="<html>...</html>",
        )

        with (
            patch.object(mod, "get_settings", return_value=self.mock_settings),
            patch.object(mod, "get_document", return_value=mock_result),
        ):
            result = await mod.rag_get_document(
                "https://example.com/page.html", format="original"
            )

        assert "Format: original" in result
        assert "<html>...</html>" in result

    async def test_missing_source_returns_error(self) -> None:
        """存在しない source_id でエラーが返ること."""
        from rag.rag_knowledge import DocumentResult

        mod = import_module("rag.server")
        mock_result = DocumentResult(
            source_id="nonexistent",
            title="",
            source_type="",
            format="text",
            content="",
            error="ソースが見つかりません: nonexistent",
        )

        with (
            patch.object(mod, "get_settings", return_value=self.mock_settings),
            patch.object(mod, "get_document", return_value=mock_result),
        ):
            result = await mod.rag_get_document("nonexistent")

        assert "エラー:" in result
        assert "ソースが見つかりません" in result

    async def test_truncation_with_max_response_chars(self) -> None:
        """rag_max_response_chars でトランケーションされ、通知文込みで上限内に収まること."""
        from rag.rag_knowledge import DocumentResult

        mod = import_module("rag.server")
        max_chars = 200
        self.mock_settings.rag_max_response_chars = max_chars

        long_content = "あ" * 1000
        mock_result = DocumentResult(
            source_id="https://example.com/long",
            title="Long Page",
            source_type="web",
            format="text",
            content=long_content,
        )

        with (
            patch.object(mod, "get_settings", return_value=self.mock_settings),
            patch.object(mod, "get_document", return_value=mock_result),
        ):
            result = await mod.rag_get_document("https://example.com/long")

        assert "トランケートされました" in result
        assert "--output" in result
        # 通知文込みで上限以内に収まること
        assert len(result) <= max_chars

    async def test_no_truncation_when_limit_is_none(self) -> None:
        """rag_max_response_chars=None のときトランケーションされないこと."""
        from rag.rag_knowledge import DocumentResult

        mod = import_module("rag.server")
        self.mock_settings.rag_max_response_chars = None

        long_content = "あ" * 1000
        mock_result = DocumentResult(
            source_id="https://example.com/long",
            title="Long Page",
            source_type="web",
            format="text",
            content=long_content,
        )

        with (
            patch.object(mod, "get_settings", return_value=self.mock_settings),
            patch.object(mod, "get_document", return_value=mock_result),
        ):
            result = await mod.rag_get_document("https://example.com/long")

        assert "トランケートされました" not in result
        assert long_content in result

    async def test_binary_file_returns_info(self) -> None:
        """format=original でバイナリファイルの場合、MIME情報が返ること."""
        from rag.rag_knowledge import DocumentResult

        mod = import_module("rag.server")
        mock_result = DocumentResult(
            source_id="https://example.com/doc.pdf",
            title="PDF Doc",
            source_type="web",
            format="original",
            content="バイナリファイルです（MIME: application/pdf, サイズ: 1,234 bytes）。\nテキスト形式で取得するには format=text を指定してください。",
            is_binary=True,
        )

        with (
            patch.object(mod, "get_settings", return_value=self.mock_settings),
            patch.object(mod, "get_document", return_value=mock_result),
        ):
            result = await mod.rag_get_document(
                "https://example.com/doc.pdf", format="original"
            )

        assert "application/pdf" in result
        assert "format=text" in result

    async def test_invalid_format_returns_error(self) -> None:
        """無効な format 値でエラーが返ること."""
        mod = import_module("rag.server")

        result = await mod.rag_get_document("source", format="invalid")

        assert "無効な format" in result


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


class TestRagCrawlZennTool:
    """rag_crawl_zenn ツールのテスト（#168）."""

    @pytest.mark.asyncio
    async def test_constrained_client_receives_settings(self) -> None:
        """ConstrainedClient に設定値が正しく渡されること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.ingest_content = AsyncMock(return_value=5)
        mock_settings = MagicMock()
        mock_settings.rag_zenn_max_articles = 50
        mock_settings.rag_zenn_request_timeout = 15
        mock_settings.rag_zenn_request_interval = 0.3

        mock_client_instance = AsyncMock()
        mock_client_instance.get = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                json=MagicMock(return_value={"articles": [], "next_page": None}),
            )
        )
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(
            return_value=mock_client_instance
        )
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(mod, "_get_rag_service", return_value=mock_service),
            patch.object(mod, "get_settings", return_value=mock_settings),
            patch.object(mod, "ConstrainedClient", mock_client_cls),
        ):
            await mod.rag_crawl_zenn("testuser")

        mock_client_cls.assert_called_once_with(
            request_timeout=15,
            request_interval=0.3,
        )

    @pytest.mark.asyncio
    async def test_empty_username_returns_error(self) -> None:
        """空のユーザー名でエラーメッセージを返すこと."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_settings = MagicMock()
        mock_settings.rag_zenn_max_articles = 50

        with (
            patch.object(mod, "_get_rag_service", return_value=mock_service),
            patch.object(mod, "get_settings", return_value=mock_settings),
        ):
            result = await mod.rag_crawl_zenn("")

        assert "エラー" in result
        assert "username" in result

    @pytest.mark.asyncio
    async def test_invalid_max_articles_returns_error(self) -> None:
        """不正な max_articles でエラーメッセージを返すこと."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_settings = MagicMock()
        mock_settings.rag_zenn_max_articles = 50

        with (
            patch.object(mod, "_get_rag_service", return_value=mock_service),
            patch.object(mod, "get_settings", return_value=mock_settings),
        ):
            result = await mod.rag_crawl_zenn("testuser", max_articles=-1)

        assert "エラー" in result

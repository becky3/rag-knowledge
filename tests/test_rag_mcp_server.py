"""RAG MCPサーバーのテスト.

仕様: docs/specs/rag-knowledge.md, docs/specs/search-response.md, docs/specs/rebuild-stats.md,
      docs/specs/site-ingest.md
16個のRAGツール（rag_search, rag_get_document, rag_add, rag_crawl, rag_crawl_preview,
rag_crawl_zenn, rag_crawl_bluesky, rag_add_youtube, rag_crawl_youtube,
rag_add_document, rag_crawl_documents, rag_add_journal,
rag_site_ingest, rag_delete, rag_rebuild, rag_stats）が
MCPサーバーとして公開されていることを検証する。
"""

from __future__ import annotations

import contextlib
from importlib import import_module
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rag.rag_knowledge import (
    BM25SearchItem,
    RawSearchResults,
    VectorSearchItem,
)
from rag.server import _configure_and_run, _reset_pipeline_controller, _reset_rag_service


@pytest.fixture(autouse=True)
def _reset_rag_global_state() -> None:
    """各テスト前にRAGサービスのグローバル状態をリセットする."""
    _reset_rag_service()
    _reset_pipeline_controller()


@pytest.mark.asyncio
async def test_rag_server_exposes_tools() -> None:
    """RAG MCPサーバーが21個のツールを公開すること."""
    mod = import_module("rag.server")
    server = mod.mcp

    tools = await server.list_tools()
    tool_names = {t.name for t in tools}

    expected = {
        "rag_search", "rag_get_document", "rag_add", "rag_crawl",
        "rag_crawl_preview", "rag_crawl_zenn", "rag_crawl_bluesky",
        "rag_add_youtube", "rag_crawl_youtube",
        "rag_add_document", "rag_add_journal", "rag_crawl_documents",
        "rag_site_ingest",
        "rag_update_aozora_catalog", "rag_search_aozora",
        "rag_add_aozora", "rag_crawl_aozora",
        "rag_delete", "rag_rebuild", "rag_stats",
        "rag_list_recent",
    }
    assert tool_names == expected, f"Expected {expected}, got {tool_names}"


@pytest.mark.asyncio
async def test_rag_server_tool_count() -> None:
    """RAG MCPサーバーのツール数が正確に21であること."""
    mod = import_module("rag.server")
    server = mod.mcp

    tools = await server.list_tools()
    assert len(tools) == 21


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

    def _mock_preview_context(
        self,
        mod: object,
        preview_return: list[dict[str, str]] | None = None,
        preview_side_effect: Exception | None = None,
    ) -> contextlib.AbstractContextManager[AsyncMock]:
        """crawl_preview 用モックコンテキストを生成する."""
        from contextlib import contextmanager

        mock_settings = MagicMock()
        mock_settings.rag_crawl_default_depth = 1
        mock_settings.source_store_dir = "/tmp/test_source_store"
        mock_settings.rag_crawl_request_timeout = 10
        mock_settings.rag_crawl_delay_sec = 0.5

        mock_ingester_instance = AsyncMock()
        if preview_side_effect:
            mock_ingester_instance.crawl_preview = AsyncMock(
                side_effect=preview_side_effect,
            )
        else:
            mock_ingester_instance.crawl_preview = AsyncMock(
                return_value=preview_return if preview_return is not None else [],
            )

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        @contextmanager
        def ctx():
            with (
                patch.object(mod, "get_settings", return_value=mock_settings),
                patch("pathlib.Path.mkdir"),
                patch.object(mod, "SourceStore", return_value=MagicMock()),
                patch.object(mod, "_create_web_ingester", return_value=mock_ingester_instance),
                patch.object(mod, "ConstrainedClient", return_value=mock_client),
            ):
                yield mock_ingester_instance

        return ctx()

    @pytest.mark.asyncio
    async def test_crawl_preview_returns_page_list(self) -> None:
        """クロール対象ページの一覧テキストが返ること."""
        mod = import_module("rag.server")
        pages = [
            {"url": "https://example.com/page1", "title": "ページ1"},
            {"url": "https://example.com/page2", "title": "ページ2"},
        ]

        with self._mock_preview_context(mod, preview_return=pages):
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

        with self._mock_preview_context(mod, preview_return=[]):
            result = await mod.rag_crawl_preview("https://example.com/empty")

        assert result == "対象ページが見つかりませんでした"

    @pytest.mark.asyncio
    async def test_crawl_preview_shows_fallback_title(self) -> None:
        """タイトル取得不可の場合にフォールバックテキストが表示されること."""
        mod = import_module("rag.server")
        pages = [{"url": "https://example.com/page1", "title": ""}]

        with self._mock_preview_context(mod, preview_return=pages):
            result = await mod.rag_crawl_preview("https://example.com/index")

        assert "(タイトル取得不可)" in result

    @pytest.mark.asyncio
    async def test_crawl_preview_with_pattern(self) -> None:
        """patternパラメータがcrawl_previewに渡されること."""
        mod = import_module("rag.server")

        with self._mock_preview_context(mod, preview_return=[]) as mock_ingester:
            await mod.rag_crawl_preview(
                "https://example.com/index", pattern=r"\.html$"
            )

        mock_ingester.crawl_preview.assert_called_once()
        call_kwargs = mock_ingester.crawl_preview.call_args
        assert call_kwargs[1].get("pattern") == r"\.html$"

    @pytest.mark.asyncio
    async def test_crawl_preview_value_error(self) -> None:
        """URL検証エラー時にエラーメッセージが返ること."""
        mod = import_module("rag.server")

        with self._mock_preview_context(
            mod, preview_side_effect=ValueError("許可されていないスキームです"),
        ):
            result = await mod.rag_crawl_preview("ftp://example.com")

        assert "エラー:" in result
        assert "許可されていないスキームです" in result

    @pytest.mark.asyncio
    async def test_crawl_preview_unexpected_error(self) -> None:
        """予期しないエラー時にエラーメッセージが返ること."""
        mod = import_module("rag.server")

        with self._mock_preview_context(
            mod, preview_side_effect=RuntimeError("Unexpected"),
        ):
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
        self.mock_settings.source_store_dir = ""
        self.mock_settings.converted_store_dir = ""

    async def test_stats_contains_domain_summary(self) -> None:
        """インデックスセクションにドメイン別サマリが含まれること."""
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
                                "title": "Page1",
                                "chunks": 5,
                            },
                            {
                                "url": "https://example.com/page2",
                                "title": "Page2",
                                "chunks": 3,
                            },
                        ],
                    },
                    {
                        "domain": "other.com",
                        "pages": [
                            {
                                "url": "https://other.com/doc",
                                "title": "Doc",
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

        assert "📊 RAG Knowledge 統計" in result
        assert "総チャンク数: 150" in result
        assert "ソース数: 3" in result
        assert "example.com: 2 pages (8 chunks)" in result
        assert "other.com: 1 pages (10 chunks)" in result

    async def test_stats_empty_sources(self) -> None:
        """ソースが空の場合はドメイン別が含まれないこと."""
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

        assert "📊 RAG Knowledge 統計" in result
        assert "総チャンク数: 0" in result
        assert "ドメイン別:" not in result

    async def test_stats_domain_truncation_when_exceeds_limit(self) -> None:
        """ドメイン表示上限を超える場合に省略メッセージが出ること."""
        mod = import_module("rag.server")

        # 3ドメイン分のデータを用意し、上限を2に設定
        self.mock_settings.rag_stats_max_sources = 2
        self.mock_service.get_stats = AsyncMock(
            return_value={
                "total_chunks": 30,
                "source_count": 3,
                "sources": [
                    {
                        "domain": f"domain{i}.com",
                        "pages": [
                            {
                                "url": f"https://domain{i}.com/p",
                                "title": "P",
                                "chunks": 10,
                            },
                        ],
                    }
                    for i in range(3)
                ],
            }
        )

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_stats()

        assert "domain0.com" in result
        assert "domain1.com" in result
        assert "domain2.com" not in result
        assert "以下省略" in result

    async def test_stats_four_sections(self) -> None:
        """4セクション構成の出力フォーマット."""
        mod = import_module("rag.server")

        self.mock_service.get_stats = AsyncMock(
            return_value={
                "total_chunks": 5,
                "source_count": 1,
                "sources": [],
            }
        )

        with (
            patch.object(mod, "_get_rag_service", return_value=self.mock_service),
            patch.object(mod, "get_settings", return_value=self.mock_settings),
        ):
            result = await mod.rag_stats()

        assert "■ source_store" in result
        assert "■ converted_store" in result
        assert "■ インデックス" in result
        assert "■ パイプライン" in result


class TestRagCrawlZennTool:
    """rag_crawl_zenn ツールのテスト（#168, CLI サブプロセス移行後）."""

    @pytest.mark.asyncio
    async def test_crawl_zenn_delegates_to_cli_subprocess(self) -> None:
        """rag_crawl_zenn が _run_cli_subprocess に正しい引数を渡すこと."""
        mod = import_module("rag.server")

        mock_result = {
            "placed": 5, "skipped": 2, "overwritten": 0, "errors": 0,
            "error_details": [],
        }
        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock, return_value=mock_result,
        ) as mock_cli:
            result = await mod.rag_crawl_zenn("testuser")

        mock_cli.assert_called_once_with(
            "crawl-zenn", ["testuser"], ctx=None,
        )
        assert "5件配置" in result

    @pytest.mark.asyncio
    async def test_crawl_zenn_passes_options(self) -> None:
        """content_type, max_articles, force がCLI引数に変換されること."""
        mod = import_module("rag.server")

        mock_result = {
            "placed": 0, "skipped": 0, "overwritten": 0, "errors": 0,
            "error_details": [],
        }
        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock, return_value=mock_result,
        ) as mock_cli:
            await mod.rag_crawl_zenn(
                "testuser", max_articles=10, content_type="articles", force=True,
            )

        call_args = mock_cli.call_args
        cli_args = call_args[0][1]
        assert "--content-type" in cli_args
        assert "articles" in cli_args
        assert "--max-articles" in cli_args
        assert "10" in cli_args
        assert "--force" in cli_args

    @pytest.mark.asyncio
    async def test_crawl_zenn_lock_conflict(self) -> None:
        """ロック競合時に専用エラーメッセージを返すこと."""
        mod = import_module("rag.server")

        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock,
            side_effect=mod.CLISubprocessError("ロック取得失敗", lock_conflict=True),
        ):
            result = await mod.rag_crawl_zenn("testuser")

        assert "別のインジェストが実行中" in result

    @pytest.mark.asyncio
    async def test_crawl_zenn_cli_error(self) -> None:
        """CLI エラー時にエラーメッセージを返すこと."""
        mod = import_module("rag.server")

        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock,
            side_effect=mod.CLISubprocessError("username が空です"),
        ):
            result = await mod.rag_crawl_zenn("")

        assert "エラー" in result
        assert "Zenn" in result


# --- CLI サブプロセス基盤テスト（#409） ---


class TestIsLockConflictError:
    """_is_lock_conflict_error のテスト."""

    def test_detects_lock_conflict_english(self) -> None:
        """'lock conflict' を含むメッセージでTrue."""
        mod = import_module("rag.server")
        assert mod._is_lock_conflict_error("lock conflict detected") is True

    def test_detects_already_locked(self) -> None:
        """'already locked' を含むメッセージでTrue."""
        mod = import_module("rag.server")
        assert mod._is_lock_conflict_error("file is already locked") is True

    def test_detects_japanese_lock_conflict(self) -> None:
        """'ロック競合' を含むメッセージでTrue."""
        mod = import_module("rag.server")
        assert mod._is_lock_conflict_error("別のインジェストが実行中です（ロック競合）") is True

    def test_case_insensitive(self) -> None:
        """大文字小文字を区別しないこと."""
        mod = import_module("rag.server")
        assert mod._is_lock_conflict_error("LOCK CONFLICT detected") is True

    def test_no_false_positive_on_lock_alone(self) -> None:
        """'lock' 単独では誤判定しないこと."""
        mod = import_module("rag.server")
        assert mod._is_lock_conflict_error("unlock failed") is False
        assert mod._is_lock_conflict_error("file lock acquisition failed") is False

    def test_no_match(self) -> None:
        """ロック関連キーワードを含まないメッセージでFalse."""
        mod = import_module("rag.server")
        assert mod._is_lock_conflict_error("general error occurred") is False


class TestCLISubprocessError:
    """CLISubprocessError のテスト."""

    def test_default_lock_conflict_false(self) -> None:
        """lock_conflict のデフォルトが False."""
        mod = import_module("rag.server")
        err = mod.CLISubprocessError("test error")
        assert err.lock_conflict is False
        assert str(err) == "test error"

    def test_lock_conflict_true(self) -> None:
        """lock_conflict=True が設定されること."""
        mod = import_module("rag.server")
        err = mod.CLISubprocessError("lock failed", lock_conflict=True)
        assert err.lock_conflict is True


class TestFormatCliIngestResult:
    """_format_cli_ingest_result のテスト."""

    def test_basic_format(self) -> None:
        """基本的な結果フォーマット."""
        mod = import_module("rag.server")
        result = {
            "placed": 3, "skipped": 1, "overwritten": 0,
            "errors": 0, "error_details": [],
        }
        text = mod._format_cli_ingest_result(result)
        assert "3件配置" in text

    def test_with_context(self) -> None:
        """context パラメータが出力に含まれること."""
        mod = import_module("rag.server")
        result = {
            "placed": 1, "skipped": 0, "overwritten": 0,
            "errors": 0, "error_details": [],
        }
        text = mod._format_cli_ingest_result(result, context="https://example.com")
        assert "https://example.com" in text

    def test_with_errors(self) -> None:
        """エラー詳細が出力に含まれること."""
        mod = import_module("rag.server")
        result = {
            "placed": 0, "skipped": 0, "overwritten": 0,
            "errors": 2, "error_details": ["fail1", "fail2"],
        }
        text = mod._format_cli_ingest_result(result)
        assert "エラー: 2件" in text
        assert "fail1" in text
        assert "fail2" in text

    def test_with_pipeline_summary(self) -> None:
        """pipeline データが含まれる場合にフォーマットされること."""
        mod = import_module("rag.server")
        result = {
            "placed": 1, "skipped": 0, "overwritten": 0,
            "errors": 0, "error_details": [],
            "pipeline": {
                "mode": "incremental",
                "total_files": 1, "processed": 1, "skipped": 0, "errors": [],
            },
        }
        text = mod._format_cli_ingest_result(result)
        assert "1件配置" in text


# --- 移行済みツールのテスト（#409） ---


class TestRagAddTool:
    """rag_add ツールのテスト（CLI サブプロセス移行後）."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """正常系: CLI サブプロセスの結果がフォーマットされて返ること."""
        mod = import_module("rag.server")
        mock_result = {
            "placed": 1, "skipped": 0, "overwritten": 0,
            "errors": 0, "error_details": [],
        }
        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock, return_value=mock_result,
        ) as mock_cli:
            result = await mod.rag_add("https://example.com/page")

        mock_cli.assert_called_once_with("add", ["https://example.com/page"], ctx=None)
        assert "1件配置" in result

    @pytest.mark.asyncio
    async def test_lock_conflict(self) -> None:
        """ロック競合時に専用メッセージを返すこと."""
        mod = import_module("rag.server")
        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock,
            side_effect=mod.CLISubprocessError("lock conflict", lock_conflict=True),
        ):
            result = await mod.rag_add("https://example.com/page")

        assert "別のインジェストが実行中" in result

    @pytest.mark.asyncio
    async def test_cli_error(self) -> None:
        """CLISubprocessError 時にエラーメッセージを返すこと."""
        mod = import_module("rag.server")
        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock,
            side_effect=mod.CLISubprocessError("connection timeout"),
        ):
            result = await mod.rag_add("https://example.com/page")

        assert "エラー" in result
        assert "https://example.com/page" in result

    @pytest.mark.asyncio
    async def test_unexpected_error(self) -> None:
        """予期しない例外時にエラーメッセージを返すこと."""
        mod = import_module("rag.server")
        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected"),
        ):
            result = await mod.rag_add("https://example.com/page")

        assert "エラー" in result


class TestRagDeleteTool:
    """rag_delete ツールのテスト（CLI サブプロセス移行後）."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """正常系: 削除成功メッセージが返ること."""
        mod = import_module("rag.server")
        mock_result = {"deleted": True, "pipeline": None}
        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock, return_value=mock_result,
        ) as mock_cli:
            result = await mod.rag_delete("https://example.com/page")

        mock_cli.assert_called_once_with("delete", ["https://example.com/page"], ctx=None)
        assert "削除しました" in result

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        """ソースが見つからない場合のメッセージが返ること."""
        mod = import_module("rag.server")
        mock_result = {"not_found": True}
        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock, return_value=mock_result,
        ):
            result = await mod.rag_delete("https://example.com/missing")

        assert "見つかりませんでした" in result

    @pytest.mark.asyncio
    async def test_lock_conflict(self) -> None:
        """ロック競合時に専用メッセージを返すこと."""
        mod = import_module("rag.server")
        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock,
            side_effect=mod.CLISubprocessError("排他制御エラー", lock_conflict=True),
        ):
            result = await mod.rag_delete("https://example.com/page")

        assert "別の操作が実行中" in result


class TestRagRebuildTool:
    """rag_rebuild ツールのテスト（CLI サブプロセス移行後）."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """正常系: 再構築結果がフォーマットされて返ること."""
        mod = import_module("rag.server")
        mock_result = {
            "mode": "incremental",
            "total_files": 10, "processed": 8, "skipped": 2, "errors": [],
            "elapsed": 5.5,
        }
        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock, return_value=mock_result,
        ) as mock_cli:
            result = await mod.rag_rebuild("incremental")

        mock_cli.assert_called_once_with(
            "rebuild", ["--mode", "incremental"], ctx=None,
        )
        assert "差分更新" in result

    @pytest.mark.asyncio
    async def test_with_source_type(self) -> None:
        """source_type 指定時にCLI引数に含まれること."""
        mod = import_module("rag.server")
        mock_result = {
            "mode": "full",
            "total_files": 5, "processed": 5, "skipped": 0, "errors": [],
            "elapsed": 10.0,
        }
        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock, return_value=mock_result,
        ) as mock_cli:
            await mod.rag_rebuild("full", source_type="web")

        call_args = mock_cli.call_args[0][1]
        assert "--source-type" in call_args
        assert "web" in call_args

    @pytest.mark.asyncio
    async def test_lock_conflict(self) -> None:
        """ロック競合時に専用メッセージを返すこと."""
        mod = import_module("rag.server")
        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock,
            side_effect=mod.CLISubprocessError("lock conflict", lock_conflict=True),
        ):
            result = await mod.rag_rebuild("full")

        assert "別の再構築が実行中" in result

    @pytest.mark.asyncio
    async def test_cli_error(self) -> None:
        """CLISubprocessError 時にエラーメッセージを返すこと."""
        mod = import_module("rag.server")
        with patch.object(
            mod, "_run_cli_subprocess", new_callable=AsyncMock,
            side_effect=mod.CLISubprocessError("rebuild failed"),
        ):
            result = await mod.rag_rebuild("full")

        assert "エラー" in result

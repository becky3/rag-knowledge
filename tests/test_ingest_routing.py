"""rag_ingest / rag_ingest_batch ルーティングとMCPツール・CLIのテスト

仕様: docs/specs/rag-knowledge.md
Issue: #91
"""

from __future__ import annotations

import argparse
from importlib import import_module
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rag.ingesters.web import WebIngester
from rag.rag_knowledge import RAGKnowledgeService
from rag.server import _reset_rag_service
from rag.vector_store import VectorStore
from rag.web_crawler import CrawledPage, WebCrawler


# --- フィクスチャ ---


@pytest.fixture
def mock_vector_store() -> MagicMock:
    """モック VectorStore を作成する."""
    mock = MagicMock(spec=VectorStore)
    mock.add_documents = AsyncMock(return_value=1)
    mock.search = AsyncMock(return_value=[])
    mock.delete_by_source = AsyncMock(return_value=0)
    mock.delete_stale_chunks = AsyncMock(return_value=0)
    mock.get_stats = MagicMock(return_value={
        "total_chunks": 0,
        "source_count": 0,
        "sources": [],
    })
    return mock


@pytest.fixture
def mock_web_crawler() -> MagicMock:
    """モック WebCrawler を作成する."""
    mock = MagicMock(spec=WebCrawler)
    mock.crawl_page = AsyncMock(return_value=None)
    mock.crawl_index_page = AsyncMock(return_value=[])
    mock.validate_url = MagicMock(side_effect=lambda url: url)
    mock._crawl_delay = 0.0
    return mock


@pytest.fixture
def service_with_web_ingester(
    mock_vector_store: MagicMock,
    mock_web_crawler: MagicMock,
) -> RAGKnowledgeService:
    """web インジェスター登録済みの RAGKnowledgeService を作成する."""
    web_ingester = WebIngester(web_crawler=mock_web_crawler)
    service = RAGKnowledgeService(
        vector_store=mock_vector_store,
        web_crawler=mock_web_crawler,
        chunk_size=200,
        chunk_overlap=30,
        similarity_threshold=None,
        web_ingester=web_ingester,
    )
    service.register_ingester("web", web_ingester)
    return service


@pytest.fixture(autouse=True)
def _reset_rag_global_state() -> None:
    """各テスト前にRAGサービスのグローバル状態をリセットする."""
    _reset_rag_service()


# --- ルーティング機構テスト ---


class TestIngesterRouting:
    """ソースタイプ → インジェスターのルーティングテスト."""

    def test_register_ingester(
        self,
        mock_vector_store: MagicMock,
        mock_web_crawler: MagicMock,
    ) -> None:
        """インジェスターが正しく登録されること."""
        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
        )
        web_ingester = WebIngester(web_crawler=mock_web_crawler)
        service.register_ingester("web", web_ingester)

        # _get_ingester で取得できること
        assert service._get_ingester("web") is web_ingester

    def test_get_ingester_unregistered_raises(
        self,
        mock_vector_store: MagicMock,
        mock_web_crawler: MagicMock,
    ) -> None:
        """未登録のソースタイプで ValueError が発生すること."""
        service = RAGKnowledgeService(
            vector_store=mock_vector_store,
            web_crawler=mock_web_crawler,
            chunk_size=200,
            chunk_overlap=30,
            similarity_threshold=None,
        )
        with pytest.raises(ValueError, match="未登録のソースタイプです: zenn"):
            service._get_ingester("zenn")

    def test_get_ingester_shows_registered_types(
        self,
        service_with_web_ingester: RAGKnowledgeService,
    ) -> None:
        """未登録エラーメッセージに登録済みソースタイプが含まれること."""
        with pytest.raises(ValueError, match="登録済み: web"):
            service_with_web_ingester._get_ingester("local_file")


# --- ingest テスト ---


class TestIngest:
    """RAGKnowledgeService.ingest() のテスト."""

    async def test_ingest_web_source_type(
        self,
        service_with_web_ingester: RAGKnowledgeService,
        mock_web_crawler: MagicMock,
        mock_vector_store: MagicMock,
    ) -> None:
        """web ソースタイプで単一コンテンツが取り込まれること."""
        mock_web_crawler.crawl_page.return_value = CrawledPage(
            url="https://example.com/page",
            title="Test Page",
            text="Test content for ingesting.",
            crawled_at="2024-01-01T00:00:00+00:00",
        )
        mock_vector_store.add_documents.return_value = 1

        result = await service_with_web_ingester.ingest(
            "web", "https://example.com/page"
        )

        assert result == 1
        mock_vector_store.add_documents.assert_called_once()

    async def test_ingest_unregistered_source_type(
        self,
        service_with_web_ingester: RAGKnowledgeService,
    ) -> None:
        """未登録のソースタイプで ValueError が発生すること."""
        with pytest.raises(ValueError, match="未登録のソースタイプです: unknown"):
            await service_with_web_ingester.ingest(
                "unknown", "some-identifier"
            )

    async def test_ingest_returns_zero_on_fetch_failure(
        self,
        service_with_web_ingester: RAGKnowledgeService,
        mock_web_crawler: MagicMock,
    ) -> None:
        """fetch_single が None を返した場合に 0 を返すこと."""
        mock_web_crawler.crawl_page.return_value = None

        result = await service_with_web_ingester.ingest(
            "web", "https://example.com/fail"
        )

        assert result == 0


# --- ingest_batch テスト ---


class TestIngestBatch:
    """RAGKnowledgeService.ingest_batch() のテスト."""

    async def test_ingest_batch_web_source_type(
        self,
        service_with_web_ingester: RAGKnowledgeService,
        mock_web_crawler: MagicMock,
        mock_vector_store: MagicMock,
    ) -> None:
        """web ソースタイプで一括取り込みが動作すること."""
        mock_web_crawler.crawl_index_page.return_value = [
            "https://example.com/p1",
            "https://example.com/p2",
        ]
        mock_web_crawler.crawl_page.side_effect = [
            CrawledPage(
                url="https://example.com/p1",
                title="P1",
                text="Content 1 for testing.",
                crawled_at="2024-01-01T00:00:00+00:00",
            ),
            CrawledPage(
                url="https://example.com/p2",
                title="P2",
                text="Content 2 for testing.",
                crawled_at="2024-01-01T00:00:00+00:00",
            ),
        ]
        mock_vector_store.add_documents.return_value = 1

        result = await service_with_web_ingester.ingest_batch(
            "web",
            "https://example.com/index",
            {"url_pattern": r"p\d"},
        )

        assert result["ingested"] == 2
        assert result["chunks_stored"] >= 2
        assert result["errors"] == 0

    async def test_ingest_batch_unregistered_source_type(
        self,
        service_with_web_ingester: RAGKnowledgeService,
    ) -> None:
        """未登録のソースタイプで ValueError が発生すること."""
        with pytest.raises(ValueError, match="未登録のソースタイプです"):
            await service_with_web_ingester.ingest_batch(
                "unknown", "some-source"
            )

    async def test_ingest_batch_no_items_discovered(
        self,
        service_with_web_ingester: RAGKnowledgeService,
        mock_web_crawler: MagicMock,
    ) -> None:
        """discover で 0 件の場合にエラーなしで返ること."""
        mock_web_crawler.crawl_index_page.return_value = []

        result = await service_with_web_ingester.ingest_batch(
            "web", "https://example.com/empty"
        )

        assert result["ingested"] == 0
        assert result["chunks_stored"] == 0
        assert result["errors"] == 0

    async def test_ingest_batch_partial_failure(
        self,
        service_with_web_ingester: RAGKnowledgeService,
        mock_web_crawler: MagicMock,
        mock_vector_store: MagicMock,
    ) -> None:
        """一部失敗しても成功分が取り込まれ、エラーが記録されること."""
        mock_web_crawler.crawl_index_page.return_value = [
            "https://example.com/ok",
            "https://example.com/fail",
        ]
        mock_web_crawler.crawl_page.side_effect = [
            CrawledPage(
                url="https://example.com/ok",
                title="OK",
                text="Content that works.",
                crawled_at="2024-01-01T00:00:00+00:00",
            ),
            None,  # 失敗
        ]
        mock_vector_store.add_documents.return_value = 1

        result = await service_with_web_ingester.ingest_batch(
            "web", "https://example.com/index"
        )

        assert result["ingested"] == 1
        assert result["errors"] == 1

    async def test_ingest_batch_progress_callback(
        self,
        service_with_web_ingester: RAGKnowledgeService,
        mock_web_crawler: MagicMock,
        mock_vector_store: MagicMock,
    ) -> None:
        """進捗コールバックが呼ばれること."""
        mock_web_crawler.crawl_index_page.return_value = [
            "https://example.com/p1",
        ]
        mock_web_crawler.crawl_page.return_value = CrawledPage(
            url="https://example.com/p1",
            title="P1",
            text="Content.",
            crawled_at="2024-01-01T00:00:00+00:00",
        )
        mock_vector_store.add_documents.return_value = 1

        callback = AsyncMock()
        await service_with_web_ingester.ingest_batch(
            "web",
            "https://example.com/index",
            progress_callback=callback,
        )

        callback.assert_called_once_with(1, 1)


# --- MCP ツールテスト ---


class TestRagIngestTool:
    """rag_ingest MCP ツールのテスト."""

    async def test_rag_ingest_success(self) -> None:
        """正常取り込みで成功メッセージが返ること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.ingest = AsyncMock(return_value=5)

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            result = await mod.rag_ingest("web", "https://example.com/page")

        assert "取り込み完了" in result
        assert "5チャンク" in result
        mock_service.ingest.assert_called_once_with(
            "web", "https://example.com/page", None
        )

    async def test_rag_ingest_with_options(self) -> None:
        """オプション付きで呼び出せること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.ingest = AsyncMock(return_value=3)

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            result = await mod.rag_ingest(
                "web", "https://example.com/page", {"key": "value"}
            )

        assert "取り込み完了" in result
        mock_service.ingest.assert_called_once_with(
            "web", "https://example.com/page", {"key": "value"}
        )

    async def test_rag_ingest_value_error(self) -> None:
        """ValueError 時にエラーメッセージが返ること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.ingest = AsyncMock(
            side_effect=ValueError("未登録のソースタイプです: unknown")
        )

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            result = await mod.rag_ingest("unknown", "identifier")

        assert "エラー:" in result
        assert "未登録のソースタイプです" in result

    async def test_rag_ingest_zero_chunks(self) -> None:
        """0 チャンク（取り込み失敗）時にエラーメッセージが返ること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.ingest = AsyncMock(return_value=0)

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            result = await mod.rag_ingest("web", "https://example.com/fail")

        assert "エラー:" in result

    async def test_rag_ingest_unexpected_error(self) -> None:
        """予期しないエラー時にエラーメッセージが返ること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.ingest = AsyncMock(side_effect=RuntimeError("Unexpected"))

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            result = await mod.rag_ingest("web", "https://example.com/page")

        assert "エラー:" in result


class TestRagIngestBatchTool:
    """rag_ingest_batch MCP ツールのテスト."""

    async def test_rag_ingest_batch_success(self) -> None:
        """正常取り込みでサマリーが返ること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.ingest_batch = AsyncMock(
            return_value={"ingested": 3, "chunks_stored": 15, "errors": 0}
        )

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            result = await mod.rag_ingest_batch(
                "web", "https://example.com/index"
            )

        assert "完了" in result
        assert "3件取り込み" in result
        assert "15チャンク" in result
        assert "エラー: 0件" in result

    async def test_rag_ingest_batch_with_options(self) -> None:
        """オプション付きで呼び出せること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.ingest_batch = AsyncMock(
            return_value={"ingested": 1, "chunks_stored": 5, "errors": 0}
        )

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            result = await mod.rag_ingest_batch(
                "web",
                "https://example.com/index",
                {"url_pattern": r"page\d+"},
            )

        assert "完了" in result
        mock_service.ingest_batch.assert_called_once_with(
            "web",
            "https://example.com/index",
            {"url_pattern": r"page\d+"},
        )

    async def test_rag_ingest_batch_value_error(self) -> None:
        """ValueError 時にエラーメッセージが返ること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.ingest_batch = AsyncMock(
            side_effect=ValueError("未登録のソースタイプです: bad")
        )

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            result = await mod.rag_ingest_batch("bad", "source")

        assert "エラー:" in result
        assert "未登録のソースタイプです" in result

    async def test_rag_ingest_batch_unexpected_error(self) -> None:
        """予期しないエラー時にエラーメッセージが返ること."""
        mod = import_module("rag.server")
        mock_service = AsyncMock()
        mock_service.ingest_batch = AsyncMock(
            side_effect=RuntimeError("Unexpected")
        )

        with patch.object(mod, "_get_rag_service", return_value=mock_service):
            result = await mod.rag_ingest_batch("web", "source")

        assert "エラー:" in result


# --- MCP サーバーツール数テスト ---


class TestMcpServerToolRegistration:
    """MCP サーバーのツール登録テスト."""

    async def test_rag_server_exposes_eight_tools(self) -> None:
        """RAG MCPサーバーが8つのツールを公開すること."""
        mod = import_module("rag.server")
        server = mod.mcp

        tools = await server.list_tools()
        tool_names = {t.name for t in tools}

        expected = {
            "rag_search", "rag_add", "rag_crawl", "rag_crawl_preview",
            "rag_delete", "rag_stats", "rag_ingest", "rag_ingest_batch",
        }
        assert tool_names == expected, f"Expected {expected}, got {tool_names}"

    async def test_rag_server_tool_count(self) -> None:
        """RAG MCPサーバーのツール数が正確に8であること."""
        mod = import_module("rag.server")
        server = mod.mcp

        tools = await server.list_tools()
        assert len(tools) == 8


# --- CLI ingest サブコマンドテスト ---


class TestCliIngestParseOptions:
    """CLI _parse_options のテスト."""

    def test_parse_single_option(self) -> None:
        """単一オプションが正しくパースされること."""
        from rag.cli import _parse_options

        result = _parse_options(["key=value"])
        assert result == {"key": "value"}

    def test_parse_multiple_options(self) -> None:
        """複数オプションが正しくパースされること."""
        from rag.cli import _parse_options

        result = _parse_options(["k1=v1", "k2=v2"])
        assert result == {"k1": "v1", "k2": "v2"}

    def test_parse_option_with_equals_in_value(self) -> None:
        """値に = が含まれる場合も正しくパースされること."""
        from rag.cli import _parse_options

        result = _parse_options(["pattern=a=b"])
        assert result == {"pattern": "a=b"}

    def test_parse_invalid_option_exits(self) -> None:
        """key=value 形式でない場合に SystemExit が発生すること."""
        from rag.cli import _parse_options

        with pytest.raises(SystemExit):
            _parse_options(["invalid"])


class TestCliIngestSubcommand:
    """CLI ingest サブコマンドのテスト."""

    async def test_single_ingest_mode(self) -> None:
        """単一取り込みモードが正しく動作すること."""
        from rag.cli import run_ingest

        args = argparse.Namespace(
            source_type="web",
            identifier="https://example.com/page",
            batch=False,
            source=None,
            option=[],
        )

        mock_service = AsyncMock()
        mock_service.ingest = AsyncMock(return_value=3)

        with patch("rag.cli._create_ingest_service", return_value=mock_service):
            await run_ingest(args)

        mock_service.ingest.assert_called_once_with(
            "web", "https://example.com/page", None
        )

    async def test_batch_ingest_mode(self) -> None:
        """一括取り込みモードが正しく動作すること."""
        from rag.cli import run_ingest

        args = argparse.Namespace(
            source_type="web",
            identifier=None,
            batch=True,
            source="https://example.com/index",
            option=["url_pattern=page\\d+"],
        )

        mock_service = AsyncMock()
        mock_service.ingest_batch = AsyncMock(
            return_value={"ingested": 2, "chunks_stored": 10, "errors": 0}
        )

        with patch("rag.cli._create_ingest_service", return_value=mock_service):
            await run_ingest(args)

        mock_service.ingest_batch.assert_called_once_with(
            "web",
            "https://example.com/index",
            {"url_pattern": "page\\d+"},
        )

    async def test_single_mode_missing_identifier_exits(self) -> None:
        """単一モードで --identifier がない場合に SystemExit が発生すること."""
        from rag.cli import run_ingest

        args = argparse.Namespace(
            source_type="web",
            identifier=None,
            batch=False,
            source=None,
            option=[],
        )

        with pytest.raises(SystemExit):
            await run_ingest(args)

    async def test_batch_mode_missing_source_exits(self) -> None:
        """一括モードで --source がない場合に SystemExit が発生すること."""
        from rag.cli import run_ingest

        args = argparse.Namespace(
            source_type="web",
            identifier=None,
            batch=True,
            source=None,
            option=[],
        )

        with pytest.raises(SystemExit):
            await run_ingest(args)

    async def test_single_mode_value_error_exits(self) -> None:
        """単一モードで ValueError が発生した場合に SystemExit が発生すること."""
        from rag.cli import run_ingest

        args = argparse.Namespace(
            source_type="unknown",
            identifier="something",
            batch=False,
            source=None,
            option=[],
        )

        mock_service = AsyncMock()
        mock_service.ingest = AsyncMock(
            side_effect=ValueError("未登録のソースタイプです")
        )

        with (
            patch("rag.cli._create_ingest_service", return_value=mock_service),
            pytest.raises(SystemExit),
        ):
            await run_ingest(args)

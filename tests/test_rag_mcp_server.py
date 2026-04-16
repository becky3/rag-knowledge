"""RAG MCPサーバーのテスト.

仕様: docs/specs/rag-knowledge.md, docs/specs/search-response.md, docs/specs/rebuild-stats.md,
      docs/specs/site-ingest.md
18個のRAGツール（rag_search, rag_get_document,
rag_crawl_zenn, rag_crawl_bluesky, rag_add_youtube, rag_crawl_youtube,
rag_add_document, rag_crawl_documents, rag_add_journal,
rag_site_ingest, rag_delete, rag_rebuild, rag_stats,
rag_update_aozora_catalog, rag_search_aozora, rag_add_aozora, rag_crawl_aozora,
rag_list_recent）が
MCPサーバーとして公開されていることを検証する。
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import logging

import pytest

from rag.server import _configure_and_run, _reset_safe_browsing_client


@pytest.fixture(autouse=True)
def _reset_rag_global_state() -> None:
    """各テスト前にRAGサービスのグローバル状態をリセットする."""
    _reset_safe_browsing_client()


@pytest.mark.asyncio
async def test_rag_server_exposes_tools() -> None:
    """RAG MCPサーバーが18個のツールを公開すること."""
    mod = import_module("rag.server")
    server = mod.mcp

    tools = await server.list_tools()
    tool_names = {t.name for t in tools}

    expected = {
        "rag_search", "rag_get_document",
        "rag_crawl_zenn", "rag_crawl_bluesky",
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
    """RAG MCPサーバーのツール数が正確に18であること."""
    mod = import_module("rag.server")
    server = mod.mcp

    tools = await server.list_tools()
    assert len(tools) == 18


class TestRagSearchOutput:
    """rag_search ツールのチャンク単位出力フォーマットテスト（CLI 委譲版）."""

    def _make_cli_result(
        self,
        vector_results: list[dict[str, object]] | None = None,
        bm25_results: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return {
            "query": "テスト",
            "vector_results": vector_results or [],
            "bm25_results": bm25_results or [],
        }

    async def test_output_contains_vector_and_bm25_sections(self) -> None:
        """出力にベクトル検索結果とBM25検索結果のセクションが含まれること."""
        from rag.server import rag_search

        cli_result = self._make_cli_result(
            vector_results=[{"text": "ベクトルの結果テキスト", "source_url": "https://example.com/vec1", "distance": 0.234, "chunk_index": 2, "title": "ガイドページ", "source_type": "web", "total_chunks": 15, "collected_at": "", "section_path": ""}],
            bm25_results=[{"text": "BM25の結果テキスト", "source_url": "https://example.com/bm25_1", "score": 4.521, "doc_id": "doc1", "chunk_index": 4, "title": "サンプル記事", "source_type": "zenn", "total_chunks": 20, "collected_at": "", "section_path": ""}],
        )

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=cli_result) as mock_subprocess:
            result = await rag_search("テストクエリ")

        mock_subprocess.assert_called_once_with("search", ["--query", "テストクエリ"])
        assert "## ベクトル検索結果 (意味的類似度)" in result
        assert "## BM25 検索結果 (キーワード一致)" in result

    async def test_output_contains_chunk_metadata(self) -> None:
        """各結果にSource/Title/Chunk/Typeメタデータが含まれること."""
        from rag.server import rag_search

        cli_result = self._make_cli_result(
            vector_results=[{"text": "チャンクテキスト", "source_url": "https://example.com/docs/guide", "distance": 0.234, "chunk_index": 2, "title": "ガイドページ", "source_type": "web", "total_chunks": 15, "collected_at": "", "section_path": ""}],
        )

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=cli_result):
            result = await rag_search("テスト")

        assert "Source: https://example.com/docs/guide" in result
        assert "Title: ガイドページ" in result
        assert "Chunk: 3/15" in result
        assert "Type: web" in result
        assert "チャンクテキスト" in result

    async def test_chunk_position_with_unknown_total(self) -> None:
        """total_chunks=0（レガシーデータ）のとき Chunk: N/? と表示されること."""
        from rag.server import rag_search

        cli_result = self._make_cli_result(
            vector_results=[{"text": "テキスト", "source_url": "https://example.com/page1", "distance": 0.1, "chunk_index": 4, "total_chunks": 0, "title": "", "source_type": "", "collected_at": "", "section_path": ""}],
        )

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=cli_result):
            result = await rag_search("テスト")

        assert "Chunk: 5/?" in result

    async def test_output_contains_raw_scores(self) -> None:
        """出力に生スコアが含まれること."""
        from rag.server import rag_search

        cli_result = self._make_cli_result(
            vector_results=[{"text": "テキスト", "source_url": "https://example.com/v", "distance": 0.567, "chunk_index": 0, "total_chunks": 0, "title": "", "source_type": "", "collected_at": "", "section_path": ""}],
            bm25_results=[{"text": "テキスト", "source_url": "https://example.com/b", "score": 3.456, "doc_id": "d1", "chunk_index": 0, "total_chunks": 0, "title": "", "source_type": "", "collected_at": "", "section_path": ""}],
        )

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=cli_result):
            result = await rag_search("テスト")

        assert "[distance=0.567]" in result
        assert "[score=3.456]" in result

    async def test_empty_results_returns_not_found_message(self) -> None:
        """0件時に「該当する情報が見つかりませんでした」が返ること."""
        from rag.server import rag_search

        cli_result = self._make_cli_result()

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=cli_result):
            result = await rag_search("存在しないクエリ")

        assert result == "該当する情報が見つかりませんでした"

    async def test_chunk_text_returned_directly(self) -> None:
        """チャンクテキストがそのまま返却されること（ページ全文ではない）."""
        from rag.server import rag_search

        cli_result = self._make_cli_result(
            vector_results=[{"text": "これはチャンクテキストです", "source_url": "https://example.com/page1", "distance": 0.1, "chunk_index": 0, "title": "ページ1", "source_type": "web", "total_chunks": 5, "collected_at": "", "section_path": ""}],
        )

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=cli_result):
            result = await rag_search("テスト")

        assert "これはチャンクテキストです" in result

    async def test_same_source_different_chunks_shown_individually(self) -> None:
        """同一ソースの異なるチャンクが個別に表示されること."""
        from rag.server import rag_search

        cli_result = self._make_cli_result(
            vector_results=[
                {"text": "チャンク1のテキスト", "source_url": "https://example.com/page", "distance": 0.1, "chunk_index": 0, "title": "ページ", "source_type": "web", "total_chunks": 3, "collected_at": "", "section_path": ""},
                {"text": "チャンク2のテキスト", "source_url": "https://example.com/page", "distance": 0.2, "chunk_index": 2, "title": "ページ", "source_type": "web", "total_chunks": 3, "collected_at": "", "section_path": ""},
            ],
        )

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=cli_result):
            result = await rag_search("テスト")

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
        mock_settings.rag_log_dir = None

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
        mock_settings.rag_http_host = "127.0.0.1"
        mock_settings.rag_http_port = 9090
        mock_settings.rag_dns_rebinding_protection = True
        mock_settings.rag_log_dir = None

        with (
            patch.object(mod, "get_settings", return_value=mock_settings),
            patch.object(mod.mcp, "run") as mock_run,
        ):
            _configure_and_run()

        mock_run.assert_called_once_with(transport="streamable-http")
        assert mod.mcp.settings.host == "127.0.0.1"
        assert mod.mcp.settings.port == 9090

    def test_keyboard_interrupt_graceful_shutdown(self) -> None:
        """Ctrl+C (KeyboardInterrupt) で終了コード130で終了すること (#42)."""
        mod = import_module("rag.server")
        mock_settings = MagicMock()
        mock_settings.rag_transport = "http"
        mock_settings.rag_http_host = "127.0.0.1"
        mock_settings.rag_http_port = 8080
        mock_settings.rag_dns_rebinding_protection = True
        mock_settings.rag_log_dir = None

        with (
            patch.object(mod, "get_settings", return_value=mock_settings),
            patch.object(
                mod.mcp, "run", side_effect=KeyboardInterrupt
            ),
            patch.object(mod.logger, "info") as mock_log,
            pytest.raises(SystemExit, match="130"),
        ):
            _configure_and_run()

        mock_log.assert_any_call("MCP server shut down")

    def test_stdio_keyboard_interrupt_graceful_shutdown(self) -> None:
        """stdio モードでも KeyboardInterrupt で終了コード130で終了すること (#42)."""
        mod = import_module("rag.server")
        mock_settings = MagicMock()
        mock_settings.rag_transport = "stdio"
        mock_settings.rag_log_dir = None

        with (
            patch.object(mod, "get_settings", return_value=mock_settings),
            patch.object(
                mod.mcp, "run", side_effect=KeyboardInterrupt
            ),
            patch.object(mod.logger, "info") as mock_log,
            pytest.raises(SystemExit, match="130"),
        ):
            _configure_and_run()

        mock_log.assert_any_call("MCP server shut down")

    def test_shutdown_log_on_normal_exit(self) -> None:
        """正常終了時もシャットダウンログが出力されること (#42)."""
        mod = import_module("rag.server")
        mock_settings = MagicMock()
        mock_settings.rag_transport = "stdio"
        mock_settings.rag_log_dir = None

        with (
            patch.object(mod, "get_settings", return_value=mock_settings),
            patch.object(mod.mcp, "run"),
            patch.object(mod.logger, "info") as mock_log,
        ):
            _configure_and_run()

        mock_log.assert_any_call("MCP server shut down")


class TestAttachLogFileHandler:
    """_attach_log_file_handler のテスト (#588)."""

    def _build_rag_logger(self) -> logging.Logger:
        """毎回新しい名前空間を用意してグローバル状態汚染を避ける."""
        import uuid

        return logging.getLogger(f"rag_test_{uuid.uuid4().hex}")

    def test_handler_not_attached_when_rag_log_dir_is_none(self) -> None:
        from rag.infrastructure.log_file_handler import SessionRotatingFileHandler
        from rag.server import _attach_log_file_handler

        rag_logger = self._build_rag_logger()
        mock_settings = MagicMock()
        mock_settings.rag_log_dir = None

        _attach_log_file_handler(
            rag_logger, mock_settings, logging.Formatter("%(message)s")
        )

        assert not any(
            isinstance(h, SessionRotatingFileHandler) for h in rag_logger.handlers
        )

    def test_handler_attached_when_rag_log_dir_set(
        self, tmp_path: Path
    ) -> None:
        from rag.infrastructure.log_file_handler import SessionRotatingFileHandler
        from rag.server import _attach_log_file_handler

        rag_logger = self._build_rag_logger()
        mock_settings = MagicMock()
        mock_settings.rag_log_dir = str(tmp_path)
        mock_settings.rag_log_file_max_bytes = 1_000_000

        _attach_log_file_handler(
            rag_logger, mock_settings, logging.Formatter("%(message)s")
        )
        try:
            file_handlers = [
                h for h in rag_logger.handlers
                if isinstance(h, SessionRotatingFileHandler)
            ]
            assert len(file_handlers) == 1
        finally:
            for h in list(rag_logger.handlers):
                h.close()
                rag_logger.removeHandler(h)


class TestRagGetDocumentTool:
    """rag_get_document ツールのテスト（CLI 委譲版）."""

    async def test_format_text_returns_document(self) -> None:
        """format=text でドキュメントが返ること."""
        from rag.server import rag_get_document

        cli_result: dict[str, object] = {
            "source_id": "https://example.com/docs/guide",
            "title": "ガイドページ",
            "source_type": "web",
            "format": "text",
            "content": "これはドキュメント全文です。",
            "is_binary": False,
            "collected_at": "",
            "extra": {},
        }

        mock_settings = MagicMock()
        mock_settings.rag_max_response_chars = None

        with (
            patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=cli_result),
            patch("rag.server.get_settings", return_value=mock_settings),
        ):
            result = await rag_get_document("https://example.com/docs/guide")

        assert "Source: https://example.com/docs/guide" in result
        assert "Title: ガイドページ" in result
        assert "Type: web" in result
        assert "Format: text" in result
        assert "これはドキュメント全文です。" in result

    async def test_format_original_returns_document(self) -> None:
        """format=original でドキュメントが返ること."""
        from rag.server import rag_get_document

        cli_result: dict[str, object] = {
            "source_id": "https://example.com/page.html",
            "title": "HTMLページ",
            "source_type": "web",
            "format": "original",
            "content": "<html>...</html>",
            "is_binary": False,
            "collected_at": "",
            "extra": {},
        }

        mock_settings = MagicMock()
        mock_settings.rag_max_response_chars = None

        with (
            patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=cli_result),
            patch("rag.server.get_settings", return_value=mock_settings),
        ):
            result = await rag_get_document("https://example.com/page.html", format="original")

        assert "Format: original" in result
        assert "<html>...</html>" in result

    async def test_cli_error_returns_error(self) -> None:
        """CLI サブプロセスエラー時にエラーメッセージを返すこと."""
        from rag.server import CLISubprocessError, rag_get_document

        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError("ソースが見つかりません"),
        ):
            result = await rag_get_document("nonexistent")

        assert "エラー" in result

    async def test_truncation_with_max_response_chars(self) -> None:
        """rag_max_response_chars でトランケーションされ、通知文込みで上限内に収まること."""
        from rag.server import rag_get_document

        max_chars = 200
        long_content = "あ" * 1000
        cli_result: dict[str, object] = {
            "source_id": "https://example.com/long",
            "title": "Long Page",
            "source_type": "web",
            "format": "text",
            "content": long_content,
            "is_binary": False,
            "collected_at": "",
            "extra": {},
        }

        mock_settings = MagicMock()
        mock_settings.rag_max_response_chars = max_chars

        with (
            patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=cli_result),
            patch("rag.server.get_settings", return_value=mock_settings),
        ):
            result = await rag_get_document("https://example.com/long")

        assert "トランケートされました" in result
        assert "--output" in result
        assert len(result) <= max_chars

    async def test_no_truncation_when_limit_is_none(self) -> None:
        """rag_max_response_chars=None のときトランケーションされないこと."""
        from rag.server import rag_get_document

        long_content = "あ" * 1000
        cli_result: dict[str, object] = {
            "source_id": "https://example.com/long",
            "title": "Long Page",
            "source_type": "web",
            "format": "text",
            "content": long_content,
            "is_binary": False,
            "collected_at": "",
            "extra": {},
        }

        mock_settings = MagicMock()
        mock_settings.rag_max_response_chars = None

        with (
            patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=cli_result),
            patch("rag.server.get_settings", return_value=mock_settings),
        ):
            result = await rag_get_document("https://example.com/long")

        assert "トランケートされました" not in result
        assert long_content in result

    async def test_invalid_format_returns_error(self) -> None:
        """無効な format 値でエラーが返ること."""
        from rag.server import rag_get_document

        result = await rag_get_document("source", format="invalid")

        assert "無効な format" in result


class TestRagStatsOutput:
    """rag_stats ツールの出力フォーマットテスト（CLI 委譲版）."""

    def _make_cli_result(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "source_store": {"total_files": 0, "total_size": 0},
            "converted_store": {"total_files": 0, "total_size": 0},
            "index": {"total_chunks": 0, "source_count": 0},
            "pipeline": {"status": "unconfigured"},
        }
        base.update(overrides)
        return base

    async def test_stats_contains_index_data(self) -> None:
        """インデックスセクションの統計が表示されること."""
        from rag.server import rag_stats

        cli_result = self._make_cli_result(
            index={"total_chunks": 150, "source_count": 3},
        )

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=cli_result):
            result = await rag_stats()

        assert "📊 RAG Knowledge 統計" in result
        assert "総チャンク数: 150" in result
        assert "ソース数: 3" in result

    async def test_stats_empty_sources(self) -> None:
        """ソースが空の場合."""
        from rag.server import rag_stats

        cli_result = self._make_cli_result(
            index={"total_chunks": 0, "source_count": 0},
        )

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=cli_result):
            result = await rag_stats()

        assert "📊 RAG Knowledge 統計" in result
        assert "総チャンク数: 0" in result

    async def test_stats_four_sections(self) -> None:
        """4セクション構成の出力フォーマット."""
        from rag.server import rag_stats

        cli_result = self._make_cli_result(
            index={"total_chunks": 5, "source_count": 1},
        )

        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value=cli_result):
            result = await rag_stats()

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
                "total_files": 1, "processed": 1, "errors": [],
            },
        }
        text = mod._format_cli_ingest_result(result)
        assert "1件配置" in text


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
            "total_files": 10, "processed": 8, "errors": [], "warnings": [],
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
            "convert": {
                "mode": "convert",
                "total_files": 5, "processed": 5, "errors": [], "warnings": [],
            },
            "index": {
                "mode": "index",
                "total_files": 5, "processed": 5, "errors": [], "warnings": [],
            },
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


class TestRagSiteIngestSafeBrowsing:
    """rag_site_ingest の Safe Browsing チェックテスト（#481）."""

    @pytest.mark.asyncio
    async def test_unsafe_url_returns_error(self) -> None:
        """起点 URL が危険判定された場合にエラーを返すこと."""
        mod = import_module("rag.server")

        from rag.safe_browsing import SafeBrowsingResult, ThreatMatch, ThreatType

        unsafe_result = SafeBrowsingResult(
            url="https://malicious.example.com",
            is_safe=False,
            threats=[
                ThreatMatch(
                    threat_type=ThreatType.MALWARE,
                    platform_type="ANY_PLATFORM",
                    threat_url="https://malicious.example.com",
                ),
            ],
        )

        mock_sb_client = AsyncMock()
        mock_sb_client.check_url = AsyncMock(return_value=unsafe_result)

        mock_settings = MagicMock()

        with (
            patch.object(mod, "_get_safe_browsing_client", return_value=mock_sb_client),
            patch.object(mod, "get_settings", return_value=mock_settings),
            patch("rag.utils.url.check_ssrf"),
        ):
            result = await mod.rag_site_ingest("https://malicious.example.com")

        assert "エラー" in result
        assert "安全でない" in result
        assert "MALWARE" in result

    @pytest.mark.asyncio
    async def test_safe_browsing_disabled_skips_check(self) -> None:
        """Safe Browsing クライアントが None の場合はスキップして CLI に委譲されること."""
        mod = import_module("rag.server")

        mock_cli_result = {"ingest": {"placed": 1, "skipped": 0, "errors": 0}}

        with (
            patch.object(mod, "_get_safe_browsing_client", return_value=None),
            patch.object(mod, "_run_cli_subprocess", new_callable=AsyncMock, return_value=mock_cli_result) as mock_run,
            patch.object(mod, "_format_cli_ingest_result", return_value="取り込み完了"),
        ):
            result = await mod.rag_site_ingest(url="https://example.com")

        # Safe Browsing でブロックされず、CLI 委譲まで到達していること
        mock_run.assert_called_once()
        assert "安全でない" not in result

"""Zenn MCP ツールの統合テスト

仕様: docs/specs/features/zenn-ingester.md
Issue: #114

テスト対象: MCP ツール rag_ingest_zenn, rag_add_zenn の統合
テスト手法: サービス層のモックを使った統合テスト
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rag.server import _reset_rag_service, rag_add_zenn, rag_ingest_zenn


@pytest.fixture(autouse=True)
def _reset_service() -> None:
    """各テスト前にグローバルサービスをリセットする."""
    _reset_rag_service()


# --- rag_ingest_zenn テスト ---


class TestRagIngestZennTool:
    """MCP ツール rag_ingest_zenn のテスト."""

    async def test_ingest_zenn_dry_run(self) -> None:
        """dry_run 時に記事一覧が表示されること."""
        mock_service = MagicMock()
        mock_service.ingest_zenn = AsyncMock(
            return_value={
                "articles_found": 3,
                "articles_ingested": 0,
                "chunks_stored": 0,
                "errors": 0,
                "dry_run": True,
                "limit_reached": False,
                "articles": [
                    {"slug": "article-1", "title": ""},
                    {"slug": "article-2", "title": ""},
                    {"slug": "article-3", "title": ""},
                ],
            }
        )

        with patch("rag.server._get_rag_service", return_value=mock_service):
            result = await rag_ingest_zenn(
                username="testuser", dry_run=True,
            )

        assert "[dry-run]" in result
        assert "3件" in result
        assert "article-1" in result
        assert "article-2" in result
        assert "article-3" in result
        mock_service.ingest_zenn.assert_called_once_with(
            "testuser", dry_run=True, no_limit=False,
        )

    async def test_ingest_zenn_full(self) -> None:
        """通常の一括取り込みが動作すること."""
        mock_service = MagicMock()
        mock_service.ingest_zenn = AsyncMock(
            return_value={
                "articles_found": 5,
                "articles_ingested": 5,
                "chunks_stored": 25,
                "errors": 0,
                "dry_run": False,
                "limit_reached": False,
                "articles": [],
            }
        )

        with patch("rag.server._get_rag_service", return_value=mock_service):
            result = await rag_ingest_zenn(username="testuser")

        assert "取り込み完了" in result
        assert "発見=5件" in result
        assert "取り込み=5件" in result
        assert "チャンク=25" in result
        assert "エラー=0件" in result

    async def test_ingest_zenn_with_errors(self) -> None:
        """エラーが含まれる場合にエラー数が表示されること."""
        mock_service = MagicMock()
        mock_service.ingest_zenn = AsyncMock(
            return_value={
                "articles_found": 5,
                "articles_ingested": 3,
                "chunks_stored": 15,
                "errors": 2,
                "dry_run": False,
                "limit_reached": False,
                "articles": [],
            }
        )

        with patch("rag.server._get_rag_service", return_value=mock_service):
            result = await rag_ingest_zenn(username="testuser")

        assert "エラー=2件" in result

    async def test_ingest_zenn_no_limit(self) -> None:
        """no_limit パラメータが正しくサービスに渡されること."""
        mock_service = MagicMock()
        mock_service.ingest_zenn = AsyncMock(
            return_value={
                "articles_found": 100,
                "articles_ingested": 100,
                "chunks_stored": 500,
                "errors": 0,
                "dry_run": False,
                "limit_reached": False,
                "articles": [],
            }
        )

        with patch("rag.server._get_rag_service", return_value=mock_service):
            await rag_ingest_zenn(
                username="testuser", no_limit=True,
            )

        mock_service.ingest_zenn.assert_called_once_with(
            "testuser", dry_run=False, no_limit=True,
        )

    async def test_ingest_zenn_runtime_error(self) -> None:
        """RuntimeError 時にエラーメッセージが返されること."""
        mock_service = MagicMock()
        mock_service.ingest_zenn = AsyncMock(
            side_effect=RuntimeError("ZennIngester が設定されていません")
        )

        with patch("rag.server._get_rag_service", return_value=mock_service):
            result = await rag_ingest_zenn(username="testuser")

        assert "エラー:" in result
        assert "ZennIngester" in result

    async def test_ingest_zenn_unexpected_error(self) -> None:
        """予期しないエラー時にエラーメッセージが返されること."""
        mock_service = MagicMock()
        mock_service.ingest_zenn = AsyncMock(
            side_effect=Exception("unexpected error")
        )

        with patch("rag.server._get_rag_service", return_value=mock_service):
            result = await rag_ingest_zenn(username="testuser")

        assert "エラー:" in result
        assert "testuser" in result


# --- rag_add_zenn テスト ---


class TestRagAddZennTool:
    """MCP ツール rag_add_zenn のテスト."""

    async def test_add_zenn_success(self) -> None:
        """正常に記事を取り込めること."""
        mock_service = MagicMock()
        mock_service.add_zenn = AsyncMock(return_value=5)

        with patch("rag.server._get_rag_service", return_value=mock_service):
            result = await rag_add_zenn(slug="test-article")

        assert "取り込みました" in result
        assert "test-article" in result
        assert "5チャンク" in result
        mock_service.add_zenn.assert_called_once_with("test-article")

    async def test_add_zenn_not_found(self) -> None:
        """記事が見つからない場合にエラーメッセージが返されること."""
        mock_service = MagicMock()
        mock_service.add_zenn = AsyncMock(return_value=0)

        with patch("rag.server._get_rag_service", return_value=mock_service):
            result = await rag_add_zenn(slug="nonexistent")

        assert "エラー:" in result
        assert "nonexistent" in result

    async def test_add_zenn_invalid_slug(self) -> None:
        """不正な slug で ValueError メッセージが返されること."""
        mock_service = MagicMock()
        mock_service.add_zenn = AsyncMock(
            side_effect=ValueError("不正な Zenn 記事 slug です")
        )

        with patch("rag.server._get_rag_service", return_value=mock_service):
            result = await rag_add_zenn(slug="invalid slug")

        assert "エラー:" in result
        assert "不正な Zenn 記事 slug" in result

    async def test_add_zenn_runtime_error(self) -> None:
        """RuntimeError 時にエラーメッセージが返されること."""
        mock_service = MagicMock()
        mock_service.add_zenn = AsyncMock(
            side_effect=RuntimeError("ZennIngester が設定されていません")
        )

        with patch("rag.server._get_rag_service", return_value=mock_service):
            result = await rag_add_zenn(slug="test-article")

        assert "エラー:" in result
        assert "ZennIngester" in result

    async def test_add_zenn_unexpected_error(self) -> None:
        """予期しないエラー時にエラーメッセージが返されること."""
        mock_service = MagicMock()
        mock_service.add_zenn = AsyncMock(
            side_effect=Exception("unexpected error")
        )

        with patch("rag.server._get_rag_service", return_value=mock_service):
            result = await rag_add_zenn(slug="test-article")

        assert "エラー:" in result
        assert "test-article" in result

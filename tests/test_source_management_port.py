"""SourceManagementPort (RealSourceManagementAdapter) のテスト

仕様: docs/specs/rag-knowledge.md / docs/specs/search-response.md
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.admin.source_management_port import RealSourceManagementAdapter
from rag.vector_store import RetrievalResult, VectorStore

from factories import make_source_management_adapter


@pytest.fixture
def mock_vector_store() -> MagicMock:
    """モックVectorStoreを作成する."""
    mock = MagicMock(spec=VectorStore)
    mock.delete_by_source = AsyncMock(return_value=0)
    return mock


@pytest.fixture
def adapter(
    mock_vector_store: MagicMock,
) -> RealSourceManagementAdapter:
    """RealSourceManagementAdapter インスタンスを作成する."""
    return make_source_management_adapter(
        vector_store=mock_vector_store,
    )


class TestDeleteSource:
    """delete_source() のテスト."""

    async def test_delete_source(
        self,
        adapter: RealSourceManagementAdapter,
        mock_vector_store: MagicMock,
    ) -> None:
        """ソースURL指定で削除できること."""
        mock_vector_store.delete_by_source.return_value = 8

        result = await adapter.delete_source("https://example.com/page1")

        assert result == 8
        mock_vector_store.delete_by_source.assert_called_once_with(
            "https://example.com/page1",
        )

    async def test_delete_source_normalizes_fragment_url(
        self,
        adapter: RealSourceManagementAdapter,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC37: delete_source() がフラグメント付きURLを正規化して削除すること.

        後方互換対応として、正規化済みURLとフラグメント付き元URLの両方で削除を試みる。
        """
        mock_vector_store.delete_by_source.side_effect = [3, 2]

        result = await adapter.delete_source("https://example.com/page#section")

        assert result == 5
        assert mock_vector_store.delete_by_source.call_count == 2
        calls = mock_vector_store.delete_by_source.call_args_list
        assert calls[0][0][0] == "https://example.com/page"
        assert calls[1][0][0] == "https://example.com/page#section"

    async def test_delete_source_without_fragment(
        self,
        adapter: RealSourceManagementAdapter,
        mock_vector_store: MagicMock,
    ) -> None:
        """フラグメントなしURLの場合は1回だけdelete_by_sourceが呼ばれること."""
        mock_vector_store.delete_by_source.return_value = 5

        result = await adapter.delete_source("https://example.com/page")

        assert result == 5
        mock_vector_store.delete_by_source.assert_called_once_with(
            "https://example.com/page",
        )

    async def test_delete_source_removes_from_bm25(
        self,
        mock_vector_store: MagicMock,
    ) -> None:
        """AC6: delete_source() が BM25 インデックスからもドキュメントを削除すること."""
        from rag.bm25_index import BM25Index

        mock_bm25_index = MagicMock(spec=BM25Index)
        mock_bm25_index.delete_by_source = MagicMock(return_value=5)

        adapter = make_source_management_adapter(
            vector_store=mock_vector_store,
            bm25_index=mock_bm25_index,
        )

        mock_vector_store.delete_by_source.return_value = 5

        result = await adapter.delete_source("https://example.com/page1")

        assert result == 5
        mock_bm25_index.delete_by_source.assert_called_once_with(
            "https://example.com/page1",
        )


class TestGetFullPageText:
    """get_full_page_text() のテスト (Issue #27)."""

    async def test_get_full_page_text_joins_chunks(
        self,
        adapter: RealSourceManagementAdapter,
        mock_vector_store: MagicMock,
    ) -> None:
        """チャンクが '\\n' で結合されること."""
        mock_vector_store.get_chunks_by_source = AsyncMock(
            return_value=[
                RetrievalResult(
                    text="チャンク1のテキスト",
                    metadata={"source_id": "https://example.com/page", "chunk_index": 0},
                    distance=0.0,
                ),
                RetrievalResult(
                    text="チャンク2のテキスト",
                    metadata={"source_id": "https://example.com/page", "chunk_index": 1},
                    distance=0.0,
                ),
                RetrievalResult(
                    text="チャンク3のテキスト",
                    metadata={"source_id": "https://example.com/page", "chunk_index": 2},
                    distance=0.0,
                ),
            ],
        )

        result = await adapter.get_full_page_text("https://example.com/page")

        assert result == "チャンク1のテキスト\nチャンク2のテキスト\nチャンク3のテキスト"
        mock_vector_store.get_chunks_by_source.assert_called_once_with(
            "https://example.com/page",
        )

    async def test_get_full_page_text_empty_chunks(
        self,
        adapter: RealSourceManagementAdapter,
        mock_vector_store: MagicMock,
    ) -> None:
        """チャンクが存在しない場合に空文字列が返ること."""
        mock_vector_store.get_chunks_by_source = AsyncMock(return_value=[])

        result = await adapter.get_full_page_text("https://example.com/nonexistent")

        assert result == ""
        mock_vector_store.get_chunks_by_source.assert_called_once_with(
            "https://example.com/nonexistent",
        )

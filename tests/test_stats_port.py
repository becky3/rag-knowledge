"""StatsPort (RealStatsAdapter) のテスト

仕様: docs/specs/rag-knowledge.md / docs/specs/infrastructure/content-listing.md
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from rag.admin.stats_port import RealStatsAdapter
from rag.vector_store import VectorStore

from factories import make_stats_adapter


@pytest.fixture
def mock_vector_store() -> MagicMock:
    """モックVectorStoreを作成する."""
    mock = MagicMock(spec=VectorStore)
    mock.get_stats = MagicMock(return_value={"total_chunks": 10})
    return mock


@pytest.fixture
def adapter(
    mock_vector_store: MagicMock,
) -> RealStatsAdapter:
    """RealStatsAdapter インスタンスを作成する."""
    return make_stats_adapter(
        vector_store=mock_vector_store,
    )


class TestGetStats:
    """get_stats() のテスト."""

    async def test_get_stats(
        self,
        adapter: RealStatsAdapter,
        mock_vector_store: MagicMock,
    ) -> None:
        """統計情報を取得できること."""
        mock_vector_store.get_stats.return_value = {"total_chunks": 100}

        result = await adapter.get_stats()

        assert result["total_chunks"] == 100
        assert "source_count" not in result
        assert "sources" not in result

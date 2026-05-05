"""統計・一覧 Port + Real Adapter.

仕様: docs/specs/rag-knowledge.md / docs/specs/infrastructure/content-listing.md

ナレッジベースの統計と source_type 別ソース一覧を抽象化する。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from rag.admin.formatting import format_file_size

if TYPE_CHECKING:
    from rag.vector_store import VectorStore

logger = logging.getLogger(__name__)


class StatsPort(Protocol):
    """統計・一覧 Port."""

    async def get_stats(self) -> dict[str, object]:
        """ナレッジベース統計（総チャンク数）を返す."""
        ...

    def list_recent(
        self,
        source_type: str,
        limit: int,
        ascending: bool = False,
        filters: dict[str, str] | None = None,
    ) -> str:
        """指定 source_type のソースを公開日時順で一覧取得する."""
        ...


class RealStatsAdapter:
    """StatsPort の本番実装."""

    def __init__(
        self,
        vector_store: VectorStore,
        *,
        source_store_dir: Path | None = None,
    ) -> None:
        """RealStatsAdapter を初期化する.

        Args:
            vector_store: ベクトルストア
            source_store_dir: source_store ルートディレクトリ（list_recent で使用）
        """
        self._vector_store = vector_store
        self._source_store_dir = source_store_dir

    async def get_stats(self) -> dict[str, object]:
        """ナレッジベース統計（総チャンク数）."""
        return await asyncio.to_thread(self._vector_store.get_stats)

    def list_recent(
        self,
        source_type: str,
        limit: int,
        ascending: bool = False,
        filters: dict[str, str] | None = None,
    ) -> str:
        """指定 source_type のソースを公開日時順で一覧取得する."""
        if self._source_store_dir is None:
            raise RuntimeError(
                "list_recent requires source_store_dir; "
                "RealStatsAdapter must be constructed with it",
            )
        return list_recent_sources(
            source_store_dir=str(self._source_store_dir),
            source_type=source_type,
            limit=limit,
            ascending=ascending,
            filters=filters,
        )


def list_recent_sources(
    source_store_dir: str,
    source_type: str,
    limit: int,
    ascending: bool = False,
    filters: dict[str, str] | None = None,
) -> str:
    """指定 source_type のソースを published_at でソートして一覧取得する（MCP/CLI 共通ロジック）.

    仕様: docs/specs/infrastructure/content-listing.md
    """
    from rag.store.metadata_db import MetadataDB

    db_path = Path(source_store_dir) / "metadata.db"
    if not db_path.exists():
        return f"source_type: {source_type}（0件 / 全0件）"

    db = MetadataDB(db_path)
    try:
        db.initialize()
        from rag.store.models import SourceType

        st = cast("SourceType", source_type)
        try:
            sources = db.list_sources(
                source_type=st,
                limit=limit,
                ascending=ascending,
                filters=filters,
            )
            total = db.count_sources_by_type(source_type=st, filters=filters)
        except ValueError as e:
            return f"エラー: {e}"
    finally:
        db.close()

    logger.info(
        "list-recent: source_type=%s, total=%d, returned=%d",
        source_type, total, len(sources),
    )
    for i, src in enumerate(sources, 1):
        logger.info(
            "list-recent result %d: source_id=%s, title=%r",
            i, src.source_id, src.title,
        )

    if not sources:
        return f"source_type: {source_type}（0件 / 全0件）"

    order_label = "古い順" if ascending else "新しい順"
    lines: list[str] = [
        f"source_type: {source_type}（{len(sources)}件 / 全{total}件, {order_label}）",
    ]

    for i, src in enumerate(sources, 1):
        lines.append("")
        lines.append(f"{i}. {src.title}")
        lines.append(f"   Source: {src.source_id}")
        lines.append(f"   Published: {src.published_at}")
        lines.append(f"   Size: {format_file_size(src.file_size)}")

    return "\n".join(lines)

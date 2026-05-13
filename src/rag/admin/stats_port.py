"""統計・一覧 Port + Real Adapter.

仕様: docs/specs/rag-knowledge.md / docs/specs/infrastructure/content-listing.md

ナレッジベースの統計と source_type 別ソース一覧を抽象化する。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from rag.admin.formatting import format_file_size

if TYPE_CHECKING:
    from rag.vector_store import VectorStore

logger = logging.getLogger(__name__)

# rag_list_by_date_range / list-by-date-range の日付パラメータは JST(+09:00) 起点で
# inclusive 範囲を構築する。MetadataDB の published_at カラムは
# `normalize_published_at()` により UTC マイクロ秒 6 桁固定 ISO 8601 で統一されているため、
# 範囲境界も同書式（UTC マイクロ秒 6 桁固定）に揃えて文字列比較する。
_JST_TZ = timezone(timedelta(hours=9))
_YYYYMMDD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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

    def list_by_date_range(
        self,
        date_from: str,
        date_to: str,
        source_type: str | None,
        limit: int,
        ascending: bool = False,
        filters: dict[str, str] | None = None,
    ) -> str:
        """published_at 範囲でソースを一覧取得する."""
        ...

    def close(self) -> None:
        """リソースを解放する（VectorStore.close への委譲）."""
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

    def list_by_date_range(
        self,
        date_from: str,
        date_to: str,
        source_type: str | None,
        limit: int,
        ascending: bool = False,
        filters: dict[str, str] | None = None,
    ) -> str:
        """published_at 範囲でソースを一覧取得する."""
        if self._source_store_dir is None:
            raise RuntimeError(
                "list_by_date_range requires source_store_dir; "
                "RealStatsAdapter must be constructed with it",
            )
        return list_sources_by_date_range(
            source_store_dir=str(self._source_store_dir),
            date_from=date_from,
            date_to=date_to,
            source_type=source_type,
            limit=limit,
            ascending=ascending,
            filters=filters,
        )

    def close(self) -> None:
        """リソースを解放する."""
        self._vector_store.close()


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

        st = cast(SourceType, source_type)
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


def validate_date_string(value: str, *, field_name: str) -> str:
    """YYYY-MM-DD 形式の日付文字列を検証する.

    Raises:
        ValueError: 形式が不正な場合
    """
    if not _YYYYMMDD_RE.match(value):
        msg = (
            f"{field_name} は YYYY-MM-DD 形式で指定してください "
            f"(指定値: {value!r})"
        )
        raise ValueError(msg)
    # 月日の妥当性チェック（例: 2026-13-01 を弾く）
    try:
        datetime.strptime(value, "%Y-%m-%d")  # noqa: DTZ007
    except ValueError as e:
        msg = f"{field_name} の日付が不正です (指定値: {value!r}): {e}"
        raise ValueError(msg) from None
    return value


def to_jst_range_iso(date_from: str, date_to: str) -> tuple[str, str]:
    """YYYY-MM-DD 形式の date_from/date_to を、JST 起点 inclusive 範囲の UTC ISO 8601 に変換する.

    MetadataDB の published_at は `normalize_published_at()` により UTC マイクロ秒 6 桁固定
    ISO 8601 に統一されているため、範囲境界も同書式に揃える。

    返り値: (date_from_iso, date_to_iso)
    - date_from_iso: JST {date_from} 00:00:00.000000 を UTC に変換したマイクロ秒 6 桁固定 ISO 8601
    - date_to_iso:   JST {date_to} 23:59:59.999999 を UTC に変換したマイクロ秒 6 桁固定 ISO 8601

    Raises:
        ValueError: いずれかが YYYY-MM-DD 形式でないか、date_from > date_to の場合
    """
    validate_date_string(date_from, field_name="date_from")
    validate_date_string(date_to, field_name="date_to")
    if date_from > date_to:
        msg = (
            f"date_from は date_to 以前の日付を指定してください "
            f"(date_from={date_from!r}, date_to={date_to!r})"
        )
        raise ValueError(msg)
    from_dt = datetime.combine(
        datetime.strptime(date_from, "%Y-%m-%d").date(),  # noqa: DTZ007
        time(0, 0, 0, 0),
        tzinfo=_JST_TZ,
    )
    to_dt = datetime.combine(
        datetime.strptime(date_to, "%Y-%m-%d").date(),  # noqa: DTZ007
        time(23, 59, 59, 999_999),
        tzinfo=_JST_TZ,
    )
    date_from_iso = from_dt.astimezone(UTC).isoformat(timespec="microseconds")
    date_to_iso = to_dt.astimezone(UTC).isoformat(timespec="microseconds")
    return date_from_iso, date_to_iso


def list_sources_by_date_range(
    source_store_dir: str,
    date_from: str,
    date_to: str,
    source_type: str | None,
    limit: int,
    ascending: bool = False,
    filters: dict[str, str] | None = None,
) -> str:
    """published_at 範囲でソースを取得して整形済みテキストを返す（MCP/CLI 共通ロジック）.

    仕様: docs/specs/infrastructure/content-listing.md
    """
    from rag.store.metadata_db import MetadataDB
    from rag.store.models import SourceType

    try:
        date_from_iso, date_to_iso = to_jst_range_iso(date_from, date_to)
    except ValueError as e:
        return f"エラー: {e}"

    header_type = source_type if source_type else "all"

    db_path = Path(source_store_dir) / "metadata.db"
    if not db_path.exists():
        return f"date_range: {date_from}〜{date_to}（{header_type}, 0件 / 全0件）"

    db = MetadataDB(db_path)
    try:
        db.initialize()
        st = cast(SourceType, source_type) if source_type else None
        try:
            sources = db.list_sources_by_date_range(
                date_from_iso=date_from_iso,
                date_to_iso=date_to_iso,
                source_type=st,
                limit=limit,
                ascending=ascending,
                filters=filters,
            )
            total = db.count_sources_by_date_range(
                date_from_iso=date_from_iso,
                date_to_iso=date_to_iso,
                source_type=st,
                filters=filters,
            )
        except ValueError as e:
            return f"エラー: {e}"
    finally:
        db.close()

    logger.info(
        "list-by-date-range: date_from=%s, date_to=%s, source_type=%s,"
        " total=%d, returned=%d",
        date_from, date_to, header_type, total, len(sources),
    )
    for i, src in enumerate(sources, 1):
        logger.info(
            "list-by-date-range result %d: source_id=%s, title=%r",
            i, src.source_id, src.title,
        )

    if not sources:
        return f"date_range: {date_from}〜{date_to}（{header_type}, 0件 / 全0件）"

    order_label = "古い順" if ascending else "新しい順"
    lines: list[str] = [
        f"date_range: {date_from}〜{date_to}"
        f"（{header_type}, {len(sources)}件 / 全{total}件, {order_label}）",
    ]
    for i, src in enumerate(sources, 1):
        lines.append("")
        lines.append(f"{i}. {src.title}")
        lines.append(f"   Type: {src.source_type}")
        lines.append(f"   Source: {src.source_id}")
        lines.append(f"   Published: {src.published_at}")
        lines.append(f"   Size: {format_file_size(src.file_size)}")
    return "\n".join(lines)

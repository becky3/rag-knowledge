"""Ingester ファミリーの共通基底.

仕様: docs/specs/architecture.md §3.1, §3.4
仕様: docs/specs/ingesters/common.md
"""

from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rag.store.source_store import SourceStore


class BaseIngester(ABC):
    """Ingester ファミリーの共通基底.

    Ingester は取り込み機構として、外部データを取得して source_store に
    raw bytes を配置し、`IngestResult` を返す。元データの加工は最小限に
    留める（インデックス化向けの加工は converter の責務）。

    本基底は Ingester ファミリーが共通で保持する依存（source_store）を
    強制する。エントリポイント（add_*, crawl_*, ingest_* 等）の名前と
    シグネチャは媒体ごとに異なるため、抽象メソッドとしては定義しない。
    上位層からは ``BaseIngester`` 型として Ingester 一般を扱える。
    """

    def __init__(self, source_store: SourceStore) -> None:
        self._source_store = source_store

    @property
    def source_store(self) -> SourceStore:
        """配置先の SourceStore."""
        return self._source_store

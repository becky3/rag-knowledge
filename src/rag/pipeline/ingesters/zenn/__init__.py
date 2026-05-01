"""Zenn インジェスター package.

仕様: docs/specs/ingesters/zenn.md

公開シンボルは ``_facade`` から再 export する。
本 package の構造は docs/specs/architecture.md「正解パターン」セクションを参照。
"""

from __future__ import annotations

from rag.pipeline.ingesters.zenn._facade import (
    MAX_ARTICLES_HARD_LIMIT,
    MAX_PAGINATION_PAGES,
    ZENN_API_BASE,
    ZennIngester,
    parse_zenn_url,
)
from rag.pipeline.ingesters.zenn.fetcher_protocol import (
    RealZennFetcher,
    ZennFetcher,
    ZennKind,
    create_zenn_fetcher,
)

__all__ = [
    "MAX_ARTICLES_HARD_LIMIT",
    "MAX_PAGINATION_PAGES",
    "ZENN_API_BASE",
    "RealZennFetcher",
    "ZENN_API_BASE",
    "ZennFetcher",
    "ZennIngester",
    "ZennKind",
    "create_zenn_fetcher",
    "parse_zenn_url",
]

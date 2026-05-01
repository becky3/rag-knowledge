"""青空文庫インジェスター package.

仕様: docs/specs/ingesters/aozora.md

公開シンボルは ``_facade`` および ``fetcher_protocol`` から再 export する。
"""

from __future__ import annotations

from rag.pipeline.ingesters.aozora._facade import (
    CATALOG_REL_PATH,
    CATALOG_ZIP_URL,
    GITHUB_RAW_BASE,
    MAX_SEARCH_LIMIT,
    MAX_WORKS_HARD_LIMIT,
    AozoraIngester,
)
from rag.pipeline.ingesters.aozora.fetcher_protocol import (
    AozoraFetcher,
    RealAozoraFetcher,
    create_aozora_fetcher,
)

__all__ = [
    "CATALOG_REL_PATH",
    "CATALOG_ZIP_URL",
    "GITHUB_RAW_BASE",
    "MAX_SEARCH_LIMIT",
    "MAX_WORKS_HARD_LIMIT",
    "AozoraFetcher",
    "AozoraIngester",
    "RealAozoraFetcher",
    "create_aozora_fetcher",
]

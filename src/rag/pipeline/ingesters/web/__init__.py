"""WebIngester package — Scrapy subprocess 経由の Web ページ取り込み.

仕様: docs/specs/architecture.md §3.3
仕様: docs/specs/site-ingest.md
"""

from rag.pipeline.ingesters.web._facade import SiteIngestExecution, WebIngester
from rag.pipeline.ingesters.web.web_delegator import (
    RealWebDelegator,
    WebDelegator,
    create_web_delegator,
)

__all__ = [
    "RealWebDelegator",
    "SiteIngestExecution",
    "WebDelegator",
    "WebIngester",
    "create_web_delegator",
]

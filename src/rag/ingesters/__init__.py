"""インジェスタープラグイン

仕様: docs/specs/rag-knowledge.md
Issue: #62
"""

from .base import BaseIngester, IngestedContent
from .web import WebIngester
from .zenn import DiscoverResult, ZennIngester, format_zenn_ingest_result

__all__ = [
    "BaseIngester",
    "DiscoverResult",
    "IngestedContent",
    "WebIngester",
    "ZennIngester",
    "format_zenn_ingest_result",
]

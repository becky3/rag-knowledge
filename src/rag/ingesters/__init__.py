"""インジェスタープラグイン

仕様: docs/specs/rag-knowledge.md
Issue: #62
"""

from .base import BaseIngester, IngestedContent
from .web import WebIngester
from .zenn import ZennIngester

__all__ = [
    "BaseIngester",
    "IngestedContent",
    "WebIngester",
    "ZennIngester",
]

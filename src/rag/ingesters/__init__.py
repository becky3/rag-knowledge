"""インジェスタープラグイン

仕様: docs/specs/rag-knowledge.md
Issue: #62
"""

from .base import BaseIngester, IngestedContent
from .local_file import LocalFileIngester
from .web import WebIngester
from .zenn import ZennIngester

__all__ = [
    "BaseIngester",
    "IngestedContent",
    "LocalFileIngester",
    "WebIngester",
    "ZennIngester",
]

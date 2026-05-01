"""Local インジェスター package.

仕様: docs/specs/ingesters/local.md

公開シンボルは ``_facade`` および ``fetcher_protocol`` から再 export する。

備考: PDF/AsciiDoc 抽出は本 package のスコープ外（converter 層が担当）。
そのため LocalFetcher Protocol は filesystem 抽象化に限定される。
QA イテレーション速度向上は #713（converter Fake 化）で対応する。
"""

from __future__ import annotations

from rag.pipeline.ingesters.local._facade import (
    DEFAULT_SUPPORTED_EXTENSIONS,
    MAX_FILES_HARD_LIMIT,
    _UPLOAD_DIR,
    LocalIngester,
    UploadMode,
)
from rag.pipeline.ingesters.local.fetcher_protocol import (
    LocalFetcher,
    RealLocalFetcher,
    create_local_fetcher,
)

__all__ = [
    "DEFAULT_SUPPORTED_EXTENSIONS",
    "MAX_FILES_HARD_LIMIT",
    "LocalFetcher",
    "LocalIngester",
    "RealLocalFetcher",
    "UploadMode",
    "_UPLOAD_DIR",
    "create_local_fetcher",
]

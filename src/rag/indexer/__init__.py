"""インデクサーパッケージ.

仕様: docs/specs/indexer.md

converted_store のテキストファイルからチャンキング・Embedding・インデックス構築を行う。
"""

from rag.indexer.indexer import Indexer

__all__ = ["Indexer"]

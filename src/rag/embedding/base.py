"""Embedding生成の抽象基底クラス
仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import abc


class EmbeddingProvider(abc.ABC):
    """Embedding生成の抽象基底クラス.

    仕様: docs/specs/rag-knowledge.md
    """

    @abc.abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """テキストリストをベクトルリストに変換する."""

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """ドキュメント用Embedding（デフォルトはembed()に委譲）."""
        return await self.embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        """クエリ用Embedding（デフォルトはembed()に委譲）."""
        result = await self.embed([text])
        return result[0]

    @abc.abstractmethod
    async def is_available(self) -> bool:
        """プロバイダーが利用可能かチェックする."""

"""LM Studio経由のEmbeddingプロバイダー
仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import logging

from openai import AsyncOpenAI

from ..config import DEFAULT_LMSTUDIO_BASE_URL
from .base import EmbeddingProvider

logger = logging.getLogger(__name__)


class LMStudioEmbedding(EmbeddingProvider):
    """LM Studio経由のEmbeddingプロバイダー.

    仕様: docs/specs/rag-knowledge.md
    """

    DOCUMENT_PREFIX = "search_document: "
    QUERY_PREFIX = "search_query: "

    def __init__(
        self,
        base_url: str = DEFAULT_LMSTUDIO_BASE_URL,
        model: str = "ruri-v3-310m",
        prefix_enabled: bool = False,
    ) -> None:
        normalized = base_url.rstrip("/")
        if not normalized.endswith("/v1"):
            normalized = f"{normalized}/v1"
        self._client = AsyncOpenAI(base_url=normalized, api_key="lm-studio")
        self._model = model
        self._prefix_enabled = prefix_enabled

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """テキストリストをベクトルリストに変換する."""
        response = await self._client.embeddings.create(
            model=self._model,
            input=texts,
        )
        return [item.embedding for item in response.data]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """ドキュメント用Embedding（プレフィックス有効時はsearch_document:を付加）."""
        if self._prefix_enabled:
            texts = [self.DOCUMENT_PREFIX + t for t in texts]
        return await self.embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        """クエリ用Embedding（プレフィックス有効時はsearch_query:を付加）."""
        if self._prefix_enabled:
            text = self.QUERY_PREFIX + text
        result = await self.embed([text])
        return result[0]

    async def is_available(self) -> bool:
        """モデル一覧取得で疎通確認する."""
        try:
            await self._client.models.list()
            return True
        except Exception as e:
            logger.debug("LM Studio embedding is not available: %s", e)
            return False

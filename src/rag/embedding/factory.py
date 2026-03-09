"""Embeddingプロバイダー生成ファクトリ
仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

from typing import Literal

from ..config import RAGSettings
from .base import EmbeddingProvider
from .lmstudio_embedding import LMStudioEmbedding
from .openai_embedding import OpenAIEmbedding


def get_embedding_provider(
    settings: RAGSettings,
    provider_setting: Literal["local", "online"],
) -> EmbeddingProvider:
    """設定に応じたEmbeddingプロバイダーを返す.

    Args:
        settings: アプリケーション設定
        provider_setting: プロバイダー設定（"local" or "online"）

    Returns:
        対応するEmbeddingプロバイダー
    """
    if provider_setting == "online":
        return OpenAIEmbedding(
            api_key=settings.openai_api_key,
            model=settings.embedding_model_online,
        )
    return LMStudioEmbedding(
        base_url=settings.lmstudio_base_url,
        model=settings.embedding_model_local,
        prefix_enabled=settings.embedding_prefix_enabled,
    )

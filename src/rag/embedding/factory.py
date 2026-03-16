"""Embeddingプロバイダー生成ファクトリ
仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

from typing import Literal

from py_common_lib.secrets import SecretNotFoundError, SecretStoreError, get_secret

from ..config import RAGSettings
from .base import EmbeddingProvider
from .lmstudio_embedding import LMStudioEmbedding
from .openai_embedding import OpenAIEmbedding

_SERVICE_NAME = "rag-knowledge"


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
        try:
            api_key = get_secret("OPENAI_API_KEY", service=_SERVICE_NAME)
        except SecretNotFoundError:
            msg = "OPENAI_API_KEY is not registered in the secret store"
            raise ValueError(msg) from None
        except SecretStoreError as e:
            msg = f"Failed to retrieve OPENAI_API_KEY from the secret store: {e}"
            raise ValueError(msg) from e
        if not api_key:
            msg = "OPENAI_API_KEY is registered but empty"
            raise ValueError(msg)
        return OpenAIEmbedding(
            api_key=api_key,
            model=settings.embedding_model_online,
        )
    return LMStudioEmbedding(
        base_url=settings.lmstudio_base_url,
        model=settings.embedding_model_local,
        prefix_enabled=settings.embedding_prefix_enabled,
    )

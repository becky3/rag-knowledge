"""Embeddingプロバイダーのテスト (Issue #115).

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.config import DEFAULT_LMSTUDIO_BASE_URL, RAGSettings as Settings
from rag.embedding.base import EmbeddingProvider
from rag.embedding.factory import get_embedding_provider
from rag.embedding.lmstudio_embedding import LMStudioEmbedding
from rag.embedding.openai_embedding import OpenAIEmbedding


def test_embedding_provider_interface() -> None:
    """AC1: EmbeddingProvider 抽象基底クラスが embed() と is_available() メソッドを定義すること."""
    assert hasattr(EmbeddingProvider, "embed")
    assert hasattr(EmbeddingProvider, "is_available")

    # ABC なので直接インスタンス化できない
    with pytest.raises(TypeError):
        EmbeddingProvider()  # type: ignore[abstract]


def test_lmstudio_is_subclass() -> None:
    """AC1: LMStudioEmbedding が EmbeddingProvider のサブクラスであること."""
    assert issubclass(LMStudioEmbedding, EmbeddingProvider)


def test_openai_is_subclass() -> None:
    """AC1: OpenAIEmbedding が EmbeddingProvider のサブクラスであること."""
    assert issubclass(OpenAIEmbedding, EmbeddingProvider)


@pytest.mark.asyncio
async def test_lmstudio_embedding_converts_text() -> None:
    """AC2: LMStudioEmbedding が LM Studio 経由でテキストをベクトルに変換できること."""
    provider = LMStudioEmbedding(
        base_url=DEFAULT_LMSTUDIO_BASE_URL,
        model="nomic-embed-text",
    )

    # AsyncOpenAI.embeddings.create をモック
    mock_item_1 = MagicMock()
    mock_item_1.embedding = [0.1, 0.2, 0.3]
    mock_item_2 = MagicMock()
    mock_item_2.embedding = [0.4, 0.5, 0.6]

    mock_response = MagicMock()
    mock_response.data = [mock_item_1, mock_item_2]

    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    result = await provider.embed(["hello", "world"])

    assert result == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    provider._client.embeddings.create.assert_awaited_once_with(
        model="nomic-embed-text",
        input=["hello", "world"],
    )


@pytest.mark.asyncio
async def test_lmstudio_embedding_is_available_true() -> None:
    """AC2: LMStudioEmbedding の is_available() が接続成功時に True を返すこと."""
    provider = LMStudioEmbedding()
    provider._client.models.list = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]

    assert await provider.is_available() is True


@pytest.mark.asyncio
async def test_lmstudio_embedding_is_available_false() -> None:
    """AC2: LMStudioEmbedding の is_available() が接続失敗時に False を返すこと."""
    provider = LMStudioEmbedding()
    provider._client.models.list = AsyncMock(side_effect=Exception("connection refused"))  # type: ignore[method-assign]

    assert await provider.is_available() is False


@pytest.mark.asyncio
async def test_openai_embedding_converts_text() -> None:
    """AC3: OpenAIEmbedding が OpenAI Embeddings API でテキストをベクトルに変換できること."""
    provider = OpenAIEmbedding(api_key="sk-test", model="text-embedding-3-small")

    mock_item = MagicMock()
    mock_item.embedding = [0.7, 0.8, 0.9]

    mock_response = MagicMock()
    mock_response.data = [mock_item]

    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    result = await provider.embed(["test text"])

    assert result == [[0.7, 0.8, 0.9]]
    provider._client.embeddings.create.assert_awaited_once_with(
        model="text-embedding-3-small",
        input=["test text"],
    )


@pytest.mark.asyncio
async def test_openai_embedding_is_available() -> None:
    """AC3: OpenAIEmbedding の is_available() が APIキー有無で判定すること."""
    provider_with_key = OpenAIEmbedding(api_key="sk-test")
    assert await provider_with_key.is_available() is True

    provider_without_key = OpenAIEmbedding(api_key="")
    assert await provider_without_key.is_available() is False


def test_factory_returns_correct_provider_local() -> None:
    """AC4: get_embedding_provider() が 'local' 設定で LMStudioEmbedding を返すこと."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    provider = get_embedding_provider(settings, "local")
    assert isinstance(provider, LMStudioEmbedding)


def test_factory_returns_correct_provider_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4: get_embedding_provider() が 'online' 設定で OpenAIEmbedding を返すこと."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    provider = get_embedding_provider(settings, "online")
    assert isinstance(provider, OpenAIEmbedding)


def test_factory_uses_settings_model_local() -> None:
    """AC4: ファクトリが Settings の embedding_model_local を使用すること."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    provider = get_embedding_provider(settings, "local")
    assert isinstance(provider, LMStudioEmbedding)
    assert provider._model == settings.embedding_model_local


def test_factory_uses_settings_model_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4: ファクトリが Settings の embedding_model_online を使用すること."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("EMBEDDING_MODEL_ONLINE", "text-embedding-3-large")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    provider = get_embedding_provider(settings, "online")
    assert isinstance(provider, OpenAIEmbedding)
    assert provider._model == "text-embedding-3-large"


def test_embedding_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4: Embedding関連設定のデフォルト値が正しいこと."""
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL_LOCAL", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL_ONLINE", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.embedding_provider == "local"
    assert settings.embedding_model_local == "nomic-embed-text"
    assert settings.embedding_model_online == "text-embedding-3-small"


def test_embedding_settings_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4: Embedding関連設定が環境変数で変更可能であること."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "online")
    monkeypatch.setenv("EMBEDDING_MODEL_LOCAL", "custom-embed-model")
    monkeypatch.setenv("EMBEDDING_MODEL_ONLINE", "text-embedding-3-large")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.embedding_provider == "online"
    assert settings.embedding_model_local == "custom-embed-model"
    assert settings.embedding_model_online == "text-embedding-3-large"


def test_lmstudio_embedding_default_params() -> None:
    """LMStudioEmbedding のデフォルトパラメータが正しいこと."""
    provider = LMStudioEmbedding()
    assert provider._client.base_url.host == "localhost"
    # base_url にホストのみ指定しても /v1 がコード側で付加される
    assert provider._client.base_url.path == "/v1/"
    assert provider._model == "nomic-embed-text"


def test_lmstudio_embedding_custom_params() -> None:
    """LMStudioEmbedding のカスタムパラメータが反映されること."""
    provider = LMStudioEmbedding(
        base_url="http://192.168.1.100:5000",
        model="custom-embed",
    )
    assert provider._client.base_url.host == "192.168.1.100"
    assert provider._client.base_url.path == "/v1/"
    assert provider._model == "custom-embed"


def test_lmstudio_embedding_base_url_with_v1_suffix_no_duplication() -> None:
    """base_url に /v1 が既に含まれている場合、/v1/v1 にならないこと."""
    provider = LMStudioEmbedding(base_url="http://localhost:1234/v1")
    assert provider._client.base_url.path == "/v1/"


# --- Embedding prefix tests (Issue #517) ---


@pytest.mark.asyncio
async def test_embed_documents_default_delegates_to_embed() -> None:
    """embed_documents() がデフォルトで embed() に委譲すること."""
    provider = LMStudioEmbedding(prefix_enabled=False)

    mock_item_1 = MagicMock()
    mock_item_1.embedding = [0.1, 0.2, 0.3]
    mock_response = MagicMock()
    mock_response.data = [mock_item_1]
    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    result = await provider.embed_documents(["hello"])
    assert result == [[0.1, 0.2, 0.3]]
    provider._client.embeddings.create.assert_awaited_once_with(
        model="nomic-embed-text",
        input=["hello"],
    )


@pytest.mark.asyncio
async def test_embed_query_default_delegates_to_embed() -> None:
    """embed_query() がデフォルトで embed() に委譲し単一ベクトルを返すこと."""
    provider = LMStudioEmbedding(prefix_enabled=False)

    mock_item = MagicMock()
    mock_item.embedding = [0.4, 0.5, 0.6]
    mock_response = MagicMock()
    mock_response.data = [mock_item]
    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    result = await provider.embed_query("hello")
    assert result == [0.4, 0.5, 0.6]
    provider._client.embeddings.create.assert_awaited_once_with(
        model="nomic-embed-text",
        input=["hello"],
    )


@pytest.mark.asyncio
async def test_prefix_enabled_adds_document_prefix() -> None:
    """prefix_enabled=True で embed_documents() にドキュメントプレフィックスが付加されること."""
    provider = LMStudioEmbedding(prefix_enabled=True)

    mock_item = MagicMock()
    mock_item.embedding = [0.1, 0.2, 0.3]
    mock_response = MagicMock()
    mock_response.data = [mock_item]
    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    await provider.embed_documents(["hello"])
    provider._client.embeddings.create.assert_awaited_once_with(
        model="nomic-embed-text",
        input=["search_document: hello"],
    )


@pytest.mark.asyncio
async def test_prefix_enabled_adds_query_prefix() -> None:
    """prefix_enabled=True で embed_query() にクエリプレフィックスが付加されること."""
    provider = LMStudioEmbedding(prefix_enabled=True)

    mock_item = MagicMock()
    mock_item.embedding = [0.7, 0.8, 0.9]
    mock_response = MagicMock()
    mock_response.data = [mock_item]
    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    await provider.embed_query("hello")
    provider._client.embeddings.create.assert_awaited_once_with(
        model="nomic-embed-text",
        input=["search_query: hello"],
    )


@pytest.mark.asyncio
async def test_prefix_disabled_no_prefix_on_documents() -> None:
    """prefix_enabled=False で embed_documents() にプレフィックスが付かないこと."""
    provider = LMStudioEmbedding(prefix_enabled=False)

    mock_item = MagicMock()
    mock_item.embedding = [0.1, 0.2, 0.3]
    mock_response = MagicMock()
    mock_response.data = [mock_item]
    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    await provider.embed_documents(["hello"])
    provider._client.embeddings.create.assert_awaited_once_with(
        model="nomic-embed-text",
        input=["hello"],
    )


@pytest.mark.asyncio
async def test_openai_embed_documents_delegates_to_embed() -> None:
    """OpenAIEmbedding の embed_documents() がデフォルト実装（embed委譲）で動作すること."""
    provider = OpenAIEmbedding(api_key="sk-test")

    mock_item = MagicMock()
    mock_item.embedding = [1.0, 2.0, 3.0]
    mock_response = MagicMock()
    mock_response.data = [mock_item]
    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    result = await provider.embed_documents(["test"])
    assert result == [[1.0, 2.0, 3.0]]


@pytest.mark.asyncio
async def test_openai_embed_query_delegates_to_embed() -> None:
    """OpenAIEmbedding の embed_query() がデフォルト実装（embed委譲）で動作すること."""
    provider = OpenAIEmbedding(api_key="sk-test")

    mock_item = MagicMock()
    mock_item.embedding = [1.0, 2.0, 3.0]
    mock_response = MagicMock()
    mock_response.data = [mock_item]
    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    result = await provider.embed_query("test")
    assert result == [1.0, 2.0, 3.0]


def test_embedding_prefix_enabled_setting_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """embedding_prefix_enabled のデフォルト値が True であること."""
    monkeypatch.delenv("EMBEDDING_PREFIX_ENABLED", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.embedding_prefix_enabled is True


def test_embedding_prefix_enabled_setting_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """embedding_prefix_enabled が環境変数で変更可能であること."""
    monkeypatch.setenv("EMBEDDING_PREFIX_ENABLED", "true")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.embedding_prefix_enabled is True


def test_factory_passes_prefix_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """ファクトリが prefix_enabled を LMStudioEmbedding に渡すこと."""
    monkeypatch.setenv("EMBEDDING_PREFIX_ENABLED", "true")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    provider = get_embedding_provider(settings, "local")
    assert isinstance(provider, LMStudioEmbedding)
    assert provider._prefix_enabled is True


def test_factory_passes_prefix_enabled_by_default() -> None:
    """ファクトリがデフォルトで prefix_enabled=True を渡すこと."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    provider = get_embedding_provider(settings, "local")
    assert isinstance(provider, LMStudioEmbedding)
    assert provider._prefix_enabled is True

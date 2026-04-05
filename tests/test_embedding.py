"""Embeddingプロバイダーのテスト (Issue #115, #541).

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import APIConnectionError, APITimeoutError
from py_common_lib.secrets import SecretNotFoundError

from factories import make_lmstudio_embedding_args, make_openai_embedding_args
from settings_defaults import TEST_SETTINGS_DEFAULTS
from rag.config import RAGSettings as Settings
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
        **make_lmstudio_embedding_args(),
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
        model=provider._model,
        input=["hello", "world"],
    )


@pytest.mark.asyncio
async def test_lmstudio_embedding_is_available_true() -> None:
    """AC2: LMStudioEmbedding の is_available() が接続成功時に True を返すこと."""
    provider = LMStudioEmbedding(**make_lmstudio_embedding_args())
    provider._client.models.list = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]

    assert await provider.is_available() is True


@pytest.mark.asyncio
async def test_lmstudio_embedding_is_available_false() -> None:
    """AC2: LMStudioEmbedding の is_available() が接続失敗時に False を返すこと."""
    provider = LMStudioEmbedding(**make_lmstudio_embedding_args())
    provider._client.models.list = AsyncMock(side_effect=Exception("connection refused"))  # type: ignore[method-assign]

    assert await provider.is_available() is False


@pytest.mark.asyncio
async def test_openai_embedding_converts_text() -> None:
    """AC3: OpenAIEmbedding が OpenAI Embeddings API でテキストをベクトルに変換できること."""
    provider = OpenAIEmbedding(**make_openai_embedding_args())

    mock_item = MagicMock()
    mock_item.embedding = [0.7, 0.8, 0.9]

    mock_response = MagicMock()
    mock_response.data = [mock_item]

    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    result = await provider.embed(["test text"])

    assert result == [[0.7, 0.8, 0.9]]
    provider._client.embeddings.create.assert_awaited_once_with(
        model=provider._model,
        input=["test text"],
    )


@pytest.mark.asyncio
async def test_openai_embedding_is_available() -> None:
    """AC3: OpenAIEmbedding の is_available() が APIキー有無で判定すること."""
    provider_with_key = OpenAIEmbedding(**make_openai_embedding_args(api_key="sk-test"))
    assert await provider_with_key.is_available() is True

    provider_without_key = OpenAIEmbedding(**make_openai_embedding_args(api_key=""))
    assert await provider_without_key.is_available() is False


def test_factory_returns_correct_provider_local() -> None:
    """AC4: get_embedding_provider() が 'local' 設定で LMStudioEmbedding を返すこと."""
    settings = Settings(**TEST_SETTINGS_DEFAULTS)
    provider = get_embedding_provider(settings, "local")
    assert isinstance(provider, LMStudioEmbedding)


def test_factory_returns_correct_provider_online() -> None:
    """AC4: get_embedding_provider() が 'online' 設定で OpenAIEmbedding を返すこと."""
    settings = Settings(**TEST_SETTINGS_DEFAULTS)
    with patch("rag.embedding.factory.get_secret", return_value="sk-test"):
        provider = get_embedding_provider(settings, "online")
    assert isinstance(provider, OpenAIEmbedding)


def test_factory_raises_on_missing_api_key() -> None:
    """OPENAI_API_KEY 未登録時に ValueError を送出すること."""
    settings = Settings(**TEST_SETTINGS_DEFAULTS)
    with patch(
        "rag.embedding.factory.get_secret",
        side_effect=SecretNotFoundError("not found"),
    ):
        with pytest.raises(ValueError, match="not registered"):
            get_embedding_provider(settings, "online")


def test_factory_raises_on_empty_api_key() -> None:
    """OPENAI_API_KEY が空文字列の場合に ValueError を送出すること."""
    settings = Settings(**TEST_SETTINGS_DEFAULTS)
    with patch("rag.embedding.factory.get_secret", return_value=""):
        with pytest.raises(ValueError, match="empty"):
            get_embedding_provider(settings, "online")


def test_factory_uses_settings_model_local() -> None:
    """AC4: ファクトリが Settings の embedding_model_local を使用すること."""
    settings = Settings(**TEST_SETTINGS_DEFAULTS)
    provider = get_embedding_provider(settings, "local")
    assert isinstance(provider, LMStudioEmbedding)
    assert provider._model == settings.embedding_model_local


def test_factory_uses_settings_model_online() -> None:
    """AC4: ファクトリが Settings の embedding_model_online を使用すること."""
    settings = Settings(**{**TEST_SETTINGS_DEFAULTS, "embedding_model_online": "text-embedding-3-large"})
    with patch("rag.embedding.factory.get_secret", return_value="sk-test"):
        provider = get_embedding_provider(settings, "online")
    assert isinstance(provider, OpenAIEmbedding)
    assert provider._model == "text-embedding-3-large"


def test_embedding_settings_accepted() -> None:
    """AC4: Embedding関連設定が正しく受け入れられること."""
    settings = Settings(**TEST_SETTINGS_DEFAULTS)
    assert settings.embedding_provider == "local"
    assert settings.embedding_model_local  # デフォルト値が設定されていること
    assert settings.embedding_model_online  # デフォルト値が設定されていること


def test_embedding_settings_configurable() -> None:
    """AC4: Embedding関連設定が設定可能であること."""
    settings = Settings(**{
        **TEST_SETTINGS_DEFAULTS,
        "embedding_provider": "online",
        "embedding_model_local": "custom-embed-model",
        "embedding_model_online": "text-embedding-3-large",
    })
    assert settings.embedding_provider == "online"
    assert settings.embedding_model_local == "custom-embed-model"
    assert settings.embedding_model_online == "text-embedding-3-large"


def test_lmstudio_embedding_default_params() -> None:
    """LMStudioEmbedding のファクトリデフォルトパラメータが設定されていること."""
    provider = LMStudioEmbedding(**make_lmstudio_embedding_args())
    assert provider._client.base_url.host == "localhost"
    # base_url にホストのみ指定しても /v1 がコード側で付加される
    assert provider._client.base_url.path == "/v1/"
    assert provider._model  # モデルが設定されていること


def test_lmstudio_embedding_custom_params() -> None:
    """LMStudioEmbedding のカスタムパラメータが反映されること."""
    provider = LMStudioEmbedding(
        **make_lmstudio_embedding_args(
            base_url="http://192.168.1.100:5000",
            model="custom-embed",
        ),
    )
    assert provider._client.base_url.host == "192.168.1.100"
    assert provider._client.base_url.path == "/v1/"
    assert provider._model == "custom-embed"


def test_lmstudio_embedding_base_url_with_v1_suffix_no_duplication() -> None:
    """base_url に /v1 が既に含まれている場合、/v1/v1 にならないこと."""
    provider = LMStudioEmbedding(**make_lmstudio_embedding_args(base_url="http://localhost:1234/v1"))
    assert provider._client.base_url.path == "/v1/"


# --- Embedding prefix tests (Issue #517) ---


@pytest.mark.asyncio
async def test_embed_documents_default_delegates_to_embed() -> None:
    """embed_documents() がデフォルトで embed() に委譲すること."""
    provider = LMStudioEmbedding(**make_lmstudio_embedding_args(prefix_enabled=False))

    mock_item_1 = MagicMock()
    mock_item_1.embedding = [0.1, 0.2, 0.3]
    mock_response = MagicMock()
    mock_response.data = [mock_item_1]
    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    result = await provider.embed_documents(["hello"])
    assert result == [[0.1, 0.2, 0.3]]
    provider._client.embeddings.create.assert_awaited_once_with(
        model=provider._model,
        input=["hello"],
    )


@pytest.mark.asyncio
async def test_embed_query_default_delegates_to_embed() -> None:
    """embed_query() がデフォルトで embed() に委譲し単一ベクトルを返すこと."""
    provider = LMStudioEmbedding(**make_lmstudio_embedding_args(prefix_enabled=False))

    mock_item = MagicMock()
    mock_item.embedding = [0.4, 0.5, 0.6]
    mock_response = MagicMock()
    mock_response.data = [mock_item]
    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    result = await provider.embed_query("hello")
    assert result == [0.4, 0.5, 0.6]
    provider._client.embeddings.create.assert_awaited_once_with(
        model=provider._model,
        input=["hello"],
    )


@pytest.mark.asyncio
async def test_prefix_enabled_adds_document_prefix() -> None:
    """prefix_enabled=True で embed_documents() にドキュメントプレフィックスが付加されること."""
    provider = LMStudioEmbedding(**make_lmstudio_embedding_args(prefix_enabled=True))

    mock_item = MagicMock()
    mock_item.embedding = [0.1, 0.2, 0.3]
    mock_response = MagicMock()
    mock_response.data = [mock_item]
    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    await provider.embed_documents(["hello"])
    provider._client.embeddings.create.assert_awaited_once_with(
        model=provider._model,
        input=["search_document: hello"],
    )


@pytest.mark.asyncio
async def test_prefix_enabled_adds_query_prefix() -> None:
    """prefix_enabled=True で embed_query() にクエリプレフィックスが付加されること."""
    provider = LMStudioEmbedding(**make_lmstudio_embedding_args(prefix_enabled=True))

    mock_item = MagicMock()
    mock_item.embedding = [0.7, 0.8, 0.9]
    mock_response = MagicMock()
    mock_response.data = [mock_item]
    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    await provider.embed_query("hello")
    provider._client.embeddings.create.assert_awaited_once_with(
        model=provider._model,
        input=["search_query: hello"],
    )


@pytest.mark.asyncio
async def test_prefix_disabled_no_prefix_on_documents() -> None:
    """prefix_enabled=False で embed_documents() にプレフィックスが付かないこと."""
    provider = LMStudioEmbedding(**make_lmstudio_embedding_args(prefix_enabled=False))

    mock_item = MagicMock()
    mock_item.embedding = [0.1, 0.2, 0.3]
    mock_response = MagicMock()
    mock_response.data = [mock_item]
    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    await provider.embed_documents(["hello"])
    provider._client.embeddings.create.assert_awaited_once_with(
        model=provider._model,
        input=["hello"],
    )


@pytest.mark.asyncio
async def test_openai_embed_documents_delegates_to_embed() -> None:
    """OpenAIEmbedding の embed_documents() がデフォルト実装（embed委譲）で動作すること."""
    provider = OpenAIEmbedding(**make_openai_embedding_args())

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
    provider = OpenAIEmbedding(**make_openai_embedding_args())

    mock_item = MagicMock()
    mock_item.embedding = [1.0, 2.0, 3.0]
    mock_response = MagicMock()
    mock_response.data = [mock_item]
    provider._client.embeddings.create = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    result = await provider.embed_query("test")
    assert result == [1.0, 2.0, 3.0]


def test_embedding_prefix_enabled_setting_accepted() -> None:
    """embedding_prefix_enabled が正しく受け入れられること."""
    settings = Settings(**TEST_SETTINGS_DEFAULTS)
    assert settings.embedding_prefix_enabled is True


def test_embedding_prefix_enabled_setting_configurable() -> None:
    """embedding_prefix_enabled が設定可能であること."""
    settings = Settings(**{**TEST_SETTINGS_DEFAULTS, "embedding_prefix_enabled": True})
    assert settings.embedding_prefix_enabled is True


def test_factory_passes_prefix_enabled() -> None:
    """ファクトリが prefix_enabled を LMStudioEmbedding に渡すこと."""
    settings = Settings(**{**TEST_SETTINGS_DEFAULTS, "embedding_prefix_enabled": True})
    provider = get_embedding_provider(settings, "local")
    assert isinstance(provider, LMStudioEmbedding)
    assert provider._prefix_enabled is True


def test_factory_passes_prefix_enabled_by_default() -> None:
    """ファクトリがデフォルトで prefix_enabled=True を渡すこと."""
    settings = Settings(**TEST_SETTINGS_DEFAULTS)
    provider = get_embedding_provider(settings, "local")
    assert isinstance(provider, LMStudioEmbedding)
    assert provider._prefix_enabled is True


# --- Embedding retry tests (Issue #541) ---


@pytest.mark.asyncio
async def test_embed_retries_on_connection_error() -> None:
    """接続エラー時にリトライが実行され、成功時に結果を返すこと."""
    provider = LMStudioEmbedding(
        **make_lmstudio_embedding_args(retry_count=2, retry_base_delay=0.01),
    )

    mock_item = MagicMock()
    mock_item.embedding = [0.1, 0.2, 0.3]
    mock_response = MagicMock()
    mock_response.data = [mock_item]

    provider._client.embeddings.create = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            APIConnectionError(request=MagicMock()),
            mock_response,
        ],
    )

    result = await provider.embed(["hello"])
    assert result == [[0.1, 0.2, 0.3]]
    assert provider._client.embeddings.create.await_count == 2


@pytest.mark.asyncio
async def test_embed_retries_on_timeout_error() -> None:
    """タイムアウトエラー時にリトライが実行されること."""
    provider = LMStudioEmbedding(
        **make_lmstudio_embedding_args(retry_count=2, retry_base_delay=0.01),
    )

    mock_item = MagicMock()
    mock_item.embedding = [0.4, 0.5, 0.6]
    mock_response = MagicMock()
    mock_response.data = [mock_item]

    provider._client.embeddings.create = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            APITimeoutError(request=MagicMock()),
            mock_response,
        ],
    )

    result = await provider.embed(["world"])
    assert result == [[0.4, 0.5, 0.6]]
    assert provider._client.embeddings.create.await_count == 2


@pytest.mark.asyncio
async def test_embed_raises_after_retry_exhaustion() -> None:
    """リトライ上限到達時にエラーが伝播すること."""
    provider = LMStudioEmbedding(
        **make_lmstudio_embedding_args(retry_count=2, retry_base_delay=0.01),
    )

    provider._client.embeddings.create = AsyncMock(  # type: ignore[method-assign]
        side_effect=APIConnectionError(request=MagicMock()),
    )

    with pytest.raises(APIConnectionError):
        await provider.embed(["fail"])

    # 初回 + 2 リトライ = 3 回
    assert provider._client.embeddings.create.await_count == 3


@pytest.mark.asyncio
async def test_embed_no_retry_on_other_errors() -> None:
    """接続・タイムアウト以外のエラーはリトライせず即座に伝播すること."""
    provider = LMStudioEmbedding(
        **make_lmstudio_embedding_args(retry_count=2, retry_base_delay=0.01),
    )

    provider._client.embeddings.create = AsyncMock(  # type: ignore[method-assign]
        side_effect=ValueError("unexpected"),
    )

    with pytest.raises(ValueError, match="unexpected"):
        await provider.embed(["fail"])

    assert provider._client.embeddings.create.await_count == 1


@pytest.mark.asyncio
async def test_embed_no_retry_when_retry_count_zero() -> None:
    """retry_count=0 のときリトライしないこと."""
    provider = LMStudioEmbedding(
        **make_lmstudio_embedding_args(retry_count=0, retry_base_delay=0.01),
    )

    provider._client.embeddings.create = AsyncMock(  # type: ignore[method-assign]
        side_effect=APIConnectionError(request=MagicMock()),
    )

    with pytest.raises(APIConnectionError):
        await provider.embed(["fail"])

    assert provider._client.embeddings.create.await_count == 1


def test_factory_passes_retry_settings() -> None:
    """ファクトリがリトライ設定を LMStudioEmbedding に渡すこと."""
    settings = Settings(**{
        **TEST_SETTINGS_DEFAULTS,
        "rag_embedding_retry_count": 5,
        "rag_embedding_retry_base_delay": 2.0,
    })
    provider = get_embedding_provider(settings, "local")
    assert isinstance(provider, LMStudioEmbedding)
    assert provider._retry_count == 5
    assert provider._retry_base_delay == 2.0

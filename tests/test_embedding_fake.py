"""FakeEmbedding と factory の fake_mode 分岐のテスト.

仕様: docs/specs/infrastructure/fake-mode.md
"""

from __future__ import annotations

from typing import Any

import pytest

from rag.config import RAGSettings as Settings
from rag.embedding._fake import FakeEmbedding
from rag.embedding.base import EmbeddingProvider
from rag.embedding.factory import get_embedding_provider
from settings_defaults import TEST_SETTINGS_DEFAULTS


def _make_settings(**overrides: Any) -> Settings:
    return Settings(**{**TEST_SETTINGS_DEFAULTS, **overrides})


class TestFakeEmbedding:
    def test_implements_provider_interface(self) -> None:
        fake = FakeEmbedding(dimensions=8)
        assert isinstance(fake, EmbeddingProvider)

    @pytest.mark.asyncio
    async def test_is_available_returns_true(self) -> None:
        fake = FakeEmbedding(dimensions=8)
        assert await fake.is_available() is True

    @pytest.mark.asyncio
    async def test_embed_returns_correct_dimensions(self) -> None:
        fake = FakeEmbedding(dimensions=16)
        vectors = await fake.embed(["text1", "text2"])
        assert len(vectors) == 2
        assert all(len(v) == 16 for v in vectors)

    @pytest.mark.asyncio
    async def test_embed_is_deterministic(self) -> None:
        """同じ入力テキストに同じベクトルを返すこと."""
        fake = FakeEmbedding(dimensions=32)
        v1 = (await fake.embed(["hello"]))[0]
        v2 = (await fake.embed(["hello"]))[0]
        assert v1 == v2

    @pytest.mark.asyncio
    async def test_different_texts_yield_different_vectors(self) -> None:
        fake = FakeEmbedding(dimensions=32)
        v1 = (await fake.embed(["hello"]))[0]
        v2 = (await fake.embed(["world"]))[0]
        assert v1 != v2

    @pytest.mark.asyncio
    async def test_l2_normalized(self) -> None:
        """戻り値ベクトルが L2 正規化済みであること."""
        fake = FakeEmbedding(dimensions=64)
        vectors = await fake.embed(["alpha", "beta gamma", "delta"])
        for v in vectors:
            norm = sum(x * x for x in v) ** 0.5
            assert abs(norm - 1.0) < 1e-9

    @pytest.mark.asyncio
    async def test_embed_query_matches_embed(self) -> None:
        fake = FakeEmbedding(dimensions=8)
        q = await fake.embed_query("query text")
        embedded = (await fake.embed(["query text"]))[0]
        assert q == embedded

    @pytest.mark.asyncio
    async def test_embed_documents_default_delegates_to_embed(self) -> None:
        fake = FakeEmbedding(dimensions=8)
        docs = await fake.embed_documents(["a", "b"])
        embedded = await fake.embed(["a", "b"])
        assert docs == embedded

    def test_invalid_dimensions_raises(self) -> None:
        with pytest.raises(ValueError, match="1 以上"):
            FakeEmbedding(dimensions=0)


class TestFactoryFakeMode:
    def test_returns_fake_when_fake_mode_true(self) -> None:
        settings = _make_settings(rag_embedding_fake_mode=True)
        provider = get_embedding_provider(settings, "local")
        assert isinstance(provider, FakeEmbedding)

    def test_fake_mode_overrides_provider_setting(self) -> None:
        """fake_mode=True 時は provider_setting='online' でも Fake が返ること."""
        settings = _make_settings(rag_embedding_fake_mode=True)
        provider = get_embedding_provider(settings, "online")
        assert isinstance(provider, FakeEmbedding)

    def test_fake_dimensions_passed_through(self) -> None:
        settings = _make_settings(
            rag_embedding_fake_mode=True,
            rag_embedding_fake_dimensions=256,
        )
        provider = get_embedding_provider(settings, "local")
        assert isinstance(provider, FakeEmbedding)
        assert provider._dimensions == 256

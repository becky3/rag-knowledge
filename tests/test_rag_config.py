"""RAGSettings のバリデーションテスト.

Issue: #168

テスト方針:
- Zenn インジェスター設定項目のデフォルト値・許容範囲・境界値を検証
- 環境変数からの読み込みを検証
- pydantic ValidationError の発生を検証
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rag.config import RAGSettings

_ZENN_ENV_VARS = [
    "RAG_ZENN_MAX_ARTICLES",
    "RAG_ZENN_REQUEST_TIMEOUT",
    "RAG_ZENN_REQUEST_INTERVAL",
]


@pytest.fixture(autouse=True)
def _clear_zenn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """テスト間で Zenn 関連の環境変数をクリアする."""
    for var in _ZENN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _make_settings(**overrides: object) -> RAGSettings:
    """テスト用 RAGSettings を生成する（.env を読み込まない）."""
    return RAGSettings(_env_file=None, **overrides)  # type: ignore[call-arg]


# --- Zenn インジェスター: rag_zenn_max_articles ---


class TestZennMaxArticles:
    """rag_zenn_max_articles のバリデーションテスト."""

    def test_default_value(self) -> None:
        """デフォルト値が 50 であること."""
        settings = _make_settings()
        assert settings.rag_zenn_max_articles == 50

    def test_valid_value(self) -> None:
        """許容範囲内の値が設定できること."""
        settings = _make_settings(rag_zenn_max_articles=1)
        assert settings.rag_zenn_max_articles == 1

        settings = _make_settings(rag_zenn_max_articles=100)
        assert settings.rag_zenn_max_articles == 100

    def test_below_minimum(self) -> None:
        """下限未満でバリデーションエラーになること."""
        with pytest.raises(ValidationError):
            _make_settings(rag_zenn_max_articles=0)

    def test_above_maximum(self) -> None:
        """上限超過でバリデーションエラーになること."""
        with pytest.raises(ValidationError):
            _make_settings(rag_zenn_max_articles=101)

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """環境変数から読み込めること."""
        monkeypatch.setenv("RAG_ZENN_MAX_ARTICLES", "75")
        settings = _make_settings()
        assert settings.rag_zenn_max_articles == 75


# --- Zenn インジェスター: rag_zenn_request_timeout ---


class TestZennRequestTimeout:
    """rag_zenn_request_timeout のバリデーションテスト."""

    def test_default_value(self) -> None:
        """デフォルト値が 30 であること."""
        settings = _make_settings()
        assert settings.rag_zenn_request_timeout == 30

    def test_valid_range(self) -> None:
        """許容範囲の境界値が設定できること."""
        settings = _make_settings(rag_zenn_request_timeout=1)
        assert settings.rag_zenn_request_timeout == 1

        settings = _make_settings(rag_zenn_request_timeout=120)
        assert settings.rag_zenn_request_timeout == 120

    def test_below_minimum(self) -> None:
        """下限未満でバリデーションエラーになること."""
        with pytest.raises(ValidationError):
            _make_settings(rag_zenn_request_timeout=0)

    def test_above_maximum(self) -> None:
        """上限超過でバリデーションエラーになること."""
        with pytest.raises(ValidationError):
            _make_settings(rag_zenn_request_timeout=121)

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """環境変数から読み込めること."""
        monkeypatch.setenv("RAG_ZENN_REQUEST_TIMEOUT", "60")
        settings = _make_settings()
        assert settings.rag_zenn_request_timeout == 60


# --- Zenn インジェスター: rag_zenn_request_interval ---


class TestZennRequestInterval:
    """rag_zenn_request_interval のバリデーションテスト."""

    def test_default_value(self) -> None:
        """デフォルト値が 1.0 であること."""
        settings = _make_settings()
        assert settings.rag_zenn_request_interval == 1.0

    def test_valid_range(self) -> None:
        """許容範囲の境界値が設定できること."""
        settings = _make_settings(rag_zenn_request_interval=0.1)
        assert settings.rag_zenn_request_interval == 0.1

        settings = _make_settings(rag_zenn_request_interval=60.0)
        assert settings.rag_zenn_request_interval == 60.0

    def test_below_minimum(self) -> None:
        """下限未満でバリデーションエラーになること."""
        with pytest.raises(ValidationError):
            _make_settings(rag_zenn_request_interval=0.05)

    def test_above_maximum(self) -> None:
        """上限超過でバリデーションエラーになること."""
        with pytest.raises(ValidationError):
            _make_settings(rag_zenn_request_interval=60.1)

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """環境変数から float として読み込めること."""
        monkeypatch.setenv("RAG_ZENN_REQUEST_INTERVAL", "0.5")
        settings = _make_settings()
        assert settings.rag_zenn_request_interval == 0.5

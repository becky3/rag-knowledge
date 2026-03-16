"""RAGSettings のバリデーションテスト.

Issue: #168, #207

テスト方針:
- Zenn インジェスター設定項目のデフォルト値・許容範囲・境界値を検証
- コンストラクタ引数での設定を検証
- pydantic ValidationError の発生を検証
- 3層分離のバリデーションロジックを検証
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from rag.config import RAGSettings, _ENV_FIELD_NAMES, _load_toml_config


def _make_settings(**overrides: object) -> RAGSettings:
    """テスト用 RAGSettings を生成する."""
    return RAGSettings(**overrides)


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


# --- 3層分離バリデーション ---


class TestTomlConfigValidation:
    """_load_toml_config のバリデーションテスト (#207)."""

    def test_rejects_env_fields_in_toml(self, tmp_path: Path) -> None:
        """config.toml に環境依存フィールドが含まれている場合 ValueError."""
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('embedding_provider = "online"\n')
        with patch("rag.config._TOML_FILE", toml_file):
            with pytest.raises(ValueError, match="環境依存設定"):
                _load_toml_config()

    def test_rejects_unknown_fields_in_toml(self, tmp_path: Path) -> None:
        """config.toml に未知のフィールドが含まれている場合 ValueError."""
        toml_file = tmp_path / "config.toml"
        toml_file.write_text("unknown_setting = 42\n")
        with patch("rag.config._TOML_FILE", toml_file):
            with pytest.raises(ValueError, match="未知の設定"):
                _load_toml_config()

    def test_valid_toml_loads_successfully(self, tmp_path: Path) -> None:
        """有効な config.toml が正常に読み込めること."""
        toml_file = tmp_path / "config.toml"
        toml_file.write_text("rag_chunk_size = 500\n")
        with patch("rag.config._TOML_FILE", toml_file):
            data = _load_toml_config()
        assert data["rag_chunk_size"] == 500

    def test_missing_toml_returns_empty(self, tmp_path: Path) -> None:
        """config.toml が存在しない場合は空辞書を返すこと."""
        toml_file = tmp_path / "nonexistent.toml"
        with patch("rag.config._TOML_FILE", toml_file):
            data = _load_toml_config()
        assert data == {}

    def test_env_field_names_match_env_loader(self) -> None:
        """_ENV_FIELD_NAMES が _EnvLoader のフィールドと一致すること."""
        from rag.config import _EnvLoader

        assert _ENV_FIELD_NAMES == frozenset(_EnvLoader.model_fields.keys())

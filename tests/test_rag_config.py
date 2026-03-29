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

from settings_defaults import TEST_SETTINGS_DEFAULTS
from rag.config import RAGSettings, _ENV_FIELD_NAMES, _load_toml_config


def _make_settings(**overrides: object) -> RAGSettings:
    """テスト用 RAGSettings を生成する（全必須フィールドをデフォルト値付きで提供）."""
    return RAGSettings(**{**TEST_SETTINGS_DEFAULTS, **overrides})


# --- Zenn インジェスター: rag_zenn_max_articles ---


class TestZennMaxArticles:
    """rag_zenn_max_articles のバリデーションテスト."""

    def test_default_value(self) -> None:
        """テスト用デフォルト値が 50 であること."""
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
        """テスト用デフォルト値が 30 であること."""
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
        """テスト用デフォルト値が 1.0 であること."""
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

    def test_missing_toml_raises_error(self, tmp_path: Path) -> None:
        """config.toml が存在しない場合は FileNotFoundError."""
        toml_file = tmp_path / "nonexistent.toml"
        with patch("rag.config._TOML_FILE", toml_file):
            with pytest.raises(FileNotFoundError, match="config.toml"):
                _load_toml_config()

    def test_env_field_names_match_env_loader(self) -> None:
        """_ENV_FIELD_NAMES が _EnvLoader のフィールドと一致すること."""
        from rag.config import _EnvLoader

        assert _ENV_FIELD_NAMES == frozenset(_EnvLoader.model_fields.keys())


class TestGetSettingsIntegration:
    """get_settings() の統合テスト (#207)."""

    # config.toml の全必須フィールド（TOML形式文字列）
    _TOML_CONTENT = """\
embedding_model_local = "nomic-embed-text"
embedding_model_online = "text-embedding-3-small"
embedding_prefix_enabled = true
rag_chunk_size = 200
rag_chunk_overlap = 30
rag_retrieval_count = 3
rag_hybrid_search_enabled = false
rag_vector_weight = 0.90
rag_bm25_k1 = 2.5
rag_bm25_b = 0.50
chromadb_collection_name = "knowledge"
rag_crawl_max_concurrent = 5
rag_max_crawl_pages = 50
rag_crawl_delay_sec = 1.0
rag_crawl_default_depth = 1
rag_crawl_max_errors = 5
rag_respect_robots_txt = true
rag_robots_txt_cache_ttl = 3600
rag_url_safety_check = false
rag_url_safety_cache_ttl = 300
rag_url_safety_timeout = 5.0
rag_stats_max_sources = 100
rag_list_recent_limit = 20
rag_zenn_max_articles = 50
rag_zenn_request_timeout = 30
rag_zenn_request_interval = 1.0
rag_document_supported_extensions = ".md,.txt,.pdf,.adoc"
rag_crawl_request_timeout = 30
rag_document_http_mode_enabled = false
rag_document_allowed_dirs = ""
rag_upload_max_file_size_mb = 50
rag_bluesky_appview_url = "https://public.api.bsky.app"
rag_bluesky_max_posts = 200
rag_bluesky_request_timeout = 30
rag_bluesky_request_interval = 1.0
rag_bluesky_include_reposts = true
rag_youtube_max_videos = 100
rag_youtube_request_interval = 5.0
rag_youtube_request_timeout = 30
rag_youtube_transcript_languages = ["ja", "en"]
rag_youtube_merge_gap_sec = 2.0
rag_youtube_merge_max_chars = 300
rag_youtube_max_duration = 14400
rag_aozora_max_works = 200
rag_aozora_request_interval = 1.0
rag_aozora_request_timeout = 30
rag_pdf_backend = "auto"
rag_pdf_mineru_mfd_conf_thres = 0.6
rag_pdf_quality_ufffd_threshold = 0.10
rag_pdf_quality_greek_threshold = 0.15
rag_pdf_quality_cjk_min_threshold = 0.05
rag_pdf_quality_min_chars_per_page = 10
rag_pdf_quality_sample_pages = 10
hnsw_m = 48
hnsw_construction_ef = 400
hnsw_search_ef = 300
site_ingest_delay_sec = 0.1
site_ingest_max_pages = 10000
site_ingest_download_timeout = 30
site_ingest_timeout_sec = 7200
site_ingest_error_count = 10
"""

    def _set_all_env(self, monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
        """全必須 env フィールドを monkeypatch で設定する."""
        defaults = {
            "EMBEDDING_PROVIDER": "local",
            "LMSTUDIO_BASE_URL": "http://localhost:1234",
            "CHROMADB_PERSIST_DIR": "./chroma_db",
            "BM25_PERSIST_DIR": "./bm25_index",
            "SOURCE_STORE_DIR": "./source_store",
            "CONVERTED_STORE_DIR": "./converted_store",
            "RAG_TRANSPORT": "stdio",
            "RAG_HTTP_HOST": "127.0.0.1",
            "RAG_HTTP_PORT": "8081",
            "RAG_DNS_REBINDING_PROTECTION": "true",
            "RAG_DEBUG_LOG_ENABLED": "false",
            "RAG_YOUTUBE_WHISPER_MODEL": "base",
            "RAG_YOUTUBE_WHISPER_DEVICE": "cpu",
            "SITE_INGEST_TEMP_DIR": ".tmp/site_ingest",
        }
        defaults.update(overrides)
        for key, value in defaults.items():
            monkeypatch.setenv(key, value)

    def test_env_values_reflected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """.env の値が RAGSettings に反映されること."""
        from rag.config import get_settings

        get_settings.cache_clear()
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(self._TOML_CONTENT)
        self._set_all_env(monkeypatch, EMBEDDING_PROVIDER="online")
        with patch("rag.config._TOML_FILE", toml_file):
            settings = get_settings()
            assert settings.embedding_provider == "online"
        get_settings.cache_clear()

    def test_toml_values_reflected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """config.toml の値が RAGSettings に反映されること."""
        from rag.config import get_settings

        get_settings.cache_clear()
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(self._TOML_CONTENT.replace(
            "rag_chunk_size = 200", "rag_chunk_size = 999"
        ))
        self._set_all_env(monkeypatch)
        with patch("rag.config._TOML_FILE", toml_file):
            settings = get_settings()
            assert settings.rag_chunk_size == 999
        get_settings.cache_clear()

    def test_env_and_toml_merged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """.env と config.toml の値が統合されること."""
        from rag.config import get_settings

        get_settings.cache_clear()
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(self._TOML_CONTENT.replace(
            "rag_chunk_size = 200", "rag_chunk_size = 300"
        ))
        self._set_all_env(monkeypatch, RAG_TRANSPORT="http")
        with patch("rag.config._TOML_FILE", toml_file):
            settings = get_settings()
            assert settings.rag_chunk_size == 300
            assert settings.rag_transport == "http"
        get_settings.cache_clear()

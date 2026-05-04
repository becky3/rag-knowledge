"""YoutubeFetcher ファクトリと log_fake_mode_status のテスト.

仕様: docs/specs/infrastructure/fake-mode.md
仕様: docs/specs/infrastructure/fake-adapters/youtube.md
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from rag.config import RAGSettings, _EnvLoader, log_fake_mode_status
from rag.pipeline.ingesters._fake.youtube import FakeYoutubeFetcher
from rag.pipeline.ingesters.youtube_fetcher import (
    RealYoutubeFetcher,
    create_youtube_fetcher,
)
from settings_defaults import TEST_SETTINGS_DEFAULTS


def _make_settings(**overrides: Any) -> RAGSettings:
    data = dict(TEST_SETTINGS_DEFAULTS)
    data.update(overrides)
    return RAGSettings(**data)


class TestCreateYoutubeFetcher:
    def test_returns_fake_fetcher_when_fake_mode_true(self) -> None:
        settings = _make_settings(rag_youtube_fake_mode=True)
        fetcher = create_youtube_fetcher(settings)
        assert isinstance(fetcher, FakeYoutubeFetcher)

    def test_returns_real_fetcher_when_fake_mode_false(self) -> None:
        settings = _make_settings(rag_youtube_fake_mode=False)
        fetcher = create_youtube_fetcher(settings)
        assert isinstance(fetcher, RealYoutubeFetcher)

    def test_raises_when_fixture_dir_missing(self, tmp_path: Path) -> None:
        non_existent = tmp_path / "does_not_exist"
        settings = _make_settings(
            rag_youtube_fake_mode=True,
            rag_youtube_fake_fixture_dir=str(non_existent),
        )
        with pytest.raises(FileNotFoundError, match="fake fixture"):
            create_youtube_fetcher(settings)


@pytest.fixture
def _enable_rag_logger_propagation() -> Any:
    """``rag`` 名前空間ロガーの propagate を強制 True にする.

    server._configure_and_run が rag ロガーの propagate を False に設定する場合があり、
    その状態が他テストから影響して caplog がレコードを捕捉できなくなる。
    本 fixture は rag.config に対する caplog のキャプチャを保証する。
    """
    rag_logger = logging.getLogger("rag")
    rag_config_logger = logging.getLogger("rag.config")
    original_rag_propagate = rag_logger.propagate
    original_rag_config_propagate = rag_config_logger.propagate
    rag_logger.propagate = True
    rag_config_logger.propagate = True
    try:
        yield
    finally:
        rag_logger.propagate = original_rag_propagate
        rag_config_logger.propagate = original_rag_config_propagate


class TestEmbeddingFakeModeSettings:
    """Issue #722 で追加された Embedding fake mode の Settings 統合.

    本番デフォルトを False（real）に変更したため、`_EnvLoader` および
    `RAGSettings` の Field 定義が変更後の状態であることを直接検証する。
    pytest autouse fixture が env で True を強制するため、Field レベルの
    回帰検出は本テストでのみ可能。
    """

    def test_env_loader_has_embedding_fake_mode_field(self) -> None:
        assert "rag_embedding_fake_mode" in _EnvLoader.model_fields
        assert "rag_embedding_fake_dimensions" in _EnvLoader.model_fields

    def test_env_loader_default_is_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`_EnvLoader.rag_embedding_fake_mode` のデフォルトが False（real）.

        autouse fixture `_force_embedding_fake_mode` が env で `true` を強制するため、
        Field デフォルトを直接検証するには env から該当キーを除去する必要がある。
        """
        monkeypatch.delenv("RAG_EMBEDDING_FAKE_MODE", raising=False)
        monkeypatch.delenv("RAG_EMBEDDING_FAKE_DIMENSIONS", raising=False)
        loader = _EnvLoader(
            embedding_provider="local",
            lmstudio_base_url="http://localhost:1234",
            chromadb_persist_dir="./chroma_db",
            bm25_persist_dir="./bm25_index",
            source_store_dir="./source_store",
            converted_store_dir="./converted_store",
            rag_transport="stdio",
            rag_http_host="127.0.0.1",
            rag_http_port=8081,
            rag_dns_rebinding_protection=True,
            rag_debug_log_enabled=False,
        )  # type: ignore[call-arg]
        assert loader.rag_embedding_fake_mode is False
        assert loader.rag_embedding_fake_dimensions == 768

    def test_rag_settings_has_embedding_fake_mode_field(self) -> None:
        assert "rag_embedding_fake_mode" in RAGSettings.model_fields
        assert "rag_embedding_fake_dimensions" in RAGSettings.model_fields

    def test_env_loader_no_local_fake_mode_field(self) -> None:
        """Local Fake は廃止済み（Issue #722）。Field が完全に削除されていること."""
        assert "rag_local_fake_mode" not in _EnvLoader.model_fields
        assert "rag_local_fake_fixture_dir" not in _EnvLoader.model_fields
        assert "rag_local_fake_mode" not in RAGSettings.model_fields
        assert "rag_local_fake_fixture_dir" not in RAGSettings.model_fields


class TestLogFakeModeStatus:
    def test_fake_mode_emits_warning(
        self, caplog: pytest.LogCaptureFixture,
        _enable_rag_logger_propagation: None,
    ) -> None:
        settings = _make_settings(rag_youtube_fake_mode=True)
        with caplog.at_level(logging.WARNING, logger="rag.config"):
            log_fake_mode_status(settings)
        assert any(
            "[FAKE MODE: youtube]" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_real_mode_emits_info(
        self, caplog: pytest.LogCaptureFixture,
        _enable_rag_logger_propagation: None,
    ) -> None:
        settings = _make_settings(
            rag_youtube_fake_mode=False,
            rag_bluesky_fake_mode=False,
            rag_embedding_fake_mode=False,
            rag_web_fake_mode=False,
            rag_scrapy_fake_mode=False,
            rag_zenn_fake_mode=False,
            rag_aozora_fake_mode=False,
        )
        with caplog.at_level(logging.INFO, logger="rag.config"):
            log_fake_mode_status(settings)
        records = [r for r in caplog.records if r.name == "rag.config"]
        assert any(
            "YouTube は REAL モードで起動中" in r.message
            and r.levelno == logging.INFO
            for r in records
        )
        assert not any("[FAKE MODE:" in r.message for r in records)

    def test_embedding_fake_mode_true_emits_warning(
        self, caplog: pytest.LogCaptureFixture,
        _enable_rag_logger_propagation: None,
    ) -> None:
        """Embedding fake モード明示有効時のみ WARNING ラベルを出力する."""
        settings = _make_settings(rag_embedding_fake_mode=True)
        with caplog.at_level(logging.WARNING, logger="rag.config"):
            log_fake_mode_status(settings)
        assert any(
            "[FAKE MODE: embedding]" in r.message
            and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_embedding_fake_mode_false_no_log(
        self, caplog: pytest.LogCaptureFixture,
        _enable_rag_logger_propagation: None,
    ) -> None:
        """Embedding fake モード未設定（デフォルト False）時はログを出さない.

        他 source と異なり Embedding はテスト専用フックのため、
        real モードでは INFO ログも出力しない。
        """
        settings = _make_settings(
            rag_youtube_fake_mode=False,
            rag_bluesky_fake_mode=False,
            rag_embedding_fake_mode=False,
            rag_web_fake_mode=False,
            rag_scrapy_fake_mode=False,
            rag_zenn_fake_mode=False,
            rag_aozora_fake_mode=False,
        )
        with caplog.at_level(logging.DEBUG, logger="rag.config"):
            log_fake_mode_status(settings)
        embedding_records = [
            r for r in caplog.records
            if r.name == "rag.config" and "embedding" in r.message.lower()
        ]
        assert embedding_records == []

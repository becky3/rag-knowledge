"""YoutubeFetcher ファクトリと log_fake_mode_status のテスト.

仕様: docs/specs/infrastructure/fake-mode.md
仕様: docs/specs/infrastructure/fake-adapters/youtube.md
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from rag.config import RAGSettings, log_fake_mode_status
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

"""プロセス起動シーケンス・ログファイル handler の attach.

仕様: docs/specs/rag-knowledge.md「server 構造」「アプリケーションログ」
     docs/specs/infrastructure/upload-auth.md
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from py_common_lib.logging import SessionRotatingFileHandler

from .. import config
from ..config import RAGSettings, validate_utf8_environment
from . import transport
from .logging_setup import LOG_FILE_PREFIX

logger = logging.getLogger("rag.server")


def _attach_log_file_handler(
    rag_logger: logging.Logger,
    settings: RAGSettings,
    formatter: logging.Formatter,
) -> None:
    """rag_log_dir が設定されている場合にファイルハンドラを追加する."""
    if settings.rag_log_dir is None:
        return
    if any(
        isinstance(h, SessionRotatingFileHandler) for h in rag_logger.handlers
    ):
        return
    log_dir = Path(settings.rag_log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = SessionRotatingFileHandler(
            log_dir=log_dir,
            prefix=LOG_FILE_PREFIX,
            started_at=datetime.now(),
            max_bytes=settings.rag_log_file_max_bytes,
        )
    except Exception:
        logger.exception(
            "Failed to set up log file output (RAG_LOG_DIR=%s)",
            settings.rag_log_dir,
        )
        raise
    file_handler.setFormatter(formatter)
    rag_logger.addHandler(file_handler)


def _configure_and_run() -> None:
    """トランスポート設定に基づいて MCP サーバーを起動する."""
    validate_utf8_environment()

    # rag 名前空間ロガーの設定（uvicorn/FastMCP のルートロガーを上書きしない）
    rag_logger = logging.getLogger("rag")
    log_formatter = logging.Formatter(
        "[MCP] %(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    if not rag_logger.handlers:
        handler = logging.StreamHandler(sys.__stderr__)
        handler.setFormatter(log_formatter)
        rag_logger.addHandler(handler)
    rag_logger.propagate = False

    settings = config.get_settings()

    _attach_log_file_handler(rag_logger, settings, log_formatter)

    # ログレベル設定
    log_level = logging.DEBUG if settings.rag_debug_log_enabled else logging.INFO
    rag_logger.setLevel(log_level)
    # uvicorn の dictConfig が parent chain を再構成するため、
    # rag.server ロガーにも直接レベルを設定する
    logger.setLevel(log_level)

    # Fake モード状態の起動時ログ（仕様: docs/specs/infrastructure/fake-mode.md）
    from ..config import log_fake_mode_status

    log_fake_mode_status(settings)

    # HTTP モードの事前検証（外部依存の起動前に設定の妥当性を確認する）
    transport_mode = settings.rag_transport
    if transport_mode == "http":
        bind_error = transport._validate_bind_address(
            settings.rag_http_host,
            dns_rebinding_protection=settings.rag_dns_rebinding_protection,
        )
        if bind_error is not None:
            logger.error(bind_error)
            raise SystemExit(1)

        key_error = transport._check_api_key_registered()
        if key_error is not None:
            logger.error(key_error)
            raise SystemExit(1)

    # ChromaDB サーバーの起動確保（グレースフルデグレード: 失敗しても MCP は稼働継続）
    import atexit

    from ..infrastructure.chromadb_manager import ChromaDBServerManager

    chromadb_manager = ChromaDBServerManager(
        host=settings.chromadb_server_host,
        port=settings.chromadb_server_port,
        persist_dir=settings.chromadb_persist_dir,
        auto_start=settings.chromadb_auto_start,
    )
    chromadb_manager.ensure_server_running()
    atexit.register(chromadb_manager.shutdown)

    # mcp インスタンスは package import 時に初期化済み
    from ._mcp import mcp

    if transport_mode == "http":
        mcp.settings.host = settings.rag_http_host
        mcp.settings.port = settings.rag_http_port
        if not settings.rag_dns_rebinding_protection:
            if mcp.settings.transport_security is not None:
                mcp.settings.transport_security.enable_dns_rebinding_protection = (
                    False
                )
            else:
                logger.warning(
                    "transport_security is None; "
                    "cannot disable DNS rebinding protection"
                )

    if transport_mode == "http":
        logger.info(
            "Starting MCP server: transport=%s, host=%s, port=%d",
            transport_mode, settings.rag_http_host, settings.rag_http_port,
        )
    else:
        logger.info("Starting MCP server: transport=%s", transport_mode)

    try:
        if transport_mode == "http":
            mcp.run(transport="streamable-http")
        else:
            mcp.run()
    except KeyboardInterrupt:
        logger.info("MCP server shut down")
        raise SystemExit(130)
    else:
        logger.info("MCP server shut down")

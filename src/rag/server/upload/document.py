"""POST /upload/document custom_route.

仕様: docs/specs/infrastructure/content-upload.md
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import logging
import os
import tempfile
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import cli_subprocess, http_auth
from .._mcp import mcp
from ..cli_subprocess import CLISubprocessError
from ..logging_setup import _sanitize_log_value
from ..tools.ingest_local import _get_supported_extensions
from . import _helpers
from ._helpers import (
    _upload_error,
    _upload_lock_conflict_response,
    _upload_success,
)
from ... import config
from ...errors import CliErrorCode
from ...pipeline.ingesters.local import _UPLOAD_DIR as _LOCAL_UPLOAD_DIR
from ...upload import sanitize_filename as sanitize_upload_filename

logger = logging.getLogger("rag.server")

_VALID_UPLOAD_MODES: frozenset[str] = frozenset({"fail", "replace"})


@mcp.custom_route("/upload/document", methods=["POST"])  # type: ignore[untyped-decorator]
async def upload_document(request: Request) -> Response:
    """ドキュメントファイルをアップロードしてインジェストする."""
    # 認証チェック
    auth_error = await http_auth._check_api_key(request)
    if auth_error is not None:
        return auth_error

    sanitized = ""
    try:
        settings = config.get_settings()
        max_size_bytes = settings.rag_upload_max_file_size_mb * 1024 * 1024

        # ファイル読み取り
        result = await _helpers._read_upload_file(request, max_size_bytes)
        if isinstance(result, JSONResponse):
            return result
        data, raw_filename = result

        # フォームフィールド取得
        form = await request.form()
        upload_mode = str(form.get("upload_mode", "fail"))
        if upload_mode not in _VALID_UPLOAD_MODES:
            valid = ", ".join(sorted(_VALID_UPLOAD_MODES))
            return _upload_error(400, f"無効な upload_mode: {upload_mode!r}（有効値: {valid}）")

        # ファイル名サニタイズ・拡張子チェック
        try:
            sanitized = sanitize_upload_filename(raw_filename)
        except ValueError as e:
            return _upload_error(400, str(e))

        supported = _get_supported_extensions()
        ext = Path(sanitized).suffix.lower()
        if not ext or ext not in supported:
            return _upload_error(
                400,
                f"対応していないファイル形式です: {sanitized!r}（対応: {', '.join(supported)}）",
            )

        # 一時ファイルにコンテンツを書き出し、CLI --file 経由で処理
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=Path(sanitized).suffix,
            ) as tmp:
                tmp.write(data)
                tmp_path = tmp.name

            args = ["--file", tmp_path, "--filename", sanitized]
            if upload_mode != "fail":
                args.extend(["--upload-mode", upload_mode])

            cli_result = await cli_subprocess._run_cli_subprocess("add-document", args)

            _today = _dt.date.today()
            source_id = f"local/{_LOCAL_UPLOAD_DIR}/{_today.year}/{_today.month:02d}/{_today.day:02d}/{sanitized}"
            logger.info("Document uploaded: %s", _sanitize_log_value(sanitized))
            return _upload_success(
                f"ドキュメントを取り込みました: {sanitized}",
                source_id=source_id,
                cli_result=cli_result,
            )
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

    except CLISubprocessError as e:
        if e.lock_conflict:
            return _upload_lock_conflict_response(e)
        if e.code == CliErrorCode.FILE_EXISTS.value:
            return _upload_error(409, str(e))
        logger.error("Upload document CLI error for %s: %s", sanitized, e)
        return _upload_error(500, "インジェスト処理中にエラーが発生しました")
    except Exception:
        logger.exception("Upload document failed: %s", sanitized)
        return _upload_error(500, "インジェスト処理中にエラーが発生しました")

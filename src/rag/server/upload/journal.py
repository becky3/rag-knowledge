"""POST /upload/journal custom_route.

仕様: docs/specs/infrastructure/content-upload.md
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import cli_subprocess, http_auth
from .._mcp import mcp
from ..cli_subprocess import CLISubprocessError
from ..logging_setup import _sanitize_log_value
from . import _helpers
from ._helpers import (
    _decode_form_value,
    _upload_error,
    _upload_lock_conflict_response,
    _upload_success,
)
from ... import config
from ...errors import CliErrorCode
from ...upload import sanitize_filename as sanitize_upload_filename

logger = logging.getLogger("rag.server")


@mcp.custom_route("/upload/journal", methods=["POST"])  # type: ignore[untyped-decorator]
async def upload_journal(request: Request) -> Response:
    """ジャーナル Markdown ファイルをアップロードしてインジェストする."""
    # 認証チェック
    auth_error = await http_auth._check_api_key(request)
    if auth_error is not None:
        return auth_error

    title = ""
    repository = ""
    try:
        settings = config.get_settings()
        max_size_bytes = settings.rag_upload_max_file_size_mb * 1024 * 1024

        # ファイル読み取り
        result = await _helpers._read_upload_file(request, max_size_bytes)
        if isinstance(result, JSONResponse):
            return result
        data, raw_filename = result

        # ファイル名サニタイズ・拡張子チェック
        try:
            sanitized = sanitize_upload_filename(raw_filename)
        except ValueError as e:
            return _upload_error(400, str(e))

        if not sanitized.lower().endswith(".md"):
            return _upload_error(
                400,
                f"ジャーナルは .md ファイルのみ対応しています: {sanitized!r}",
            )

        # フォームフィールド取得
        form = await request.form()
        title = _decode_form_value(str(form.get("title", ""))).strip()
        repository = _decode_form_value(str(form.get("repository", ""))).strip()
        entry_id = form.get("entry_id")
        entry_id_str = _decode_form_value(str(entry_id)).strip() if entry_id else None

        if not title:
            return _upload_error(400, "title が未指定です")
        if not repository:
            return _upload_error(400, "repository が未指定です")

        # UTF-8 デコードチェック（ジャーナルは Markdown テキストのため）
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return _upload_error(400, "ファイルが UTF-8 としてデコードできません")

        # 一時ファイルにコンテンツを書き出し、CLI --file 経由で処理
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=".md",
            ) as tmp:
                tmp.write(data)
                tmp_path = tmp.name

            args = ["--file", tmp_path, "--title", title, "--repository", repository]
            if entry_id_str:
                args.extend(["--entry-id", entry_id_str])

            cli_result = await cli_subprocess._run_cli_subprocess("add-journal", args)

            # CLI の result から entry_id を取得（利用可能な場合）
            resolved_entry_id = cli_result.get("entry_id") or entry_id_str or title
            source_id = f"journal/{repository}/{resolved_entry_id}.md"
            logger.info("Journal uploaded: %s/%s", _sanitize_log_value(repository), _sanitize_log_value(resolved_entry_id))
            return _upload_success(
                f"ジャーナルエントリを登録しました: {repository}/{resolved_entry_id}",
                source_id=source_id,
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
        logger.error("Upload journal CLI error for %s/%s: %s", repository, title, e)
        return _upload_error(500, "インジェスト処理中にエラーが発生しました")
    except Exception:
        logger.exception("Upload journal failed: %s/%s", repository, title)
        return _upload_error(500, "インジェスト処理中にエラーが発生しました")

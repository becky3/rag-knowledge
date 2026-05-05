"""Upload HTTP API の API キー認証 middleware.

仕様: docs/specs/infrastructure/upload-auth.md
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import Response

from ..config import UPLOAD_API_KEY_NAME, UPLOAD_API_KEY_SERVICE
from .upload._helpers import _upload_error

logger = logging.getLogger("rag.server")


async def _check_api_key(request: Request) -> Response | None:
    """Upload HTTP API リクエストの API キー認証.

    仕様: docs/specs/infrastructure/upload-auth.md

    Returns:
        None: 認証成功、Response: エラーレスポンス（認証失敗 or 内部エラー）
    """
    # 遅延 import: テストが `py_common_lib.secrets.get_secret` を patch 可能にするため、
    # call 時に毎回 source モジュールから lookup する。
    import hmac

    from py_common_lib.secrets import SecretNotFoundError, SecretStoreError, get_secret

    header_value = request.headers.get("X-API-Key")
    if header_value is None:
        logger.warning("API key header missing")
        return _upload_error(401, "Authentication required")

    try:
        stored_key = get_secret(
            UPLOAD_API_KEY_NAME, service=UPLOAD_API_KEY_SERVICE,
        )
    except (SecretNotFoundError, SecretStoreError):
        logger.exception("Failed to retrieve API key from keyring")
        return _upload_error(500, "Internal server error")

    if not hmac.compare_digest(header_value, stored_key):
        logger.warning("Invalid API key")
        return _upload_error(401, "Authentication required")

    return None

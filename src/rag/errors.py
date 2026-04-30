"""CLI エラーコード定義."""

from __future__ import annotations

from enum import Enum


class CliErrorCode(str, Enum):
    """CLI JSON エラー出力のエラー種別コード.

    SSoT: _schema/enums.yml の cli_error_code
    """

    VALIDATION_ERROR = "VALIDATION_ERROR"
    LOCK_CONFLICT = "LOCK_CONFLICT"
    CONFIG_MISSING = "CONFIG_MISSING"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    FILE_EXISTS = "FILE_EXISTS"
    INTERNAL_ERROR = "INTERNAL_ERROR"

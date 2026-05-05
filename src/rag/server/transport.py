"""HTTP モードのバインドアドレス検証・API キー登録確認.

仕様: docs/specs/infrastructure/upload-auth.md
"""

from __future__ import annotations

import ipaddress

from ..config import UPLOAD_API_KEY_NAME, UPLOAD_API_KEY_SERVICE


def _validate_bind_address(
    host: str, *, dns_rebinding_protection: bool
) -> str | None:
    """HTTP モードのバインドアドレスを検証する.

    仕様: docs/specs/infrastructure/upload-auth.md

    Returns:
        None: 検証成功、str: エラーメッセージ
    """
    if host == "0.0.0.0":
        if dns_rebinding_protection:
            return (
                "Binding to 0.0.0.0 is not allowed. "
                "Use a specific private IP address (e.g., 192.168.x.x) "
                "for LAN access, or 127.0.0.1 for local access. "
                "Or set RAG_DNS_REBINDING_PROTECTION=false to allow 0.0.0.0."
            )
        return None

    if host in ("127.0.0.1", "localhost"):
        return None

    # IP アドレスとして解析して RFC 1918 プライベートアドレスか判定
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # ホスト名は DNS 解決を行わないため、安全性を判定できない。
        # パブリックアドレスに解決される可能性があるため、HTTPS 未対応の現状では拒否する。
        return (
            f"Binding to hostname {host} may resolve to a public address and "
            "requires HTTPS. HTTPS support is not yet available (see #449). "
            "Use 127.0.0.1, localhost, or a private IP address instead."
        )

    if addr.is_private:
        return None

    # パブリックアドレス: HTTPS が必要（#449 未実装のため常に拒否）
    return (
        f"Binding to public address {host} requires HTTPS. "
        "HTTPS support is not yet available (see #449). "
        "Use 127.0.0.1 or a private IP address instead."
    )


def _check_api_key_registered() -> str | None:
    """keyring に API キーが登録されているか確認する.

    仕様: docs/specs/infrastructure/upload-auth.md

    Returns:
        None: 登録済み、str: エラーメッセージ
    """
    # 遅延 import: テストが `py_common_lib.secrets.get_secret` を patch 可能にするため、
    # call 時に毎回 source モジュールから lookup する。
    from py_common_lib.secrets import SecretNotFoundError, SecretStoreError, get_secret

    try:
        key = get_secret(UPLOAD_API_KEY_NAME, service=UPLOAD_API_KEY_SERVICE)
    except SecretNotFoundError:
        return (
            "API key is not registered in keyring. "
            "Run 'uv run python -m rag.cli generate-api-key --save' first."
        )
    except SecretStoreError as exc:
        return f"Failed to access keyring: {exc}"

    if not key or not key.strip():
        return (
            "API key in keyring is empty. "
            "Run 'uv run python -m rag.cli generate-api-key --save --force' to regenerate."
        )

    return None

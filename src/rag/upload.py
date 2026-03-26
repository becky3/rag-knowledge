"""コンテンツアップロード層 — MCP ツール経由ファイル受け取り用ユーティリティ.

仕様: docs/specs/infrastructure/content-upload.md
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path


def decode_upload_content(content: str, encoding: str) -> bytes:
    """MCP ツールから受け取ったコンテンツ文字列をバイト列に変換する.

    Args:
        content: MCP ツールが受け取ったコンテンツ文字列
        encoding: ``"text"`` または ``"base64"``

    Returns:
        デコード済みバイト列

    Raises:
        ValueError: 不正な encoding 値、不正な base64 文字列、デコード後が 0 バイトの場合
    """
    if encoding == "text":
        data = content.encode("utf-8")
    elif encoding == "base64":
        try:
            data = base64.b64decode(content, validate=True)
        except binascii.Error as e:
            raise ValueError(f"不正な base64 文字列です: {e}") from e
    else:
        raise ValueError(
            f"encoding の値が不正です: {encoding!r}（許容値: 'text', 'base64'）"
        )

    if len(data) == 0:
        raise ValueError("コンテンツが空です（デコード後 0 バイト）")

    return data


def sanitize_filename(filename: str) -> str:
    """ファイル名をサニタイズしてファイル名部分のみを返す.

    ディレクトリセパレータ（``/``、``\\``）や ``..`` を含む場合、
    ``Path(filename).name`` でファイル名部分のみを採用する。

    Args:
        filename: MCP ツールから受け取ったファイル名

    Returns:
        サニタイズ済みファイル名（ディレクトリ部分を除いたファイル名のみ）

    Raises:
        ValueError: filename が空文字列またはサニタイズ後のファイル名が空・``..`` の場合
    """
    if not filename:
        raise ValueError("filename が空文字列です")

    # バックスラッシュを統一して OS 非依存にする
    name = Path(filename.replace("\\", "/")).name
    if not name or name == "..":
        raise ValueError(
            f"filename からファイル名部分を取得できません: {filename!r}"
        )

    return name

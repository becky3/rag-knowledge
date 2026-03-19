"""URL ↔ source_store パス変換.

仕様: docs/specs/source-store.md

Web URL を source_store のファイルパスに変換する。
Windows のファイルパス禁止文字は全角文字で代替する。
"""

from __future__ import annotations

from urllib.parse import urlparse

# Windows ファイルパス禁止文字 → 全角代替（半角→全角）
_HALF_TO_FULL: dict[str, str] = {
    ":": "\uff1a",  # ：
    "?": "\uff1f",  # ？
    "*": "\uff0a",  # ＊
    "<": "\uff1c",  # ＜
    ">": "\uff1e",  # ＞
    "|": "\uff5c",  # ｜
    '"': "\uff02",  # ＂
}

# 逆変換テーブル（全角→半角）
_FULL_TO_HALF: dict[str, str] = {v: k for k, v in _HALF_TO_FULL.items()}


def _escape_path(raw: str) -> str:
    """パス文字列内の Windows 禁止文字を全角に置換する."""
    result = raw
    for half, full in _HALF_TO_FULL.items():
        result = result.replace(half, full)
    return result


def _unescape_path(escaped: str) -> str:
    """パス文字列内の全角文字を半角に戻す."""
    result = escaped
    for full, half in _FULL_TO_HALF.items():
        result = result.replace(full, half)
    return result


def url_to_path(url: str) -> str:
    """URL を source_store 内の相対パスに変換する.

    Args:
        url: 変換元の URL

    Returns:
        source_store 内の相対パス（例: ``web/https/example.com/docs/guide``）

    Raises:
        ValueError: サポートされていない URL スキームの場合
    """
    parsed = urlparse(url)

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        msg = f"サポートされていない URL スキーム: {scheme}"
        raise ValueError(msg)

    # ホスト名 + ポート（netloc でケースを保持。urlparse.hostname は小文字化するため使用しない）
    netloc = parsed.netloc
    # netloc からユーザー情報を除去（user:pass@host の場合）
    if "@" in netloc:
        netloc = netloc.split("@", 1)[1]
    host = _escape_path(netloc)

    # パス部分（先頭の / を除去）
    path = parsed.path.lstrip("/")

    # クエリパラメータ（? を全角に置換して結合）
    query = parsed.query
    if query:
        path = f"{path}{_HALF_TO_FULL['?']}{query}"

    # フラグメントは除去（仕様: フラグメント違いは同一ファイル）

    # Windows 禁止文字をエスケープ（host は既にエスケープ済み）
    path = _escape_path(path)

    # web/{scheme}/{host}/{path}
    parts = [p for p in [host, path] if p]
    return "web/" + scheme + "/" + "/".join(parts)


def path_to_url(rel_path: str) -> str:
    """source_store 内の相対パスを URL に逆変換する.

    Args:
        rel_path: source_store 内の相対パス（例: ``web/https/example.com/docs/guide``）

    Returns:
        復元した URL 文字列

    Raises:
        ValueError: web/ プレフィックスでない、またはスキームが不正な場合
    """
    if not rel_path.startswith("web/"):
        msg = f"web/ プレフィックスではありません: {rel_path}"
        raise ValueError(msg)

    # web/ を除去
    remainder = rel_path[4:]

    # スキーム部分を抽出
    if remainder.startswith("https/"):
        scheme = "https"
        rest = remainder[6:]
    elif remainder.startswith("http/"):
        scheme = "http"
        rest = remainder[5:]
    else:
        msg = f"不正なスキーム: {remainder}"
        raise ValueError(msg)

    # 全角文字を半角に戻す
    rest = _unescape_path(rest)

    return f"{scheme}://{rest}"

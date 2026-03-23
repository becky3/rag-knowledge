"""フィルタ文字列パーサー

rag_search の filters パラメータ（key=value 形式）をパースする共通モジュール。
MCP サーバー（server.py）と CLI（cli.py）の両方から使用される。

仕様: docs/specs/search-response.md
"""

from __future__ import annotations


def parse_filters(filters_str: str) -> dict[str, str]:
    """key=value 形式のフィルタ文字列をパースする.

    Args:
        filters_str: フィルタ文字列（例: "repository=rag-knowledge,tag=dev"）

    Returns:
        キー・値の辞書（値は常に文字列）

    Raises:
        ValueError: パースに失敗した場合
    """
    result: dict[str, str] = {}
    for pair in filters_str.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(
                f"不正なフィルタ形式: {pair!r}（key=value 形式で指定してください。"
                f"例: repository=rag-knowledge）"
            )
        key, _, value = pair.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"フィルタのキーが空です: {pair!r}")
        result[key] = value
    return result

"""テキスト正規化.

仕様: docs/specs/converter.md「テキスト正規化」

変換処理（HTML → Markdown、PDF テキスト抽出、JSON → テキスト）の後に
共通の正規化処理を適用する。パススルーファイルには適用しない。
"""

from __future__ import annotations

import re


def normalize_text(text: str) -> str:
    """変換後テキストの正規化を行う.

    - 各行の末尾にある空白文字を除去
    - 連続する空行を最大1行に圧縮

    Args:
        text: 変換後テキスト

    Returns:
        正規化済みテキスト
    """
    # 各行の末尾にある空白文字を除去
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    # 連続する空行を最大1行に圧縮
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

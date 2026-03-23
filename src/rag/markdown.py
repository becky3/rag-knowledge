"""RAG用Markdownコンバーター

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

from typing import Any

from markdownify import MarkdownConverter


class RagMarkdownConverter(MarkdownConverter):  # type: ignore[misc]
    """RAG用カスタムMarkdownコンバーター.

    リンクURLと画像URLを除去し、テキスト情報のみを保持する。
    RAGではリンク先URLはチャンクサイズの無駄遣いとなり、
    出典情報は source_url メタデータで管理するため不要。
    """

    def convert_a(self, el: Any, text: str, convert_as_inline: bool) -> str:
        """リンクはテキストのみ保持（URLは出典管理で別途管理）."""
        return text or ""

    def convert_img(self, el: Any, text: str, convert_as_inline: bool) -> str:
        """画像タグはalt属性のみ保持（RAGでは画像不要）."""
        alt: str = el.attrs.get("alt", None) or ""
        return alt

    def convert_rt(self, el: Any, text: str, convert_as_inline: bool) -> str:
        """ふりがな（rt）を半角括弧付きで保持する."""
        return f"({text})" if text.strip() else ""

    def convert_rp(self, el: Any, text: str, convert_as_inline: bool) -> str:
        """ルビ括弧（rp）を除去する（rt ハンドラで半角括弧を付与するため）."""
        return ""

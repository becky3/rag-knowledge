"""管理系 dto 群.

仕様: docs/specs/search-response.md
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DocumentResult:
    """ドキュメント全文取得の結果.

    Attributes:
        source_id: ソース識別子
        title: コンテンツのタイトル
        source_type: ソース種別
        format: 取得形式（"text" または "original"）
        content: ドキュメントテキスト（バイナリの場合は情報文字列）
        is_binary: バイナリファイルか否か
        error: エラーメッセージ（エラー時のみ）
    """

    source_id: str
    title: str
    source_type: str
    format: str
    content: str
    is_binary: bool = False
    error: str | None = None
    collected_at: str = ""
    extra: dict[str, object] = field(default_factory=dict)

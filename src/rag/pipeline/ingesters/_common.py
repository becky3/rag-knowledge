"""インジェスター共通ユーティリティ.

仕様: docs/specs/ingesters/common.md
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

ProgressCallback = Callable[[int, int, str], None]


@dataclass
class IngestResult:
    """インジェスターの配置結果."""

    placed: int = 0
    skipped: int = 0
    overwritten: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)

    def summary(self, *, context: str = "") -> str:
        """結果サマリーテキストを生成する."""
        parts = [f"完了: {self.placed}件配置"]
        if self.skipped > 0:
            parts.append(f"スキップ: {self.skipped}件")
        if self.errors > 0:
            parts.append(f"エラー: {self.errors}件")
        if context:
            parts.append(f"（{context}）")
        result = " / ".join(parts)
        if self.error_details:
            result += "\n" + "\n".join(
                f"  - {detail}" for detail in self.error_details
            )
        return result


def now_iso() -> str:
    """現在時刻を ISO 8601 形式で返す."""
    return datetime.now(timezone.utc).isoformat()

"""インジェスター共通ユーティリティ.

仕様: docs/specs/ingesters/common.md
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from py_common_lib.httpx import ConstrainedClient

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


async def fetch_get(client: ConstrainedClient, url: str) -> httpx.Response:
    """HTTP GET + ステータスチェックを統一的に行う共通ヘルパー.

    仕様: docs/specs/ingesters/common.md

    ConstrainedClient はサーキットブレーカーの責務と HTTP ステータスの責務を
    分離する設計であり、呼び出し側がステータスチェックを行う必要がある。
    本ヘルパーは「ConstrainedClient 経由で GET → ステータスが 2xx 以外なら
    httpx.HTTPStatusError を送出」という契約を提供し、ステータスチェック
    漏れを構造的に防ぐ。3xx も例外化する（リダイレクト追従が必要な用途は
    媒体固有ヘルパーを使うこと）。

    呼び出し側は例外を捕捉してログ出力（URL・ステータスコードを含む）の
    うえ、媒体ごとの方針（スキップ継続 / 中断 / リトライ）を選択すること。
    """
    resp = await client.get(url)
    if not resp.is_success:
        raise httpx.HTTPStatusError(
            f"HTTP {resp.status_code} error for url '{url}'",
            request=resp.request,
            response=resp,
        )
    return resp

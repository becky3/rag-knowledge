"""インジェスター共通ユーティリティ.

仕様: docs/specs/ingesters/common.md
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from py_common_lib.httpx import ConstrainedClient

ProgressCallback = Callable[[int, int, str], None]


class IngestErrorCategory(Enum):
    """インジェスト失敗の種別.

    値定義の SSoT は `_schema/enums.yml` の `ingest_error_category` カテゴリ。
    `IngestResult.error_details` / `partial_failure_details` の `category`
    フィールドにはこの Enum の `value`（snake_case 文字列）を記録する。
    """

    METADATA_FETCH = "metadata_fetch"
    MEDIA_DOWNLOAD = "media_download"
    PLACEMENT = "placement"
    DELEGATION = "delegation"


@dataclass
class IngestResult:
    """インジェスターの配置結果.

    `error_details` / `partial_failure_details` の dict 構造:

    | フィールド | 必須 | 内容 |
    |---|:-:|---|
    | `category` | 必須 | 失敗種別。`IngestErrorCategory` の `value`（`metadata_fetch` / `media_download` / `placement` / `delegation`） |
    | `target` | 必須 | 識別子（`rel_path` / `source_id` / `slug` / `book_id` 等） |
    | `status` | 任意 | HTTP ステータスコード |
    | `url` | 任意 | 失敗した URL |
    | `message` | 任意 | 追加説明（例外メッセージ等） |

    `category` は `IngestErrorCategory` の Enum 値に統一する（仕様: `docs/specs/ingesters/common.md`）。
    列挙外の値は使用しない。集計・再取り込み判定で信頼できる集合として扱えるようにするため。

    不変条件:
    - `aborted=True` のときは必ず `errors > 0`、`placed > 0`、`overwritten > 0` のいずれかが成立する。
      `aborted` 単独で結果有無を判定してはならない（CLI の早期 return 判定でも `is_empty()` を使う）。
    """

    placed: int = 0
    skipped: int = 0
    overwritten: int = 0
    errors: int = 0
    error_details: list[dict[str, Any]] = field(default_factory=list)
    partial_failures: int = 0
    partial_failure_details: list[dict[str, Any]] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str | None = None

    def is_empty(self) -> bool:
        """配置・上書き・エラーが全て 0 件かを判定する.

        CLI の早期 return 判定（パイプライン処理スキップ可否）の SSoT。
        この戻り値が True のときに限り、後段のパイプライン処理（git commit + index 更新）を
        スキップしてよい。`overwritten > 0` の場合はパイプライン処理が必要なため False を返す。
        """
        return self.placed == 0 and self.overwritten == 0 and self.errors == 0

    def summary(self, *, context: str = "") -> str:
        """結果サマリーテキストを生成する."""
        parts = [f"完了: {self.placed}件配置"]
        if self.overwritten > 0:
            parts.append(f"上書き: {self.overwritten}件")
        if self.skipped > 0:
            parts.append(f"スキップ: {self.skipped}件")
        if self.errors > 0:
            parts.append(f"エラー: {self.errors}件")
        if self.partial_failures > 0:
            parts.append(f"部分失敗: {self.partial_failures}件")
        if context:
            parts.append(f"（{context}）")
        result = " / ".join(parts)
        if self.aborted:
            result = f"【処理中断: {self.abort_reason}】\n{result}"
        if self.error_details:
            result += "\n" + "\n".join(
                f"  - {_format_detail(detail)}" for detail in self.error_details
            )
        if self.partial_failure_details:
            result += "\n" + "\n".join(
                f"  - {_format_detail(detail)}"
                for detail in self.partial_failure_details
            )
        return result


def _format_detail(detail: dict[str, Any]) -> str:
    """error_details / partial_failure_details の dict を表示用文字列に整形する.

    仕様: docs/specs/ingesters/common.md「失敗の観測性」

    フォーマットは `{target} [{category}]` 固定。`status` / `url` / `message` 等の
    詳細は dict 本体に残し、ログ・プログラム処理から参照する。summary は運用者が
    ざっと状況を把握するための表示であり、情報量を一定に保つ。
    """
    category = detail.get("category", "unknown")
    target = detail.get("target", "")
    return f"{target} [{category}]"


def extract_http_status(exc: BaseException) -> int | None:
    """例外から HTTP ステータスコードを抽出する.

    `httpx.HTTPStatusError` なら `response.status_code` を返し、
    それ以外の例外では None を返す。複数インジェスターが
    `error_details` / `partial_failure_details` に `status` フィールドを
    付与する際の共通ヘルパー。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None


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

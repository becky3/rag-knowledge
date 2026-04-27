"""インジェスター共通ユーティリティのテスト.

仕様: docs/specs/ingesters/common.md

テスト方針:
- fetch_get が 2xx レスポンスをそのまま返すこと
- fetch_get が 2xx 以外（3xx・4xx・5xx）で httpx.HTTPStatusError を送出すること
  （契約: 非 2xx は全て例外化。3xx の例外化は「リダイレクト追従は fetch_get
  ではなく媒体固有ヘルパーを使う」という設計の裏付けでもある）
- 例外に URL とステータスコードが含まれること
- IngestResult.is_empty() が CLI 早期 return 判定の SSoT として正しく動作すること
  （回帰防止: overwritten>0 のときに is_empty()==False となることを保証する。
  この保証が壊れると、CLI の早期 return が overwritten ケースでパイプライン処理を
  スキップするバグが再発する）
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from rag.pipeline.ingesters._common import IngestResult, fetch_get


class TestIngestResultIsEmpty:
    """IngestResult.is_empty() の判定ロジックを検証する.

    CLI の `run_ingest_*` 系コマンドが早期 return 判定にこのメソッドを使用するため、
    placed=0 / overwritten=0 / errors=0 の AND 条件が崩れるとパイプライン処理スキップの
    バグが再発する。
    """

    def test_all_zero_returns_true(self) -> None:
        assert IngestResult().is_empty() is True

    def test_placed_one_returns_false(self) -> None:
        assert IngestResult(placed=1).is_empty() is False

    def test_overwritten_one_returns_false(self) -> None:
        """上書き 1 件で is_empty()==False になる（CLI 早期 return スキップを防ぐ）."""
        assert IngestResult(overwritten=1).is_empty() is False

    def test_errors_one_returns_false(self) -> None:
        assert IngestResult(errors=1).is_empty() is False

    def test_skipped_only_returns_true(self) -> None:
        """skipped は is_empty() の判定対象外（CLI 早期 return が望ましいケース）."""
        assert IngestResult(skipped=5).is_empty() is True

    def test_aborted_with_errors_returns_false(self) -> None:
        """aborted=True 時の不変条件（errors>0 なら is_empty()==False）."""
        assert IngestResult(aborted=True, errors=1).is_empty() is False


def _make_response(status_code: int, url: str = "https://example.com") -> httpx.Response:
    """ステータスコード指定のテスト用 httpx.Response を生成する."""
    request = httpx.Request("GET", url)
    return httpx.Response(status_code, content=b"body", request=request)


async def test_fetch_get_returns_response_on_2xx() -> None:
    """2xx レスポンスがそのまま返されること."""
    client = AsyncMock()
    client.get = AsyncMock(return_value=_make_response(200))
    resp = await fetch_get(client, "https://example.com")
    assert resp.status_code == 200


async def test_fetch_get_raises_on_3xx() -> None:
    """3xx レスポンスでも HTTPStatusError が送出されること.

    リダイレクト追従が必要な用途は媒体固有ヘルパーを使う設計のため、
    fetch_get は 3xx を例外化する契約。
    """
    client = AsyncMock()
    client.get = AsyncMock(return_value=_make_response(302))
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await fetch_get(client, "https://example.com")
    assert exc_info.value.response.status_code == 302


async def test_fetch_get_raises_on_4xx() -> None:
    """4xx レスポンスで HTTPStatusError が送出されること."""
    client = AsyncMock()
    client.get = AsyncMock(return_value=_make_response(404))
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await fetch_get(client, "https://example.com")
    assert exc_info.value.response.status_code == 404


async def test_fetch_get_raises_on_5xx() -> None:
    """5xx レスポンスで HTTPStatusError が送出されること."""
    client = AsyncMock()
    client.get = AsyncMock(return_value=_make_response(503))
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await fetch_get(client, "https://example.com/api")
    assert exc_info.value.response.status_code == 503
    assert "example.com" in str(exc_info.value)

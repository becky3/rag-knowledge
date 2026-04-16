"""インジェスター共通ユーティリティのテスト.

仕様: docs/specs/ingesters/common.md

テスト方針:
- fetch_get が 2xx レスポンスをそのまま返すこと
- fetch_get が 4xx・5xx レスポンスで httpx.HTTPStatusError を送出すること
- 例外に URL とステータスコードが含まれること
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from rag.pipeline.ingesters._common import fetch_get


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

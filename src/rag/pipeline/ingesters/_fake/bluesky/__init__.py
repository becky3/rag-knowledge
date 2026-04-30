"""BlueSky Fake Adapter（Fake Fetcher + Fake MediaDownloader）.

仕様: docs/specs/infrastructure/fake-adapters/bluesky.md

BlueSky インジェスターの外部アクセス処理（AT Protocol API + メディア DL）を
fake で代替する。シナリオ切替で正常系・異常系の両方をカバーする。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

# Fetcher 系シナリオ
FETCHER_SCENARIOS = (
    "happy",
    "not_found_handle",
    "not_found_post",
    "circuit_breaker",
)

# MediaDownloader 系シナリオ
MEDIA_DOWNLOADER_SCENARIOS = (
    "happy",
    "download_error",
)

# 最小限の synthetic 画像 / 動画バイナリ（壊れた実データではないが構造的にバイト列として識別可能）
_SYNTHETIC_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_SYNTHETIC_TS_PAYLOAD = b"\x47synthetic-ts-fake-payload"


class FakeBlueskyFetcher:
    """JSON fixture を返す Fake BlueskyFetcher 実装.

    仕様: docs/specs/infrastructure/fake-adapters/bluesky.md
    """

    def __init__(
        self,
        fixture_dir: Path,
        *,
        scenario: str = "happy",
        feed: dict[str, Any] | None = None,
        posts: dict[str, Any] | None = None,
        did_map: dict[str, str] | None = None,
    ) -> None:
        if scenario not in FETCHER_SCENARIOS:
            raise ValueError(
                f"未知のシナリオ: {scenario!r}。利用可能: {FETCHER_SCENARIOS}"
            )
        if not fixture_dir.is_dir():
            raise FileNotFoundError(
                f"Fake fixture ディレクトリが見つかりません: {fixture_dir}"
            )
        self._fixture_dir = fixture_dir
        self._scenario = scenario
        self._custom_feed = feed
        self._custom_posts = posts
        self._custom_did_map = did_map

    def _load_json(self, name: str) -> Any:
        path = self._fixture_dir / name
        if not path.exists():
            raise FileNotFoundError(
                f"Fake fixture が見つかりません: {path}"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _circuit_breaker_error() -> RuntimeError:
        return RuntimeError("circuit breaker tripped (fake bluesky)")

    @staticmethod
    def _http_error(status_code: int, url: str) -> httpx.HTTPStatusError:
        request = httpx.Request("GET", url)
        response = httpx.Response(status_code, request=request)
        return httpx.HTTPStatusError(
            f"HTTP {status_code} error for url '{url}'",
            request=request,
            response=response,
        )

    async def get_author_feed(
        self,
        actor: str,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if self._scenario == "circuit_breaker":
            raise self._circuit_breaker_error()
        if self._custom_feed is not None:
            return dict(self._custom_feed)
        data: dict[str, Any] = self._load_json("feed_happy.json")
        return data

    async def get_posts(
        self,
        at_uris: list[str],
    ) -> dict[str, Any]:
        if self._scenario == "circuit_breaker":
            raise self._circuit_breaker_error()
        if self._scenario == "not_found_post":
            return {"posts": []}
        if self._custom_posts is not None:
            return dict(self._custom_posts)
        data: dict[str, Any] = self._load_json("posts_happy.json")
        return data

    async def resolve_handle(
        self,
        handle: str,
    ) -> dict[str, Any]:
        if self._scenario == "circuit_breaker":
            raise self._circuit_breaker_error()
        if self._scenario == "not_found_handle":
            raise self._http_error(
                404,
                f"https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle?handle={handle}",
            )
        if self._custom_did_map is not None and handle in self._custom_did_map:
            return {"did": self._custom_did_map[handle]}
        data: dict[str, Any] = self._load_json("resolve_handle_happy.json")
        return data


class FakeBlueskyMediaDownloader:
    """固定バイトを返す Fake MediaDownloader 実装.

    仕様: docs/specs/infrastructure/fake-adapters/bluesky.md
    """

    def __init__(
        self,
        *,
        scenario: str = "happy",
        image_data: bytes | None = None,
        image_content_type: str = "image/png",
        video_data: bytes | None = None,
    ) -> None:
        if scenario not in MEDIA_DOWNLOADER_SCENARIOS:
            raise ValueError(
                f"未知のシナリオ: {scenario!r}。利用可能: {MEDIA_DOWNLOADER_SCENARIOS}"
            )
        self._scenario = scenario
        self._image_data = (
            image_data if image_data is not None else _SYNTHETIC_PNG_HEADER
        )
        self._image_content_type = image_content_type
        self._video_data = (
            video_data if video_data is not None else _SYNTHETIC_TS_PAYLOAD
        )

    @staticmethod
    def _download_error() -> RuntimeError:
        return RuntimeError("media download error (fake bluesky)")

    async def download_image(
        self,
        url: str,
    ) -> tuple[bytes, str]:
        if self._scenario == "download_error":
            raise self._download_error()
        return self._image_data, self._image_content_type

    async def download_hls_video(
        self,
        playlist_url: str,
    ) -> bytes:
        if self._scenario == "download_error":
            raise self._download_error()
        return self._video_data

"""YouTube Fake Fetcher.

仕様: docs/specs/infrastructure/fake-adapters/youtube.md

YouTube インジェスターの外部アクセス処理 (yt-dlp / youtube-transcript-api /
faster-whisper) を fake で代替する。シナリオ切替で正常系・異常系の両方をカバー。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# シナリオ名の集合（仕様書のテーブルと同期）
SCENARIOS = (
    "happy",
    "no_subtitle",
    "transcripts_disabled",
    "ip_blocked",
    "metadata_error",
    "duration_exceeded",
    "missing_channel_id",
    "empty_playlist",
    "playlist_expand_error",
)


class FakeYoutubeFetcher:
    """JSON fixture を返す Fake YoutubeFetcher 実装.

    仕様: docs/specs/infrastructure/fake-adapters/youtube.md
    """

    def __init__(
        self,
        fixture_dir: Path,
        *,
        scenario: str = "happy",
        metadata: dict[str, Any] | None = None,
        snippets: list[dict[str, Any]] | None = None,
        language: str | None = None,
        playlist_entries: list[dict[str, Any]] | None = None,
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(
                f"未知のシナリオ: {scenario!r}。利用可能: {SCENARIOS}"
            )
        if not fixture_dir.is_dir():
            raise FileNotFoundError(
                f"Fake fixture ディレクトリが見つかりません: {fixture_dir}"
            )
        self._fixture_dir = fixture_dir
        self._scenario = scenario
        self._custom_metadata = metadata
        self._custom_snippets = snippets
        self._custom_language = language
        self._custom_playlist_entries = playlist_entries

    def _load_json(self, name: str) -> Any:
        path = self._fixture_dir / name
        if not path.exists():
            raise FileNotFoundError(
                f"Fake fixture が見つかりません: {path}"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    async def fetch_metadata(
        self, video_id: str, request_timeout: int
    ) -> dict[str, Any]:
        if self._scenario == "metadata_error":
            raise RuntimeError(f"Fake metadata error for {video_id}")
        if self._custom_metadata is not None:
            return dict(self._custom_metadata)
        if self._scenario == "duration_exceeded":
            data: dict[str, Any] = self._load_json("metadata_duration_exceeded.json")
            return {**data, "id": video_id}
        if self._scenario == "missing_channel_id":
            data = self._load_json("metadata_missing_channel_id.json")
            return {**data, "id": video_id}
        data = self._load_json("metadata_happy.json")
        return {**data, "id": video_id}

    async def fetch_subtitle(
        self, video_id: str, languages: list[str]
    ) -> tuple[list[dict[str, Any]], str]:
        if self._scenario == "transcripts_disabled":
            from youtube_transcript_api import TranscriptsDisabled  # safety:allowed

            raise TranscriptsDisabled(video_id)
        if self._scenario == "no_subtitle":
            from youtube_transcript_api import NoTranscriptFound  # safety:allowed

            raise NoTranscriptFound(video_id, languages, None)
        if self._scenario == "ip_blocked":
            from youtube_transcript_api import RequestBlocked  # safety:allowed

            raise RequestBlocked(video_id)
        snippets = (
            list(self._custom_snippets)
            if self._custom_snippets is not None
            else self._load_json("transcript_ja.json")
        )
        language = (
            self._custom_language
            if self._custom_language is not None
            else (languages[0] if languages else "ja")
        )
        return snippets, language

    async def transcribe_audio(
        self,
        video_id: str,
        languages: list[str],
        whisper_model: str,
        whisper_device: str,
        request_timeout: int,
    ) -> tuple[list[dict[str, Any]], str]:
        snippets = (
            list(self._custom_snippets)
            if self._custom_snippets is not None
            else self._load_json("transcript_whisper.json")
        )
        language = (
            self._custom_language
            if self._custom_language is not None
            else (languages[0] if languages else "ja")
        )
        return snippets, language

    async def expand_playlist(
        self, playlist_url: str, max_videos: int, request_timeout: int
    ) -> list[dict[str, Any]]:
        if self._scenario == "playlist_expand_error":
            raise RuntimeError(f"Fake playlist expand error for {playlist_url}")
        if self._scenario == "empty_playlist":
            return []
        if self._custom_playlist_entries is not None:
            return list(self._custom_playlist_entries)[:max_videos]
        entries: list[dict[str, Any]] = self._load_json("playlist_happy.json")
        return entries[:max_videos]

    def unload_whisper(self) -> None:
        """Fake は Whisper モデルを保持しないため no-op."""
        return

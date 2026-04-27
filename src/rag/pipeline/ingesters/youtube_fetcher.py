"""YouTube Fetcher Protocol と Real 実装、DI ファクトリ.

仕様: docs/specs/infrastructure/fake-mode.md
仕様: docs/specs/infrastructure/fake-adapters/youtube.md

YouTube インジェスターの外部アクセス処理を抽象化した Protocol を定義し、
Real 実装（yt-dlp / youtube-transcript-api / faster-whisper）と
.env 経由の DI ファクトリを提供する。

Real 実装の処理ロジックは元々 YoutubeIngester の private メソッド
(_fetch_metadata / _fetch_subtitle / _transcribe_with_whisper / _expand_playlist)
にあったものを単純に再配置している。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from rag.pipeline.ingesters.youtube import MAX_AUDIO_FILE_SIZE_MB

if TYPE_CHECKING:
    from rag.config import RAGSettings

logger = logging.getLogger(__name__)


class YoutubeFetcher(Protocol):
    """YouTube 外部アクセス処理の抽象 Port.

    Real / Fake で同じシグネチャを実装する。戻り値の dict 構造は yt-dlp の
    info_dict 形式に準拠する（外部ライブラリとの境界）。
    """

    async def fetch_metadata(
        self, video_id: str, request_timeout: int
    ) -> dict[str, Any]:
        """動画メタデータを取得する."""
        ...

    async def fetch_subtitle(
        self, video_id: str, languages: list[str]
    ) -> tuple[list[dict[str, Any]], str]:
        """字幕を取得する.

        Returns:
            (snippets, language)

        Raises:
            youtube_transcript_api.TranscriptsDisabled: 字幕が無効化されている
            youtube_transcript_api.NoTranscriptFound: 指定言語の字幕が無い
            youtube_transcript_api.RequestBlocked: IP ブロック等で取得失敗
        """
        ...

    async def transcribe_audio(
        self,
        video_id: str,
        languages: list[str],
        whisper_model: str,
        whisper_device: str,
        request_timeout: int,
    ) -> tuple[list[dict[str, Any]], str]:
        """音声 DL + Whisper 文字起こし.

        Returns:
            (snippets, language)
        """
        ...

    async def expand_playlist(
        self, playlist_url: str, max_videos: int, request_timeout: int
    ) -> list[dict[str, Any]]:
        """プレイリストを展開する."""
        ...


class RealYoutubeFetcher:
    """yt-dlp / youtube-transcript-api / faster-whisper を使う実 Fetcher 実装.

    既存の YoutubeIngester._fetch_* メソッドの処理ロジックをそのまま再配置している。
    """

    def __init__(self) -> None:
        # Whisper モデルは初回呼び出し時に遅延初期化してキャッシュする。
        # threading.Lock で worker スレッド内の transcribe 全体を直列化（faster-whisper はスレッドセーフでないため）
        self._whisper_model_instance: Any = None
        self._whisper_lock = threading.Lock()

    async def fetch_metadata(
        self, video_id: str, request_timeout: int
    ) -> dict[str, Any]:
        import yt_dlp  # safety:allowed

        loop = asyncio.get_running_loop()

        def _extract() -> dict[str, Any]:
            ydl_opts: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": request_timeout,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info: dict[str, Any] = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}",
                    download=False,
                )
                return info

        return await loop.run_in_executor(None, _extract)

    async def fetch_subtitle(
        self, video_id: str, languages: list[str]
    ) -> tuple[list[dict[str, Any]], str]:
        from youtube_transcript_api import YouTubeTranscriptApi  # safety:allowed

        loop = asyncio.get_running_loop()

        def _fetch() -> tuple[list[dict[str, Any]], str]:
            ytt_api = YouTubeTranscriptApi()
            transcript = ytt_api.fetch(video_id, languages=languages)
            snippets: list[dict[str, Any]] = []
            for s in transcript.snippets:
                snippets.append({
                    "start": s.start,
                    "end": s.start + s.duration,
                    "text": s.text,
                })
            language = (
                transcript.language if hasattr(transcript, "language") else languages[0]
            )
            return snippets, language

        return await loop.run_in_executor(None, _fetch)

    async def transcribe_audio(
        self,
        video_id: str,
        languages: list[str],
        whisper_model: str,
        whisper_device: str,
        request_timeout: int,
    ) -> tuple[list[dict[str, Any]], str]:
        from faster_whisper import WhisperModel  # safety:allowed

        loop = asyncio.get_running_loop()
        tmpdir = tempfile.mkdtemp(prefix="rag_youtube_")

        try:
            audio_path = await self._download_audio(video_id, tmpdir, request_timeout)

            file_size_mb = Path(audio_path).stat().st_size / (1024 * 1024)
            if file_size_mb > MAX_AUDIO_FILE_SIZE_MB:
                logger.warning(
                    "音声ファイルサイズが上限を超えています (video_id=%s): %.0fMB > %dMB。スキップします",
                    video_id,
                    file_size_mb,
                    MAX_AUDIO_FILE_SIZE_MB,
                )
                return [], languages[0]

            def _transcribe() -> tuple[list[dict[str, Any]], str]:
                # threading.Lock で worker スレッド内の初期化 + transcribe を直列化
                with self._whisper_lock:
                    if self._whisper_model_instance is None:
                        self._whisper_model_instance = WhisperModel(
                            whisper_model,
                            device=whisper_device,
                            compute_type="float16" if whisper_device == "cuda" else "int8",
                        )
                    model = self._whisper_model_instance
                    segments, info = model.transcribe(
                        audio_path,
                        language=languages[0],
                        beam_size=5,
                        vad_filter=True,
                    )
                    snippets: list[dict[str, Any]] = []
                    for seg in segments:
                        snippets.append({
                            "start": seg.start,
                            "end": seg.end,
                            "text": seg.text.strip(),
                        })
                    return snippets, info.language if hasattr(info, "language") else languages[0]

            return await loop.run_in_executor(None, _transcribe)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def expand_playlist(
        self, playlist_url: str, max_videos: int, request_timeout: int
    ) -> list[dict[str, Any]]:
        import yt_dlp  # safety:allowed

        loop = asyncio.get_running_loop()

        def _expand() -> list[dict[str, Any]]:
            ydl_opts: dict[str, Any] = {
                "extract_flat": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": request_timeout,
                "playlistend": max_videos,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info: dict[str, Any] = ydl.extract_info(playlist_url, download=False)
                entries: list[dict[str, Any]] = list(info.get("entries", []) or [])
                return entries

        return await loop.run_in_executor(None, _expand)

    async def _download_audio(
        self, video_id: str, tmpdir: str, request_timeout: int
    ) -> str:
        import yt_dlp  # safety:allowed

        loop = asyncio.get_running_loop()

        def _download() -> str:
            outtmpl = str(Path(tmpdir) / "%(id)s.%(ext)s")
            ydl_opts: dict[str, Any] = {
                "format": "ba[ext=m4a]/ba/b",
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "wav"},
                ],
                "outtmpl": outtmpl,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": request_timeout,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
            wav_files = list(Path(tmpdir).glob("*.wav"))
            if not wav_files:
                raise FileNotFoundError(f"音声ファイルが見つかりません: {tmpdir}")
            return str(wav_files[0])

        return await loop.run_in_executor(None, _download)


def create_youtube_fetcher(settings: RAGSettings) -> YoutubeFetcher:
    """Settings から Real / Fake のいずれかを選択して返すファクトリ.

    .env の RAG_YOUTUBE_FAKE_MODE が true の場合は FakeYoutubeFetcher を返し、
    実 YouTube アクセスを発生させない。
    """
    if settings.rag_youtube_fake_mode:
        from rag.pipeline.ingesters._fake.youtube import FakeYoutubeFetcher

        fixture_dir = Path(settings.rag_youtube_fake_fixture_dir)
        if not fixture_dir.is_absolute():
            project_root = Path(__file__).parent.parent.parent.parent.parent
            fixture_dir = project_root / fixture_dir
        if not fixture_dir.exists():
            raise FileNotFoundError(
                f"YouTube fake fixture ディレクトリが見つかりません: {fixture_dir}。"
                f"RAG_YOUTUBE_FAKE_FIXTURE_DIR を確認してください"
            )
        return FakeYoutubeFetcher(fixture_dir=fixture_dir)
    return RealYoutubeFetcher()

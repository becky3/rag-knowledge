"""YouTube インジェスター.

仕様: docs/specs/ingesters/youtube.md

YouTube 動画の字幕・音声文字起こしを取得し、
source_store にファイルを配置する。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from rag.pipeline.ingesters._common import IngestResult, ProgressCallback, now_iso

if TYPE_CHECKING:
    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)

# ハードリミット
MAX_VIDEOS_HARD_LIMIT = 500
MIN_REQUEST_INTERVAL = 0.1
MAX_AUDIO_FILE_SIZE_MB = 500
CIRCUIT_BREAKER_THRESHOLD = 5

# video_id の正規表現
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_video_id(url: str) -> str:
    """YouTube URL から video_id を抽出する.

    対応形式:
    - https://www.youtube.com/watch?v={id}
    - https://youtu.be/{id}

    Raises:
        ValueError: 不正な URL 形式の場合
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""

    # youtube.com/watch?v=
    if host in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        qs = parse_qs(parsed.query)
        video_ids = qs.get("v")
        if video_ids and _VIDEO_ID_RE.match(video_ids[0]):
            return video_ids[0]

    # youtu.be/{id}
    if host == "youtu.be":
        vid = parsed.path.lstrip("/").split("/")[0].split("?")[0]
        if _VIDEO_ID_RE.match(vid):
            return vid

    raise ValueError(f"不正な YouTube URL です: {url}")


def extract_playlist_id(url: str) -> str:
    """YouTube プレイリスト URL から playlist_id を抽出する.

    Raises:
        ValueError: 不正な URL 形式の場合
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host not in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        raise ValueError(f"不正な YouTube プレイリスト URL です: {url}")
    qs = parse_qs(parsed.query)
    playlist_ids = qs.get("list")
    if not playlist_ids or not playlist_ids[0]:
        raise ValueError(f"プレイリスト ID が見つかりません: {url}")
    return playlist_ids[0]


def _validate_max_videos(max_videos: object) -> int:
    """max_videos のバリデーション + クランプ."""
    if isinstance(max_videos, bool) or not isinstance(max_videos, int):
        raise TypeError(
            f"max_videos は整数で指定してください（受け取った値: {max_videos!r}）"
        )
    if max_videos <= 0:
        raise ValueError(
            f"max_videos は 1 以上で指定してください（受け取った値: {max_videos}）"
        )
    if max_videos > MAX_VIDEOS_HARD_LIMIT:
        logger.warning(
            "max_videos が上限 %d を超えています（%d）。%d にクランプします",
            MAX_VIDEOS_HARD_LIMIT,
            max_videos,
            MAX_VIDEOS_HARD_LIMIT,
        )
        return MAX_VIDEOS_HARD_LIMIT
    return max_videos


class YoutubeIngester:
    """YouTube インジェスター.

    YouTube 動画の字幕/文字起こしを取得し、
    source_store に JSON ファイルとして配置する。
    """

    def __init__(
        self,
        source_store: SourceStore,
        *,
        max_videos: int,
        request_interval: float,
        request_timeout: int,
        whisper_model: str,
        whisper_device: str,
        transcript_languages: list[str] | None,
        max_duration: int,
    ) -> None:
        self._store = source_store
        self._max_videos = _validate_max_videos(max_videos)
        self._request_interval = max(request_interval, MIN_REQUEST_INTERVAL)
        self._request_timeout = request_timeout
        self._whisper_model_name = whisper_model
        self._whisper_device = whisper_device
        self._transcript_languages = transcript_languages or ["ja", "en"]
        self._max_duration = max_duration
        self._whisper_model_instance: Any = None  # 遅延初期化キャッシュ
        self._whisper_lock = threading.Lock()  # スレッドセーフなモデルアクセス

    @property
    def request_interval(self) -> float:
        """リクエスト間隔（秒）."""
        return self._request_interval

    async def ingest_video(
        self,
        video_url: str,
        *,
        playlist_id: str | None = None,
    ) -> IngestResult:
        """単一 YouTube 動画を取得し source_store に配置する.

        Args:
            video_url: YouTube 動画 URL
            playlist_id: プレイリスト経由の場合のプレイリスト ID

        Returns:
            配置結果
        """
        result = IngestResult()

        # URL パース
        video_id = extract_video_id(video_url)

        # メタデータ取得
        try:
            metadata = await self._fetch_metadata(video_id)
        except Exception as e:
            # プログラミングエラーは伝播させる
            if isinstance(e, (TypeError, AttributeError, ImportError)):
                raise
            logger.error("メタデータ取得失敗 (video_id=%s): %s", video_id, e)
            result.errors += 1
            result.error_details.append({
                "category": "metadata_fetch",
                "target": video_id,
                "message": f"メタデータ取得失敗: {e}",
            })
            return result

        # 動画長チェック（duration 不明時はスキップ — 長時間音声DL防止）
        duration = metadata.get("duration") or 0
        if not duration:
            logger.warning(
                "動画長が不明です (video_id=%s)。スキップします",
                video_id,
            )
            result.skipped += 1
            return result
        if duration > self._max_duration:
            logger.warning(
                "動画長が上限を超えています (video_id=%s, duration=%ds, max=%ds)",
                video_id,
                duration,
                self._max_duration,
            )
            result.skipped += 1
            return result

        # channel_id チェック（チャンネル別階層のため必須）
        raw_channel_id = metadata.get("channel_id")
        if not raw_channel_id:
            logger.error("channel_id が取得できません (video_id=%s)", video_id)
            result.errors += 1
            result.error_details.append({
                "category": "metadata_fetch",
                "target": video_id,
                "message": "channel_id missing",
            })
            return result
        # パストラバーサル防止: 安全な文字のみ許可
        channel_id = re.sub(r"[^A-Za-z0-9_-]", "_", raw_channel_id)

        # 字幕取得 → Whisper フォールバック
        try:
            snippets, transcript_source, language = await self._fetch_transcript(
                video_id
            )
        except Exception as e:
            # プログラミングエラーは伝播させる
            if isinstance(e, (TypeError, AttributeError, ImportError)):
                raise
            logger.error("字幕/文字起こし失敗 (video_id=%s): %s", video_id, e)
            result.errors += 1
            result.error_details.append({
                "category": "metadata_fetch",
                "target": video_id,
                "message": f"字幕/文字起こし失敗: {e}",
            })
            return result

        # JSON データ構築
        json_data: dict[str, Any] = {
            "video_id": video_id,
            "title": metadata.get("title") or "",
            "channel_id": channel_id,
            "uploader": metadata.get("uploader") or "",
            "upload_date": metadata.get("upload_date") or "",
            "duration": duration,
            "description": metadata.get("description") or "",
            "transcript_source": transcript_source,
            "language": language,
            "snippets": snippets,
        }
        if transcript_source == "whisper":
            json_data["whisper_model"] = self._whisper_model_name

        # source_store 配置（place_file 経由で配置・.meta 生成を統一）
        rel_path = f"youtube/{channel_id}/{video_id}.json"
        json_str = json.dumps(json_data, ensure_ascii=False, indent=2)
        data_bytes = json_str.encode("utf-8")

        # 重複チェック（上書き方式。placed と overwritten は排他計上。書き込み前に判定）
        dest = self._store.root_dir / rel_path
        is_overwrite = dest.exists()

        meta_dict: dict[str, Any] = {
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "source_type": "youtube",
            "title": metadata.get("title") or "",
            "collected_at": now_iso(),
            "video_id": video_id,
            "channel_id": channel_id,
            "uploader": metadata.get("uploader") or "",
            "upload_date": metadata.get("upload_date") or "",
            "duration": metadata.get("duration") or 0,
            "transcript_source": transcript_source,
        }
        if playlist_id:
            meta_dict["playlist_id"] = playlist_id

        try:
            self._store.place_file(
                source_type="youtube",
                data=data_bytes,
                rel_path=rel_path,
                metadata=meta_dict,
            )
        except Exception as exc:
            logger.exception("動画の配置に失敗しました: %s", rel_path)
            result.errors += 1
            result.error_details.append({
                "category": "placement",
                "target": rel_path,
                "message": str(exc),
            })
            return result

        if is_overwrite:
            result.overwritten += 1
        else:
            result.placed += 1
        logger.info(
            "配置完了: %s (source=%s, snippets=%d)",
            rel_path,
            transcript_source,
            len(snippets),
        )
        return result

    async def crawl_playlist(
        self,
        playlist_url: str,
        *,
        max_videos: int | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> IngestResult:
        """プレイリスト内の動画を一括取り込みする.

        Args:
            playlist_url: YouTube プレイリスト URL
            max_videos: 取得する最大動画数（None の場合はインスタンス設定を使用）
            progress_callback: 進捗コールバック (processed, total, current)

        Returns:
            配置結果
        """
        logger.info(
            "Playlist crawl started: url=%s, max_videos=%s",
            playlist_url,
            max_videos if max_videos is not None else self._max_videos,
        )
        result = IngestResult()

        playlist_id = extract_playlist_id(playlist_url)
        effective_max = _validate_max_videos(
            max_videos if max_videos is not None else self._max_videos
        )

        # プレイリスト展開
        try:
            video_entries = await self._expand_playlist(playlist_url, effective_max)
        except Exception as e:
            if isinstance(e, (TypeError, AttributeError, ImportError)):
                raise
            logger.error("プレイリスト展開失敗: %s", e)
            result.errors += 1
            result.error_details.append({
                "category": "metadata_fetch",
                "target": playlist_id,
                "message": f"プレイリスト展開失敗: {e}",
            })
            return result

        logger.info(
            "Playlist expanded: %d videos found", len(video_entries),
        )

        if not video_entries:
            logger.info("プレイリストに動画がありません: %s", playlist_url)
            return result

        # 各動画を順次処理
        consecutive_errors = 0
        entries_to_process = video_entries[:effective_max]
        for i, entry in enumerate(entries_to_process):
            video_id = entry.get("id", "")
            if not video_id:
                result.errors += 1
                result.error_details.append({
                    "category": "metadata_fetch",
                    "target": f"entry_{i}",
                    "message": "video_id missing",
                })
                consecutive_errors += 1
                if consecutive_errors >= CIRCUIT_BREAKER_THRESHOLD:
                    logger.error(
                        "サーキットブレーカー発動: %d 回連続失敗",
                        CIRCUIT_BREAKER_THRESHOLD,
                    )
                    result.aborted = True
                    result.abort_reason = "circuit breaker"
                    break
                continue

            video_url = f"https://www.youtube.com/watch?v={video_id}"

            try:
                single_result = await self.ingest_video(
                    video_url, playlist_id=playlist_id
                )
                result.placed += single_result.placed
                result.skipped += single_result.skipped
                result.overwritten += single_result.overwritten
                result.errors += single_result.errors
                result.error_details.extend(single_result.error_details)

                if single_result.errors > 0:
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0

            except Exception as e:
                logger.error("動画処理失敗 (video_id=%s): %s", video_id, e)
                result.errors += 1
                result.error_details.append({
                    "category": "metadata_fetch",
                    "target": video_id,
                    "message": f"動画処理失敗: {e}",
                })
                consecutive_errors += 1

            if consecutive_errors >= CIRCUIT_BREAKER_THRESHOLD:
                logger.error(
                    "サーキットブレーカー発動: %d 回連続失敗",
                    CIRCUIT_BREAKER_THRESHOLD,
                )
                result.aborted = True
                result.abort_reason = "circuit breaker"
                break

            if progress_callback is not None:
                progress_callback(i + 1, len(entries_to_process), video_url)

            # リクエスト間隔待機（次の動画がある場合のみ）
            if i < len(entries_to_process) - 1:
                await asyncio.sleep(self._request_interval)

        logger.info(
            "Playlist crawl completed: placed=%d, skipped=%d, errors=%d",
            result.placed, result.skipped, result.errors,
        )
        return result

    async def _fetch_metadata(self, video_id: str) -> dict[str, Any]:
        """yt-dlp でメタデータを取得する."""
        import yt_dlp  # safety:allowed

        loop = asyncio.get_running_loop()

        def _extract() -> dict[str, Any]:
            ydl_opts: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": self._request_timeout,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info: dict[str, Any] = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}",
                    download=False,
                )
                return info

        return await loop.run_in_executor(None, _extract)

    async def _fetch_transcript(
        self, video_id: str
    ) -> tuple[list[dict[str, Any]], str, str]:
        """字幕取得 → Whisper フォールバック.

        Whisper フォールバックは字幕が存在しない場合（TranscriptsDisabled,
        NoTranscriptFound）のみ発動する。API エラー（IP ブロック等）では
        フォールバックせずエラーを伝播する。

        Returns:
            (snippets, transcript_source, language)
        """
        from youtube_transcript_api import (  # safety:allowed
            NoTranscriptFound,
            TranscriptsDisabled,
        )

        # 字幕取得を試みる
        try:
            snippets, language = await self._fetch_subtitle(video_id)
            return snippets, "subtitle", language
        except (TranscriptsDisabled, NoTranscriptFound) as e:
            logger.info(
                "字幕なし (video_id=%s): %s — Whisper フォールバック",
                video_id,
                type(e).__name__,
            )

        # Whisper フォールバック（字幕が存在しない場合のみ）
        snippets, language = await self._transcribe_with_whisper(video_id)
        return snippets, "whisper", language

    async def _fetch_subtitle(
        self, video_id: str
    ) -> tuple[list[dict[str, Any]], str]:
        """youtube-transcript-api で字幕を取得する.

        Returns:
            (snippets, language)
        """
        from youtube_transcript_api import YouTubeTranscriptApi  # safety:allowed

        loop = asyncio.get_running_loop()

        def _fetch() -> tuple[list[dict[str, Any]], str]:
            ytt_api = YouTubeTranscriptApi()
            transcript = ytt_api.fetch(
                video_id, languages=self._transcript_languages
            )
            snippets: list[dict[str, Any]] = []
            for s in transcript.snippets:
                snippets.append({
                    "start": s.start,
                    "end": s.start + s.duration,
                    "text": s.text,
                })
            language = transcript.language if hasattr(transcript, "language") else self._transcript_languages[0]
            return snippets, language

        return await loop.run_in_executor(None, _fetch)

    async def _transcribe_with_whisper(
        self, video_id: str
    ) -> tuple[list[dict[str, Any]], str]:
        """yt-dlp で音声DL → faster-whisper で文字起こし.

        Returns:
            (snippets, language)
        """
        from faster_whisper import WhisperModel  # safety:allowed

        loop = asyncio.get_running_loop()
        tmpdir = tempfile.mkdtemp(prefix="rag_youtube_")

        try:
            # 音声ダウンロード
            audio_path = await self._download_audio(video_id, tmpdir)

            # ファイルサイズチェック（超過時は警告 + 空 snippets で返す）
            file_size_mb = Path(audio_path).stat().st_size / (1024 * 1024)
            if file_size_mb > MAX_AUDIO_FILE_SIZE_MB:
                logger.warning(
                    "音声ファイルサイズが上限を超えています (video_id=%s): %.0fMB > %dMB。スキップします",
                    video_id,
                    file_size_mb,
                    MAX_AUDIO_FILE_SIZE_MB,
                )
                return [], self._transcript_languages[0]

            # faster-whisper で文字起こし（モデルは遅延初期化 + キャッシュ、Lock で排他）
            def _transcribe() -> tuple[list[dict[str, Any]], str]:
                with self._whisper_lock:
                    if self._whisper_model_instance is None:
                        self._whisper_model_instance = WhisperModel(
                            self._whisper_model_name,
                            device=self._whisper_device,
                            compute_type="float16" if self._whisper_device == "cuda" else "int8",
                        )
                    model = self._whisper_model_instance
                    segments, info = model.transcribe(
                        audio_path,
                        language=self._transcript_languages[0],
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
                    return snippets, info.language if hasattr(info, "language") else self._transcript_languages[0]

            return await loop.run_in_executor(None, _transcribe)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def _download_audio(self, video_id: str, tmpdir: str) -> str:
        """yt-dlp で音声をダウンロードする."""
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
                "socket_timeout": self._request_timeout,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
            # WAV ファイルを探す
            wav_files = list(Path(tmpdir).glob("*.wav"))
            if not wav_files:
                raise FileNotFoundError(
                    f"音声ファイルが見つかりません: {tmpdir}"
                )
            return str(wav_files[0])

        return await loop.run_in_executor(None, _download)

    async def _expand_playlist(
        self, playlist_url: str, max_videos: int
    ) -> list[dict[str, Any]]:
        """yt-dlp でプレイリストを展開する."""
        import yt_dlp  # safety:allowed

        loop = asyncio.get_running_loop()

        def _expand() -> list[dict[str, Any]]:
            ydl_opts: dict[str, Any] = {
                "extract_flat": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": self._request_timeout,
                "playlistend": max_videos,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info: dict[str, Any] = ydl.extract_info(
                    playlist_url, download=False
                )
                entries: list[dict[str, Any]] = list(info.get("entries", []) or [])
                return entries

        return await loop.run_in_executor(None, _expand)


"""YouTube インジェスター.

仕様: docs/specs/ingesters/youtube.md
仕様: docs/specs/infrastructure/fake-adapters/youtube.md

YouTube 動画の字幕・音声文字起こしを取得し、
source_store にファイルを配置する。

外部アクセス処理は YoutubeFetcher Protocol を経由する。
Real / Fake の切替は呼び出し元が create_youtube_fetcher() ファクトリで決定する。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qs, urlparse

from rag.pipeline.ingesters._common import (
    IngestErrorCategory,
    IngestResult,
    ProgressCallback,
    now_iso,
)

if TYPE_CHECKING:
    from rag.pipeline.ingesters.youtube_fetcher import YoutubeFetcher
    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)

# ハードリミット
MAX_VIDEOS_HARD_LIMIT = 500
MIN_REQUEST_INTERVAL = 0.1
JITTER_MIN_RATIO = 0.3
CIRCUIT_BREAKER_THRESHOLD = 5
# Whisper 処理時の音声ファイルサイズ上限（動画長上限と相補的にストレージ・処理コストを抑制）
MAX_AUDIO_FILE_SIZE_MB = 500

# video_id の正規表現
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# 仕様: docs/specs/ingesters/youtube.md「対応 URL 形式」が SSoT
_YOUTUBE_HOSTS = ("www.youtube.com", "youtube.com", "m.youtube.com")
_VIDEO_PATH_PREFIXES = ("/shorts/", "/live/", "/embed/", "/v/")


def _extract_video_id_candidate(url: str) -> str | None:
    """URL から video_id 候補を抽出する.

    Returns:
        - str (空文字の可能性あり): URL が動画 URL のパターンに該当する場合の video_id 候補。
          形式（11 文字）の妥当性は呼び出し側で別途検証する。
        - None: 動画 URL のパターンに該当しない場合（チャンネル URL、プレイリスト URL、別ホスト等）。
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    # host を lowercase 化している以上、一貫性のためパスのプレフィックス判定も
    # 大文字小文字を区別せずに行う。video_id 部分は大文字小文字を保持するため、
    # 抽出は parsed.path（元のパス）から行う
    path_lower = parsed.path.lower()

    if host == "youtu.be":
        if not path_lower.lstrip("/"):
            return None
        return parsed.path.lstrip("/").split("/")[0]

    if host in _YOUTUBE_HOSTS:
        if path_lower in ("/watch", "/watch/"):
            qs = parse_qs(parsed.query, keep_blank_values=True)
            video_ids = qs.get("v")
            if video_ids is not None:
                return video_ids[0]
        for prefix in _VIDEO_PATH_PREFIXES:
            if path_lower.startswith(prefix):
                return parsed.path[len(prefix) :].split("/")[0]

    return None


def is_youtube_video_url(url: str) -> bool:
    """URL が有効な YouTube 動画 URL かを判定する.

    仕様: docs/specs/ingesters/youtube.md「対応 URL 形式」
    """
    return classify_youtube_url(url) == "video"


def classify_youtube_url(url: str) -> Literal["video", "malformed", "not_youtube"]:
    """URL を YouTube 動画 URL の観点で分類する.

    仕様: docs/specs/ingesters/youtube.md「対応 URL 形式」

    Returns:
        - "video": 動画 URL のパターンに該当し、video_id 形式（11 文字）も有効
        - "malformed": 動画 URL のパターンに該当するが video_id 形式が不正
        - "not_youtube": 動画 URL のパターンに該当しない
    """
    candidate = _extract_video_id_candidate(url)
    if candidate is None:
        return "not_youtube"
    if _VIDEO_ID_RE.match(candidate):
        return "video"
    return "malformed"


def extract_video_id(url: str) -> str:
    """YouTube URL から video_id を抽出する.

    仕様: docs/specs/ingesters/youtube.md「対応 URL 形式」

    Raises:
        ValueError: 不正な URL 形式の場合
    """
    candidate = _extract_video_id_candidate(url)
    if candidate and _VIDEO_ID_RE.match(candidate):
        return candidate
    raise ValueError(f"不正な YouTube URL です: {url}")


def extract_playlist_id(url: str) -> str:
    """YouTube プレイリスト URL から playlist_id を抽出する.

    Raises:
        ValueError: 不正な URL 形式の場合
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
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
        fetcher: YoutubeFetcher,
        max_videos: int,
        request_interval: float,
        request_timeout: int,
        whisper_model: str,
        whisper_device: str,
        transcript_languages: list[str] | None,
        max_duration: int,
    ) -> None:
        self._store = source_store
        self._fetcher = fetcher
        self._max_videos = _validate_max_videos(max_videos)
        self._request_interval = max(request_interval, MIN_REQUEST_INTERVAL)
        self._request_timeout = request_timeout
        self._whisper_model_name = whisper_model
        self._whisper_device = whisper_device
        self._transcript_languages = transcript_languages or ["ja", "en"]
        self._max_duration = max_duration

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
            metadata = await self._fetcher.fetch_metadata(
                video_id, self._request_timeout,
            )
        except Exception as e:
            # プログラミングエラーは伝播させる
            if isinstance(e, (TypeError, AttributeError, ImportError)):
                raise
            logger.error("メタデータ取得失敗 (video_id=%s): %s", video_id, e)
            result.errors += 1
            result.error_details.append({
                "category": IngestErrorCategory.METADATA_FETCH.value,
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
                "category": IngestErrorCategory.METADATA_FETCH.value,
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
                "category": IngestErrorCategory.METADATA_FETCH.value,
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
                "category": IngestErrorCategory.PLACEMENT.value,
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
                "category": IngestErrorCategory.METADATA_FETCH.value,
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
                    "category": IngestErrorCategory.METADATA_FETCH.value,
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
                    "category": IngestErrorCategory.METADATA_FETCH.value,
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

            # リクエスト間隔待機（次の動画がある場合のみ、ジッター付き）
            if i < len(entries_to_process) - 1:
                jitter = max(
                    random.uniform(
                        self._request_interval * JITTER_MIN_RATIO,
                        self._request_interval,
                    ),
                    MIN_REQUEST_INTERVAL,
                )
                await asyncio.sleep(jitter)

        logger.info(
            "Playlist crawl completed: placed=%d, skipped=%d, errors=%d",
            result.placed, result.skipped, result.errors,
        )
        return result

    async def _fetch_transcript(
        self, video_id: str
    ) -> tuple[list[dict[str, Any]], str, str]:
        """字幕取得 → Whisper フォールバック.

        Whisper フォールバックは字幕が存在しない場合（TranscriptsDisabled,
        NoTranscriptFound）のみ発動する。API エラー（IP ブロック等）では
        フォールバックせずエラーを伝播する。

        外部アクセス自体は YoutubeFetcher Protocol 経由で実行する。

        Returns:
            (snippets, transcript_source, language)
        """
        from youtube_transcript_api import (  # safety:allowed
            NoTranscriptFound,
            TranscriptsDisabled,
        )

        try:
            snippets, language = await self._fetcher.fetch_subtitle(
                video_id, self._transcript_languages
            )
            return snippets, "subtitle", language
        except (TranscriptsDisabled, NoTranscriptFound) as e:
            logger.info(
                "字幕なし (video_id=%s): %s — Whisper フォールバック",
                video_id,
                type(e).__name__,
            )

        snippets, language = await self._fetcher.transcribe_audio(
            video_id,
            self._transcript_languages,
            self._whisper_model_name,
            self._whisper_device,
            self._request_timeout,
        )
        return snippets, "whisper", language

    async def _expand_playlist(
        self, playlist_url: str, max_videos: int
    ) -> list[dict[str, Any]]:
        """YoutubeFetcher 経由でプレイリストを展開する."""
        return await self._fetcher.expand_playlist(
            playlist_url, max_videos, self._request_timeout
        )


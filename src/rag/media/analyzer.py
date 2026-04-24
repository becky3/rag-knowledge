"""メディア解析モジュール — 画像・動画を Vision モデルでテキスト化する.

仕様: docs/specs/infrastructure/media-analysis.md

LM Studio の OpenAI 互換 API を使用し、画像・動画の内容をテキストに変換する。
コンバーターから呼び出され、メディアファイルの解析テキストを返す。
"""

from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import tempfile
import threading
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

# Vision API に送信するプロンプト
_IMAGE_PROMPT = (
    "この画像の内容を日本語で端的に説明してください。"
    "感想や推測は含めず、画像に写っているテキスト・物体・人物・動作・状況を客観的に記述してください。"
)


class MediaAnalyzer:
    """画像・動画を Vision モデルでテキスト化する.

    仕様: docs/specs/infrastructure/media-analysis.md
    """

    _HEALTHCHECK_TIMEOUT = 5.0
    _ffmpeg_available: bool | None = None
    _ffmpeg_lock = threading.Lock()

    def __init__(
        self,
        *,
        lmstudio_base_url: str,
        vision_model: str,
        reasoning_effort: str,
        frame_interval: int,
        max_tokens: int,
        api_timeout: float = 180.0,
    ) -> None:
        base_url = lmstudio_base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        self._base_url = base_url
        self._vision_model = vision_model
        self._reasoning_effort = reasoning_effort
        self._frame_interval = frame_interval
        self._max_tokens = max_tokens
        self._api_timeout = api_timeout
        self._available_cache: bool | None = None
        self._cache_lock = threading.Lock()

    def is_available(self) -> bool:
        """LM Studio の Vision モデルが利用可能かを返す.

        初回呼び出し時に HTTP チェックを行い結果をキャッシュする。
        threading.Lock でスレッドセーフに動作する（asyncio.to_thread 対応）。
        """
        if self._available_cache is not None:
            return self._available_cache
        with self._cache_lock:
            if self._available_cache is not None:
                return self._available_cache
            try:
                with httpx.Client(timeout=self._HEALTHCHECK_TIMEOUT) as client:  # safety:allowed
                    resp = client.get(f"{self._base_url}/models")
                    resp.raise_for_status()
                self._available_cache = True
            except (httpx.HTTPError, httpx.ConnectError, OSError):
                self._available_cache = False
            return self._available_cache

    def check_and_cache_availability(self) -> bool:
        """利用可否を確認しキャッシュに保存する.

        バッチ処理の開始時に呼び出し、以降の is_available() 呼び出しで
        繰り返しの HTTP リクエストを発行しないようにする。
        """
        return self.is_available()

    def clear_availability_cache(self) -> None:
        """利用可否キャッシュをクリアする."""
        with self._cache_lock:
            self._available_cache = None

    def analyze_image(self, image_path: Path) -> str:
        """画像を Vision モデルで解析し、テキストを返す.

        Args:
            image_path: 画像ファイルパス

        Returns:
            解析テキスト。解析できなかった場合は空文字列。
        """
        if not image_path.exists():
            logger.warning("画像ファイルが存在しません: %s", image_path)
            return ""

        if image_path.stat().st_size == 0:
            logger.warning("0 バイトの画像ファイルです: %s", image_path)
            return ""

        try:
            image_data, media_type = self._prepare_image(image_path)
        except Exception:
            logger.warning("画像の読み込みに失敗しました: %s", image_path, exc_info=True)
            return ""

        return self._call_vision_api(image_data, media_type)

    def analyze_video(self, video_path: Path) -> str:
        """動画からフレームを抽出し、各フレームを Vision モデルで解析して結合テキストを返す.

        Args:
            video_path: 動画ファイルパス

        Returns:
            タイムスタンプ付きの結合テキスト。解析できなかった場合は空文字列。
        """
        if not video_path.exists():
            logger.warning("動画ファイルが存在しません: %s", video_path)
            return ""

        if video_path.stat().st_size == 0:
            logger.warning("0 バイトの動画ファイルです: %s", video_path)
            return ""

        if not self._is_ffmpeg_available():
            logger.warning("ffmpeg が未インストールのため動画解析をスキップします")
            return ""

        tmp_dir: Path | None = None
        try:
            tmp_dir, frames = self._extract_frames(video_path)

            if not frames:
                logger.warning("フレーム抽出結果が 0 枚です: %s", video_path)
                return ""

            parts: list[str] = []
            for frame_path, timestamp_sec in frames:
                text = self.analyze_image(frame_path)
                if text:
                    ts_label = self._format_timestamp(timestamp_sec)
                    parts.append(f"[{ts_label}] {text}")

            return "\n".join(parts)
        except Exception:
            logger.warning(
                "動画のフレーム抽出に失敗しました: %s", video_path, exc_info=True
            )
            return ""
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _prepare_image(self, image_path: Path) -> tuple[str, str]:
        """画像を読み込み、必要なら変換して base64 エンコードする.

        Returns:
            (base64 エンコード済みデータ, MIME タイプ)
        """
        suffix = image_path.suffix.lower()

        if suffix == ".webp":
            with Image.open(image_path) as img:
                buf = BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=90)
            data = buf.getvalue()
            media_type = "image/jpeg"
        else:
            data = image_path.read_bytes()
            media_type = self._guess_media_type(suffix)

        return base64.b64encode(data).decode("ascii"), media_type

    def _call_vision_api(self, image_b64: str, media_type: str) -> str:
        """Vision API に画像を送信してテキストを取得する."""
        payload = {
            "model": self._vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{image_b64}",
                            },
                        },
                        {
                            "type": "text",
                            "text": _IMAGE_PROMPT,
                        },
                    ],
                }
            ],
            "max_tokens": self._max_tokens,
            "reasoning_effort": self._reasoning_effort,
        }

        try:
            with httpx.Client(timeout=self._api_timeout) as client:  # safety:allowed
                resp = client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                )
                resp.raise_for_status()
                result = resp.json()
        except (httpx.HTTPError, httpx.ConnectError, OSError, ValueError) as e:
            logger.warning("Vision API 呼び出しに失敗しました: %s", e)
            return ""

        try:
            text: str | None = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            logger.warning("Vision API レスポンスが空です")
            return ""

        if not text:
            logger.warning("Vision API レスポンスが空です")
            return ""

        return str(text).strip()

    def _extract_frames(
        self, video_path: Path
    ) -> tuple[Path, list[tuple[Path, float]]]:
        """ffmpeg で動画からフレームを抽出する.

        Returns:
            (一時ディレクトリ, [(フレーム画像パス, タイムスタンプ秒), ...])
        """
        tmp_dir = Path(tempfile.mkdtemp(prefix="rag_video_"))

        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-vf",
                f"fps=1/{self._frame_interval}",
                "-q:v",
                "2",
                str(tmp_dir / "frame_%04d.jpg"),
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)

            frame_files = sorted(tmp_dir.glob("frame_*.jpg"))
            frames: list[tuple[Path, float]] = []
            for i, frame_file in enumerate(frame_files):
                timestamp_sec = float(i * self._frame_interval)
                frames.append((frame_file, timestamp_sec))
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        return tmp_dir, frames

    @classmethod
    def _is_ffmpeg_available(cls) -> bool:
        """ffmpeg がインストールされているかチェックする."""
        if cls._ffmpeg_available is not None:
            return cls._ffmpeg_available
        with cls._ffmpeg_lock:
            if cls._ffmpeg_available is not None:
                return cls._ffmpeg_available
            cls._ffmpeg_available = shutil.which("ffmpeg") is not None
            return cls._ffmpeg_available

    @staticmethod
    def _format_timestamp(seconds: float) -> str:
        """秒数を [M:SS] 形式にフォーマットする."""
        total = int(seconds)
        minutes = total // 60
        secs = total % 60
        return f"{minutes}:{secs:02d}"

    @staticmethod
    def _guess_media_type(suffix: str) -> str:
        """ファイル拡張子から MIME タイプを推測する."""
        mapping = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
            ".webp": "image/webp",
        }
        return mapping.get(suffix, "image/jpeg")


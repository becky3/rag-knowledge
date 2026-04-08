"""メディア解析モジュールのテスト.

仕様: docs/specs/infrastructure/media-analysis.md
Issue: #558

テスト方針:
- Vision API はモック（ローカル LM Studio への依存を排除）
- ffmpeg subprocess はモック（CI 環境での実行を考慮）
- webp → JPEG 変換は実際の Pillow 処理をテスト
- エッジケース（0バイト、破損ファイル、API エラー等）を網羅
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from PIL import Image

from rag.media.analyzer import MediaAnalyzer

# httpx.Response にはモック時に request が必要
_DUMMY_REQUEST = httpx.Request("GET", "http://localhost/")


def _make_analyzer(**kwargs: object) -> MediaAnalyzer:
    """テスト用 MediaAnalyzer を生成する."""
    defaults = {
        "lmstudio_base_url": "http://localhost:1234/v1",
        "vision_model": "gemma-3-4b-it",
        "reasoning_effort": "low",
        "frame_interval": 5,
        "max_tokens": 1024,
    }
    defaults.update(kwargs)
    return MediaAnalyzer(**defaults)  # type: ignore[arg-type]


def _make_vision_response(text: str) -> dict:
    """Vision API のモックレスポンスを生成する."""
    return {
        "choices": [
            {
                "message": {
                    "content": text,
                }
            }
        ]
    }


# ============================================================
# is_available
# ============================================================


class TestIsAvailable:
    """is_available のテスト."""

    def test_returns_true_when_lmstudio_responds(self) -> None:
        analyzer = _make_analyzer()
        mock_response = httpx.Response(200, json={"data": []}, request=_DUMMY_REQUEST)
        with patch("rag.media.analyzer.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value = mock_client
            assert analyzer.is_available() is True

    def test_returns_false_when_lmstudio_is_down(self) -> None:
        analyzer = _make_analyzer()
        with patch("rag.media.analyzer.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = httpx.ConnectError("Connection refused")
            mock_client_cls.return_value = mock_client
            assert analyzer.is_available() is False


# ============================================================
# analyze_image
# ============================================================


class TestAnalyzeImage:
    """analyze_image のテスト."""

    def test_analyzes_jpeg_image(self, tmp_path: Path) -> None:
        """JPEG 画像を正常に解析できる."""
        analyzer = _make_analyzer()
        img = Image.new("RGB", (100, 100), color="red")
        img_path = tmp_path / "test.jpg"
        img.save(img_path, format="JPEG")

        mock_response = httpx.Response(
            200, json=_make_vision_response("A red square image"),
            request=_DUMMY_REQUEST,
        )
        with patch("rag.media.analyzer.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = analyzer.analyze_image(img_path)
            assert result == "A red square image"

            # API に正しい payload が送られたか確認
            call_args = mock_client.post.call_args
            payload = call_args.kwargs["json"]
            assert payload["model"] == "gemma-3-4b-it"
            assert payload["max_tokens"] == 1024
            assert payload["reasoning_effort"] == "low"

    def test_converts_webp_to_jpeg(self, tmp_path: Path) -> None:
        """webp 画像が JPEG に変換されて API に送信される."""
        analyzer = _make_analyzer()
        img = Image.new("RGB", (50, 50), color="blue")
        img_path = tmp_path / "test.webp"
        img.save(img_path, format="WEBP")

        mock_response = httpx.Response(
            200, json=_make_vision_response("A blue image"),
            request=_DUMMY_REQUEST,
        )
        with patch("rag.media.analyzer.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = analyzer.analyze_image(img_path)
            assert result == "A blue image"

            # MIME タイプが image/jpeg に変換されているか確認
            call_args = mock_client.post.call_args
            payload = call_args.kwargs["json"]
            image_url = payload["messages"][0]["content"][0]["image_url"]["url"]
            assert image_url.startswith("data:image/jpeg;base64,")

    def test_analyzes_png_image(self, tmp_path: Path) -> None:
        """PNG 画像を正常に解析できる."""
        analyzer = _make_analyzer()
        img = Image.new("RGBA", (100, 100), color="green")
        img_path = tmp_path / "test.png"
        img.save(img_path, format="PNG")

        mock_response = httpx.Response(
            200, json=_make_vision_response("A green image"),
            request=_DUMMY_REQUEST,
        )
        with patch("rag.media.analyzer.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = analyzer.analyze_image(img_path)
            assert result == "A green image"

            # MIME タイプが image/png であるか確認
            call_args = mock_client.post.call_args
            payload = call_args.kwargs["json"]
            image_url = payload["messages"][0]["content"][0]["image_url"]["url"]
            assert image_url.startswith("data:image/png;base64,")

    def test_returns_empty_for_nonexistent_file(self, tmp_path: Path) -> None:
        """存在しないファイルに対して空文字列を返す."""
        analyzer = _make_analyzer()
        result = analyzer.analyze_image(tmp_path / "nonexistent.jpg")
        assert result == ""

    def test_returns_empty_for_zero_byte_file(self, tmp_path: Path) -> None:
        """0 バイトファイルに対して空文字列を返す."""
        analyzer = _make_analyzer()
        img_path = tmp_path / "empty.jpg"
        img_path.write_bytes(b"")
        result = analyzer.analyze_image(img_path)
        assert result == ""

    def test_non_webp_corrupted_data_is_sent_to_api(self, tmp_path: Path) -> None:
        """非 webp の破損データはそのまま API に送信される."""
        analyzer = _make_analyzer()
        img_path = tmp_path / "corrupted.jpg"
        img_path.write_bytes(b"not a valid image data")
        # _prepare_image で Pillow が webp でない場合は read_bytes で通るが、
        # webp の場合に Pillow の Image.open が失敗する
        # jpg は read_bytes で読むのでエラーにならない → API がエラーを返す
        mock_response = httpx.Response(
            200, json=_make_vision_response("Some text"),
            request=_DUMMY_REQUEST,
        )
        with patch("rag.media.analyzer.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client
            # 非 webp 形式なので read_bytes は成功し、API には送信される
            result = analyzer.analyze_image(img_path)
            assert result == "Some text"

    def test_returns_empty_for_corrupted_webp(self, tmp_path: Path) -> None:
        """破損した webp ファイルに対して空文字列を返す."""
        analyzer = _make_analyzer()
        img_path = tmp_path / "corrupted.webp"
        img_path.write_bytes(b"not a valid webp data")
        result = analyzer.analyze_image(img_path)
        assert result == ""

    def test_returns_empty_on_api_error(self, tmp_path: Path) -> None:
        """API エラー時に空文字列を返す."""
        analyzer = _make_analyzer()
        img = Image.new("RGB", (10, 10), color="white")
        img_path = tmp_path / "test.jpg"
        img.save(img_path, format="JPEG")

        with patch("rag.media.analyzer.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.ConnectError("Connection refused")
            mock_client_cls.return_value = mock_client

            result = analyzer.analyze_image(img_path)
            assert result == ""

    def test_returns_empty_on_empty_api_response(self, tmp_path: Path) -> None:
        """API レスポンスが空の場合に空文字列を返す."""
        analyzer = _make_analyzer()
        img = Image.new("RGB", (10, 10), color="white")
        img_path = tmp_path / "test.jpg"
        img.save(img_path, format="JPEG")

        mock_response = httpx.Response(200, json={"choices": []}, request=_DUMMY_REQUEST)
        with patch("rag.media.analyzer.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = analyzer.analyze_image(img_path)
            assert result == ""

    def test_returns_empty_on_null_content(self, tmp_path: Path) -> None:
        """API レスポンスの content が None の場合に空文字列を返す."""
        analyzer = _make_analyzer()
        img = Image.new("RGB", (10, 10), color="white")
        img_path = tmp_path / "test.jpg"
        img.save(img_path, format="JPEG")

        mock_response = httpx.Response(
            200, json={"choices": [{"message": {"content": None}}]},
            request=_DUMMY_REQUEST,
        )
        with patch("rag.media.analyzer.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = analyzer.analyze_image(img_path)
            assert result == ""


# ============================================================
# analyze_video
# ============================================================


class TestAnalyzeVideo:
    """analyze_video のテスト."""

    def test_returns_empty_for_nonexistent_file(self, tmp_path: Path) -> None:
        """存在しない動画ファイルに対して空文字列を返す."""
        analyzer = _make_analyzer()
        result = analyzer.analyze_video(tmp_path / "nonexistent.mp4")
        assert result == ""

    def test_returns_empty_for_zero_byte_file(self, tmp_path: Path) -> None:
        """0 バイトの動画ファイルに対して空文字列を返す."""
        analyzer = _make_analyzer()
        video_path = tmp_path / "empty.mp4"
        video_path.write_bytes(b"")
        result = analyzer.analyze_video(video_path)
        assert result == ""

    def test_returns_empty_when_ffmpeg_not_available(
        self, tmp_path: Path
    ) -> None:
        """ffmpeg 未インストール時に空文字列を返す."""
        analyzer = _make_analyzer()
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        with patch.object(
            MediaAnalyzer, "_is_ffmpeg_available", return_value=False
        ):
            result = analyzer.analyze_video(video_path)
            assert result == ""

    def test_analyzes_video_with_frames(self, tmp_path: Path) -> None:
        """動画フレームを解析してタイムスタンプ付きテキストを返す."""
        analyzer = _make_analyzer(frame_interval=5)
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        # フレーム画像を作成
        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()
        for i in range(3):
            img = Image.new("RGB", (10, 10), color="red")
            img.save(frames_dir / f"frame_{i + 1:04d}.jpg", format="JPEG")

        mock_response = httpx.Response(
            200, json=_make_vision_response("Frame description"),
            request=_DUMMY_REQUEST,
        )

        with (
            patch.object(
                MediaAnalyzer, "_is_ffmpeg_available", return_value=True
            ),
            patch.object(
                MediaAnalyzer,
                "_extract_frames",
                return_value=(
                    frames_dir,
                    [
                        (frames_dir / "frame_0001.jpg", 0.0),
                        (frames_dir / "frame_0002.jpg", 5.0),
                        (frames_dir / "frame_0003.jpg", 10.0),
                    ],
                ),
            ),
            patch("rag.media.analyzer.httpx.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = analyzer.analyze_video(video_path)

            expected = (
                "[0:00] Frame description\n"
                "[0:05] Frame description\n"
                "[0:10] Frame description"
            )
            assert result == expected

    def test_returns_empty_when_extraction_fails(self, tmp_path: Path) -> None:
        """ffmpeg フレーム抽出失敗時に空文字列を返す."""
        analyzer = _make_analyzer()
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        with (
            patch.object(
                MediaAnalyzer, "_is_ffmpeg_available", return_value=True
            ),
            patch.object(
                MediaAnalyzer,
                "_extract_frames",
                side_effect=RuntimeError("ffmpeg failed"),
            ),
        ):
            result = analyzer.analyze_video(video_path)
            assert result == ""

    def test_returns_empty_when_no_frames_extracted(
        self, tmp_path: Path
    ) -> None:
        """フレーム抽出結果が 0 枚の場合に空文字列を返す."""
        analyzer = _make_analyzer()
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()

        with (
            patch.object(
                MediaAnalyzer, "_is_ffmpeg_available", return_value=True
            ),
            patch.object(
                MediaAnalyzer,
                "_extract_frames",
                return_value=(frames_dir, []),
            ),
        ):
            result = analyzer.analyze_video(video_path)
            assert result == ""

    def test_cleans_up_temp_dir_on_success(self, tmp_path: Path) -> None:
        """正常終了時に一時ディレクトリが削除される."""
        analyzer = _make_analyzer()
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()
        img = Image.new("RGB", (10, 10), color="red")
        img.save(frames_dir / "frame_0001.jpg", format="JPEG")

        mock_response = httpx.Response(
            200, json=_make_vision_response("Frame text"),
            request=_DUMMY_REQUEST,
        )

        with (
            patch.object(
                MediaAnalyzer, "_is_ffmpeg_available", return_value=True
            ),
            patch.object(
                MediaAnalyzer,
                "_extract_frames",
                return_value=(
                    frames_dir,
                    [(frames_dir / "frame_0001.jpg", 0.0)],
                ),
            ),
            patch("rag.media.analyzer.httpx.Client") as mock_client_cls,
            patch("rag.media.analyzer.shutil.rmtree") as mock_rmtree,
        ):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            analyzer.analyze_video(video_path)

            mock_rmtree.assert_called_once_with(
                frames_dir, ignore_errors=True
            )


# ============================================================
# _format_timestamp
# ============================================================


class TestFormatTimestamp:
    """_format_timestamp のテスト."""

    def test_zero_seconds(self) -> None:
        assert MediaAnalyzer._format_timestamp(0.0) == "0:00"

    def test_seconds_only(self) -> None:
        assert MediaAnalyzer._format_timestamp(5.0) == "0:05"

    def test_minutes_and_seconds(self) -> None:
        assert MediaAnalyzer._format_timestamp(65.0) == "1:05"

    def test_exact_minute(self) -> None:
        assert MediaAnalyzer._format_timestamp(120.0) == "2:00"

    def test_large_timestamp(self) -> None:
        assert MediaAnalyzer._format_timestamp(3661.0) == "61:01"


# ============================================================
# _guess_media_type
# ============================================================


class TestGuessMediaType:
    """_guess_media_type のテスト."""

    def test_jpg(self) -> None:
        assert MediaAnalyzer._guess_media_type(".jpg") == "image/jpeg"

    def test_jpeg(self) -> None:
        assert MediaAnalyzer._guess_media_type(".jpeg") == "image/jpeg"

    def test_png(self) -> None:
        assert MediaAnalyzer._guess_media_type(".png") == "image/png"

    def test_gif(self) -> None:
        assert MediaAnalyzer._guess_media_type(".gif") == "image/gif"

    def test_unknown_defaults_to_jpeg(self) -> None:
        assert MediaAnalyzer._guess_media_type(".xyz") == "image/jpeg"


# ============================================================
# webp → JPEG 変換
# ============================================================


class TestWebpConversion:
    """webp → JPEG 変換のテスト."""

    def test_webp_converted_to_jpeg_bytes(self, tmp_path: Path) -> None:
        """webp 画像が正しく JPEG バイト列に変換される."""
        analyzer = _make_analyzer()
        img = Image.new("RGB", (50, 50), color="green")
        img_path = tmp_path / "test.webp"
        img.save(img_path, format="WEBP")

        b64_data, media_type = analyzer._prepare_image(img_path)

        assert media_type == "image/jpeg"
        decoded = base64.b64decode(b64_data)
        # JPEG マジックナンバー (FFD8FF)
        assert decoded[:2] == b"\xff\xd8"

    def test_non_webp_reads_raw_bytes(self, tmp_path: Path) -> None:
        """非 webp 画像は raw バイトとして読み込まれる."""
        analyzer = _make_analyzer()
        img = Image.new("RGB", (50, 50), color="blue")
        img_path = tmp_path / "test.png"
        img.save(img_path, format="PNG")

        b64_data, media_type = analyzer._prepare_image(img_path)

        assert media_type == "image/png"
        decoded = base64.b64decode(b64_data)
        # PNG マジックナンバー
        assert decoded[:4] == b"\x89PNG"

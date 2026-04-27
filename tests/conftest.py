"""pytest 共通設定 + autouse 安全網.

仕様: docs/specs/infrastructure/fake-mode.md

YouTube 関連の外部ライブラリ（yt_dlp / youtube_transcript_api / faster_whisper）
を session スコープの autouse fixture で `_RaiseOnUse` クラスに差し替え、
テストで Fake Fetcher 注入を忘れた場合に即座に検出する。

`RAG_TESTS_ALLOW_NETWORK=1` を環境変数で設定すると安全網は解除される
（手動の本番回帰検証等の特殊用途）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest


# tests/factories.py を import 可能にする（既存テストの慣習）
sys.path.insert(0, str(Path(__file__).parent))


class _RaiseOnUse:
    """YouTube 関連外部ライブラリの呼び出しを RuntimeError でブロックするセンチネル.

    インスタンス化・属性アクセス・呼び出しのいずれでも RuntimeError を発生させる。
    例外クラス（TranscriptsDisabled 等）の import は阻害しないため、
    クラス単位（YoutubeDL / YouTubeTranscriptApi / WhisperModel）でのみ差し替える。
    """

    _hint = (
        "テスト内で実 YouTube ライブラリが呼び出されました。"
        "Fake Fetcher (FakeYoutubeFetcher) を注入してください。"
        "実アクセスを許可する場合は環境変数 RAG_TESTS_ALLOW_NETWORK=1 を設定してください。"
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError(_RaiseOnUse._hint)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(_RaiseOnUse._hint)

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(_RaiseOnUse._hint)


@pytest.fixture(autouse=True, scope="session")
def _block_real_youtube_access() -> None:
    """YouTube 関連の外部ライブラリトップレベルクラスを _RaiseOnUse に差し替える.

    対象: yt_dlp.YoutubeDL / youtube_transcript_api.YouTubeTranscriptApi /
    faster_whisper.WhisperModel
    """
    if os.environ.get("RAG_TESTS_ALLOW_NETWORK") == "1":
        return

    import yt_dlp  # safety:allowed
    import youtube_transcript_api  # safety:allowed

    yt_dlp.YoutubeDL = _RaiseOnUse  # type: ignore[misc,assignment]
    youtube_transcript_api.YouTubeTranscriptApi = _RaiseOnUse  # type: ignore[misc,assignment]

    try:
        import faster_whisper  # safety:allowed

        faster_whisper.WhisperModel = _RaiseOnUse  # type: ignore[misc,assignment]
    except ImportError:
        # CPU 環境などで faster_whisper 未インストールの場合はスキップ
        pass

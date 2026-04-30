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
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


# tests/factories.py を import 可能にする（既存テストの慣習）
sys.path.insert(0, str(Path(__file__).parent))


def pytest_configure(config: pytest.Config) -> None:
    """package layout の整合性を起動時に検証する.

    flat module（``foo.py``）と同名 package（``foo/__init__.py``）が共存すると
    Python の import 解決順序が implementation-defined になり、テストで pass
    しても本番で予期せぬ挙動を引き起こすリスクがある（過去にバグとして発生）。

    Issue #704 の bluesky.py / bluesky/ 解体作業で誤って flat module が復活した
    インシデントを構造的に防ぐため、pytest 起動時に同名衝突を検出して fail-fast
    する。本チェックは src/ 配下のみを対象とし、test 側の同名衝突は対象外。

    **fixture 配置の運用ルール**: `_fake/<source>/data/` 等の fixture 格納
    ディレクトリには `.py` ファイルを置かないこと。本実装は ``src/`` 配下の
    全 ``.py`` を rglob で走査するため、fixture ディレクトリに同名 stem の
    ``.py`` が混入すると意図しない衝突として検出される可能性がある（ただし
    衝突相手が無ければ誤発火はしない）。fixture は ``.json`` 等の data 形式
    で配置する。

    関連: Issue #709（fail-fast 化の体系的整備）で CI 段階の同種チェック予定。
    """
    src_root = Path(__file__).parent.parent / "src"
    if not src_root.is_dir():
        return

    seen: dict[str, Path] = {}
    conflicts: list[tuple[str, Path, Path]] = []

    for py_file in src_root.rglob("*.py"):
        if py_file.name == "__init__.py":
            module_path = str(py_file.parent.relative_to(src_root)).replace(
                os.sep, ".",
            )
        else:
            module_path = str(py_file.relative_to(src_root)).removesuffix(
                ".py",
            ).replace(os.sep, ".")
        if module_path in seen:
            conflicts.append((module_path, seen[module_path], py_file))
        else:
            seen[module_path] = py_file

    if conflicts:
        msg_lines = [
            "package layout conflict detected (flat module + package coexist):",
        ]
        for mod, first, second in conflicts:
            msg_lines.append(f"  - {mod!r}: {first} <-> {second}")
        msg_lines.append(
            "Either delete the flat module or rename the package to avoid "
            "implementation-defined import resolution.",
        )
        raise pytest.UsageError("\n".join(msg_lines))


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
def _force_embedding_fake_mode() -> Iterator[None]:
    """テスト中は Embedding Fake モードを環境変数で強制する.

    仕様: docs/specs/infrastructure/fake-mode.md

    factory.get_embedding_provider が pydantic Settings 経由で
    `RAG_EMBEDDING_FAKE_MODE=true` を読み込むため、ここで環境変数に
    明示設定する。subprocess 越境テスト（e2e）でも同じ環境変数を引き継ぐ。
    `RAG_TESTS_ALLOW_NETWORK=1` 設定時のみ強制を解除する。

    AsyncOpenAI クライアントクラス自体を `_RaiseOnUse` に差し替える方式は
    採用しない。Embedding 以外の用途で OpenAI クライアントを使う将来コードを
    誤爆させるリスクがあるため、`.env` + DI ファクトリ経由で Fake を選択させる
    本機構（production fake モードと同じ経路）に揃える。

    pytest.MonkeyPatch.context() で session 終了時に環境変数を確実に元に戻す。
    生 os.environ 書き換えだと session 跨ぎや test runner 並用時に副作用が残る。

    個別テストで Real Embedding を要求する場合は、環境変数を上書きするのではなく
    Settings インスタンスを直接構築して `rag_embedding_fake_mode=False` を渡す
    パターンを使う:

        settings = Settings(**{**TEST_SETTINGS_DEFAULTS, "rag_embedding_fake_mode": False})
        provider = get_embedding_provider(settings, "local")
    """
    if os.environ.get("RAG_TESTS_ALLOW_NETWORK") == "1":
        yield
        return
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RAG_EMBEDDING_FAKE_MODE", "true")
        yield


@pytest.fixture(autouse=True, scope="session")
def _force_bluesky_fake_mode() -> Iterator[None]:
    """テスト中は BlueSky Fake モードを環境変数で強制する.

    仕様: docs/specs/infrastructure/fake-mode.md
    仕様: docs/specs/infrastructure/fake-adapters/bluesky.md

    create_bluesky_fetcher / create_bluesky_media_downloader が pydantic Settings
    経由で `RAG_BLUESKY_FAKE_MODE=true` を読み込むため、ここで環境変数に明示設定
    する。subprocess 越境テスト（e2e）でも同じ環境変数を引き継ぐ。

    `RAG_TESTS_ALLOW_NETWORK=1` 設定時のみ強制を解除する（手動の本番回帰検証等の
    特殊用途）。

    httpx クライアントクラス自体を `_RaiseOnUse` に差し替える方式は採用しない。
    bluesky 以外の用途で httpx を使う多くの既存コードを誤爆させるため、`.env` +
    DI ファクトリ経由で Fake を選択させる本機構（production fake モードと同じ
    経路）に揃える。

    個別テストで Real Adapter を要求する場合は、環境変数を上書きするのではなく
    factory に Real を直接渡すか、Settings インスタンスを直接構築して
    `rag_bluesky_fake_mode=False` を渡すパターンを使う。
    """
    if os.environ.get("RAG_TESTS_ALLOW_NETWORK") == "1":
        yield
        return
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RAG_BLUESKY_FAKE_MODE", "true")
        yield


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

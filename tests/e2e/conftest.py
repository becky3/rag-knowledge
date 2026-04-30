"""L2 Mock E2E 共通 fixture.

仕様: docs/specs/workflows/qa-strategy.md

subprocess 越境環境で MCP server / CLI / ChromaDB を実プロセス起動し、
外部 API・Embedding を Fake Adapter で置換した状態でパイプライン全体の
regression を検出する。

Test Double 注入は `.env` + DI ファクトリ経由のみ（patch.object 禁止）。
fixture は subprocess 起動時に環境変数 `RAG_*_FAKE_MODE=true` を渡すことで、
子プロセスの DI ファクトリに Fake Adapter を選択させる。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import httpx
import pytest


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# subprocess 起動の readiness 待機タイムアウト（秒）
_READY_TIMEOUT = 60.0
# readiness ポーリング間隔（秒）
_POLL_INTERVAL = 0.5
# subprocess 停止の terminate → kill 待機タイムアウト（秒）
_SHUTDOWN_TIMEOUT = 10.0


def _get_free_port() -> int:
    """OS から空き TCP ポートを 1 つ取得する.

    SO_REUSEADDR を有効化して bind し、close 直後に同じポートが使われやすくする。
    バインド直後に close するため、close → 子プロセス bind までの短時間に
    別プロセスが同ポートを取る TOCTOU race は理論上存在するが、CI 並列実行で
    実問題が出ない限り受容する（pytest テスト用途では十分な安定性）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_http_ready(url: str, timeout: float = _READY_TIMEOUT) -> bool:
    """HTTP エンドポイントが応答するまで待機する.

    MCP server の `/mcp` エンドポイントは GET で 4xx を返す仕様（POST 専用）。
    本関数は MCP server 専用の liveness 確認として 4xx 応答を「サーバー応答あり =
    ready」と扱う（`status_code < 500` で判定）。一般用途の HTTP ヘルスチェック
    関数として流用する場合、2xx 限定にするなど呼び出し側で要件に合わせて再検討する。

    Args:
        url: ヘルスチェック URL
        timeout: 最大待機秒数

    Returns:
        timeout までに 5xx 未満の応答があれば True
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=2.0)
            if resp.status_code < 500:
                return True
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError):
            pass
        time.sleep(_POLL_INTERVAL)
    return False


@pytest.fixture(scope="session")
def e2e_chromadb_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """セッション共通の ChromaDB 永続化ディレクトリ.

    本番ストレージから完全に分離するため `tmp_path_factory` の session 一時ディレクトリを使う。
    pytest 終了時に自動クリーンアップされる。
    """
    return tmp_path_factory.mktemp("e2e_chromadb")


@pytest.fixture(scope="session")
def e2e_source_store_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """セッション共通の source_store ディレクトリ."""
    return tmp_path_factory.mktemp("e2e_source_store")


@pytest.fixture(scope="session")
def e2e_converted_store_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """セッション共通の converted_store ディレクトリ."""
    return tmp_path_factory.mktemp("e2e_converted_store")


@pytest.fixture(scope="session")
def e2e_bm25_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """セッション共通の BM25 インデックスディレクトリ."""
    return tmp_path_factory.mktemp("e2e_bm25")


@pytest.fixture(scope="session")
def e2e_site_ingest_temp_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """セッション共通の Scrapy 一時ディレクトリ."""
    return tmp_path_factory.mktemp("e2e_site_ingest")


@pytest.fixture(scope="session")
def e2e_chromadb_port() -> int:
    """ChromaDB 用の動的ポート."""
    return _get_free_port()


@pytest.fixture(scope="session")
def e2e_mcp_http_port() -> int:
    """MCP server HTTP モード用の動的ポート."""
    return _get_free_port()


@pytest.fixture(scope="session")
def e2e_subprocess_env(
    e2e_chromadb_dir: Path,
    e2e_source_store_dir: Path,
    e2e_converted_store_dir: Path,
    e2e_bm25_dir: Path,
    e2e_site_ingest_temp_dir: Path,
    e2e_chromadb_port: int,
    e2e_mcp_http_port: int,
) -> dict[str, str]:
    """subprocess に渡す環境変数の SSoT.

    `_EnvLoader` (src/rag/config.py) の必須 Field をすべて環境変数で渡す。
    `RAG_*_FAKE_MODE=true` を付与し、子プロセスの DI ファクトリで
    Fake Adapter (YouTube / Embedding) が選択されるようにする。

    fake モードの環境変数規約は docs/specs/infrastructure/fake-mode.md に従う。
    """
    env = os.environ.copy()
    env.update({
        # Embedding 接続
        "EMBEDDING_PROVIDER": "local",
        "LMSTUDIO_BASE_URL": "http://127.0.0.1:1234/v1",
        "RAG_EMBEDDING_CONCURRENCY": "8",
        # ストレージ
        "CHROMADB_PERSIST_DIR": str(e2e_chromadb_dir),
        "BM25_PERSIST_DIR": str(e2e_bm25_dir),
        "SOURCE_STORE_DIR": str(e2e_source_store_dir),
        "CONVERTED_STORE_DIR": str(e2e_converted_store_dir),
        # ChromaDB サーバー接続
        "CHROMADB_SERVER_HOST": "127.0.0.1",
        "CHROMADB_SERVER_PORT": str(e2e_chromadb_port),
        "CHROMADB_AUTO_START": "true",
        # MCP transport
        "RAG_TRANSPORT": "http",
        "RAG_HTTP_HOST": "127.0.0.1",
        "RAG_HTTP_PORT": str(e2e_mcp_http_port),
        "RAG_DNS_REBINDING_PROTECTION": "false",
        # デバッグ・ログ
        "RAG_DEBUG_LOG_ENABLED": "false",
        # YouTube インジェスター（Whisper は Fake Fetcher 経由のため値は使われない）
        "RAG_YOUTUBE_WHISPER_MODEL": "base",
        "RAG_YOUTUBE_WHISPER_DEVICE": "cpu",
        # Fake Adapter 一括有効化
        "RAG_YOUTUBE_FAKE_MODE": "true",
        "RAG_YOUTUBE_FAKE_FIXTURE_DIR": "src/rag/pipeline/ingesters/_fake/youtube/data",
        "RAG_BLUESKY_FAKE_MODE": "true",
        "RAG_BLUESKY_FAKE_FIXTURE_DIR": "src/rag/pipeline/ingesters/_fake/bluesky/data",
        "RAG_EMBEDDING_FAKE_MODE": "true",
        "RAG_EMBEDDING_FAKE_DIMENSIONS": "768",
        # Scrapy 一時ディレクトリ
        "SITE_INGEST_TEMP_DIR": str(e2e_site_ingest_temp_dir),
        # ChromaDB telemetry を抑止（実プロセス起動時の余計な外部通信を防ぐ）
        "ANONYMIZED_TELEMETRY": "False",
    })
    return env


def _terminate_process(proc: subprocess.Popen[str], name: str) -> None:
    """subprocess を terminate → 必要なら kill して停止する."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=_SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=_SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            pytest.fail(f"{name} subprocess failed to stop within timeout")


def _drain_stream(stream: IO[str] | None, buffer: list[str]) -> None:
    """subprocess の stdout / stderr を行単位で読み続け buffer に蓄積する.

    パイプバッファが満杯になると subprocess 側の write がブロックされ
    hang につながる。本関数は thread として常時 readline() を呼び続け、
    buffer に蓄積することでパイプを drain し続ける。
    DEVNULL にリダイレクトすると debug 不能になるため drain 方式を採用。
    """
    if stream is None:
        return
    try:
        for line in iter(stream.readline, ""):
            buffer.append(line)
    except (OSError, ValueError):
        # subprocess 終了 / stream close 時の例外は drain thread 終了として扱う
        pass


@pytest.fixture(scope="session")
def e2e_mcp_server(
    e2e_subprocess_env: dict[str, str],
    e2e_mcp_http_port: int,
) -> Iterator[str]:
    """MCP server を HTTP モードで起動する session fixture.

    内部で ChromaDB を auto_start するため、本 fixture を使うテストは
    ChromaDB の手動起動を必要としない。

    stdout / stderr は drain thread で常時読み続け、パイプバッファ満杯による
    subprocess hang を防止する。起動失敗時は両ストリームの蓄積バッファを
    fail message に含める。

    Yields:
        MCP server のベース URL（例: http://127.0.0.1:8081）
    """
    cmd = [sys.executable, "-m", "rag.server"]
    proc = subprocess.Popen(
        cmd,
        cwd=str(_PROJECT_ROOT),
        env=e2e_subprocess_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    stdout_buf: list[str] = []
    stderr_buf: list[str] = []
    stdout_thread = threading.Thread(
        target=_drain_stream, args=(proc.stdout, stdout_buf), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_drain_stream, args=(proc.stderr, stderr_buf), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    base_url = f"http://127.0.0.1:{e2e_mcp_http_port}"
    # MCP server には専用 healthz エンドポイントがないため、/mcp への接続成功で readiness 判定する。
    # /mcp は GET で 4xx を返すが、それは「サーバーが応答している」ことの証左になる。
    if not _wait_for_http_ready(f"{base_url}/mcp"):
        # 起動失敗時は drain buffer から両ストリームを取得して fail
        _terminate_process(proc, "mcp_server")
        stdout_thread.join(timeout=_SHUTDOWN_TIMEOUT)
        stderr_thread.join(timeout=_SHUTDOWN_TIMEOUT)
        stdout_out = "".join(stdout_buf) or "<empty stdout>"
        stderr_out = "".join(stderr_buf) or "<empty stderr>"
        pytest.fail(
            f"MCP server failed to become ready at {base_url} within {_READY_TIMEOUT}s\n"
            f"stdout:\n{stdout_out}\n\nstderr:\n{stderr_out}"
        )

    try:
        yield base_url
    finally:
        _terminate_process(proc, "mcp_server")
        stdout_thread.join(timeout=_SHUTDOWN_TIMEOUT)
        stderr_thread.join(timeout=_SHUTDOWN_TIMEOUT)


def run_cli(
    args: list[str],
    env: dict[str, str],
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    """e2e テストから CLI を短命起動するヘルパー.

    fixture ではなくユーティリティ関数として提供する（テストごとに引数が変わるため）。

    Args:
        args: `python -m rag.cli` の後に続ける引数列
        env: subprocess に渡す環境変数（通常 e2e_subprocess_env を使う）
        timeout: 最大実行時間（秒）

    Returns:
        CompletedProcess（returncode / stdout / stderr を含む）
    """
    cmd = [sys.executable, "-m", "rag.cli", *args]
    return subprocess.run(
        cmd,
        cwd=str(_PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )

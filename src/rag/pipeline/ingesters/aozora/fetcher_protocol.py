"""Aozora Fetcher Protocol と Real 実装、DI ファクトリ.

仕様: docs/specs/infrastructure/fake-mode.md
仕様: docs/specs/infrastructure/fake-adapters/aozora.md

青空文庫インジェスターの外部アクセス処理（カタログ ZIP 取得 + 作品 XHTML 取得）を
抽象化した Protocol を定義し、Real 実装（httpx 経由）と .env 経由の DI ファクトリを
提供する。

ZIP の解凍 / CSV パース / 著作権チェック / GitHub Raw URL 変換は本 Protocol の
スコープ外（インジェスター本体に残す）。Real / Fake いずれも raw bytes を返す。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from py_common_lib.httpx import ConstrainedClient  # safety:allowed

from rag.pipeline.ingesters._common import fetch_get

if TYPE_CHECKING:
    from rag.config import RAGSettings


class AozoraFetcher(Protocol):
    """青空文庫外部アクセス処理の抽象 Port.

    Real / Fake で同じシグネチャを実装する。戻り値はいずれも raw bytes
    （カタログ ZIP / 作品 XHTML）。境界外（インジェスター本体）が
    zipfile / csv / GitHub Raw URL 変換などの構造化処理を行う。
    """

    async def __aenter__(self) -> "AozoraFetcher":
        ...

    async def __aexit__(self, *exc_info: Any) -> None:
        ...

    async def fetch_catalog_zip(self) -> bytes:
        """カタログ ZIP（list_person_all_extended_utf8.zip）の生 bytes を取得する."""
        ...

    async def fetch_xhtml(self, github_url: str) -> bytes:
        """指定 GitHub Raw URL から作品 XHTML の生 bytes を取得する."""
        ...


class RealAozoraFetcher:
    """青空文庫への実アクセス実装.

    ``ConstrainedClient`` を内部生成し、``fetch_get`` 経由でカタログ ZIP /
    作品 XHTML を取得する。client のライフサイクルは ``async with`` で管理する。
    """

    def __init__(
        self,
        *,
        request_timeout: int,
        request_interval: float,
    ) -> None:
        self._request_timeout = request_timeout
        self._request_interval = request_interval
        self._client: ConstrainedClient | None = None

    async def __aenter__(self) -> "RealAozoraFetcher":
        self._client = ConstrainedClient(
            request_timeout=self._request_timeout,
            request_interval=self._request_interval,
        )
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc_info)
            self._client = None

    def _get_client(self) -> ConstrainedClient:
        if self._client is None:
            raise RuntimeError(
                "RealAozoraFetcher は async with で初期化してから使用してください"
            )
        return self._client

    async def fetch_catalog_zip(self) -> bytes:
        from rag.pipeline.ingesters.aozora._facade import CATALOG_ZIP_URL

        resp = await fetch_get(self._get_client(), CATALOG_ZIP_URL)
        return bytes(resp.content)

    async def fetch_xhtml(self, github_url: str) -> bytes:
        resp = await fetch_get(self._get_client(), github_url)
        return bytes(resp.content)


def create_aozora_fetcher(settings: RAGSettings) -> AozoraFetcher:
    """Settings から Real / Fake のいずれかを選択して返すファクトリ.

    .env の RAG_AOZORA_FAKE_MODE が true の場合は FakeAozoraFetcher を返し、
    実 青空文庫 / GitHub Raw アクセスを発生させない。
    """
    if settings.rag_aozora_fake_mode:
        from rag.pipeline.ingesters._fake.aozora import FakeAozoraFetcher

        fixture_dir = Path(settings.rag_aozora_fake_fixture_dir)
        if not fixture_dir.is_absolute():
            project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
            fixture_dir = project_root / fixture_dir
        if not fixture_dir.exists():
            raise FileNotFoundError(
                f"Aozora fake fixture ディレクトリが見つかりません: {fixture_dir}。"
                f"RAG_AOZORA_FAKE_FIXTURE_DIR を確認してください"
            )
        return FakeAozoraFetcher(fixture_dir=fixture_dir)
    # Aozora 用 Settings には request_timeout / request_interval が無いため、
    # 共有の rag_zenn_request_* に倣って妥当値を使用する。本 Issue では aozora 専用の
    # Settings 追加はスコープ外として扱い、デフォルト値で初期化する。
    return RealAozoraFetcher(
        request_timeout=30,
        request_interval=1.0,
    )

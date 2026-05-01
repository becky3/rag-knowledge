"""Zenn Fetcher Protocol と Real 実装、DI ファクトリ.

仕様: docs/specs/infrastructure/fake-mode.md
仕様: docs/specs/infrastructure/fake-adapters/zenn.md

Zenn インジェスターの外部アクセス処理を抽象化した Protocol を定義し、
Real 実装（Zenn API + httpx 経由）と .env 経由の DI ファクトリを提供する。

Real 実装の処理ロジックは元々 ZennIngester の private メソッド
（``_discover_slugs`` のページネーション API 呼び出し、``_fetch_and_place_single``
の詳細 API 呼び出し）にあったものを単純に再配置している。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from py_common_lib.httpx import ConstrainedClient  # safety:allowed

from rag.pipeline.ingesters._common import fetch_get

if TYPE_CHECKING:
    from rag.config import RAGSettings


# Zenn API ベース URL（_facade.py と同期: 単一定義の SSoT は _facade.py 側）
ZENN_API_BASE = "https://zenn.dev/api"

ZennKind = Literal["articles", "scraps"]


class ZennFetcher(Protocol):
    """Zenn 外部アクセス処理の抽象 Port.

    Real / Fake で同じシグネチャを実装する。戻り値の dict 構造は Zenn API の
    レスポンス形式に準拠する（外部 API との境界）。
    """

    async def list_contents(
        self,
        kind: ZennKind,
        username: str,
        page: int,
    ) -> dict[str, Any]:
        """コンテンツ一覧 API を呼び出す.

        Args:
            kind: "articles" または "scraps"
            username: Zenn ユーザー名
            page: ページ番号（1-indexed）

        Returns:
            Zenn API の応答 dict。``{kind: [...], "next_page": int | None}`` 形式
        """
        ...

    async def fetch_content_detail(
        self,
        kind: ZennKind,
        slug: str,
    ) -> dict[str, Any]:
        """コンテンツ詳細 API を呼び出す.

        Args:
            kind: "articles" または "scraps"
            slug: コンテンツ slug

        Returns:
            Zenn API の応答 dict。``{"article": {...}}`` または ``{"scrap": {...}}``
        """
        ...


class RealZennFetcher:
    """Zenn API への実アクセス実装.

    ``ConstrainedClient`` を内部生成し、``fetch_get`` 経由で API を叩く。
    factory は本クラスのインスタンスを返す前にコンテキストマネージャ
    （``async with``）で client のライフサイクルを管理する必要がある。
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

    async def __aenter__(self) -> "RealZennFetcher":
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
                "RealZennFetcher は async with で初期化してから使用してください"
            )
        return self._client

    async def list_contents(
        self,
        kind: ZennKind,
        username: str,
        page: int,
    ) -> dict[str, Any]:
        url = f"{ZENN_API_BASE}/{kind}?username={username}&order=latest&page={page}"
        resp = await fetch_get(self._get_client(), url)
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"Zenn API 応答が dict ではありません: {type(data)}")
        return data

    async def fetch_content_detail(
        self,
        kind: ZennKind,
        slug: str,
    ) -> dict[str, Any]:
        url = f"{ZENN_API_BASE}/{kind}/{slug}"
        resp = await fetch_get(self._get_client(), url)
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"Zenn API 応答が dict ではありません: {type(data)}")
        return data


def create_zenn_fetcher(settings: RAGSettings) -> ZennFetcher:
    """Settings から Real / Fake のいずれかを選択して返すファクトリ.

    .env の RAG_ZENN_FAKE_MODE が true の場合は FakeZennFetcher を返し、
    実 Zenn API アクセスを発生させない。
    """
    if settings.rag_zenn_fake_mode:
        from rag.pipeline.ingesters._fake.zenn import FakeZennFetcher

        fixture_dir = Path(settings.rag_zenn_fake_fixture_dir)
        if not fixture_dir.is_absolute():
            project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
            fixture_dir = project_root / fixture_dir
        if not fixture_dir.exists():
            raise FileNotFoundError(
                f"Zenn fake fixture ディレクトリが見つかりません: {fixture_dir}。"
                f"RAG_ZENN_FAKE_FIXTURE_DIR を確認してください"
            )
        return FakeZennFetcher(fixture_dir=fixture_dir)
    return RealZennFetcher(
        request_timeout=settings.rag_zenn_request_timeout,
        request_interval=settings.rag_zenn_request_interval,
    )

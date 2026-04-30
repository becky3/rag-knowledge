"""BlueSky Fetcher Protocol（AT Protocol API アクセスの抽象化）.

仕様: docs/specs/infrastructure/fake-mode.md
仕様: docs/specs/infrastructure/fake-adapters/bluesky.md
仕様: docs/specs/ingesters/bluesky.md

BlueSky インジェスターの AT Protocol API アクセス処理を抽象化した Protocol を
定義する。Real / Fake 両方が同じシグネチャを実装し、戻り値は AT Protocol
AppView の生レスポンス JSON（`dict[str, Any]` 越境許容、外部境界での受信形式）。

Real 実装は ConstrainedClient をコンストラクタ注入で受け取る。.env 経由の
DI ファクトリ ``create_bluesky_fetcher`` が ``RAG_BLUESKY_FAKE_MODE`` を見て
Real / Fake のいずれかを返す。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlencode

from rag.pipeline.ingesters._common import fetch_get

if TYPE_CHECKING:
    from py_common_lib.httpx import ConstrainedClient

    from rag.config import RAGSettings


class BlueskyFetcher(Protocol):
    """AT Protocol API への HTTP 通信を抽象化する Port.

    抽象化対象は AT Protocol AppView の 3 エンドポイント:

    - ``app.bsky.feed.getAuthorFeed``: 著者タイムライン取得
    - ``app.bsky.feed.getPosts``: AT URI 指定の投稿取得
    - ``com.atproto.identity.resolveHandle``: handle → DID 解決

    戻り値は AT Protocol AppView のレスポンス JSON をそのまま返す。
    レスポンス構造の詳細は ``docs/specs/ingesters/bluesky.md`` 「外部連携」
    セクションを参照。
    """

    async def get_author_feed(
        self,
        actor: str,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """``getAuthorFeed`` API を呼び出してフィードを 1 ページ取得する.

        Args:
            actor: BlueSky handle または DID
            limit: 1 ページあたりの取得件数（最大 100）
            cursor: ページネーションカーソル（最初のページでは None）

        Returns:
            ``{"feed": [...], "cursor": "..." | None}`` 形式の生レスポンス。
            ``cursor`` が存在しない場合は最終ページ。
        """
        ...

    async def get_posts(
        self,
        at_uris: list[str],
    ) -> dict[str, Any]:
        """``getPosts`` API を呼び出して AT URI 指定の投稿を取得する.

        Args:
            at_uris: AT URI のリスト（例: ``at://did:plc:xxx/app.bsky.feed.post/yyy``）。
                DID ベースの AT URI のみ対応する（handle ベースは空結果を返す）。

        Returns:
            ``{"posts": [...]}`` 形式の生レスポンス。指定 URI に対応する投稿が
            存在しない場合（削除済み等）は ``posts`` 配列が空で返る。
        """
        ...

    async def resolve_handle(
        self,
        handle: str,
    ) -> dict[str, Any]:
        """``resolveHandle`` API を呼び出して handle を DID に解決する.

        Args:
            handle: BlueSky ハンドル（例: ``user.bsky.social``）

        Returns:
            ``{"did": "did:plc:..."}`` 形式の生レスポンス
        """
        ...


class RealBlueskyFetcher:
    """ConstrainedClient を使う実 Fetcher 実装.

    AT Protocol AppView の HTTP 通信を内包する。BlueskyIngester から見た
    ``BlueskyFetcher`` Protocol の単一の Real 実装。HTTP DI を完結させる
    ため、外部から ConstrainedClient をコンストラクタ注入で受け取る。
    """

    def __init__(
        self,
        client: ConstrainedClient,
        *,
        appview_url: str,
    ) -> None:
        self._client = client
        self._appview_url = appview_url.rstrip("/")

    async def get_author_feed(
        self,
        actor: str,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"actor": actor, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        url = (
            f"{self._appview_url}/xrpc/app.bsky.feed.getAuthorFeed"
            f"?{urlencode(params)}"
        )
        resp = await fetch_get(self._client, url)
        data: dict[str, Any] = resp.json()
        return data

    async def get_posts(
        self,
        at_uris: list[str],
    ) -> dict[str, Any]:
        # getPosts は uris パラメータを複数指定するため doseq=True
        params = [("uris", uri) for uri in at_uris]
        url = (
            f"{self._appview_url}/xrpc/app.bsky.feed.getPosts"
            f"?{urlencode(params)}"
        )
        resp = await fetch_get(self._client, url)
        data: dict[str, Any] = resp.json()
        return data

    async def resolve_handle(
        self,
        handle: str,
    ) -> dict[str, Any]:
        url = (
            f"{self._appview_url}/xrpc/com.atproto.identity.resolveHandle"
            f"?{urlencode({'handle': handle})}"
        )
        resp = await fetch_get(self._client, url)
        data: dict[str, Any] = resp.json()
        return data


def create_bluesky_fetcher(
    settings: RAGSettings,
    client: ConstrainedClient,
) -> BlueskyFetcher:
    """Settings から Real / Fake のいずれかを選択して返すファクトリ.

    .env の ``RAG_BLUESKY_FAKE_MODE`` が true の場合は ``FakeBlueskyFetcher``
    を返し、実 BlueSky AT Protocol アクセスを発生させない。
    """
    if settings.rag_bluesky_fake_mode:
        from rag.pipeline.ingesters._fake.bluesky import FakeBlueskyFetcher

        fixture_dir = Path(settings.rag_bluesky_fake_fixture_dir)
        if not fixture_dir.is_absolute():
            project_root = Path(__file__).parent.parent.parent.parent.parent
            fixture_dir = project_root / fixture_dir
        if not fixture_dir.exists():
            raise FileNotFoundError(
                f"BlueSky fake fixture ディレクトリが見つかりません: {fixture_dir}。"
                f"RAG_BLUESKY_FAKE_FIXTURE_DIR を確認してください"
            )
        return FakeBlueskyFetcher(fixture_dir=fixture_dir)
    return RealBlueskyFetcher(
        client,
        appview_url=settings.rag_bluesky_appview_url,
    )

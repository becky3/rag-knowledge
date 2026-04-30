"""BlueSky AT Protocol API 利用層（pagination 制御 + DID 解決）.

仕様: docs/specs/ingesters/bluesky.md

``BlueskyFetcher`` Protocol の利用者層。AT Protocol の HTTP 通信そのものは
``bluesky_fetcher.py`` の ``RealBlueskyFetcher`` が担い、本モジュールは
ページネーション・カーソル管理・DID 解決の集約等のオーケストレーションを担う。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rag.pipeline.ingesters.bluesky_fetcher import BlueskyFetcher

logger = logging.getLogger(__name__)


async def iterate_author_feed(
    fetcher: BlueskyFetcher,
    handle: str,
    *,
    page_size: int,
    max_posts: int,
) -> AsyncIterator[dict[str, Any]]:
    """``getAuthorFeed`` をページネーション走査し、各 feed item を yield する.

    終了条件:
    - cursor が存在しない（最終ページ）
    - max_posts に到達
    - feed が空

    Args:
        fetcher: BlueskyFetcher Protocol 実装
        handle: BlueSky ハンドル
        page_size: 1 ページあたりの取得件数（AT Protocol 仕様上限 100）
        max_posts: 取得する最大投稿数（タイムライン全体に適用）

    Yields:
        フィードアイテム（``{"post": ..., "reason": ... | None}`` 形式）
    """
    cursor: str | None = None
    yielded = 0
    while yielded < max_posts:
        data = await fetcher.get_author_feed(
            handle, limit=page_size, cursor=cursor,
        )
        feed = data.get("feed", [])
        if not feed:
            break

        for item in feed:
            if yielded >= max_posts:
                return
            yielded += 1
            yield item

        cursor = data.get("cursor")
        if not cursor:
            break


async def fetch_posts_by_uris(
    fetcher: BlueskyFetcher,
    at_uris: list[str],
) -> list[dict[str, Any]]:
    """``getPosts`` API で投稿を一括取得する.

    Args:
        fetcher: BlueskyFetcher Protocol 実装
        at_uris: AT URI のリスト

    Returns:
        投稿オブジェクトのリスト（API レスポンスの ``posts`` 配列）
    """
    if not at_uris:
        return []
    data = await fetcher.get_posts(at_uris)
    posts = data.get("posts", [])
    return list(posts) if isinstance(posts, list) else []


async def resolve_handles_to_dids(
    fetcher: BlueskyFetcher,
    handles: Iterable[str],
) -> dict[str, str]:
    """handle リストを ``resolveHandle`` で DID に解決する（重複排除込み）.

    解決失敗（DID が空 or 例外）はログ出力してスキップし、戻り値の dict から
    除外する。呼び出し側は dict の存在確認で失敗を検知する。

    Args:
        fetcher: BlueskyFetcher Protocol 実装
        handles: ハンドル群（重複は排除して一度だけ resolve する）

    Returns:
        ``{handle: did}`` の dict。失敗した handle は含まれない。
    """
    handle_to_did: dict[str, str] = {}
    unique_handles = set(handles)
    for handle in unique_handles:
        try:
            data = await fetcher.resolve_handle(handle)
            did = data.get("did", "")
            if did:
                handle_to_did[handle] = did
            else:
                logger.warning("DID の解決結果が空です: %s", handle)
        except Exception as exc:
            logger.warning(
                "DID 解決に失敗しました: handle=%s, error=%s", handle, exc,
            )
    return handle_to_did

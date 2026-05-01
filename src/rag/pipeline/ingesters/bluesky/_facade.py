"""BlueSky インジェスター facade（オーケストレーション）.

仕様: docs/specs/ingesters/bluesky.md
仕様: docs/specs/architecture.md（正解パターン: youtube）

BlueskyIngester は外部依存（AT Protocol API / メディア DL / YouTube 委譲 /
site-ingest 委譲）を Protocol 経由で受け取り、内部のサブモジュール
（feed_fetcher / post_placer / delegations）にオーケストレーションを委譲する。

外部から見える契約:

- ``crawl_bluesky(handle, ...)``: 著者タイムライン一括取り込み
- ``ingest_posts(urls)``: AT URI 指定の投稿取り込み
- ``follow_urls(placed_items, ...)``: 配置済み投稿内 URL の自動取り込み
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from rag.pipeline.ingesters._common import (
    IngestErrorCategory,
    IngestErrorDetail,
    IngestResult,
    ProgressCallback,
)
from rag.pipeline.ingesters.bluesky.delegations import follow_urls as _follow_urls
from rag.pipeline.ingesters.bluesky.feed_fetcher import (
    fetch_posts_by_uris,
    iterate_author_feed,
    resolve_handles_to_dids,
)
from rag.pipeline.ingesters.bluesky.post_placer import place_post
from rag.pipeline.ingesters.bluesky.url_routing import parse_bluesky_url

if TYPE_CHECKING:
    from rag.config import RAGSettings
    from rag.pipeline.ingesters.bluesky_fetcher import BlueskyFetcher
    from rag.pipeline.ingesters.bluesky_media_downloader import (
        BlueskyMediaDownloader,
    )
    from rag.pipeline.ingesters.web import WebDelegator
    from rag.pipeline.ingesters.youtube_protocols import (
        YoutubeClassifier,
        YoutubeDelegator,
    )
    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)


# ハードリミット: 投稿取得上限
MAX_POSTS_HARD_LIMIT = 1000

# ページネーション 1 ページあたりの取得件数（AT Protocol 仕様上限）
FEED_PAGE_SIZE = 100


def _validate_max_posts(max_posts: object) -> int:
    """max_posts のバリデーションを行う.

    Returns:
        バリデーション済みの max_posts 値（クランプ適用後）

    Raises:
        TypeError: 非整数型（bool を含む）の場合
        ValueError: 0 以下の場合
    """
    if isinstance(max_posts, bool) or not isinstance(max_posts, int):
        raise TypeError(
            f"max_posts は整数で指定してください（受け取った値: {max_posts!r}）"
        )
    if max_posts <= 0:
        raise ValueError(
            f"max_posts は 1 以上で指定してください（受け取った値: {max_posts}）"
        )
    if max_posts > MAX_POSTS_HARD_LIMIT:
        logger.warning(
            "max_posts が上限 %d を超えています（%d）。%d にクランプします",
            MAX_POSTS_HARD_LIMIT,
            max_posts,
            MAX_POSTS_HARD_LIMIT,
        )
        return MAX_POSTS_HARD_LIMIT
    return max_posts


class BlueskyIngester:
    """BlueSky インジェスター facade.

    仕様: docs/specs/ingesters/bluesky.md

    外部依存（AT Protocol API / メディア DL / YouTube / Web）を Protocol
    経由でコンストラクタ注入で受け取る。HTTP クライアント (ConstrainedClient) は
    Fetcher / MediaDownloader 内に内包されるため、本クラスのメソッドは ``client``
    を受け取らない（factory 関数経由で生成された Adapter が HTTP DI を完結させる）。
    """

    def __init__(
        self,
        source_store: SourceStore,
        *,
        fetcher: BlueskyFetcher,
        media_downloader: BlueskyMediaDownloader,
        youtube_classifier: YoutubeClassifier,
        youtube_delegator: YoutubeDelegator | None,
        web_delegator: WebDelegator,
        max_posts: int,
        include_reposts: bool,
        force_youtube_reingest: bool,
        youtube_request_interval: float,
    ) -> None:
        self._store = source_store
        self._fetcher = fetcher
        self._media_downloader = media_downloader
        self._youtube_classifier = youtube_classifier
        self._youtube_delegator = youtube_delegator
        self._web_delegator = web_delegator
        self._max_posts = max_posts
        self._include_reposts = include_reposts
        self._force_youtube_reingest = force_youtube_reingest
        self._youtube_request_interval = youtube_request_interval

    async def crawl_bluesky(
        self,
        handle: str,
        *,
        max_posts: int | None = None,
        include_reposts: bool | None = None,
        force: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[IngestResult, list[dict[str, Any]]]:
        """BlueSky 投稿を取得し source_store に配置する.

        Returns:
            (配置結果, 配置済みフィードアイテムのリスト)
        """
        effective_include_reposts = (
            include_reposts
            if include_reposts is not None
            else self._include_reposts
        )
        logger.info(
            "BlueSky crawl started: handle=%s, max_posts=%s, include_reposts=%s, force=%s",
            handle,
            max_posts if max_posts is not None else self._max_posts,
            effective_include_reposts,
            force,
        )

        if not handle or not handle.strip():
            raise ValueError("handle が空です")
        if handle.startswith("did:"):
            raise ValueError(
                f"handle に DID 形式は指定できません: {handle}"
            )

        effective_max = _validate_max_posts(
            max_posts if max_posts is not None else self._max_posts,
        )

        result = IngestResult()
        seen_paths: set[str] = set()
        placed_items: list[dict[str, Any]] = []
        total_processed = 0

        async for item in iterate_author_feed(
            self._fetcher,
            handle,
            page_size=FEED_PAGE_SIZE,
            max_posts=effective_max,
        ):
            reason = item.get("reason")
            is_repost = (
                reason is not None
                and isinstance(reason, dict)
                and reason.get("$type") == "app.bsky.feed.defs#reasonRepost"
            )
            if is_repost and not effective_include_reposts:
                continue

            total_processed += 1

            place_outcome = await place_post(
                item,
                is_repost=is_repost,
                force=force,
                store=self._store,
                media_downloader=self._media_downloader,
                result=result,
                seen_paths=seen_paths,
            )

            if place_outcome is not None:
                placed_ok, is_overwrite = place_outcome
                if placed_ok:
                    placed_items.append(
                        {**item, "_suppress_youtube_reingest": is_overwrite},
                    )
                if progress_callback is not None:
                    post = item.get("post", {})
                    author = post.get("author", {})
                    post_uri = post.get("uri", "")
                    rkey = (
                        post_uri.rsplit("/", 1)[-1]
                        if "/" in post_uri
                        else ""
                    )
                    bsky_url = (
                        f"https://bsky.app/profile/"
                        f"{author.get('handle', '')}/post/{rkey}"
                    )
                    progress_callback(total_processed, effective_max, bsky_url)

        logger.info(
            "BlueSky crawl completed: placed=%d, overwritten=%d, skipped=%d, errors=%d",
            result.placed, result.overwritten, result.skipped, result.errors,
        )
        return result, placed_items

    async def ingest_posts(
        self,
        urls: list[str],
    ) -> tuple[IngestResult, list[dict[str, Any]]]:
        """指定 URL の BlueSky 投稿を取得して source_store に配置する.

        仕様: docs/specs/ingesters/bluesky.md「投稿取得フロー（rag_add_bluesky）」

        投稿配置完了後、呼び出し側が ``follow_urls`` を実行することで投稿内 URL の
        自動取り込みが走る。``rag_add_bluesky`` はピンポイント修復用途のため、
        placed_items の各 item には ``_suppress_youtube_reingest=False`` を付与する。

        Returns:
            (配置結果, 配置済みフィードアイテムのリスト)
        """
        result = IngestResult()
        placed_items: list[dict[str, Any]] = []

        if not urls:
            return result, placed_items

        parsed: list[tuple[str, str]] = []
        for url in urls:
            hr = parse_bluesky_url(url)
            if hr is None:
                logger.warning("BlueSky URL のパースに失敗しました: %s", url)
                result.errors += 1
                result.error_details.append(IngestErrorDetail(
                    category=IngestErrorCategory.METADATA_FETCH.value,
                    target=url,
                    message="Invalid BlueSky URL format",
                ))
                continue
            parsed.append(hr)

        if not parsed:
            return result, placed_items

        unique_handles = {h for h, _ in parsed}
        handle_to_did = await resolve_handles_to_dids(self._fetcher, unique_handles)

        for handle, rkey in parsed:
            did = handle_to_did.get(handle)
            if not did:
                result.errors += 1
                result.error_details.append(IngestErrorDetail(
                    category=IngestErrorCategory.METADATA_FETCH.value,
                    target=f"https://bsky.app/profile/{handle}/post/{rkey}",
                    message=f"Failed to resolve DID for handle: {handle}",
                ))
                continue

            at_uri = f"at://{did}/app.bsky.feed.post/{rkey}"
            try:
                posts = await fetch_posts_by_uris(self._fetcher, [at_uri])
            except Exception as exc:
                logger.warning(
                    "投稿の取得に失敗しました: %s, error=%s", at_uri, exc,
                )
                result.errors += 1
                result.error_details.append(IngestErrorDetail(
                    category=IngestErrorCategory.METADATA_FETCH.value,
                    target=f"https://bsky.app/profile/{handle}/post/{rkey}",
                    message=str(exc),
                ))
                continue

            if not posts:
                logger.warning("投稿が見つかりません: %s", at_uri)
                result.errors += 1
                result.error_details.append(IngestErrorDetail(
                    category=IngestErrorCategory.METADATA_FETCH.value,
                    target=f"https://bsky.app/profile/{handle}/post/{rkey}",
                    message="Post not found (may be deleted)",
                ))
                continue

            post_obj = posts[0]
            item: dict[str, Any] = {"post": post_obj, "reason": None}

            place_outcome = await place_post(
                item,
                is_repost=False,
                force=True,
                store=self._store,
                media_downloader=self._media_downloader,
                result=result,
            )
            if place_outcome is not None and place_outcome[0]:
                placed_items.append(
                    {**item, "_suppress_youtube_reingest": False},
                )

        logger.info(
            "BlueSky ingest_posts completed: placed=%d, overwritten=%d, errors=%d",
            result.placed, result.overwritten, result.errors,
        )
        return result, placed_items

    async def follow_urls(
        self,
        placed_items: list[dict[str, Any]],
        *,
        settings: RAGSettings,
        result: IngestResult | None = None,
    ) -> dict[str, int]:
        """配置済み投稿から URL を抽出し、site_ingest/YouTube に委譲する.

        仕様: docs/specs/ingesters/bluesky.md「投稿内 URL の自動取り込み」

        Args:
            placed_items: 配置済みフィードアイテムのリスト
            settings: site-ingest のパラメータ参照用
            result: 委譲失敗の計上先 IngestResult（指定時は errors + delegation を計上）

        Returns:
            ``{"web_placed": N, "youtube_placed": N, "skipped": N, "errors": N}``
        """
        return await _follow_urls(
            placed_items,
            classifier=self._youtube_classifier,
            youtube_delegator=self._youtube_delegator,
            web_delegator=self._web_delegator,
            source_store=self._store,
            settings=settings,
            youtube_request_interval=self._youtube_request_interval,
            force_youtube_reingest=self._force_youtube_reingest,
            result=result,
        )

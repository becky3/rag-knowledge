"""BlueSky インジェスターのテスト.

仕様: docs/specs/ingesters/bluesky.md

テスト方針:
- max_posts バリデーション（型チェック、0/負数拒否、クランプ）
- crawl_bluesky のフロー（ページネーション、重複検出、リポストフィルタ）
- .meta サイドカーファイルの生成
- ファイル構造の検証
- メディア DL（画像・動画）
- --force オプション（上書き再取得）
- recordWithMedia 時のメディア URL パス分岐
- YouTube 再取り込み制御
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rag.pipeline.ingesters._common import IngestResult
from rag.pipeline.ingesters.bluesky import (
    MAX_POSTS_HARD_LIMIT,
    BlueskyIngester,
    _escape_did,
    _ext_from_content_type,
    _extract_media_urls,
    _make_title,
    _validate_max_posts,
    classify_url,
    extract_urls_from_item,
    parse_bluesky_url,
)
from rag.store.source_store import SourceStore

from factories import make_bluesky_ingester


@pytest.fixture()
def source_store(tmp_path: Path) -> SourceStore:
    """テスト用 SourceStore を生成する."""
    store = SourceStore(tmp_path / "source_store")
    store.initialize()
    return store


def _make_feed_item(
    did: str = "did:plc:abc123",
    handle: str = "alice.bsky.social",
    rkey: str = "xyz789",
    text: str = "Sample post text",
    created_at: str = "2026-01-15T09:00:00Z",
    *,
    is_repost: bool = False,
    embed: dict | None = None,
    reply: dict | None = None,
) -> dict:
    """テスト用フィードアイテムを生成する."""
    record: dict = {
        "text": text,
        "createdAt": created_at,
    }
    if embed:
        record["embed"] = embed
    if reply:
        record["reply"] = reply

    item: dict = {
        "post": {
            "uri": f"at://{did}/app.bsky.feed.post/{rkey}",
            "cid": "test-cid",
            "author": {
                "did": did,
                "handle": handle,
                "displayName": "Alice",
            },
            "record": record,
        },
    }
    if is_repost:
        item["reason"] = {
            "$type": "app.bsky.feed.defs#reasonRepost",
            "by": {"did": "did:plc:reposter", "handle": "bob.bsky.social"},
            "indexedAt": "2026-01-16T10:00:00Z",
        }
    else:
        item["reason"] = None

    return item


def _make_mock_client(pages: list[dict]) -> AsyncMock:
    """ページネーション対応のモック ConstrainedClient を生成する."""
    client = AsyncMock()
    responses = []
    for page_data in pages:
        resp = MagicMock()
        resp.json.return_value = page_data
        responses.append(resp)
    client.get = AsyncMock(side_effect=responses)
    return client


def _make_budget_client(remaining: int = 500) -> AsyncMock:
    """バジェット付きモック ConstrainedClient を生成する."""
    client = AsyncMock()
    budget = MagicMock()
    budget.remaining = remaining
    client.budget = budget
    return client


class TestValidateMaxPosts:
    """_validate_max_posts のテスト."""

    def test_valid_value(self) -> None:
        """有効な値がそのまま返ること."""
        assert _validate_max_posts(100) == 100

    def test_clamp_above_limit(self) -> None:
        """上限超過がクランプされること."""
        assert _validate_max_posts(2000) == MAX_POSTS_HARD_LIMIT

    def test_reject_zero(self) -> None:
        """0 が拒否されること."""
        with pytest.raises(ValueError, match="1 以上"):
            _validate_max_posts(0)

    def test_reject_negative(self) -> None:
        """負数が拒否されること."""
        with pytest.raises(ValueError, match="1 以上"):
            _validate_max_posts(-1)

    def test_reject_bool(self) -> None:
        """bool が拒否されること."""
        with pytest.raises(TypeError, match="整数"):
            _validate_max_posts(True)

    def test_reject_float(self) -> None:
        """float が拒否されること."""
        with pytest.raises(TypeError, match="整数"):
            _validate_max_posts(1.5)

    def test_reject_string(self) -> None:
        """文字列が拒否されること."""
        with pytest.raises(TypeError, match="整数"):
            _validate_max_posts("100")


class TestEscapeDid:
    """_escape_did のテスト."""

    def test_escape_colons(self) -> None:
        """コロンが全角に置換されること."""
        assert _escape_did("did:plc:abc123") == "did\uff1aplc\uff1aabc123"


class TestMakeTitle:
    """_make_title のテスト."""

    def test_short_text(self) -> None:
        """50 文字以下のテキストがそのまま返ること."""
        assert _make_title("short") == "short"

    def test_long_text_truncated(self) -> None:
        """50 文字超のテキストが切り詰められること."""
        long_text = "a" * 60
        result = _make_title(long_text)
        assert result == "a" * 50 + "..."
        assert len(result) == 53


@pytest.mark.asyncio()
class TestCrawlBluesky:
    """crawl_bluesky のテスト."""

    async def test_empty_handle_raises(
        self, source_store: SourceStore
    ) -> None:
        """空のハンドルでエラーになること."""
        ingester = make_bluesky_ingester(source_store)
        with pytest.raises(ValueError, match="空"):
            await ingester.crawl_bluesky("", client=AsyncMock())

    async def test_did_handle_raises(
        self, source_store: SourceStore
    ) -> None:
        """DID 形式のハンドルでエラーになること."""
        ingester = make_bluesky_ingester(source_store)
        with pytest.raises(ValueError, match="DID"):
            await ingester.crawl_bluesky("did:plc:abc", client=AsyncMock())

    async def test_no_client_raises(
        self, source_store: SourceStore
    ) -> None:
        """client 未指定でエラーになること."""
        ingester = make_bluesky_ingester(source_store)
        with pytest.raises(ValueError, match="client"):
            await ingester.crawl_bluesky("alice.bsky.social")

    async def test_basic_crawl(self, source_store: SourceStore) -> None:
        """基本的なクロールが正常に動作すること."""
        item = _make_feed_item()
        client = _make_mock_client([{"feed": [item]}])

        ingester = make_bluesky_ingester(source_store, max_posts=3)
        result, _ = await ingester.crawl_bluesky(
            "alice.bsky.social", client=client
        )

        assert result.placed == 1
        assert result.errors == 0

        # ファイル構造の検証
        escaped = _escape_did("did:plc:abc123")
        expected_path = (
            source_store.root_dir / "bluesky" / escaped / "2026" / "01" / "xyz789.json"
        )
        assert expected_path.exists()

        # JSON 内容の検証
        data = json.loads(expected_path.read_text(encoding="utf-8"))
        assert data["post"]["uri"] == "at://did:plc:abc123/app.bsky.feed.post/xyz789"

    async def test_dedup_skips_existing(
        self, source_store: SourceStore
    ) -> None:
        """既存ファイルがスキップされること."""
        item = _make_feed_item()
        client = _make_mock_client([
            {"feed": [item]},
        ])

        ingester = make_bluesky_ingester(source_store, max_posts=3)
        # 1 回目
        await ingester.crawl_bluesky("alice.bsky.social", client=client)

        # 2 回目（同じアイテム）
        client2 = _make_mock_client([{"feed": [item]}])
        result, _ = await ingester.crawl_bluesky(
            "alice.bsky.social", client=client2
        )

        assert result.skipped == 1
        assert result.placed == 0

    async def test_repost_excluded(self, source_store: SourceStore) -> None:
        """リポストが include_reposts=false で除外されること."""
        repost_item = _make_feed_item(rkey="repost1", is_repost=True)
        normal_item = _make_feed_item(rkey="normal1")
        client = _make_mock_client([
            {"feed": [repost_item, normal_item]},
        ])

        ingester = make_bluesky_ingester(source_store, max_posts=10)
        result, _ = await ingester.crawl_bluesky(
            "alice.bsky.social",
            include_reposts=False,
            client=client,
        )

        assert result.placed == 1  # リポストが除外され、通常投稿のみ

    async def test_repost_included(self, source_store: SourceStore) -> None:
        """リポストが include_reposts=true で含まれること."""
        repost_item = _make_feed_item(rkey="repost1", is_repost=True)
        normal_item = _make_feed_item(rkey="normal1")
        client = _make_mock_client([
            {"feed": [repost_item, normal_item]},
        ])

        ingester = make_bluesky_ingester(source_store, max_posts=10)
        result, _ = await ingester.crawl_bluesky(
            "alice.bsky.social",
            include_reposts=True,
            client=client,
        )

        assert result.placed == 2

    async def test_meta_fields(self, source_store: SourceStore) -> None:
        """.meta サイドカーファイルのフィールドが正しいこと."""
        item = _make_feed_item(
            text="Sample post text",
            embed={"$type": "app.bsky.embed.images"},
        )
        item["post"]["record"]["reply"] = {"parent": {}}
        client = _make_mock_client([{"feed": [item]}])

        ingester = make_bluesky_ingester(source_store, max_posts=3)
        await ingester.crawl_bluesky("alice.bsky.social", client=client)

        # .meta ファイルの検証
        escaped = _escape_did("did:plc:abc123")
        meta_path = (
            source_store.root_dir
            / "bluesky"
            / escaped
            / "2026"
            / "01"
            / "xyz789.json.meta"
        )
        assert meta_path.exists()

        import yaml

        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        assert meta["source_type"] == "bluesky"
        assert meta["handle"] == "alice.bsky.social"
        assert meta["did"] == "did:plc:abc123"
        assert meta["rkey"] == "xyz789"
        assert meta["has_images"] is True
        assert meta["is_reply"] is True
        assert meta["is_repost"] is False

    async def test_empty_feed(self, source_store: SourceStore) -> None:
        """空のフィードで正常終了すること."""
        client = _make_mock_client([{"feed": []}])

        ingester = make_bluesky_ingester(source_store, max_posts=3)
        result, _ = await ingester.crawl_bluesky(
            "alice.bsky.social", client=client
        )

        assert result.placed == 0
        assert result.errors == 0

    async def test_max_posts_limit(self, source_store: SourceStore) -> None:
        """max_posts で取得数が制限されること."""
        items = [
            _make_feed_item(rkey=f"post{i}")
            for i in range(5)
        ]
        client = _make_mock_client([{"feed": items}])

        ingester = make_bluesky_ingester(source_store)
        result, _ = await ingester.crawl_bluesky(
            "alice.bsky.social",
            max_posts=2,
            client=client,
        )

        assert result.placed == 2


class TestExtractUrlsFromItem:
    """extract_urls_from_item のテスト."""

    def test_facets_link(self) -> None:
        """facets 内のリンクが抽出されること."""
        item = _make_feed_item()
        item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://example.com/page1",
                    }
                ]
            }
        ]
        urls = extract_urls_from_item(item)
        assert urls == ["https://example.com/page1"]

    def test_embed_external(self) -> None:
        """embed.external のリンクカードが抽出されること."""
        item = _make_feed_item(
            embed={
                "$type": "app.bsky.embed.external",
                "external": {"uri": "https://example.com/card"},
            }
        )
        urls = extract_urls_from_item(item)
        assert urls == ["https://example.com/card"]

    def test_record_with_media_external(self) -> None:
        """recordWithMedia の外部リンクカードが抽出されること."""
        item = _make_feed_item(
            embed={
                "$type": "app.bsky.embed.recordWithMedia",
                "media": {
                    "$type": "app.bsky.embed.external",
                    "external": {"uri": "https://example.com/media-card"},
                },
            }
        )
        urls = extract_urls_from_item(item)
        assert urls == ["https://example.com/media-card"]

    def test_multiple_sources_dedup(self) -> None:
        """facets と embed.external に同一 URL がある場合、重複排除されること."""
        item = _make_feed_item(
            embed={
                "$type": "app.bsky.embed.external",
                "external": {"uri": "https://example.com/shared"},
            }
        )
        item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://example.com/shared",
                    }
                ]
            }
        ]
        urls = extract_urls_from_item(item)
        assert urls == ["https://example.com/shared"]

    def test_no_urls(self) -> None:
        """URL が含まれない投稿で空リストが返ること."""
        item = _make_feed_item(text="URL のないテキスト")
        urls = extract_urls_from_item(item)
        assert urls == []

    def test_non_http_uri_excluded(self) -> None:
        """HTTP/HTTPS 以外の URI が除外されること."""
        item = _make_feed_item()
        item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "ftp://example.com/file",
                    }
                ]
            }
        ]
        urls = extract_urls_from_item(item)
        assert urls == []


class TestClassifyUrl:
    """classify_url のテスト."""

    def test_youtube_watch(self) -> None:
        """YouTube watch URL が youtube に分類されること."""
        assert classify_url("https://www.youtube.com/watch?v=abc123") == "youtube"

    def test_youtube_short(self) -> None:
        """youtu.be 短縮 URL が youtube に分類されること."""
        assert classify_url("https://youtu.be/abc123") == "youtube"

    def test_youtube_shorts(self) -> None:
        """YouTube Shorts URL が youtube に分類されること."""
        assert classify_url("https://youtube.com/shorts/abc123") == "youtube"

    def test_bsky_url_skip(self) -> None:
        """BlueSky URL が skip に分類されること."""
        assert classify_url("https://bsky.app/profile/alice.bsky.social/post/xyz") == "skip"

    def test_general_web(self) -> None:
        """一般的な URL が web に分類されること."""
        assert classify_url("https://example.com/article") == "web"

    def test_http_web(self) -> None:
        """HTTP URL が web に分類されること."""
        assert classify_url("http://example.com/page") == "web"


@pytest.mark.asyncio()
class TestFollowUrls:
    """follow_urls のテスト."""

    async def test_web_url_delegated(self, source_store: SourceStore) -> None:
        """Web URL が site_ingest（_fetch_web_urls）に委譲されること."""
        item = _make_feed_item()
        item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://example.com/article",
                    }
                ]
            }
        ]

        ingester = make_bluesky_ingester(source_store)
        with patch.object(ingester, "_fetch_web_urls", new_callable=AsyncMock, return_value=(1, 0, [])) as mock_fetch:
            stats = await ingester.follow_urls(
                [item],
                youtube_ingester=None,
            )
            mock_fetch.assert_called_once_with(["https://example.com/article"])
        assert stats["web_placed"] == 1

    async def test_youtube_url_delegated(self, source_store: SourceStore) -> None:
        """YouTube URL が YoutubeIngester に委譲されること."""
        item = _make_feed_item()
        item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://www.youtube.com/watch?v=test123",
                    }
                ]
            }
        ]

        mock_yt = AsyncMock()
        mock_yt.ingest_video = AsyncMock(
            return_value=MagicMock(placed=1, errors=0)
        )

        ingester = make_bluesky_ingester(source_store)
        stats = await ingester.follow_urls(
            [item],
            youtube_ingester=mock_yt,
        )

        mock_yt.ingest_video.assert_called_once_with(
            video_url="https://www.youtube.com/watch?v=test123"
        )
        assert stats["youtube_placed"] == 1

    async def test_youtube_url_sleep_between_urls(
        self, source_store: SourceStore
    ) -> None:
        """複数 YouTube URL の処理時に URL 間でスリープが挿入されること."""
        item = _make_feed_item()
        item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://www.youtube.com/watch?v=vid1",
                    }
                ]
            },
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://www.youtube.com/watch?v=vid2",
                    }
                ]
            },
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://www.youtube.com/watch?v=vid3",
                    }
                ]
            },
        ]

        mock_yt = AsyncMock()
        mock_yt.ingest_video = AsyncMock(
            return_value=MagicMock(placed=1, errors=0)
        )
        mock_yt.request_interval = 5.0

        ingester = make_bluesky_ingester(source_store)
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            stats = await ingester.follow_urls(
                [item],
                youtube_ingester=mock_yt,
            )

        assert mock_yt.ingest_video.call_count == 3
        assert stats["youtube_placed"] == 3
        # 最後の URL の後はスリープしない → 3 - 1 = 2 回
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(5.0)

    async def test_bsky_url_skipped(self, source_store: SourceStore) -> None:
        """BlueSky URL がスキップされること."""
        item = _make_feed_item()
        item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://bsky.app/profile/alice.bsky.social/post/abc",
                    }
                ]
            }
        ]

        ingester = make_bluesky_ingester(source_store)
        stats = await ingester.follow_urls(
            [item], youtube_ingester=None,
        )

        assert stats["skipped"] == 1
        assert stats["web_placed"] == 0
        assert stats["youtube_placed"] == 0

    async def test_error_isolated(self, source_store: SourceStore) -> None:
        """URL 先の取り込みエラーが隔離されること."""
        item = _make_feed_item()
        item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://example.com/fail",
                    }
                ]
            }
        ]

        ingester = make_bluesky_ingester(source_store)
        # _fetch_web_urls はバッチエラー隔離を行い、エラー件数と詳細を返す
        error_detail = {
            "category": "delegation",
            "target": "https://example.com/fail",
            "url": "https://example.com/fail",
            "message": "site-ingest batch failed: boom",
        }
        with patch.object(
            ingester,
            "_fetch_web_urls",
            new_callable=AsyncMock,
            return_value=(0, 1, [error_detail]),
        ):
            stats = await ingester.follow_urls(
                [item],
                youtube_ingester=None,
            )

        assert stats["errors"] == 1
        assert stats["web_placed"] == 0

    async def test_cross_item_dedup(self, source_store: SourceStore) -> None:
        """複数投稿に同一 URL がある場合、1 回のみ取り込まれること."""
        url = "https://example.com/shared-article"
        facet = {
            "features": [
                {"$type": "app.bsky.richtext.facet#link", "uri": url}
            ]
        }
        item1 = _make_feed_item(rkey="post1")
        item1["post"]["record"]["facets"] = [facet]
        item2 = _make_feed_item(rkey="post2")
        item2["post"]["record"]["facets"] = [facet]

        ingester = make_bluesky_ingester(source_store)
        with patch.object(ingester, "_fetch_web_urls", new_callable=AsyncMock, return_value=(1, 0, [])) as mock_fetch:
            stats = await ingester.follow_urls(
                [item1, item2],
                youtube_ingester=None,
            )
            # URL は重複排除されるので 1 件のリストで呼ばれる
            mock_fetch.assert_called_once_with([url])
        assert stats["web_placed"] == 1

    async def test_empty_items(self, source_store: SourceStore) -> None:
        """配置済みアイテムが空の場合、何も実行されないこと."""
        ingester = make_bluesky_ingester(source_store)
        stats = await ingester.follow_urls(
            [], youtube_ingester=None,
        )

        assert stats["web_placed"] == 0
        assert stats["youtube_placed"] == 0
        assert stats["skipped"] == 0
        assert stats["errors"] == 0


class TestExtFromContentType:
    """_ext_from_content_type のテスト."""

    def test_webp(self) -> None:
        assert _ext_from_content_type("image/webp") == ".webp"

    def test_jpeg(self) -> None:
        assert _ext_from_content_type("image/jpeg") == ".jpg"

    def test_png(self) -> None:
        assert _ext_from_content_type("image/png") == ".png"

    def test_with_charset_param(self) -> None:
        """Content-Type にパラメータが付いている場合."""
        assert _ext_from_content_type("image/webp; charset=utf-8") == ".webp"

    def test_unknown_defaults_to_webp(self) -> None:
        """不明な Content-Type はデフォルト .webp."""
        assert _ext_from_content_type("application/octet-stream") == ".webp"


class TestExtractMediaUrls:
    """_extract_media_urls のテスト."""

    def test_images_from_embed(self) -> None:
        """通常の画像 embed から fullsize URL が抽出されること."""
        item = _make_feed_item()
        item["post"]["embed"] = {
            "$type": "app.bsky.embed.images#view",
            "images": [
                {"fullsize": "https://cdn.bsky.app/img/feed_fullsize/image1.webp"},
                {"fullsize": "https://cdn.bsky.app/img/feed_fullsize/image2.webp"},
            ],
        }
        images, playlist = _extract_media_urls(item)
        assert len(images) == 2
        assert playlist is None

    def test_video_from_embed(self) -> None:
        """動画 embed から playlist URL が抽出されること."""
        item = _make_feed_item()
        item["post"]["embed"] = {
            "$type": "app.bsky.embed.video#view",
            "playlist": "https://video.bsky.app/watch/playlist.m3u8",
        }
        images, playlist = _extract_media_urls(item)
        assert len(images) == 0
        assert playlist == "https://video.bsky.app/watch/playlist.m3u8"

    def test_record_with_media_images(self) -> None:
        """recordWithMedia 時の画像 URL が media 配下から抽出されること."""
        item = _make_feed_item()
        item["post"]["embed"] = {
            "$type": "app.bsky.embed.recordWithMedia#view",
            "media": {
                "$type": "app.bsky.embed.images#view",
                "images": [
                    {"fullsize": "https://cdn.bsky.app/img/nested_image.webp"},
                ],
            },
        }
        images, playlist = _extract_media_urls(item)
        assert len(images) == 1
        assert images[0] == "https://cdn.bsky.app/img/nested_image.webp"

    def test_record_with_media_video(self) -> None:
        """recordWithMedia 時の動画 playlist URL が media 配下から抽出されること."""
        item = _make_feed_item()
        item["post"]["embed"] = {
            "$type": "app.bsky.embed.recordWithMedia#view",
            "media": {
                "$type": "app.bsky.embed.video#view",
                "playlist": "https://video.bsky.app/nested_playlist.m3u8",
            },
        }
        images, playlist = _extract_media_urls(item)
        assert len(images) == 0
        assert playlist == "https://video.bsky.app/nested_playlist.m3u8"

    def test_no_embed(self) -> None:
        """embed がない場合は空が返ること."""
        item = _make_feed_item()
        images, playlist = _extract_media_urls(item)
        assert len(images) == 0
        assert playlist is None


@pytest.mark.asyncio()
class TestMediaDownload:
    """メディア DL のテスト."""

    async def test_image_download(self, source_store: SourceStore) -> None:
        """画像が CDN から DL されて配置されること."""
        item = _make_feed_item(
            embed={"$type": "app.bsky.embed.images"},
        )
        item["post"]["embed"] = {
            "$type": "app.bsky.embed.images#view",
            "images": [
                {"fullsize": "https://cdn.bsky.app/img/image1"},
            ],
        }

        # モック CDN レスポンス
        img_resp = MagicMock()
        img_resp.headers = {"content-type": "image/webp"}
        img_resp.content = b"fake-image-data"

        # API レスポンス（フィード取得）
        api_resp = MagicMock()
        api_resp.json.return_value = {"feed": [item]}

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[api_resp, img_resp])

        ingester = make_bluesky_ingester(source_store, max_posts=3)
        result, _ = await ingester.crawl_bluesky(
            "alice.bsky.social", client=client,
        )

        assert result.placed == 1

        # メディアファイルの検証
        escaped = _escape_did("did:plc:abc123")
        media_path = (
            source_store.root_dir / "bluesky" / escaped / "2026" / "01"
            / "media" / "xyz789" / "image_0.webp"
        )
        assert media_path.exists()
        assert media_path.read_bytes() == b"fake-image-data"

    async def test_video_hls_download(self, source_store: SourceStore) -> None:
        """HLS 動画が ts セグメント結合で保存されること."""
        item = _make_feed_item(
            embed={"$type": "app.bsky.embed.video"},
        )
        item["post"]["embed"] = {
            "$type": "app.bsky.embed.video#view",
            "playlist": "https://video.bsky.app/watch/playlist.m3u8",
        }

        # モックレスポンス
        api_resp = MagicMock()
        api_resp.json.return_value = {"feed": [item]}

        playlist_resp = MagicMock()
        playlist_resp.text = "#EXTM3U\nseg0.ts\nseg1.ts\n"

        seg0_resp = MagicMock()
        seg0_resp.content = b"segment-0-"
        seg1_resp = MagicMock()
        seg1_resp.content = b"segment-1"

        client = AsyncMock()
        client.get = AsyncMock(
            side_effect=[api_resp, playlist_resp, seg0_resp, seg1_resp]
        )

        ingester = make_bluesky_ingester(source_store, max_posts=3)
        result, _ = await ingester.crawl_bluesky(
            "alice.bsky.social", client=client,
        )

        assert result.placed == 1

        escaped = _escape_did("did:plc:abc123")
        video_path = (
            source_store.root_dir / "bluesky" / escaped / "2026" / "01"
            / "media" / "xyz789" / "video_0.ts"
        )
        assert video_path.exists()
        assert video_path.read_bytes() == b"segment-0-segment-1"

    async def test_media_download_error_isolated(
        self, source_store: SourceStore,
    ) -> None:
        """メディア DL のエラーが投稿配置に影響しないこと."""
        item = _make_feed_item(
            embed={"$type": "app.bsky.embed.images"},
        )
        item["post"]["embed"] = {
            "$type": "app.bsky.embed.images#view",
            "images": [
                {"fullsize": "https://cdn.bsky.app/img/fail"},
            ],
        }

        api_resp = MagicMock()
        api_resp.json.return_value = {"feed": [item]}

        client = AsyncMock()
        # API 成功 → 画像 DL 失敗
        client.get = AsyncMock(
            side_effect=[api_resp, Exception("CDN error")]
        )

        ingester = make_bluesky_ingester(source_store, max_posts=3)
        result, placed = await ingester.crawl_bluesky(
            "alice.bsky.social", client=client,
        )

        # 投稿自体は配置される
        assert result.placed == 1
        assert len(placed) == 1

    async def test_hls_partial_download_no_file(
        self, source_store: SourceStore,
    ) -> None:
        """HLS セグメントの途中 DL 失敗で動画ファイルが残らないこと."""
        item = _make_feed_item(
            embed={"$type": "app.bsky.embed.video"},
        )
        item["post"]["embed"] = {
            "$type": "app.bsky.embed.video#view",
            "playlist": "https://video.bsky.app/watch/playlist.m3u8",
        }

        api_resp = MagicMock()
        api_resp.json.return_value = {"feed": [item]}

        playlist_resp = MagicMock()
        playlist_resp.text = "#EXTM3U\nseg0.ts\nseg1.ts\n"

        seg0_resp = MagicMock()
        seg0_resp.content = b"segment-0-"

        client = AsyncMock()
        # API 成功 → プレイリスト成功 → seg0 成功 → seg1 失敗
        client.get = AsyncMock(
            side_effect=[api_resp, playlist_resp, seg0_resp, Exception("segment error")]
        )

        ingester = make_bluesky_ingester(source_store, max_posts=3)
        result, _ = await ingester.crawl_bluesky(
            "alice.bsky.social", client=client,
        )

        # 投稿は配置されるがメディアは失敗
        assert result.placed == 1

        escaped = _escape_did("did:plc:abc123")
        video_path = (
            source_store.root_dir / "bluesky" / escaped / "2026" / "01"
            / "media" / "xyz789" / "video_0.ts"
        )
        # 部分ファイルが残っていないこと
        assert not video_path.exists()


@pytest.mark.asyncio()
class TestForceMode:
    """--force オプションのテスト."""

    async def test_force_overwrites_existing(
        self, source_store: SourceStore,
    ) -> None:
        """force=True で既存ファイルが上書きされること."""
        item = _make_feed_item(text="original text")
        client1 = _make_mock_client([{"feed": [item]}])

        ingester = make_bluesky_ingester(source_store, max_posts=10)

        # 1 回目: 通常配置
        result1, _ = await ingester.crawl_bluesky(
            "alice.bsky.social", client=client1,
        )
        assert result1.placed == 1

        # 2 回目: force=False → スキップ
        client2 = _make_mock_client([{"feed": [item]}])
        result2, _ = await ingester.crawl_bluesky(
            "alice.bsky.social", force=False, client=client2,
        )
        assert result2.skipped == 1
        assert result2.placed == 0

        # 3 回目: force=True → 上書き（排他計上: placed=0, overwritten=1）
        client3 = _make_mock_client([{"feed": [item]}])
        result3, _ = await ingester.crawl_bluesky(
            "alice.bsky.social", force=True, client=client3,
        )
        assert result3.placed == 0
        assert result3.overwritten == 1
        assert result3.skipped == 0

    async def test_force_triggers_media_download(
        self, source_store: SourceStore,
    ) -> None:
        """force=True で既存投稿のメディアも DL されること."""
        item = _make_feed_item(
            embed={"$type": "app.bsky.embed.images"},
        )
        item["post"]["embed"] = {
            "$type": "app.bsky.embed.images#view",
            "images": [
                {"fullsize": "https://cdn.bsky.app/img/image1"},
            ],
        }

        # 1 回目: メディアなし（API のみ）
        api_resp1 = MagicMock()
        api_resp1.json.return_value = {"feed": [item]}
        img_resp1 = MagicMock()
        img_resp1.headers = {"content-type": "image/webp"}
        img_resp1.content = b"first-image"
        client1 = AsyncMock()
        client1.get = AsyncMock(side_effect=[api_resp1, img_resp1])

        ingester = make_bluesky_ingester(source_store, max_posts=10)
        await ingester.crawl_bluesky("alice.bsky.social", client=client1)

        # 2 回目: force=True → メディア再 DL
        api_resp2 = MagicMock()
        api_resp2.json.return_value = {"feed": [item]}
        img_resp2 = MagicMock()
        img_resp2.headers = {"content-type": "image/jpeg"}
        img_resp2.content = b"updated-image"
        client2 = AsyncMock()
        client2.get = AsyncMock(side_effect=[api_resp2, img_resp2])

        result, _ = await ingester.crawl_bluesky(
            "alice.bsky.social", force=True, client=client2,
        )

        # 排他計上: 既存ファイル上書きなので overwritten=1, placed=0
        assert result.placed == 0
        assert result.overwritten == 1
        escaped = _escape_did("did:plc:abc123")
        media_dir = (
            source_store.root_dir / "bluesky" / escaped / "2026" / "01"
            / "media" / "xyz789"
        )
        # 新しい画像が保存されている（拡張子は Content-Type に従う）
        jpg_path = media_dir / "image_0.jpg"
        assert jpg_path.exists()
        assert jpg_path.read_bytes() == b"updated-image"


@pytest.mark.asyncio()
class TestFollowUrlsYoutubeOverwrite:
    """follow_urls の上書き/新規投稿における YouTube スキップ判定テスト."""

    async def test_overwrite_item_youtube_reingest_disabled(
        self, source_store: SourceStore,
    ) -> None:
        """上書き投稿かつ force_youtube_reingest=False で YouTube がスキップされること."""
        item = _make_feed_item()
        item["_is_overwrite"] = True
        item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://www.youtube.com/watch?v=test123",
                    }
                ]
            }
        ]

        mock_yt = AsyncMock()
        mock_yt.ingest_video = AsyncMock(
            return_value=MagicMock(placed=1, errors=0)
        )

        ingester = make_bluesky_ingester(source_store)
        stats = await ingester.follow_urls(
            [item],
            youtube_ingester=mock_yt,
            force_youtube_reingest=False,
        )

        # 上書き投稿の YouTube は呼ばれずスキップされる
        mock_yt.ingest_video.assert_not_called()
        assert stats["skipped"] == 1
        assert stats["youtube_placed"] == 0

    async def test_overwrite_item_youtube_reingest_enabled(
        self, source_store: SourceStore,
    ) -> None:
        """上書き投稿かつ force_youtube_reingest=True で YouTube が再取り込みされること."""
        item = _make_feed_item()
        item["_is_overwrite"] = True
        item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://www.youtube.com/watch?v=test123",
                    }
                ]
            }
        ]

        mock_yt = AsyncMock()
        mock_yt.ingest_video = AsyncMock(
            return_value=MagicMock(placed=1, errors=0)
        )
        mock_yt.request_interval = 5.0

        ingester = make_bluesky_ingester(source_store)
        stats = await ingester.follow_urls(
            [item],
            youtube_ingester=mock_yt,
            force_youtube_reingest=True,
        )

        mock_yt.ingest_video.assert_called_once()
        assert stats["youtube_placed"] == 1

    async def test_new_item_youtube_always_ingested(
        self, source_store: SourceStore,
    ) -> None:
        """新規投稿の YouTube URL は force_youtube_reingest=False でも取り込まれること."""
        item = _make_feed_item()
        item["_is_overwrite"] = False
        item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://www.youtube.com/watch?v=new123",
                    }
                ]
            }
        ]

        mock_yt = AsyncMock()
        mock_yt.ingest_video = AsyncMock(
            return_value=MagicMock(placed=1, errors=0)
        )

        ingester = make_bluesky_ingester(source_store)
        stats = await ingester.follow_urls(
            [item],
            youtube_ingester=mock_yt,
            force_youtube_reingest=False,
        )

        # 新規投稿の YouTube は常に取り込まれる
        mock_yt.ingest_video.assert_called_once()
        assert stats["youtube_placed"] == 1
        assert stats["skipped"] == 0

    async def test_mixed_new_and_overwrite_items(
        self, source_store: SourceStore,
    ) -> None:
        """新規と上書きが混在する場合、新規の YouTube のみ取り込まれること."""
        new_item = _make_feed_item()
        new_item["_is_overwrite"] = False
        new_item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://www.youtube.com/watch?v=new456",
                    }
                ]
            }
        ]

        overwrite_item = _make_feed_item()
        overwrite_item["_is_overwrite"] = True
        overwrite_item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://www.youtube.com/watch?v=old789",
                    }
                ]
            }
        ]

        mock_yt = AsyncMock()
        mock_yt.ingest_video = AsyncMock(
            return_value=MagicMock(placed=1, errors=0)
        )

        ingester = make_bluesky_ingester(source_store)
        stats = await ingester.follow_urls(
            [new_item, overwrite_item],
            youtube_ingester=mock_yt,
            force_youtube_reingest=False,
        )

        # 新規の YouTube のみ取り込まれ、上書きはスキップ
        mock_yt.ingest_video.assert_called_once_with(
            video_url="https://www.youtube.com/watch?v=new456"
        )
        assert stats["youtube_placed"] == 1
        assert stats["skipped"] == 1


# ---- HLS ダウンロード ----


class TestSelectHlsVariant:
    """_select_hls_variant のテスト."""

    def test_selects_lowest_bandwidth_variant(
        self, source_store: SourceStore,
    ) -> None:
        """BANDWIDTH が最小のバリアント URL を選択する."""
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=3440800,RESOLUTION=720x1280\n"
            "720p/video.m3u8?session_id=abc\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=655600,RESOLUTION=360x640\n"
            "360p/video.m3u8?session_id=abc\n"
        )
        ingester = make_bluesky_ingester(source_store)
        result = ingester._select_hls_variant(
            master, "https://video.bsky.app/playlist.m3u8",
        )
        # 720p が先に列挙されていても、BANDWIDTH 最小の 360p を選択
        assert result == "https://video.bsky.app/360p/video.m3u8?session_id=abc"

    def test_returns_none_for_empty_playlist(
        self, source_store: SourceStore,
    ) -> None:
        """バリアントが見つからない場合は None を返す."""
        master = "#EXTM3U\n#EXT-X-VERSION:3\n"
        ingester = make_bluesky_ingester(source_store)
        result = ingester._select_hls_variant(
            master, "https://video.bsky.app/playlist.m3u8",
        )
        assert result is None

    def test_resolves_relative_url(
        self, source_store: SourceStore,
    ) -> None:
        """相対パスのバリアント URL が正しく解決される."""
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=655600\n"
            "../other/video.m3u8\n"
        )
        ingester = make_bluesky_ingester(source_store)
        result = ingester._select_hls_variant(
            master, "https://video.bsky.app/hls/playlist.m3u8",
        )
        assert result == "https://video.bsky.app/other/video.m3u8"

    def test_falls_back_to_first_variant_when_no_bandwidth(
        self, source_store: SourceStore,
    ) -> None:
        """全バリアントに BANDWIDTH がない場合は最初のバリアントにフォールバックする."""
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:RESOLUTION=360x640\n"
            "360p/video.m3u8\n"
            "#EXT-X-STREAM-INF:RESOLUTION=720x1280\n"
            "720p/video.m3u8\n"
        )
        ingester = make_bluesky_ingester(source_store)
        result = ingester._select_hls_variant(
            master, "https://video.bsky.app/playlist.m3u8",
        )
        # BANDWIDTH なし → fallback_url（最初のバリアント）
        assert result == "https://video.bsky.app/360p/video.m3u8"

    def test_ignores_no_bandwidth_variant_when_others_have_it(
        self, source_store: SourceStore,
    ) -> None:
        """BANDWIDTH ありとなしが混在する場合、BANDWIDTH ありの最小を選択する."""
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:RESOLUTION=360x640\n"
            "360p/video.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=3440800,RESOLUTION=720x1280\n"
            "720p/video.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=655600,RESOLUTION=480x854\n"
            "480p/video.m3u8\n"
        )
        ingester = make_bluesky_ingester(source_store)
        result = ingester._select_hls_variant(
            master, "https://video.bsky.app/playlist.m3u8",
        )
        # BANDWIDTH なしの 360p は candidates に入らず、655600 の 480p が選択される
        assert result == "https://video.bsky.app/480p/video.m3u8"


class TestBaseDomain:
    """_base_domain のテスト."""

    def test_extracts_base_domain_from_subdomain(
        self, source_store: SourceStore,
    ) -> None:
        """サブドメイン付きホストから eTLD+1 相当を抽出する."""
        ingester = make_bluesky_ingester(source_store)
        assert ingester._base_domain("video.cdn.bsky.app") == "bsky.app"

    def test_extracts_base_domain_from_two_level_host(
        self, source_store: SourceStore,
    ) -> None:
        """2レベルのホスト名はそのまま返す."""
        ingester = make_bluesky_ingester(source_store)
        assert ingester._base_domain("bsky.app") == "bsky.app"

    def test_returns_single_label_as_is(
        self, source_store: SourceStore,
    ) -> None:
        """1ラベルのホスト名はそのまま返す."""
        ingester = make_bluesky_ingester(source_store)
        assert ingester._base_domain("localhost") == "localhost"

    def test_returns_empty_for_none(
        self, source_store: SourceStore,
    ) -> None:
        """None は空文字列を返す."""
        ingester = make_bluesky_ingester(source_store)
        assert ingester._base_domain(None) == ""


class TestGetFollowingSameOriginRedirect:
    """_get_following_same_origin_redirect のテスト."""

    @pytest.mark.asyncio()
    async def test_returns_200_response_directly(
        self, source_store: SourceStore,
    ) -> None:
        """200 レスポンスはそのまま返す."""
        mock_client = AsyncMock()
        mock_client.get.return_value = MagicMock(status_code=200, content=b"data")

        ingester = make_bluesky_ingester(source_store)
        resp = await ingester._get_following_same_origin_redirect(
            mock_client, "https://video.bsky.app/playlist.m3u8",
        )

        assert resp.status_code == 200
        assert resp.content == b"data"
        mock_client.get.assert_called_once()

    @pytest.mark.asyncio()
    async def test_follows_redirect_to_same_base_domain(
        self, source_store: SourceStore,
    ) -> None:
        """同一ベースドメインへの 302 リダイレクトを追従する."""
        redirect_resp = MagicMock(
            status_code=302,
            headers={"location": "https://video.cdn.bsky.app/data/video0.ts"},
        )
        final_resp = MagicMock(status_code=200, content=b"video-data")

        mock_client = AsyncMock()
        mock_client.get.side_effect = [redirect_resp, final_resp]

        ingester = make_bluesky_ingester(source_store)
        resp = await ingester._get_following_same_origin_redirect(
            mock_client, "https://video.bsky.app/360p/video0.ts",
        )

        assert resp.status_code == 200
        assert resp.content == b"video-data"
        assert mock_client.get.call_count == 2

    @pytest.mark.asyncio()
    async def test_rejects_redirect_to_different_domain(
        self, source_store: SourceStore,
    ) -> None:
        """異なるベースドメインへのリダイレクトは HTTPStatusError を送出する.

        旧実装では 302 レスポンスをそのまま返していたが、呼び出し側で
        3xx 本文を正常レスポンスと誤認して処理されるリスクがあったため、
        helper が契約として 2xx 以外を全て例外化する設計に変更。
        """
        import httpx as _httpx

        req = _httpx.Request("GET", "https://video.bsky.app/360p/video0.ts")
        redirect_resp = MagicMock(
            status_code=302,
            headers={"location": "https://evil.example.com/steal"},
            request=req,
        )

        mock_client = AsyncMock()
        mock_client.get.return_value = redirect_resp

        ingester = make_bluesky_ingester(source_store)
        with pytest.raises(_httpx.HTTPStatusError) as exc_info:
            await ingester._get_following_same_origin_redirect(
                mock_client, "https://video.bsky.app/360p/video0.ts",
            )
        assert "cross-domain" in str(exc_info.value)
        mock_client.get.assert_called_once()

    @pytest.mark.asyncio()
    async def test_real_httpx_response_302_then_200(
        self, source_store: SourceStore,
    ) -> None:
        """実 httpx.Response で 302→200 を追従できる（raise_for_status 回帰テスト）.

        httpx の raise_for_status は 3xx でも例外を送出する仕様のため、
        リダイレクト追従中に途中の 3xx で例外化しないことを保証する。
        """
        import httpx as _httpx
        req = _httpx.Request("GET", "https://video.bsky.app/playlist.m3u8")
        redirect_resp = _httpx.Response(
            302,
            headers={"location": "https://video.cdn.bsky.app/final.m3u8"},
            request=req,
        )
        final_resp = _httpx.Response(200, content=b"ok", request=req)

        mock_client = AsyncMock()
        mock_client.get.side_effect = [redirect_resp, final_resp]

        ingester = make_bluesky_ingester(source_store)
        resp = await ingester._get_following_same_origin_redirect(
            mock_client, "https://video.bsky.app/playlist.m3u8",
        )

        assert resp.status_code == 200
        assert resp.content == b"ok"

    @pytest.mark.asyncio()
    async def test_real_httpx_response_final_5xx_raises(
        self, source_store: SourceStore,
    ) -> None:
        """最終レスポンスが 5xx の場合に HTTPStatusError を送出する."""
        import httpx as _httpx
        req = _httpx.Request("GET", "https://video.bsky.app/playlist.m3u8")
        error_resp = _httpx.Response(503, content=b"err", request=req)

        mock_client = AsyncMock()
        mock_client.get.return_value = error_resp

        ingester = make_bluesky_ingester(source_store)
        with pytest.raises(_httpx.HTTPStatusError):
            await ingester._get_following_same_origin_redirect(
                mock_client, "https://video.bsky.app/playlist.m3u8",
            )

    @pytest.mark.asyncio()
    @pytest.mark.parametrize("status_code", [300, 303, 304])
    async def test_non_redirect_3xx_raises(
        self, source_store: SourceStore, status_code: int,
    ) -> None:
        """リダイレクト追従対象外の 3xx（300/303/304）で HTTPStatusError を送出する.

        `raise_for_status()` は 3xx を例外化しないため、`is_success` ベースの
        判定に統一している。3xx 本文を正常レスポンスと誤認させないための契約。
        """
        import httpx as _httpx
        req = _httpx.Request("GET", "https://video.bsky.app/playlist.m3u8")
        resp = _httpx.Response(status_code, content=b"err", request=req)

        mock_client = AsyncMock()
        mock_client.get.return_value = resp

        ingester = make_bluesky_ingester(source_store)
        with pytest.raises(_httpx.HTTPStatusError) as exc_info:
            await ingester._get_following_same_origin_redirect(
                mock_client, "https://video.bsky.app/playlist.m3u8",
            )
        assert exc_info.value.response.status_code == status_code

    @pytest.mark.asyncio()
    async def test_stops_at_max_redirects(
        self, source_store: SourceStore,
    ) -> None:
        """リダイレクト上限で停止し、最後のレスポンスを返す."""
        # 2回リダイレクト（上限=2 に設定）
        redirect1 = MagicMock(
            status_code=302,
            headers={"location": "https://video.cdn.bsky.app/r1"},
        )
        redirect2 = MagicMock(
            status_code=302,
            headers={"location": "https://video.cdn.bsky.app/r2"},
        )
        final = MagicMock(status_code=200, content=b"finally")

        mock_client = AsyncMock()
        mock_client.get.side_effect = [redirect1, redirect2, final]

        ingester = make_bluesky_ingester(source_store)
        resp = await ingester._get_following_same_origin_redirect(
            mock_client,
            "https://video.bsky.app/start",
            _max_redirects=2,
        )

        # 初回 GET + 2回リダイレクト追従 = 3回の GET
        assert mock_client.get.call_count == 3
        assert resp.status_code == 200


class TestDownloadHlsVideo:
    """_download_hls_video のマスタープレイリスト解決テスト."""

    @pytest.mark.asyncio()
    async def test_resolves_master_playlist_then_downloads_segments(
        self, source_store: SourceStore, tmp_path: Path,
    ) -> None:
        """マスタープレイリストを検出し、バリアント経由で ts をDLする."""
        master_text = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=655600,RESOLUTION=360x640\n"
            "360p/video.m3u8?sid=1\n"
        )
        variant_text = (
            "#EXTM3U\n"
            "#EXTINF:6.000,\n"
            "video0.ts?sid=1\n"
            "#EXTINF:5.000,\n"
            "video1.ts?sid=1\n"
        )
        seg0 = b"\x00" * 100
        seg1 = b"\xff" * 50

        mock_client = AsyncMock()
        # 1st call: master playlist, 2nd: variant, 3rd: seg0, 4th: seg1
        mock_client.get.side_effect = [
            MagicMock(text=master_text, status_code=200),
            MagicMock(text=variant_text, status_code=200),
            MagicMock(content=seg0, status_code=200),
            MagicMock(content=seg1, status_code=200),
        ]

        dest = tmp_path / "media" / "video_0.ts"
        ingester = make_bluesky_ingester(source_store)
        result = IngestResult()
        await ingester._download_hls_video(
            "https://video.bsky.app/watch/playlist.m3u8",
            dest=dest,
            client=mock_client,
            result=result,
            rel_path="bluesky/did/2025/01/rkey.json",
        )

        assert dest.exists()
        assert dest.read_bytes() == seg0 + seg1
        assert mock_client.get.call_count == 4
        assert result.partial_failures == 0

    @pytest.mark.asyncio()
    async def test_downloads_variant_playlist_directly(
        self, source_store: SourceStore, tmp_path: Path,
    ) -> None:
        """バリアントプレイリスト（#EXT-X-STREAM-INF なし）は直接 ts をDLする."""
        variant_text = (
            "#EXTM3U\n"
            "#EXTINF:6.000,\n"
            "video0.ts?sid=1\n"
        )
        seg0 = b"\xab" * 80

        mock_client = AsyncMock()
        mock_client.get.side_effect = [
            MagicMock(text=variant_text, status_code=200),
            MagicMock(content=seg0, status_code=200),
        ]

        dest = tmp_path / "media" / "video_0.ts"
        ingester = make_bluesky_ingester(source_store)
        result = IngestResult()
        await ingester._download_hls_video(
            "https://video.bsky.app/360p/video.m3u8",
            dest=dest,
            client=mock_client,
            result=result,
            rel_path="bluesky/did/2025/01/rkey.json",
        )

        assert dest.exists()
        assert dest.read_bytes() == seg0
        assert mock_client.get.call_count == 2
        assert result.partial_failures == 0


class TestPartialFailureObservability:
    """partial_failures 計上の観測性テスト.

    仕様: docs/specs/ingesters/bluesky.md / docs/specs/ingesters/common.md

    bluesky は画像・動画 DL 失敗、HLS バリアント選択不可、
    異ドメインリダイレクト、リダイレクト上限到達を
    `partial_failures` + `category="media_download"` として IngestResult に計上する。
    """

    @pytest.mark.asyncio()
    async def test_image_download_failure_counted_as_partial_failure(
        self, source_store: SourceStore, tmp_path: Path,
    ) -> None:
        """画像 DL の失敗が partial_failures に計上されること."""
        import httpx as _httpx

        item = _make_feed_item()
        item["post"]["embed"] = {
            "$type": "app.bsky.embed.images#view",
            "images": [
                {"fullsize": "https://cdn.bsky.app/img/feed_fullsize/broken.webp"},
            ],
        }

        mock_client = AsyncMock()
        request = _httpx.Request(
            "GET", "https://cdn.bsky.app/img/feed_fullsize/broken.webp",
        )
        mock_client.get = AsyncMock(
            return_value=_httpx.Response(500, content=b"", request=request),
        )

        ingester = make_bluesky_ingester(source_store)
        result = IngestResult()
        await ingester._download_media(
            item,
            media_dir=tmp_path / "media",
            client=mock_client,
            result=result,
            rel_path="bluesky/did/2025/01/rkey.json",
        )

        assert result.partial_failures == 1
        assert result.errors == 0
        detail = result.partial_failure_details[0]
        assert detail["category"] == "media_download"
        assert detail["target"] == "bluesky/did/2025/01/rkey.json"
        assert detail["url"] == "https://cdn.bsky.app/img/feed_fullsize/broken.webp"
        assert detail["status"] == 500

    @pytest.mark.asyncio()
    async def test_video_download_failure_counted_as_partial_failure(
        self, source_store: SourceStore, tmp_path: Path,
    ) -> None:
        """動画 DL の失敗が partial_failures に計上されること."""
        import httpx as _httpx

        item = _make_feed_item()
        item["post"]["embed"] = {
            "$type": "app.bsky.embed.video#view",
            "playlist": "https://video.bsky.app/watch/playlist.m3u8",
        }

        mock_client = AsyncMock()
        request = _httpx.Request(
            "GET", "https://video.bsky.app/watch/playlist.m3u8",
        )
        mock_client.get = AsyncMock(
            return_value=_httpx.Response(503, content=b"", request=request),
        )

        ingester = make_bluesky_ingester(source_store)
        result = IngestResult()
        await ingester._download_media(
            item,
            media_dir=tmp_path / "media",
            client=mock_client,
            result=result,
            rel_path="bluesky/did/2025/01/rkey.json",
        )

        assert result.partial_failures == 1
        assert result.errors == 0
        detail = result.partial_failure_details[0]
        assert detail["category"] == "media_download"
        assert detail["url"] == "https://video.bsky.app/watch/playlist.m3u8"

    @pytest.mark.asyncio()
    async def test_hls_variant_not_selectable_counted_as_partial_failure(
        self, source_store: SourceStore, tmp_path: Path,
    ) -> None:
        """マスタープレイリストからバリアントを選択できない場合に partial_failures 計上."""
        # #EXT-X-STREAM-INF を含むが、直後にバリアント URL 行が無い不正なプレイリスト
        master_text = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=655600\n"
        )

        mock_client = AsyncMock()
        mock_client.get.side_effect = [
            MagicMock(text=master_text, status_code=200),
        ]

        dest = tmp_path / "media" / "video_0.ts"
        ingester = make_bluesky_ingester(source_store)
        result = IngestResult()
        await ingester._download_hls_video(
            "https://video.bsky.app/watch/playlist.m3u8",
            dest=dest,
            client=mock_client,
            result=result,
            rel_path="bluesky/did/2025/01/rkey.json",
        )

        assert not dest.exists()
        assert result.partial_failures == 1
        detail = result.partial_failure_details[0]
        assert detail["category"] == "media_download"
        assert "HLS variant" in detail["message"]

    @pytest.mark.asyncio()
    async def test_hls_playlist_without_segments_counted_as_partial_failure(
        self, source_store: SourceStore, tmp_path: Path,
    ) -> None:
        """バリアントプレイリストに ts セグメント行が無い場合に partial_failures 計上.

        `test_hls_variant_not_selectable_counted_as_partial_failure` と対称の経路で、
        プレイリストは取得できるが ts セグメント URL 行が 1 つも無いケースをカバーする。
        """
        # ヘッダとコメントのみ。非コメント行（= ts セグメント URL 行）が存在しない
        variant_text = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:6\n"
        )

        mock_client = AsyncMock()
        mock_client.get.side_effect = [
            MagicMock(text=variant_text, status_code=200),
        ]

        dest = tmp_path / "media" / "video_0.ts"
        ingester = make_bluesky_ingester(source_store)
        result = IngestResult()
        await ingester._download_hls_video(
            "https://video.bsky.app/watch/playlist.m3u8",
            dest=dest,
            client=mock_client,
            result=result,
            rel_path="bluesky/did/2025/01/rkey.json",
        )

        assert not dest.exists()
        assert result.partial_failures == 1
        detail = result.partial_failure_details[0]
        assert detail["category"] == "media_download"
        assert "no ts segments" in detail["message"]

    @pytest.mark.asyncio()
    async def test_cross_domain_redirect_raises_from_helper(
        self, source_store: SourceStore,
    ) -> None:
        """異ドメインリダイレクトは helper 内で HTTPStatusError が送出されること.

        3xx レスポンスを呼び出し側に漏らさない契約を検証する（#587 の再発防止）。
        """
        import httpx as _httpx

        request = _httpx.Request(
            "GET", "https://video.bsky.app/watch/playlist.m3u8",
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=MagicMock(
                status_code=302,
                headers={"location": "https://evil.example.com/steal"},
                request=request,
            ),
        )

        with pytest.raises(_httpx.HTTPStatusError) as exc_info:
            await BlueskyIngester._get_following_same_origin_redirect(
                mock_client,
                "https://video.bsky.app/watch/playlist.m3u8",
            )
        assert "cross-domain" in str(exc_info.value)

    @pytest.mark.asyncio()
    async def test_cross_domain_redirect_recorded_via_outer_except(
        self, source_store: SourceStore, tmp_path: Path,
    ) -> None:
        """異ドメインリダイレクトの helper 例外が _download_media の outer except で
        partial_failures として記録されること."""
        import httpx as _httpx

        item = _make_feed_item()
        item["post"]["embed"] = {
            "$type": "app.bsky.embed.video#view",
            "playlist": "https://video.bsky.app/watch/playlist.m3u8",
        }

        request = _httpx.Request(
            "GET", "https://video.bsky.app/watch/playlist.m3u8",
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=MagicMock(
                status_code=302,
                headers={"location": "https://evil.example.com/steal"},
                request=request,
            ),
        )

        ingester = make_bluesky_ingester(source_store)
        result = IngestResult()
        await ingester._download_media(
            item,
            media_dir=tmp_path / "media",
            client=mock_client,
            result=result,
            rel_path="bluesky/did/2025/01/rkey.json",
        )

        assert result.partial_failures == 1
        detail = result.partial_failure_details[0]
        assert detail["category"] == "media_download"
        assert detail["url"] == "https://video.bsky.app/watch/playlist.m3u8"
        assert "cross-domain" in detail["message"]

    @pytest.mark.asyncio()
    async def test_redirect_limit_raises_from_helper(
        self, source_store: SourceStore,
    ) -> None:
        """リダイレクト回数上限到達で helper が HTTPStatusError を送出すること."""
        import httpx as _httpx

        request = _httpx.Request(
            "GET", "https://video.bsky.app/watch/playlist.m3u8",
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=MagicMock(
                status_code=302,
                headers={"location": "https://video.bsky.app/next"},
                request=request,
            ),
        )

        with pytest.raises(_httpx.HTTPStatusError) as exc_info:
            await BlueskyIngester._get_following_same_origin_redirect(
                mock_client,
                "https://video.bsky.app/watch/playlist.m3u8",
                _max_redirects=3,
            )
        assert "redirect limit" in str(exc_info.value)

    @pytest.mark.asyncio()
    async def test_redirect_limit_recorded_via_outer_except(
        self, source_store: SourceStore, tmp_path: Path,
    ) -> None:
        """リダイレクト上限到達の helper 例外が _download_media の outer except で
        partial_failures として記録されること."""
        import httpx as _httpx

        item = _make_feed_item()
        item["post"]["embed"] = {
            "$type": "app.bsky.embed.video#view",
            "playlist": "https://video.bsky.app/watch/playlist.m3u8",
        }

        request = _httpx.Request(
            "GET", "https://video.bsky.app/watch/playlist.m3u8",
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=MagicMock(
                status_code=302,
                headers={"location": "https://video.bsky.app/next"},
                request=request,
            ),
        )

        ingester = make_bluesky_ingester(source_store)
        result = IngestResult()
        await ingester._download_media(
            item,
            media_dir=tmp_path / "media",
            client=mock_client,
            result=result,
            rel_path="bluesky/did/2025/01/rkey.json",
        )

        assert result.partial_failures == 1
        detail = result.partial_failure_details[0]
        assert detail["category"] == "media_download"
        assert "redirect limit" in detail["message"]

    @pytest.mark.asyncio()
    async def test_redirect_without_location_raises(
        self, source_store: SourceStore,
    ) -> None:
        """3xx なのに location ヘッダが欠落している場合、helper が例外を送出すること."""
        import httpx as _httpx

        request = _httpx.Request(
            "GET", "https://video.bsky.app/watch/playlist.m3u8",
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=MagicMock(
                status_code=302,
                headers={},  # location なし
                request=request,
            ),
        )

        with pytest.raises(_httpx.HTTPStatusError) as exc_info:
            await BlueskyIngester._get_following_same_origin_redirect(
                mock_client,
                "https://video.bsky.app/watch/playlist.m3u8",
            )
        assert "location" in str(exc_info.value)

    @pytest.mark.asyncio()
    async def test_delegation_failure_in_follow_urls_recorded_as_errors(
        self, source_store: SourceStore,
    ) -> None:
        """follow_urls 経由の Web URL 委譲失敗が errors + category=delegation に計上されること."""
        item = _make_feed_item()
        item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://example.com/fail",
                    },
                ],
            },
        ]

        ingester = make_bluesky_ingester(source_store)
        error_detail = {
            "category": "delegation",
            "target": "https://example.com/fail",
            "url": "https://example.com/fail",
            "message": "site-ingest batch failed: boom",
        }
        result = IngestResult()
        with patch.object(
            ingester,
            "_fetch_web_urls",
            new_callable=AsyncMock,
            return_value=(0, 1, [error_detail]),
        ):
            stats = await ingester.follow_urls(
                [item],
                youtube_ingester=None,
                result=result,
            )

        assert stats["errors"] == 1
        assert result.errors == 1
        assert result.error_details == [error_detail]

    @pytest.mark.asyncio()
    async def test_youtube_delegation_failure_recorded_as_errors(
        self, source_store: SourceStore,
    ) -> None:
        """follow_urls 経由の YouTube 委譲失敗が errors + category=delegation に計上されること."""
        item = _make_feed_item()
        item["post"]["record"]["facets"] = [
            {
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": "https://www.youtube.com/watch?v=broken",
                    },
                ],
            },
        ]

        mock_yt = AsyncMock()
        mock_yt.ingest_video = AsyncMock(side_effect=RuntimeError("boom"))
        mock_yt.request_interval = 0

        ingester = make_bluesky_ingester(source_store)
        result = IngestResult()
        await ingester.follow_urls(
            [item],
            youtube_ingester=mock_yt,
            result=result,
        )

        assert result.errors == 1
        assert result.error_details[0]["category"] == "delegation"
        assert result.error_details[0]["url"] == (
            "https://www.youtube.com/watch?v=broken"
        )

    @pytest.mark.asyncio()
    async def test_placement_failure_uses_dict_error_detail(
        self, source_store: SourceStore,
    ) -> None:
        """投稿配置失敗時、error_details に dict が追加されること."""
        import httpx as _httpx

        item = _make_feed_item()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=_httpx.Response(
                200,
                json={"feed": [item], "cursor": None},
                request=_httpx.Request(
                    "GET", "https://example.com/xrpc/app.bsky.feed.getAuthorFeed",
                ),
            ),
        )

        ingester = make_bluesky_ingester(source_store)
        with patch.object(
            ingester._store,
            "place_file",
            side_effect=RuntimeError("disk full"),
        ):
            result, _ = await ingester.crawl_bluesky(
                "alice.bsky.social",
                max_posts=1,
                client=mock_client,
            )

        assert result.errors == 1
        assert result.error_details[0]["category"] == "placement"
        assert result.error_details[0]["message"] == "disk full"


@pytest.mark.asyncio()
class TestRunSiteIngestBatch:
    """_run_site_ingest_batch の JSON パーステスト."""

    async def test_parses_json_result(self, source_store: SourceStore) -> None:
        """JSON Lines の result 行から placed を正しく抽出すること."""
        ingester = make_bluesky_ingester(source_store)
        json_output = (
            '{"type": "progress", "processed": 1, "total": 1, "current": "https://example.com"}\n'
            '{"type": "result", "placed": 3, "overwritten": 0, "skipped": 1, "errors": 0, "elapsed": 1.5}\n'
        )
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (json_output.encode("utf-8"), b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await ingester._run_site_ingest_batch(["https://example.com"])

        assert result == 3

    async def test_returns_zero_when_no_result_line(self, source_store: SourceStore) -> None:
        """result 行がない場合 0 を返すこと."""
        ingester = make_bluesky_ingester(source_store)
        json_output = '{"type": "progress", "processed": 1, "total": 1, "current": "x"}\n'
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (json_output.encode("utf-8"), b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await ingester._run_site_ingest_batch(["https://example.com"])

        assert result == 0

    async def test_raises_on_nonzero_exit(self, source_store: SourceStore) -> None:
        """subprocess の exit code != 0 で RuntimeError を送出すること."""
        ingester = make_bluesky_ingester(source_store)
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"some error")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="site-ingest failed"):
                await ingester._run_site_ingest_batch(["https://example.com"])

    async def test_passes_output_json_flag(self, source_store: SourceStore) -> None:
        """CLI コマンドに --output json フラグが含まれること."""
        ingester = make_bluesky_ingester(source_store)
        json_output = '{"type": "result", "placed": 0}\n'
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (json_output.encode("utf-8"), b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await ingester._run_site_ingest_batch(["https://example.com"])

        cmd_args = mock_exec.call_args[0]
        output_index = cmd_args.index("--output")
        assert cmd_args[output_index + 1] == "json"


# ===========================================================================
# parse_bluesky_url
# ===========================================================================


class TestParseBlueskyUrl:
    """BlueSky URL パーサーのテスト."""

    def test_valid_url_returns_handle_and_rkey(self) -> None:
        result = parse_bluesky_url("https://bsky.app/profile/alice.bsky.social/post/abc123")
        assert result == ("alice.bsky.social", "abc123")

    def test_valid_url_with_custom_handle(self) -> None:
        result = parse_bluesky_url("https://bsky.app/profile/custom.domain.example/post/xyz789")
        assert result == ("custom.domain.example", "xyz789")

    def test_invalid_url_returns_none(self) -> None:
        assert parse_bluesky_url("https://example.com/not-bluesky") is None

    def test_profile_only_url_returns_none(self) -> None:
        assert parse_bluesky_url("https://bsky.app/profile/alice.bsky.social") is None

    def test_url_with_trailing_whitespace(self) -> None:
        result = parse_bluesky_url("  https://bsky.app/profile/alice.bsky.social/post/abc123  ")
        assert result == ("alice.bsky.social", "abc123")


# ===========================================================================
# ingest_posts
# ===========================================================================


class TestIngestPosts:
    """ingest_posts のテスト."""

    @pytest.mark.asyncio
    async def test_ingest_single_post_overwrites(self, source_store: SourceStore) -> None:
        """既存ファイルが上書きされることを確認する."""
        ingester = make_bluesky_ingester(source_store)

        # resolveHandle レスポンス
        resolve_resp = MagicMock()
        resolve_resp.json.return_value = {"did": "did:plc:abc123"}
        resolve_resp.is_success = True

        # getPosts レスポンス
        posts_resp = MagicMock()
        posts_resp.json.return_value = {
            "posts": [{
                "uri": "at://did:plc:abc123/app.bsky.feed.post/xyz789",
                "cid": "test-cid",
                "author": {"did": "did:plc:abc123", "handle": "alice.bsky.social", "displayName": "Alice"},
                "record": {"text": "Updated post", "createdAt": "2026-01-15T09:00:00Z"},
            }],
        }
        posts_resp.is_success = True

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[resolve_resp, posts_resp])

        result = await ingester.ingest_posts(
            ["https://bsky.app/profile/alice.bsky.social/post/xyz789"],
            client=client,
        )

        assert result.placed == 1
        assert result.errors == 0

    @pytest.mark.asyncio
    async def test_ingest_invalid_url_reports_error(self, source_store: SourceStore) -> None:
        """無効な URL がエラーとして計上されることを確認する."""
        ingester = make_bluesky_ingester(source_store)
        client = AsyncMock()

        result = await ingester.ingest_posts(
            ["https://example.com/not-bluesky"],
            client=client,
        )

        assert result.errors == 1
        assert result.error_details[0]["category"] == "metadata_fetch"

    @pytest.mark.asyncio
    async def test_ingest_deleted_post_reports_error(self, source_store: SourceStore) -> None:
        """削除済み投稿で空の posts が返された場合のエラー."""
        ingester = make_bluesky_ingester(source_store)

        resolve_resp = MagicMock()
        resolve_resp.json.return_value = {"did": "did:plc:abc123"}
        resolve_resp.is_success = True

        posts_resp = MagicMock()
        posts_resp.json.return_value = {"posts": []}
        posts_resp.is_success = True

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[resolve_resp, posts_resp])

        result = await ingester.ingest_posts(
            ["https://bsky.app/profile/alice.bsky.social/post/deleted123"],
            client=client,
        )

        assert result.errors == 1
        assert "not found" in result.error_details[0]["message"].lower()

    @pytest.mark.asyncio
    async def test_ingest_multiple_urls(self, source_store: SourceStore) -> None:
        """複数 URL を処理し、1 件の失敗が他に影響しないことを確認する."""
        ingester = make_bluesky_ingester(source_store)

        resolve_resp = MagicMock()
        resolve_resp.json.return_value = {"did": "did:plc:abc123"}
        resolve_resp.is_success = True

        posts_resp_ok = MagicMock()
        posts_resp_ok.json.return_value = {
            "posts": [{
                "uri": "at://did:plc:abc123/app.bsky.feed.post/ok1",
                "cid": "cid-ok",
                "author": {"did": "did:plc:abc123", "handle": "alice.bsky.social", "displayName": "Alice"},
                "record": {"text": "OK post", "createdAt": "2026-01-15T09:00:00Z"},
            }],
        }
        posts_resp_ok.is_success = True

        posts_resp_empty = MagicMock()
        posts_resp_empty.json.return_value = {"posts": []}
        posts_resp_empty.is_success = True

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[resolve_resp, posts_resp_ok, posts_resp_empty])

        result = await ingester.ingest_posts(
            [
                "https://bsky.app/profile/alice.bsky.social/post/ok1",
                "https://bsky.app/profile/alice.bsky.social/post/deleted1",
            ],
            client=client,
        )

        assert result.placed == 1
        assert result.errors == 1

    @pytest.mark.asyncio
    async def test_ingest_empty_urls_returns_empty_result(self, source_store: SourceStore) -> None:
        """空の URL リストで空の結果が返ることを確認する."""
        ingester = make_bluesky_ingester(source_store)
        client = AsyncMock()

        result = await ingester.ingest_posts([], client=client)

        assert result.placed == 0
        assert result.errors == 0

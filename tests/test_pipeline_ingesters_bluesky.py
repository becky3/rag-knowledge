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

from rag.pipeline.ingesters.bluesky import (
    MAX_POSTS_HARD_LIMIT,
    _escape_did,
    _ext_from_content_type,
    _extract_media_urls,
    _make_title,
    _validate_max_posts,
    classify_url,
    extract_urls_from_item,
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
        with patch.object(ingester, "_fetch_web_urls", new_callable=AsyncMock, return_value=(1, 0)) as mock_fetch:
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
        # _fetch_web_urls はバッチエラー隔離を行い、エラー件数を返す
        with patch.object(ingester, "_fetch_web_urls", new_callable=AsyncMock, return_value=(0, 1)):
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
        with patch.object(ingester, "_fetch_web_urls", new_callable=AsyncMock, return_value=(1, 0)) as mock_fetch:
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

        # 3 回目: force=True → 上書き
        client3 = _make_mock_client([{"feed": [item]}])
        result3, _ = await ingester.crawl_bluesky(
            "alice.bsky.social", force=True, client=client3,
        )
        assert result3.placed == 1
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

        assert result.placed == 1
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
class TestFollowUrlsForce:
    """follow_urls の force モード関連テスト."""

    async def test_force_youtube_reingest_disabled(
        self, source_store: SourceStore,
    ) -> None:
        """force=True かつ force_youtube_reingest=False で YouTube がスキップされること."""
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
            force=True,
            force_youtube_reingest=False,
        )

        # YouTube は呼ばれずスキップされる
        mock_yt.ingest_video.assert_not_called()
        assert stats["skipped"] == 1
        assert stats["youtube_placed"] == 0

    async def test_force_youtube_reingest_enabled(
        self, source_store: SourceStore,
    ) -> None:
        """force=True かつ force_youtube_reingest=True で YouTube が再取り込みされること."""
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
        mock_yt.request_interval = 5.0

        ingester = make_bluesky_ingester(source_store)
        stats = await ingester.follow_urls(
            [item],
            youtube_ingester=mock_yt,
            force=True,
            force_youtube_reingest=True,
        )

        mock_yt.ingest_video.assert_called_once()
        assert stats["youtube_placed"] == 1

"""Zenn インジェスターのテスト.

仕様: docs/specs/ingesters/zenn.md

テスト方針:
- 入力バリデーション（username、max_articles、content_type）
- 記事の取得と配置
- スクラップの取得と配置
- content_type="all" で両方取得
- .meta サイドカーファイルの生成
- article オブジェクトが空の記事のスキップ
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.pipeline.ingesters.zenn import (
    MAX_ARTICLES_HARD_LIMIT,
    parse_zenn_url,
)
from rag.store.source_store import SourceStore

from factories import make_zenn_ingester


@pytest.fixture()
def source_store(tmp_path: Path) -> SourceStore:
    """テスト用 SourceStore を生成する."""
    store = SourceStore(tmp_path / "source_store")
    store.initialize()
    return store


def _make_article_list_response(
    slugs: list[str],
    *,
    next_page: int | None = None,
) -> dict:
    """記事一覧 API のレスポンスを生成する."""
    return {
        "articles": [
            {
                "slug": slug,
                "title": f"Article: {slug}",
                "path": f"/testuser/articles/{slug}",
                "article_type": "tech",
                "published_at": "2026-01-10T12:00:00+09:00",
                "liked_count": 10,
            }
            for slug in slugs
        ],
        "next_page": next_page,
    }


def _make_article_detail_response(
    slug: str,
    *,
    body_html: str = "<p>Article content</p>",
) -> dict:
    """記事詳細 API のレスポンスを生成する."""
    return {
        "article": {
            "slug": slug,
            "title": f"Article: {slug}",
            "path": f"/testuser/articles/{slug}",
            "article_type": "tech",
            "published_at": "2026-01-10T12:00:00+09:00",
            "liked_count": 10,
            "body_html": body_html,
            "topics": [
                {"display_name": "Python", "name": "python"},
                {"display_name": "FastAPI", "name": "fastapi"},
            ],
        },
    }


def _make_scrap_list_response(
    slugs: list[str],
    *,
    next_page: int | None = None,
) -> dict:
    """スクラップ一覧 API のレスポンスを生成する."""
    return {
        "scraps": [
            {
                "slug": slug,
                "title": f"Scrap: {slug}",
                "path": f"/testuser/scraps/{slug}",
                "closed": False,
                "comments_count": 2,
                "created_at": "2026-02-14T20:48:17+09:00",
                "liked_count": 0,
                "topics": [],
            }
            for slug in slugs
        ],
        "next_page": next_page,
    }


def _make_scrap_detail_response(slug: str) -> dict:
    """スクラップ詳細 API のレスポンスを生成する."""
    return {
        "scrap": {
            "slug": slug,
            "title": f"Scrap: {slug}",
            "path": f"/testuser/scraps/{slug}",
            "closed": False,
            "comments_count": 2,
            "created_at": "2026-02-14T20:48:17+09:00",
            "liked_count": 0,
            "topics": [],
            "comments": [
                {"body_html": "<p>Comment 1</p>", "created_at": "2026-02-14T21:00:00+09:00"},
                {"body_html": "<p>Comment 2</p>", "created_at": "2026-02-14T22:00:00+09:00"},
            ],
        },
    }


def _make_mock_client(responses: list[dict]) -> AsyncMock:
    """レスポンスリスト順で返すモック ConstrainedClient を生成する."""
    client = AsyncMock()
    mocks = []
    for resp_data in responses:
        resp = MagicMock()
        resp.json.return_value = resp_data
        mocks.append(resp)
    client.get = AsyncMock(side_effect=mocks)
    return client


@pytest.mark.asyncio()
class TestInputValidation:
    """入力バリデーションのテスト."""

    async def test_empty_username_raises(
        self, source_store: SourceStore
    ) -> None:
        """空の username でエラーになること."""
        ingester = make_zenn_ingester(source_store)
        with pytest.raises(ValueError, match="空"):
            await ingester.crawl_zenn("", client=AsyncMock())

    async def test_invalid_content_type_raises(
        self, source_store: SourceStore
    ) -> None:
        """無効な content_type でエラーになること."""
        ingester = make_zenn_ingester(source_store)
        with pytest.raises(ValueError, match="content_type"):
            await ingester.crawl_zenn(
                "testuser",
                content_type="invalid",
                client=AsyncMock(),
            )

    async def test_no_client_raises(
        self, source_store: SourceStore
    ) -> None:
        """client 未指定でエラーになること."""
        ingester = make_zenn_ingester(source_store)
        with pytest.raises(ValueError, match="client"):
            await ingester.crawl_zenn("testuser")

    async def test_max_articles_zero_raises(
        self, source_store: SourceStore
    ) -> None:
        """max_articles=0 でエラーになること."""
        ingester = make_zenn_ingester(source_store)
        with pytest.raises(ValueError, match="1 以上"):
            await ingester.crawl_zenn(
                "testuser",
                max_articles=0,
                client=AsyncMock(),
            )

    async def test_max_articles_negative_raises(
        self, source_store: SourceStore
    ) -> None:
        """max_articles が負数でエラーになること."""
        ingester = make_zenn_ingester(source_store)
        with pytest.raises(ValueError, match="1 以上"):
            await ingester.crawl_zenn(
                "testuser",
                max_articles=-1,
                client=AsyncMock(),
            )

    async def test_max_articles_bool_raises(
        self, source_store: SourceStore
    ) -> None:
        """max_articles に bool でエラーになること."""
        ingester = make_zenn_ingester(source_store)
        with pytest.raises(TypeError, match="整数"):
            await ingester.crawl_zenn(
                "testuser",
                max_articles=True,
                client=AsyncMock(),
            )

    async def test_max_articles_clamp(
        self, source_store: SourceStore
    ) -> None:
        """max_articles がハードリミットにクランプされること."""
        # articles=[] でリクエストなし
        client = _make_mock_client([{"articles": [], "next_page": None}])
        ingester = make_zenn_ingester(source_store)
        result = await ingester.crawl_zenn(
            "testuser",
            max_articles=MAX_ARTICLES_HARD_LIMIT + 50,
            content_type="articles",
            client=client,
        )
        # クランプされて正常終了すること
        assert result.errors == 0


@pytest.mark.asyncio()
class TestCrawlArticles:
    """記事取得のテスト."""

    async def test_basic_articles(self, source_store: SourceStore) -> None:
        """記事が正常に取得・配置されること."""
        client = _make_mock_client([
            _make_article_list_response(["test-article"]),
            _make_article_detail_response("test-article"),
        ])

        ingester = make_zenn_ingester(source_store, max_articles=3)
        result = await ingester.crawl_zenn(
            "testuser",
            content_type="articles",
            client=client,
        )

        assert result.placed == 1
        assert result.errors == 0

        # JSON ファイル検証
        json_path = (
            source_store.root_dir / "zenn" / "testuser" / "articles" / "test-article.json"
        )
        assert json_path.exists()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["slug"] == "test-article"
        assert data["body_html"] == "<p>Article content</p>"

    async def test_empty_article_skipped(
        self, source_store: SourceStore
    ) -> None:
        """article オブジェクトが空の記事がスキップされること."""
        empty_response = {"article": {}}
        client = _make_mock_client([
            _make_article_list_response(["empty-article"]),
            empty_response,
        ])

        ingester = make_zenn_ingester(source_store, max_articles=3)
        result = await ingester.crawl_zenn(
            "testuser",
            content_type="articles",
            client=client,
        )

        assert result.placed == 0
        assert result.skipped == 1

    async def test_article_meta_fields(
        self, source_store: SourceStore
    ) -> None:
        """.meta のフィールドが正しいこと."""
        client = _make_mock_client([
            _make_article_list_response(["test-article"]),
            _make_article_detail_response("test-article"),
        ])

        ingester = make_zenn_ingester(source_store, max_articles=3)
        await ingester.crawl_zenn(
            "testuser",
            content_type="articles",
            client=client,
        )

        import yaml

        meta_path = (
            source_store.root_dir
            / "zenn"
            / "testuser"
            / "articles"
            / "test-article.json.meta"
        )
        assert meta_path.exists()
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))

        assert meta["url"] == "https://zenn.dev/testuser/articles/test-article"
        assert meta["source_type"] == "zenn"
        assert meta["content_type"] == "article"
        assert meta["article_type"] == "tech"
        assert meta["username"] == "testuser"
        assert meta["topics"] == ["Python", "FastAPI"]
        assert meta["comments_count"] == 0
        assert meta["closed"] is False


@pytest.mark.asyncio()
class TestCrawlScraps:
    """スクラップ取得のテスト."""

    async def test_basic_scraps(self, source_store: SourceStore) -> None:
        """スクラップが正常に取得・配置されること."""
        client = _make_mock_client([
            _make_scrap_list_response(["test-scrap"]),
            _make_scrap_detail_response("test-scrap"),
        ])

        ingester = make_zenn_ingester(source_store, max_articles=3)
        result = await ingester.crawl_zenn(
            "testuser",
            content_type="scraps",
            client=client,
        )

        assert result.placed == 1
        assert result.errors == 0

        # JSON 検証
        json_path = (
            source_store.root_dir / "zenn" / "testuser" / "scraps" / "test-scrap.json"
        )
        assert json_path.exists()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["comments_count"] == 2

    async def test_scrap_meta_fields(
        self, source_store: SourceStore
    ) -> None:
        """.meta のフィールドが正しいこと."""
        client = _make_mock_client([
            _make_scrap_list_response(["test-scrap"]),
            _make_scrap_detail_response("test-scrap"),
        ])

        ingester = make_zenn_ingester(source_store, max_articles=3)
        await ingester.crawl_zenn(
            "testuser",
            content_type="scraps",
            client=client,
        )

        import yaml

        meta_path = (
            source_store.root_dir
            / "zenn"
            / "testuser"
            / "scraps"
            / "test-scrap.json.meta"
        )
        assert meta_path.exists()
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))

        assert meta["url"] == "https://zenn.dev/testuser/scraps/test-scrap"
        assert meta["source_type"] == "zenn"
        assert meta["content_type"] == "scrap"
        assert meta["article_type"] == ""
        assert meta["username"] == "testuser"
        assert meta["comments_count"] == 2
        assert meta["closed"] is False


@pytest.mark.asyncio()
class TestCrawlAll:
    """content_type="all" のテスト."""

    async def test_all_content_type(self, source_store: SourceStore) -> None:
        """articles + scraps が両方取得されること."""
        client = _make_mock_client([
            # 記事一覧
            _make_article_list_response(["art1"]),
            # 記事詳細
            _make_article_detail_response("art1"),
            # スクラップ一覧
            _make_scrap_list_response(["scr1"]),
            # スクラップ詳細
            _make_scrap_detail_response("scr1"),
        ])

        ingester = make_zenn_ingester(source_store, max_articles=3)
        result = await ingester.crawl_zenn(
            "testuser",
            content_type="all",
            client=client,
        )

        assert result.placed == 2  # 記事 1 + スクラップ 1

    async def test_empty_articles(self, source_store: SourceStore) -> None:
        """記事が 0 件でも正常終了すること."""
        client = _make_mock_client([
            {"articles": [], "next_page": None},
        ])

        ingester = make_zenn_ingester(source_store, max_articles=3)
        result = await ingester.crawl_zenn(
            "testuser",
            content_type="articles",
            client=client,
        )

        assert result.placed == 0
        assert result.errors == 0


@pytest.mark.asyncio()
class TestSkipMode:
    """スキップモード（デフォルト）のテスト."""

    async def test_skip_existing_article(self, source_store: SourceStore) -> None:
        """既存ファイルがある記事はスキップされること."""
        # 事前に記事ファイルを配置
        rel_path = source_store.root_dir / "zenn" / "testuser" / "articles" / "existing.json"
        rel_path.parent.mkdir(parents=True, exist_ok=True)
        rel_path.write_text('{"slug": "existing"}', encoding="utf-8")

        # 一覧 API は "existing" を返すが、詳細 API は呼ばれないはず
        client = _make_mock_client([
            _make_article_list_response(["existing"]),
        ])

        ingester = make_zenn_ingester(source_store, max_articles=3)
        result = await ingester.crawl_zenn(
            "testuser",
            content_type="articles",
            client=client,
        )

        assert result.placed == 0
        assert result.skipped == 1
        # 詳細 API は呼ばれない（一覧 API の 1 回のみ）
        assert client.get.call_count == 1

    async def test_skip_existing_scrap(self, source_store: SourceStore) -> None:
        """既存ファイルがあるスクラップはスキップされること."""
        rel_path = source_store.root_dir / "zenn" / "testuser" / "scraps" / "existing.json"
        rel_path.parent.mkdir(parents=True, exist_ok=True)
        rel_path.write_text('{"slug": "existing"}', encoding="utf-8")

        client = _make_mock_client([
            _make_scrap_list_response(["existing"]),
        ])

        ingester = make_zenn_ingester(source_store, max_articles=3)
        result = await ingester.crawl_zenn(
            "testuser",
            content_type="scraps",
            client=client,
        )

        assert result.placed == 0
        assert result.skipped == 1
        assert client.get.call_count == 1

    async def test_force_overwrites_existing(self, source_store: SourceStore) -> None:
        """force=True で既存ファイルが上書きされ overwritten に計上されること（排他計上）."""
        rel_path = source_store.root_dir / "zenn" / "testuser" / "articles" / "existing.json"
        rel_path.parent.mkdir(parents=True, exist_ok=True)
        rel_path.write_text('{"slug": "old"}', encoding="utf-8")

        client = _make_mock_client([
            _make_article_list_response(["existing"]),
            _make_article_detail_response("existing"),
        ])

        ingester = make_zenn_ingester(source_store, max_articles=3)
        result = await ingester.crawl_zenn(
            "testuser",
            content_type="articles",
            force=True,
            client=client,
        )

        assert result.placed == 0
        assert result.overwritten == 1
        assert result.skipped == 0
        # 上書きされた内容を確認
        data = json.loads(rel_path.read_text(encoding="utf-8"))
        assert data["slug"] == "existing"
        assert "body_html" in data

    async def test_force_overwrites_existing_scrap(self, source_store: SourceStore) -> None:
        """force=True で既存スクラップが上書きされ overwritten に計上されること（排他計上）."""
        rel_path = source_store.root_dir / "zenn" / "testuser" / "scraps" / "existing.json"
        rel_path.parent.mkdir(parents=True, exist_ok=True)
        rel_path.write_text('{"slug": "old"}', encoding="utf-8")

        client = _make_mock_client([
            _make_scrap_list_response(["existing"]),
            _make_scrap_detail_response("existing"),
        ])

        ingester = make_zenn_ingester(source_store, max_articles=3)
        result = await ingester.crawl_zenn(
            "testuser",
            content_type="scraps",
            force=True,
            client=client,
        )

        assert result.placed == 0
        assert result.overwritten == 1
        assert result.skipped == 0
        data = json.loads(rel_path.read_text(encoding="utf-8"))
        assert data["comments_count"] == 2

    async def test_mixed_new_and_existing(self, source_store: SourceStore) -> None:
        """新規と既存が混在する場合、既存のみスキップされること."""
        existing_path = source_store.root_dir / "zenn" / "testuser" / "articles" / "old.json"
        existing_path.parent.mkdir(parents=True, exist_ok=True)
        existing_path.write_text('{"slug": "old"}', encoding="utf-8")

        client = _make_mock_client([
            _make_article_list_response(["old", "new-article"]),
            # "old" はスキップされるので詳細 API は "new-article" のみ
            _make_article_detail_response("new-article"),
        ])

        ingester = make_zenn_ingester(source_store, max_articles=3)
        result = await ingester.crawl_zenn(
            "testuser",
            content_type="articles",
            client=client,
        )

        assert result.placed == 1
        assert result.skipped == 1


@pytest.mark.asyncio()
class TestErrorDetailsStructured:
    """error_details dict 化の検証."""

    async def test_article_fetch_http_error_dict(
        self, source_store: SourceStore
    ) -> None:
        """記事詳細 API の HTTP エラーで error_details に dict が積まれる."""
        import httpx

        # 一覧取得は成功、詳細取得で 500 エラー
        list_resp = MagicMock()
        list_resp.json.return_value = _make_article_list_response(["bad-slug"])
        err_req = httpx.Request(
            "GET", "https://zenn.dev/api/articles/bad-slug",
        )
        err_resp = httpx.Response(500, content=b"error", request=err_req)

        client = AsyncMock()
        call_count = [0]

        async def _mock_get(url: str) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                return list_resp
            return err_resp

        client.get = AsyncMock(side_effect=_mock_get)

        ingester = make_zenn_ingester(source_store, max_articles=3)
        result = await ingester.crawl_zenn(
            "testuser",
            content_type="articles",
            client=client,
        )

        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "metadata_fetch"
        assert detail["target"] == "articles/bad-slug"
        assert detail["status"] == 500
        assert "url" in detail
        assert "message" in detail

    async def test_scrap_fetch_non_http_exception_dict(
        self, source_store: SourceStore
    ) -> None:
        """HTTPStatusError 以外の例外でも error_details に dict が積まれる（status なし）."""
        list_resp = MagicMock()
        list_resp.json.return_value = _make_scrap_list_response(["bad-scrap"])

        client = AsyncMock()
        call_count = [0]

        async def _mock_get(url: str) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                return list_resp
            raise ValueError("Parse error")

        client.get = AsyncMock(side_effect=_mock_get)

        ingester = make_zenn_ingester(source_store, max_articles=3)
        result = await ingester.crawl_zenn(
            "testuser",
            content_type="scraps",
            client=client,
        )

        assert result.errors == 1
        detail = result.error_details[0]
        assert detail["category"] == "metadata_fetch"
        assert detail["target"] == "scraps/bad-scrap"
        # 非 HTTP 例外では status / url は付与されない
        assert "status" not in detail
        assert "Parse error" in detail["message"]

    async def test_place_file_failure_category_is_placement(
        self,
        source_store: SourceStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """記事の place_file 失敗時は category='placement' で記録される."""
        # 一覧 + 詳細取得は成功、place_file で OSError
        client = _make_mock_client([
            _make_article_list_response(["good-slug"]),
            _make_article_detail_response("good-slug"),
        ])

        def _raise_oserror(**kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(source_store, "place_file", _raise_oserror)

        ingester = make_zenn_ingester(source_store, max_articles=3)
        result = await ingester.crawl_zenn(
            "testuser",
            content_type="articles",
            client=client,
        )

        assert result.errors == 1
        assert result.placed == 0
        detail = result.error_details[0]
        assert detail["category"] == "placement"
        assert detail["target"] == "zenn/testuser/articles/good-slug.json"
        assert "disk full" in detail["message"]


# ===========================================================================
# parse_zenn_url
# ===========================================================================


class TestParseZennUrl:
    """Zenn URL パーサーのテスト."""

    def test_article_url(self) -> None:
        result = parse_zenn_url("https://zenn.dev/alice/articles/my-post")
        assert result == ("alice", "articles", "my-post")

    def test_scrap_url(self) -> None:
        result = parse_zenn_url("https://zenn.dev/alice/scraps/abc123")
        assert result == ("alice", "scraps", "abc123")

    def test_invalid_url_returns_none(self) -> None:
        assert parse_zenn_url("https://example.com/not-zenn") is None

    def test_missing_slug_returns_none(self) -> None:
        assert parse_zenn_url("https://zenn.dev/alice/articles/") is None

    def test_url_with_query_params(self) -> None:
        result = parse_zenn_url("https://zenn.dev/alice/articles/my-post?ref=feed")
        assert result == ("alice", "articles", "my-post")

    def test_url_with_trailing_whitespace(self) -> None:
        result = parse_zenn_url("  https://zenn.dev/alice/articles/my-post  ")
        assert result == ("alice", "articles", "my-post")


# ===========================================================================
# ingest_contents
# ===========================================================================


def _make_ingest_article_response(slug: str = "test-slug", username: str = "testuser") -> dict:
    """ingest テスト用の記事詳細 API モックレスポンス."""
    return {
        "article": {
            "slug": slug,
            "title": "Test Article",
            "path": f"/{username}/articles/{slug}",
            "article_type": "tech",
            "published_at": "2026-01-10T12:00:00+09:00",
            "liked_count": 5,
            "topics": [{"name": "Python", "display_name": "Python"}],
            "body_html": "<p>Test body</p>",
        },
    }


def _make_ingest_scrap_response(slug: str = "test-scrap", username: str = "testuser") -> dict:
    """ingest テスト用のスクラップ詳細 API モックレスポンス."""
    return {
        "scrap": {
            "slug": slug,
            "title": "Test Scrap",
            "path": f"/{username}/scraps/{slug}",
            "created_at": "2026-02-14T20:48:17+09:00",
            "liked_count": 0,
            "topics": [],
            "comments_count": 2,
            "closed": False,
            "comments": [{"body_html": "<p>Comment 1</p>", "created_at": "2026-02-14T21:00:00+09:00"}],
        },
    }


class TestIngestContents:
    """ingest_contents のテスト."""

    @pytest.mark.asyncio
    async def test_ingest_article_places_file(self, source_store: SourceStore) -> None:
        """記事 URL で新規配置されることを確認する."""
        ingester = make_zenn_ingester(source_store)

        resp = MagicMock()
        resp.json.return_value = _make_ingest_article_response("my-post", "alice")
        resp.is_success = True

        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        result = await ingester.ingest_contents(
            ["https://zenn.dev/alice/articles/my-post"],
            client=client,
        )

        assert result.placed == 1
        assert result.errors == 0
        assert (source_store.root_dir / "zenn/alice/articles/my-post.json").exists()

    @pytest.mark.asyncio
    async def test_ingest_scrap_places_file(self, source_store: SourceStore) -> None:
        """スクラップ URL で新規配置されることを確認する."""
        ingester = make_zenn_ingester(source_store)

        resp = MagicMock()
        resp.json.return_value = _make_ingest_scrap_response("abc123", "alice")
        resp.is_success = True

        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        result = await ingester.ingest_contents(
            ["https://zenn.dev/alice/scraps/abc123"],
            client=client,
        )

        assert result.placed == 1
        assert result.errors == 0
        assert (source_store.root_dir / "zenn/alice/scraps/abc123.json").exists()

    @pytest.mark.asyncio
    async def test_ingest_overwrites_existing(self, source_store: SourceStore) -> None:
        """既存ファイルが上書きされることを確認する."""
        ingester = make_zenn_ingester(source_store)

        rel_path = "zenn/alice/articles/my-post.json"
        dest = source_store.root_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text('{"old": "data"}')

        resp = MagicMock()
        resp.json.return_value = _make_ingest_article_response("my-post", "alice")
        resp.is_success = True

        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        result = await ingester.ingest_contents(
            ["https://zenn.dev/alice/articles/my-post"],
            client=client,
        )

        assert result.overwritten == 1
        assert result.placed == 0

    @pytest.mark.asyncio
    async def test_ingest_invalid_url_reports_error(self, source_store: SourceStore) -> None:
        """無効な URL がエラーとして計上されることを確認する."""
        ingester = make_zenn_ingester(source_store)
        client = AsyncMock()

        result = await ingester.ingest_contents(
            ["https://example.com/not-zenn"],
            client=client,
        )

        assert result.errors == 1
        assert result.error_details[0]["category"] == "metadata_fetch"

    @pytest.mark.asyncio
    async def test_ingest_multiple_urls_independent(self, source_store: SourceStore) -> None:
        """複数 URL を処理し、1 件の失敗が他に影響しないことを確認する."""
        ingester = make_zenn_ingester(source_store)

        ok_resp = MagicMock()
        ok_resp.json.return_value = _make_ingest_article_response("ok-post", "alice")
        ok_resp.is_success = True

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[ok_resp, Exception("API error")])

        result = await ingester.ingest_contents(
            [
                "https://zenn.dev/alice/articles/ok-post",
                "https://zenn.dev/alice/articles/fail-post",
            ],
            client=client,
        )

        assert result.placed == 1
        assert result.errors == 1

    @pytest.mark.asyncio
    async def test_ingest_empty_urls_returns_empty_result(self, source_store: SourceStore) -> None:
        """空の URL リストで空の結果が返ることを確認する."""
        ingester = make_zenn_ingester(source_store)
        client = AsyncMock()

        result = await ingester.ingest_contents([], client=client)

        assert result.placed == 0
        assert result.errors == 0

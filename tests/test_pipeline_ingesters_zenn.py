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
from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.pipeline.ingesters.zenn import (
    MAX_ARTICLES_HARD_LIMIT,
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
        """force=True で既存ファイルが上書きされること."""
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

        assert result.placed == 1
        assert result.skipped == 0
        # 上書きされた内容を確認
        data = json.loads(rel_path.read_text(encoding="utf-8"))
        assert data["slug"] == "existing"
        assert "body_html" in data

    async def test_force_overwrites_existing_scrap(self, source_store: SourceStore) -> None:
        """force=True で既存スクラップが上書きされること."""
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

        assert result.placed == 1
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

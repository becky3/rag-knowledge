"""MCP tool への defer_indexing パラメータ + bulk 受付の振る舞いテスト.

Issue #757 / 仕様: docs/specs/rag-knowledge.md 各 rag_* tool セクション
                   docs/specs/ingesters/common.md 「`--no-pipeline` フラグ共通仕様」

検証観点:
- 全 rag_add_* / rag_crawl_* / rag_crawl_documents で defer_indexing=True 時に
  CLI subprocess の引数に `--no-pipeline` が含まれること
- bulk 化対象 (rag_add_youtube/aozora/bluesky/zenn) で list[str] 受付が動作
- bulk 化対象で空 list がエラーメッセージを返すこと
"""

from __future__ import annotations

from importlib import import_module
from unittest.mock import AsyncMock, patch

import pytest


_MOCK_RESULT = {"placed": 1, "skipped": 0, "overwritten": 0, "errors": 0}


@pytest.fixture
def _patch_cli_subprocess():
    """各 MCP tool で _run_cli_subprocess を mock 化する fixture."""
    with (
        patch(
            "rag.server.cli_subprocess._run_cli_subprocess",
            new_callable=AsyncMock,
            return_value=_MOCK_RESULT,
        ) as mock_cli,
        patch("rag.server.cli_subprocess._format_cli_ingest_result", return_value="OK"),
    ):
        yield mock_cli


class TestDeferIndexingFlag:
    """全 add-/crawl- tool で defer_indexing=True が --no-pipeline に変換されること."""

    @pytest.mark.asyncio
    async def test_rag_add_youtube_defer(self, _patch_cli_subprocess) -> None:
        mod = import_module("rag.server")
        await mod.rag_add_youtube(
            video_urls=["https://youtu.be/aaa"], defer_indexing=True,
        )
        cli_args = _patch_cli_subprocess.call_args[0][1]
        assert "--no-pipeline" in cli_args

    @pytest.mark.asyncio
    async def test_rag_add_youtube_default_no_flag(self, _patch_cli_subprocess) -> None:
        mod = import_module("rag.server")
        await mod.rag_add_youtube(video_urls=["https://youtu.be/aaa"])
        cli_args = _patch_cli_subprocess.call_args[0][1]
        assert "--no-pipeline" not in cli_args

    @pytest.mark.asyncio
    async def test_rag_crawl_youtube_defer(self, _patch_cli_subprocess) -> None:
        mod = import_module("rag.server")
        await mod.rag_crawl_youtube(
            playlist_url="https://example.com/pl", defer_indexing=True,
        )
        cli_args = _patch_cli_subprocess.call_args[0][1]
        assert "--no-pipeline" in cli_args

    @pytest.mark.asyncio
    async def test_rag_add_aozora_defer(self, _patch_cli_subprocess) -> None:
        mod = import_module("rag.server")
        await mod.rag_add_aozora(book_ids=["12345"], defer_indexing=True)
        cli_args = _patch_cli_subprocess.call_args[0][1]
        assert "--no-pipeline" in cli_args

    @pytest.mark.asyncio
    async def test_rag_crawl_aozora_defer(self, _patch_cli_subprocess) -> None:
        mod = import_module("rag.server")
        await mod.rag_crawl_aozora(person_id="00001", defer_indexing=True)
        cli_args = _patch_cli_subprocess.call_args[0][1]
        assert "--no-pipeline" in cli_args

    @pytest.mark.asyncio
    async def test_rag_add_bluesky_defer(self, _patch_cli_subprocess) -> None:
        mod = import_module("rag.server")
        await mod.rag_add_bluesky(
            urls=["https://bsky.app/profile/u/post/x"], defer_indexing=True,
        )
        cli_args = _patch_cli_subprocess.call_args[0][1]
        assert "--no-pipeline" in cli_args

    @pytest.mark.asyncio
    async def test_rag_crawl_bluesky_defer(self, _patch_cli_subprocess) -> None:
        mod = import_module("rag.server")
        await mod.rag_crawl_bluesky(handle="u.bsky.social", defer_indexing=True)
        cli_args = _patch_cli_subprocess.call_args[0][1]
        assert "--no-pipeline" in cli_args

    @pytest.mark.asyncio
    async def test_rag_add_zenn_defer(self, _patch_cli_subprocess) -> None:
        mod = import_module("rag.server")
        await mod.rag_add_zenn(
            urls=["https://zenn.dev/u/articles/x"], defer_indexing=True,
        )
        cli_args = _patch_cli_subprocess.call_args[0][1]
        assert "--no-pipeline" in cli_args

    @pytest.mark.asyncio
    async def test_rag_crawl_zenn_defer(self, _patch_cli_subprocess) -> None:
        mod = import_module("rag.server")
        await mod.rag_crawl_zenn(username="u", defer_indexing=True)
        cli_args = _patch_cli_subprocess.call_args[0][1]
        assert "--no-pipeline" in cli_args


class TestBulkListExpansion:
    """bulk 化対象で list[str] が CLI 引数として展開されること."""

    @pytest.mark.asyncio
    async def test_rag_add_youtube_multiple_urls(self, _patch_cli_subprocess) -> None:
        mod = import_module("rag.server")
        await mod.rag_add_youtube(
            video_urls=["https://youtu.be/a", "https://youtu.be/b", "https://youtu.be/c"],
        )
        cli_args = _patch_cli_subprocess.call_args[0][1]
        for u in ["https://youtu.be/a", "https://youtu.be/b", "https://youtu.be/c"]:
            assert u in cli_args

    @pytest.mark.asyncio
    async def test_rag_add_aozora_multiple_book_ids(self, _patch_cli_subprocess) -> None:
        mod = import_module("rag.server")
        await mod.rag_add_aozora(book_ids=["1234", "5678"])
        cli_args = _patch_cli_subprocess.call_args[0][1]
        assert "1234" in cli_args
        assert "5678" in cli_args

    @pytest.mark.asyncio
    async def test_rag_add_bluesky_multiple_urls(self, _patch_cli_subprocess) -> None:
        mod = import_module("rag.server")
        await mod.rag_add_bluesky(
            urls=[
                "https://bsky.app/profile/u/post/a",
                "https://bsky.app/profile/u/post/b",
            ],
        )
        cli_args = _patch_cli_subprocess.call_args[0][1]
        assert "https://bsky.app/profile/u/post/a" in cli_args
        assert "https://bsky.app/profile/u/post/b" in cli_args

    @pytest.mark.asyncio
    async def test_rag_add_zenn_multiple_urls(self, _patch_cli_subprocess) -> None:
        mod = import_module("rag.server")
        await mod.rag_add_zenn(
            urls=[
                "https://zenn.dev/u/articles/x",
                "https://zenn.dev/u/articles/y",
            ],
        )
        cli_args = _patch_cli_subprocess.call_args[0][1]
        assert "https://zenn.dev/u/articles/x" in cli_args
        assert "https://zenn.dev/u/articles/y" in cli_args


class TestBulkEmptyInputError:
    """bulk 化対象で空 list を渡すとエラーメッセージが返ること."""

    @pytest.mark.asyncio
    async def test_rag_add_youtube_empty_list(self) -> None:
        mod = import_module("rag.server")
        result = await mod.rag_add_youtube(video_urls=[])
        assert "エラー" in result
        assert "video_urls" in result

    @pytest.mark.asyncio
    async def test_rag_add_aozora_empty_list(self) -> None:
        mod = import_module("rag.server")
        result = await mod.rag_add_aozora(book_ids=[])
        assert "エラー" in result
        assert "book_ids" in result

    @pytest.mark.asyncio
    async def test_rag_add_bluesky_empty_list(self) -> None:
        mod = import_module("rag.server")
        result = await mod.rag_add_bluesky(urls=[])
        assert "エラー" in result

    @pytest.mark.asyncio
    async def test_rag_add_zenn_empty_list(self) -> None:
        mod = import_module("rag.server")
        result = await mod.rag_add_zenn(urls=[])
        assert "エラー" in result

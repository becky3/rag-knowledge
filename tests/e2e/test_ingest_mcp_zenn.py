"""Zenn 取り込みの L2 Mock E2E テスト.

仕様: docs/specs/workflows/qa-strategy.md
仕様: docs/specs/infrastructure/fake-adapters/zenn.md

MCP server / CLI を実プロセスで起動し、Zenn インジェスト → search の
パイプライン全体が subprocess 越境環境で正しく動作することを検証する。

外部 API（Zenn API）は FakeZennFetcher、Embedding は FakeEmbedding を
DI ファクトリ経由で注入する。Test Double 注入経路は `.env` + 環境変数のみ。
"""

from __future__ import annotations

import subprocess

import pytest

from ._mcp_helpers import call_mcp_tool
from .conftest import run_cli


pytestmark = pytest.mark.e2e


# Fake Fetcher の happy fixture が使う synthetic ID（fake-mode.md の規約）
_FAKE_USERNAME = "testuser"
_FAKE_ARTICLE_URL = "https://zenn.dev/testuser/articles/test-article-001"


class TestMcpZennIngest:
    """MCP 経由の Zenn インジェスト動作確認."""

    async def test_crawl_then_search_returns_chunk(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """rag_crawl_zenn で取り込んだ記事が rag_search 経路で索引化される."""
        ingest_response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_crawl_zenn",
            {"username": _FAKE_USERNAME, "content_type": "articles", "force": True},
        )
        assert "完了" in ingest_response or "placed" in ingest_response.lower(), (
            f"取り込み完了を示すテキストが応答に含まれていない: {ingest_response[:500]}"
        )

        search_response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_search",
            {"query": "Sample"},
        )
        assert search_response.strip(), (
            f"search が空応答を返した: {search_response[:500]}"
        )

    async def test_response_contains_fake_mode_label(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """fake モード時、Zenn 応答冒頭に [FAKE MODE: zenn] ラベルが付与される."""
        response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_crawl_zenn",
            {"username": _FAKE_USERNAME, "content_type": "articles", "force": True},
        )
        assert "[FAKE MODE: zenn]" in response, (
            f"fake モードラベルが応答に含まれていない: {response[:500]}"
        )

    async def test_add_zenn_url(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """rag_add_zenn で URL 指定取り込みが動作する."""
        response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_add_zenn",
            {"urls": [_FAKE_ARTICLE_URL]},
        )
        assert "完了" in response or "placed" in response.lower(), (
            f"URL 指定取り込みが完了していない: {response[:500]}"
        )


class TestCliZennIngest:
    """CLI 経由の Zenn インジェスト動作確認."""

    def test_cli_crawl_zenn_succeeds(
        self,
        e2e_subprocess_env: dict[str, str],
        e2e_mcp_server: str,  # noqa: ARG002 - ChromaDB を auto_start させるため依存
    ) -> None:
        """CLI から crawl-zenn を実行できる（subprocess 越境）."""
        result: subprocess.CompletedProcess[str] = run_cli(
            ["crawl-zenn", _FAKE_USERNAME, "--content-type", "articles", "--force"],
            env=e2e_subprocess_env,
            timeout=120.0,
        )
        assert result.returncode == 0, (
            f"CLI が非ゼロ終了: returncode={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "完了" in combined or "placed" in combined.lower(), (
            f"CLI 出力に取り込み完了の証跡なし:\n{combined[:1000]}"
        )

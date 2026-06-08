"""Site-ingest (scrapy) 取り込みの L2 Mock E2E テスト.

仕様: docs/specs/workflows/qa-strategy.md
仕様: docs/specs/infrastructure/fake-adapters/scrapy.md

MCP server / CLI を実プロセスで起動し、site-ingest → search の
パイプライン全体が subprocess 越境環境で正しく動作することを検証する。

外部 Web アクセス（Scrapy subprocess）は FakeScrapyRunner、Embedding は FakeEmbedding を
DI ファクトリ経由で注入する。Test Double 注入経路は `.env` + 環境変数のみ。

検証対象（仕様書「検出能力の対応関係」テーブルの L2 担当行）:

- プロセス間通信・環境変数伝播の問題（RAG_WEB_FAKE_MODE が子プロセスに伝わる）
- ファイルベースロック競合の振る舞い
- Fake Adapter / インジェスター本体 / DI ファクトリの regression
"""

from __future__ import annotations

import subprocess

import pytest

from ._mcp_helpers import call_mcp_tool
from .conftest import run_cli


pytestmark = pytest.mark.e2e


# Fake ScrapyRunner happy シナリオが返す synthetic URL（仕様: fake-mode.md の規約）
# テスト入力 URL は実在ドメイン（IANA 予約の example.com）を採用する。
# - 実在するため DNS / SSRF 検証を通過する
# - FakeScrapyRunner は入力 URL を無視して fixture（test.invalid 配下）を返却するため、
#   実 HTTP リクエストは発生しない
_FAKE_PAGE_URL = "https://example.com/page1"


class TestMcpSiteIngest:
    """MCP 経由の site-ingest（取得入口）動作確認."""

    async def test_ingest_then_search_returns_chunk(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """rag_site_ingest で取り込んだページが rag_search 経路で索引化される.

        Fake Embedding は SHA-256 seed の決定論的ベクトルを返すが、テキスト全体が
        完全一致しない限り cosine 類似度は意味的なものにならない。
        本テストでは「取り込み成功 → search が動作（空でない応答）」までを
        end-to-end で検証する。
        """
        ingest_response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_site_ingest",
            {"urls": [_FAKE_PAGE_URL]},
        )
        assert "完了" in ingest_response or "placed" in ingest_response.lower(), (
            f"取り込み完了を示すテキストが応答に含まれていない: {ingest_response}"
        )

        search_response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_search",
            {"query": "Sample"},
        )
        assert search_response.strip(), (
            f"search が空応答を返した（subprocess 越境経路の異常を示唆）: "
            f"{search_response[:500]}"
        )

    async def test_response_contains_fake_mode_label(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """fake モード時、site-ingest 応答冒頭に [FAKE MODE: web] ラベルが付与される.

        仕様: docs/specs/infrastructure/fake-mode.md の MCP 応答ラベル制約
        """
        response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_site_ingest",
            {"urls": [_FAKE_PAGE_URL]},
        )
        assert "[FAKE MODE: web]" in response, (
            f"fake モードラベルが応答に含まれていない: {response[:500]}"
        )

    async def test_multi_url(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """複数 URL の取得入口でも取り込みが完了する."""
        response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_site_ingest",
            {
                "urls": [
                    "https://example.com/multi1",
                    "https://example.org/multi2",
                ],
            },
        )
        assert "完了" in response or "placed" in response.lower(), (
            f"複数 URL 取り込みが完了していない: {response[:500]}"
        )


class TestMcpSiteCrawl:
    """MCP 経由の site-crawl（クロール入口）動作確認."""

    async def test_crawl_completes(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """rag_site_crawl で単一 URL 起点のクロールが完了する."""
        response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_site_crawl",
            {"url": _FAKE_PAGE_URL},
        )
        assert "完了" in response or "placed" in response.lower(), (
            f"クロール完了を示すテキストが応答に含まれていない: {response}"
        )


class TestCliSiteIngest:
    """CLI 経由の site-ingest（取得入口）動作確認."""

    def test_cli_site_ingest_succeeds(
        self,
        e2e_subprocess_env: dict[str, str],
        e2e_mcp_server: str,  # noqa: ARG002 - ChromaDB を auto_start させるため依存
    ) -> None:
        """CLI から site-ingest を実行できる（subprocess 越境）.

        e2e_mcp_server fixture により ChromaDB が auto_start されている前提で、
        CLI を別プロセスとして起動し、同じ ChromaDB に書き込む。
        FakeScrapyRunner により実 Web アクセスは発生しない。
        """
        result: subprocess.CompletedProcess[str] = run_cli(
            ["site-ingest", _FAKE_PAGE_URL],
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


class TestCliSiteCrawl:
    """CLI 経由の site-crawl（クロール入口）動作確認."""

    def test_cli_site_crawl_succeeds(
        self,
        e2e_subprocess_env: dict[str, str],
        e2e_mcp_server: str,  # noqa: ARG002 - ChromaDB を auto_start させるため依存
    ) -> None:
        """CLI から site-crawl を実行できる（subprocess 越境）."""
        result: subprocess.CompletedProcess[str] = run_cli(
            ["site-crawl", _FAKE_PAGE_URL],
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

"""Local 取り込みの L2 Mock E2E テスト.

仕様: docs/specs/workflows/qa-strategy.md

MCP server / CLI を実プロセスで起動し、Local インジェスト（ファイル添付）
→ search のパイプライン全体が subprocess 越境環境で正しく動作することを検証する。

LocalFetcher は filesystem 抽象化のみを担い、PDF/AsciiDoc 抽出は converter 層の責務。
Embedding は FakeEmbedding を DI ファクトリ経由で注入する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._mcp_helpers import call_mcp_tool
from .conftest import run_cli


pytestmark = pytest.mark.e2e


_SAMPLE_MARKDOWN_CONTENT = """# Test Document

E2E synthetic Markdown content for Local ingester verification.
"""


class TestMcpLocalIngest:
    """MCP 経由の Local インジェスト動作確認."""

    async def test_add_document_succeeds(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """rag_add_document で stdin 越しにファイルを取り込める."""
        response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_add_document",
            {
                "content": _SAMPLE_MARKDOWN_CONTENT,
                "filename": "e2e_test_document.md",
                "encoding": "text",
                "upload_mode": "replace",
            },
        )
        assert "完了" in response or "placed" in response.lower(), (
            f"取り込み完了を示すテキストが応答に含まれていない: {response[:500]}"
        )

    async def test_response_contains_embedding_fake_mode_label(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """Embedding fake モード時、Local 応答に [FAKE MODE: embedding] が付与される.

        e2e_subprocess_env で RAG_EMBEDDING_FAKE_MODE=true を設定しているため、
        rag_add_document は Embedding fake ラベルを応答冒頭に付与する。
        他 ingest e2e（aozora/bluesky/scrapy/youtube/zenn）と同等のラベル検証を維持する。
        """
        response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_add_document",
            {
                "content": _SAMPLE_MARKDOWN_CONTENT,
                "filename": "e2e_label_test.md",
                "encoding": "text",
                "upload_mode": "replace",
            },
        )
        assert "[FAKE MODE: embedding]" in response, (
            f"Embedding fake ラベルが応答に含まれていない: {response[:500]}"
        )
        # Local Fake 廃止（Issue #722）に伴い、[FAKE MODE: local] ラベルは
        # 二度と付与されないこと。本 PR の本質的変更点を回帰検出で固定する
        assert "[FAKE MODE: local]" not in response, (
            f"Local fake ラベルが残存している（Issue #722 で廃止済）: {response[:500]}"
        )


class TestCliCrawlDocuments:
    """CLI crawl-documents の subprocess 越境動作確認.

    rag_crawl_documents は HTTP モード非対応のため CLI 経由のみ実行できる。
    create_local_fetcher() の振る舞い変更（Fake 分岐削除）後も、
    CLI subprocess 経由でディレクトリ走査・取り込みが正常動作することを検証する。
    """

    def test_crawl_documents_processes_directory(
        self,
        tmp_path: Path,
        e2e_subprocess_env: dict[str, str],
        e2e_mcp_server: str,  # noqa: ARG002 — ChromaDB / source_store 共有のため起動
    ) -> None:
        """crawl-documents で指定ディレクトリ配下のファイルを取り込める."""
        target = tmp_path / "crawl_target"
        target.mkdir()
        (target / "doc_a.md").write_text(
            "# Crawl A\n\nE2E synthetic Markdown for crawl test (A).\n",
            encoding="utf-8",
        )
        (target / "doc_b.md").write_text(
            "# Crawl B\n\nE2E synthetic Markdown for crawl test (B).\n",
            encoding="utf-8",
        )

        result = run_cli(
            ["crawl-documents", str(target)],
            env=e2e_subprocess_env,
        )
        assert result.returncode == 0, (
            f"crawl-documents が失敗: stdout={result.stdout[:500]} stderr={result.stderr[:500]}"
        )
        assert "完了" in result.stdout or "placed" in result.stdout.lower(), (
            f"取り込み完了テキストが含まれない: {result.stdout[:500]}"
        )

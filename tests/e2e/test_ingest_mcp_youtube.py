"""YouTube 取り込みの L2 Mock E2E テスト.

仕様: docs/specs/workflows/qa-strategy.md
仕様: docs/specs/infrastructure/fake-adapters/youtube.md

MCP server / CLI を実プロセスで起動し、YouTube インジェスト → search の
パイプライン全体が subprocess 越境環境で正しく動作することを検証する。

外部 API（YouTube）は FakeYoutubeFetcher、Embedding は FakeEmbedding を
DI ファクトリ経由で注入する。Test Double 注入経路は `.env` + 環境変数のみ。

検証対象（仕様書「検出能力の対応関係」テーブルの L2 担当行）:

- プロセス間通信・環境変数伝播の問題
- ファイルベースロック競合の振る舞い（重複検出 / overwritten カウント）
- Fake Adapter / インジェスター本体 / DI ファクトリの regression
"""

from __future__ import annotations

import subprocess

import pytest

from ._mcp_helpers import call_mcp_tool
from .conftest import run_cli


pytestmark = pytest.mark.e2e


# Fake Fetcher の happy シナリオが使う synthetic ID（仕様: fake-mode.md の規約）
_FAKE_VIDEO_ID = "TestVideo01"
_FAKE_VIDEO_URL = f"https://www.youtube.com/watch?v={_FAKE_VIDEO_ID}"

# overwritten 検証専用の独立 video_id（他テストとの順序依存を排除する）
_OVERWRITE_TEST_VIDEO_ID = "TestVideo02"
_OVERWRITE_TEST_VIDEO_URL = f"https://www.youtube.com/watch?v={_OVERWRITE_TEST_VIDEO_ID}"


class TestMcpYoutubeIngest:
    """MCP 経由の YouTube インジェスト動作確認."""

    async def test_ingest_then_search_returns_chunk(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """rag_add_youtube で取り込んだ動画が rag_search 経路で索引化される.

        Fake Embedding は SHA-256 seed の決定論的ベクトルを返すが、テキスト全体が
        完全一致しない限り cosine 類似度は意味的なものにならない。
        本テストでは「インジェスト成功 → search が動作（空でない応答）」までを
        end-to-end で検証する。検索精度の意味的検証は L3 で扱う。
        """
        ingest_response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_add_youtube",
            {"video_url": _FAKE_VIDEO_URL},
        )
        assert "完了" in ingest_response or "placed" in ingest_response.lower(), (
            f"取り込み完了を示すテキストが応答に含まれていない: {ingest_response}"
        )

        search_response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_search",
            {"query": "sample"},
        )
        # search 経路が subprocess 越境で動作することのみ検証する。
        # 決定論的 Fake Embedding では「クエリ文字列とチャンクテキストの完全一致」
        # でしか有意な類似度が得られないため、本テストでは search が空でないことを
        # 担保し、ID 一致や順位の検証は意味的検証として L3 に委ねる。
        assert search_response.strip(), (
            f"search が空応答を返した（subprocess 越境経路の異常を示唆）: {search_response[:500]}"
        )

    async def test_response_contains_fake_mode_label(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """fake モード時、取り込み系 MCP 応答冒頭に [FAKE MODE: <source>] ラベルが付与される.

        仕様: docs/specs/infrastructure/fake-mode.md の MCP 応答ラベル制約
        """
        response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_add_youtube",
            {"video_url": _FAKE_VIDEO_URL},
        )
        assert "[FAKE MODE" in response, (
            f"fake モードラベルが応答に含まれていない: {response[:300]}"
        )

    async def test_overwritten_count_via_subprocess(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """同一動画 URL を 2 回投入した際、2 回目で overwritten カウントが反映される.

        Issue #683 で発覚した「overwritten 早期 return バグ」の subprocess 越境
        regression 検出を担う。in-process では検出できなかった経路の問題が
        本テストで検出可能になる。

        他テストとの実行順序依存を排除するため、本テスト固有の video_id
        (`_OVERWRITE_TEST_VIDEO_ID`) を使用する。session 共有 MCP server で
        別テストが先に同 video_id を取り込んでいると 1 回目の検証が破綻する。
        """
        first = await call_mcp_tool(
            e2e_mcp_server,
            "rag_add_youtube",
            {"video_url": _OVERWRITE_TEST_VIDEO_URL},
        )
        # 1 回目は新規取り込みのため「上書き」表記を含まないことを厳密に確認する。
        # （早期 return バグが「常に上書き 0 件」を返す挙動を 2 回目側で検出するため、
        # 本 1 回目アサーションは「上書きしていない」状態のベースラインを保証する）
        assert "完了" in first or "placed" in first.lower(), (
            f"1 回目の取り込みが完了していない: {first[:500]}"
        )
        assert "上書き" not in first and "overwritten" not in first.lower(), (
            f"1 回目（新規取り込み）の応答に上書き表記が含まれている: {first[:500]}"
        )

        second = await call_mcp_tool(
            e2e_mcp_server,
            "rag_add_youtube",
            {"video_url": _OVERWRITE_TEST_VIDEO_URL},
        )
        # 2 回目は overwritten カウントが反映される（早期 return バグなら反映されない）
        assert "上書き" in second or "overwritten" in second.lower(), (
            f"overwritten カウントが応答に反映されていない: {second[:500]}"
        )


class TestCliYoutubeIngest:
    """CLI 経由の YouTube インジェスト動作確認."""

    def test_cli_ingest_youtube_succeeds(
        self,
        e2e_subprocess_env: dict[str, str],
        e2e_mcp_server: str,  # noqa: ARG002 - ChromaDB を auto_start させるため依存
    ) -> None:
        """CLI から ingest-youtube を実行できる（subprocess 越境）.

        e2e_mcp_server fixture により ChromaDB が auto_start されている前提で、
        CLI を別プロセスとして起動し、同じ ChromaDB に書き込む。
        ファイルベースロック（fcntl/msvcrt）が プロセス間で正常に動作することを
        本テストが間接的に検証する。
        """
        result: subprocess.CompletedProcess[str] = run_cli(
            ["ingest-youtube", _FAKE_VIDEO_URL],
            env=e2e_subprocess_env,
            timeout=120.0,
        )
        assert result.returncode == 0, (
            f"CLI が非ゼロ終了: returncode={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        # CLI の標準出力に取り込み完了を示すテキストが含まれる
        combined = result.stdout + result.stderr
        assert _FAKE_VIDEO_ID in combined or "完了" in combined or "placed" in combined.lower(), (
            f"CLI 出力に取り込み完了の証跡なし:\n{combined[:1000]}"
        )

"""BlueSky 取り込みの L2 Mock E2E テスト.

仕様: docs/specs/workflows/qa-strategy.md
仕様: docs/specs/infrastructure/fake-adapters/bluesky.md

MCP server を実プロセスで起動し、BlueSky インジェスト → search のパイプライン
全体が subprocess 越境環境で正しく動作することを検証する。

外部 API（AT Protocol AppView）は FakeBlueskyFetcher、メディア DL は
FakeBlueskyMediaDownloader、Embedding は FakeEmbedding を DI ファクトリ経由で
注入する。Test Double 注入経路は `.env` + 環境変数のみ。

検証対象:

- 通常系（happy path）: rag_crawl_bluesky → search の subprocess 越境動作
- リポストフィルタ: include_reposts=False がリポスト除外件数まで反映する
- MCP 応答ラベル: fake モード時 [FAKE MODE: bluesky] が付与される
"""

from __future__ import annotations

import re

import pytest

from ._mcp_helpers import call_mcp_tool


pytestmark = pytest.mark.e2e


# Fake Fetcher の happy fixture が使う synthetic ID（fake-mode.md の規約）
_FAKE_HANDLE = "test.bsky.social"

# happy fixture (`feed_happy.json`) のアイテム構成（テストでの件数検証に使用）
_HAPPY_FIXTURE_TOTAL = 4
_HAPPY_FIXTURE_REPOSTS = 1
_HAPPY_FIXTURE_NON_REPOSTS = _HAPPY_FIXTURE_TOTAL - _HAPPY_FIXTURE_REPOSTS

# IngestResult.summary 出力フォーマット（src/rag/pipeline/ingesters/_common.py）に
# 沿った件数抽出パターン。日本語固定（同モジュール側で英語フォーマットは未定義）。
# `完了: N件配置` は常に出力されるが、`上書き: N件` は overwritten > 0 のときだけ
# 出力される（_common.py の summary() が条件付きで append するため）。
_PLACED_RE = re.compile(r"完了:\s*(\d+)件配置")
_OVERWRITTEN_RE = re.compile(r"上書き:\s*(\d+)件")


def _extract_placed(text: str) -> int:
    """`完了: N件配置` から N を抽出する（必須項目、欠落時は assertion error）."""
    m = _PLACED_RE.search(text)
    if m is None:
        raise AssertionError(
            f"配置件数が応答に含まれない: {text[:500]}"
        )
    return int(m.group(1))


def _extract_overwritten(text: str) -> int:
    """`上書き: N件` から N を抽出する（条件付き出力、欠落時は 0 として扱う）."""
    m = _OVERWRITTEN_RE.search(text)
    return int(m.group(1)) if m is not None else 0


class TestMcpBlueskyIngest:
    """MCP 経由の BlueSky インジェスト動作確認."""

    async def test_crawl_then_search_returns_chunk(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """rag_crawl_bluesky で取り込んだ投稿が rag_search 経路で索引化される.

        Fake Embedding は SHA-256 seed の決定論的ベクトルを返すため、
        意味的類似度の検証は L3 で扱う。本テストでは「取り込み成功 →
        search が空でない応答を返す」までを end-to-end で検証する。
        """
        ingest_response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_crawl_bluesky",
            {"handle": _FAKE_HANDLE, "max_posts": 10},
        )
        # IngestResult.summary 固定フォーマットで配置件数を検証
        # （include_reposts=True デフォルトのため fixture 全件 = 4 配置）
        placed = _extract_placed(ingest_response)
        assert placed == _HAPPY_FIXTURE_TOTAL, (
            f"配置件数が fixture 全件と一致しない: placed={placed}, "
            f"expected={_HAPPY_FIXTURE_TOTAL}, response={ingest_response[:500]}"
        )

        search_response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_search",
            {"query": "synthetic"},
        )
        assert search_response.strip(), (
            f"search が空応答を返した（subprocess 越境経路の異常を示唆）: "
            f"{search_response[:500]}"
        )

    async def test_repost_filter_excludes_reposts(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """include_reposts=False がリポスト 1 件を除外し件数に反映する.

        fake fixture (`feed_happy.json`) は 4 アイテム（通常 + リプライ +
        リポスト + 外部リンク）。include_reposts=True + force で全 4 件配置 →
        include_reposts=False + force で再取り込みすると、リポスト 1 件除外で
        3 件のみ overwrite される（IngestResult.summary の "上書き: N件" で
        件数検証可能）。

        session-scoped MCP server で他テストが先に同じ handle を取り込んでいる
        可能性があるため、両 call で force=True を指定して state 独立にする。
        """
        # 1 回目: include_reposts=True + force=True で全 4 件 placed/overwritten。
        # 直前テストの状態に依らず、常に 4 件分の配置/上書きが発生することを担保する。
        first = await call_mcp_tool(
            e2e_mcp_server,
            "rag_crawl_bluesky",
            {
                "handle": _FAKE_HANDLE,
                "max_posts": 10,
                "include_reposts": True,
                "force": True,
            },
        )
        first_total = (
            _extract_placed(first)
            + _extract_overwritten(first)
        )
        assert first_total == _HAPPY_FIXTURE_TOTAL, (
            f"include_reposts=True で配置 + 上書きの合計が fixture 全件と一致しない: "
            f"total={first_total}, expected={_HAPPY_FIXTURE_TOTAL}, "
            f"response={first[:500]}"
        )

        # 2 回目: include_reposts=False + force=True で再取り込み
        # → リポスト 1 件除外、非リポスト 3 件のみ overwrite される
        response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_crawl_bluesky",
            {
                "handle": _FAKE_HANDLE,
                "max_posts": 10,
                "include_reposts": False,
                "force": True,
            },
        )
        overwritten = _extract_overwritten(response)
        assert overwritten == _HAPPY_FIXTURE_NON_REPOSTS, (
            f"include_reposts=False で上書き件数が非リポスト件数と一致しない: "
            f"overwritten={overwritten}, expected={_HAPPY_FIXTURE_NON_REPOSTS}, "
            f"response={response[:500]}"
        )

    async def test_response_contains_fake_mode_label(
        self,
        e2e_mcp_server: str,
    ) -> None:
        """fake モード時、bluesky 取り込み MCP 応答に [FAKE MODE: bluesky] ラベルが付与される.

        仕様: docs/specs/infrastructure/fake-mode.md の MCP 応答ラベル制約
        仕様: docs/specs/infrastructure/fake-adapters/bluesky.md
        """
        response = await call_mcp_tool(
            e2e_mcp_server,
            "rag_crawl_bluesky",
            {"handle": _FAKE_HANDLE, "max_posts": 5},
        )
        assert "[FAKE MODE: bluesky]" in response, (
            f"bluesky fake モードラベルが応答に含まれていない: {response[:500]}"
        )

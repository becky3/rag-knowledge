"""Fake モード状態の表示ラベル生成.

仕様: docs/specs/infrastructure/fake-mode.md
"""

from __future__ import annotations

from typing import Literal

from ..config import get_settings

FakeSource = Literal[
    "youtube", "bluesky", "embedding", "web", "zenn", "aozora",
]

# YouTube インジェスト系 MCP ツールが利用する fake source の組
_YOUTUBE_INGEST_FAKE_SOURCES: list[FakeSource] = ["youtube", "embedding"]

# BlueSky インジェスト系 MCP ツールが利用する fake source の組
# 投稿内 URL の自動取り込みで YouTube / web (site-ingest) も委譲対象になるため、すべて並列で評価する
_BLUESKY_INGEST_FAKE_SOURCES: list[FakeSource] = [
    "bluesky", "youtube", "web", "embedding",
]

# サイト一括取り込み（site-ingest / scrapy）系 MCP ツールが利用する fake source の組
_SITE_INGEST_FAKE_SOURCES: list[FakeSource] = ["web", "embedding"]

# Zenn インジェスト系 MCP ツールが利用する fake source の組
_ZENN_INGEST_FAKE_SOURCES: list[FakeSource] = ["zenn", "embedding"]

# Aozora インジェスト系 MCP ツールが利用する fake source の組
_AOZORA_INGEST_FAKE_SOURCES: list[FakeSource] = ["aozora", "embedding"]

# Local インジェスト系 MCP ツールが利用する fake source の組
# Local 自体は filesystem 抽象化のみで Fake 化対象外。Embedding fake のみラベル付与する
_LOCAL_INGEST_FAKE_SOURCES: list[FakeSource] = ["embedding"]

# Journal インジェスト系 MCP ツールが利用する fake source の組
# Journal はユーザーがコンテンツを直接渡すため source 固有 fake は存在しないが、
# Embedding は fake モードで動作するため、ユーザーが実 API アクセス有無を判別できるようラベル付与する
_JOURNAL_INGEST_FAKE_SOURCES: list[FakeSource] = ["embedding"]


def _fake_mode_labels(active_sources: list[FakeSource]) -> str:
    """fake モード時の応答ラベル（先頭に付与する）.

    仕様: docs/specs/infrastructure/fake-mode.md

    呼び出し側は MCP ツールが利用する fake source 名を明示的に渡す。
    fake モードは複数 source（youtube / embedding 等）で独立に切替可能なため、
    ラベルは「何の」fake かを応答受信者が即座に判別できる必要がある。
    そのため source ごとのラベルを改行で並列出力する形を採用する。

    Args:
        active_sources: ラベル付与対象とする fake source 名（例: ["youtube", "embedding"]）。
            並列の複数 source を指定可能。各 source のうち fake モードが有効なもののみ
            "[FAKE MODE: <source>]\n" 形式で連結して返す。
            未知の source 名が含まれた場合は ValueError を発生させる（mypy で事前検出
            可能だが、ランタイムガードも併設して未知 source の暗黙無視を防ぐ）。

    Returns:
        ラベル文字列（fake が 1 つも有効でなければ空文字列）。

    Raises:
        ValueError: active_sources に未知の source 名が含まれている場合。
    """
    settings = get_settings()
    flags: dict[FakeSource, bool] = {
        "youtube": settings.rag_youtube_fake_mode,
        "bluesky": settings.rag_bluesky_fake_mode,
        "embedding": settings.rag_embedding_fake_mode,
        "web": settings.rag_scrapy_fake_mode,
        "zenn": settings.rag_zenn_fake_mode,
        "aozora": settings.rag_aozora_fake_mode,
    }
    parts: list[str] = []
    for source in active_sources:
        if source not in flags:
            raise ValueError(
                f"未知の fake source 名: {source!r}. 既知の source: {sorted(flags.keys())}"
            )
        if flags[source]:
            parts.append(f"[FAKE MODE: {source}]")
    if not parts:
        return ""
    return "\n".join(parts) + "\n"

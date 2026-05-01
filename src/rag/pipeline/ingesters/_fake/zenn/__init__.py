"""Fake Zenn Fetcher.

仕様: docs/specs/infrastructure/fake-mode.md
仕様: docs/specs/infrastructure/fake-adapters/zenn.md

ZennFetcher Protocol の Fake 実装。Zenn API への実アクセスを発生させず、
fixture から事前定義された応答 dict を返却する。

シナリオは ``SCENARIOS`` で定義された値のみ受け付ける。
synthetic ID 規約: slug は ``test-`` プレフィックス、username は ``testuser``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

ZennKind = Literal["articles", "scraps"]

SCENARIOS: tuple[str, ...] = ("happy", "empty", "not_found", "metadata_error")


class FakeZennFetcher:
    """ZennFetcher Protocol の Fake 実装.

    fixture ディレクトリから JSON ファイルを読み込み、事前定義された
    応答 dict を返却する。実 Zenn API アクセスは発生しない。

    Args:
        fixture_dir: fixture JSON が格納されたディレクトリ
        scenario: 採用するシナリオ名（``SCENARIOS`` のいずれか、既定: ``happy``）
    """

    def __init__(
        self,
        *,
        fixture_dir: Path,
        scenario: str = "happy",
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(
                f"未対応のシナリオ: {scenario!r}。許容値: {SCENARIOS}"
            )
        if not fixture_dir.exists():
            raise FileNotFoundError(
                f"fixture ディレクトリが見つかりません: {fixture_dir}"
            )
        self._fixture_dir = fixture_dir
        self._scenario = scenario

    async def list_contents(
        self,
        kind: ZennKind,
        username: str,
        page: int,
    ) -> dict[str, Any]:
        """一覧 API の応答を fixture から返却.

        page > 1 のときは empty ページとして扱う（Zenn API のページネーション
        終端を再現）。
        """
        del username
        if self._scenario == "metadata_error":
            raise RuntimeError("[FAKE] metadata_error scenario for list_contents")
        if self._scenario == "not_found":
            return {kind: [], "next_page": None}
        if page > 1:
            return {kind: [], "next_page": None}
        if self._scenario == "empty":
            return {kind: [], "next_page": None}
        return self._load_fixture(f"{self._scenario}_{kind}_list")

    async def fetch_content_detail(
        self,
        kind: ZennKind,
        slug: str,
    ) -> dict[str, Any]:
        """詳細 API の応答を fixture から返却."""
        del slug
        if self._scenario == "metadata_error":
            raise RuntimeError(
                "[FAKE] metadata_error scenario for fetch_content_detail",
            )
        if self._scenario == "not_found":
            raise RuntimeError("[FAKE] not_found scenario for fetch_content_detail")
        return self._load_fixture(f"{self._scenario}_{kind}_detail")

    def _load_fixture(self, name: str) -> dict[str, Any]:
        path = self._fixture_dir / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"fixture ファイルが見つかりません: {path}（scenario={self._scenario}）"
            )
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(
                f"fixture が dict ではありません: {path} ({type(data)})"
            )
        return data


__all__ = ["FakeZennFetcher", "SCENARIOS"]

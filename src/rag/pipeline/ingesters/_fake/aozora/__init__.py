"""Fake Aozora Fetcher.

仕様: docs/specs/infrastructure/fake-mode.md
仕様: docs/specs/infrastructure/fake-adapters/aozora.md

AozoraFetcher Protocol の Fake 実装。fixture から事前生成された
カタログ ZIP / 作品 XHTML の bytes を返却する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

SCENARIOS: tuple[str, ...] = ("happy", "not_found", "catalog_error")


class FakeAozoraFetcher:
    """AozoraFetcher Protocol の Fake 実装.

    fixture ディレクトリから事前生成された ZIP / XHTML ファイルの bytes を返却する。
    実 青空文庫 / GitHub Raw アクセスは発生しない。

    Args:
        fixture_dir: fixture が格納されたディレクトリ
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

    async def __aenter__(self) -> "FakeAozoraFetcher":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def fetch_catalog_zip(self) -> bytes:
        if self._scenario == "catalog_error":
            raise RuntimeError("[FAKE] catalog_error scenario for fetch_catalog_zip")
        path = self._fixture_dir / "catalog_happy.zip"
        if not path.exists():
            raise FileNotFoundError(f"fixture が見つかりません: {path}")
        return path.read_bytes()

    async def fetch_xhtml(self, github_url: str) -> bytes:
        del github_url
        if self._scenario in ("not_found", "catalog_error"):
            raise RuntimeError(f"[FAKE] {self._scenario} scenario for fetch_xhtml")
        path = self._fixture_dir / "xhtml_happy.html"
        if not path.exists():
            raise FileNotFoundError(f"fixture が見つかりません: {path}")
        return path.read_bytes()


__all__ = ["FakeAozoraFetcher", "SCENARIOS"]

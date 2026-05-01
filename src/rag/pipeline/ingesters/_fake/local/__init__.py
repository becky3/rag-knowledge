"""Fake Local Fetcher.

仕様: docs/specs/infrastructure/fake-mode.md
仕様: docs/specs/infrastructure/fake-adapters/local.md

LocalFetcher Protocol の Fake 実装。fixture ディレクトリ配下の synthetic
ファイル群を実 I/O で読み込み、ユーザーの実環境ファイルシステムへの
アクセスを排除する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

SCENARIOS: tuple[str, ...] = ("happy", "empty", "unsupported_ext")


class FakeLocalFetcher:
    """LocalFetcher Protocol の Fake 実装.

    実際のファイルシステム操作は行うが、対象は ``fixture_dir`` 配下に限定される。
    ユーザー指定の任意ディレクトリへのアクセスを排除し、テスト・QA で
    決定論的な fixture を使う目的。

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

    async def __aenter__(self) -> "FakeLocalFetcher":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    def _scenario_root(self) -> Path:
        """シナリオ別ディレクトリへの絶対パスを返す."""
        return self._fixture_dir / self._scenario

    def discover_files(
        self,
        dir_path: str,
        pattern: str,
        extensions: list[str],
    ) -> list[Path]:
        """シナリオディレクトリ配下のファイルを列挙する.

        引数の ``dir_path`` は無視される（Fake は常に固定の fixture を返す）。
        ``empty`` シナリオでは空配列を返す。
        """
        del dir_path
        if self._scenario == "empty":
            return []
        root = self._scenario_root()
        if not root.exists():
            return []
        files: list[Path] = []
        for p in root.glob(pattern):
            resolved = p.resolve()
            if not resolved.is_file():
                continue
            if resolved.suffix.lower() not in extensions:
                continue
            files.append(resolved)
        files.sort(key=lambda p: str(p))
        return files

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def get_file_size(self, path: Path) -> int:
        return path.stat().st_size


__all__ = ["FakeLocalFetcher", "SCENARIOS"]

"""Local Fetcher Protocol と Real 実装、DI ファクトリ.

仕様: docs/specs/infrastructure/fake-mode.md
仕様: docs/specs/infrastructure/fake-adapters/local.md

ローカルファイルシステムへのアクセス処理を抽象化した Protocol を定義し、
Real 実装（pathlib 直利用）と .env 経由の DI ファクトリを提供する。

PDF テキスト抽出 / AsciiDoc → Markdown 変換は **converter 層の責務** であり、
本 Protocol のスコープ外。LocalFetcher は filesystem 抽象化のみを担う。
QA イテレーション速度向上（MinerU/CUDA 起動回避）は #713 で対応する。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from rag.config import RAGSettings


class LocalFetcher(Protocol):
    """ローカルファイルシステムアクセスの抽象 Port.

    Real 実装は pathlib 直利用、Fake 実装は fixture ディレクトリベース。
    """

    async def __aenter__(self) -> "LocalFetcher":
        ...

    async def __aexit__(self, *exc_info: Any) -> None:
        ...

    def discover_files(
        self,
        dir_path: str,
        pattern: str,
        extensions: list[str],
    ) -> list[Path]:
        """指定ディレクトリ配下のファイルを glob パターンと拡張子で列挙する.

        Args:
            dir_path: 走査対象ディレクトリ（絶対パス想定）
            pattern: glob パターン
            extensions: 許可拡張子（小文字）

        Returns:
            条件に合致するファイルパスのリスト（ソート済み）
        """
        ...

    def read_bytes(self, path: Path) -> bytes:
        """指定パスのファイルを bytes として読み込む."""
        ...

    def get_file_size(self, path: Path) -> int:
        """ファイルサイズ（bytes）を取得する."""
        ...


class RealLocalFetcher:
    """ローカルファイルシステムへの実アクセス実装.

    pathlib を直接利用する。``async with`` は no-op（filesystem アクセスは
    コンテキスト不要）。
    """

    async def __aenter__(self) -> "RealLocalFetcher":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    def discover_files(
        self,
        dir_path: str,
        pattern: str,
        extensions: list[str],
    ) -> list[Path]:
        resolved_dir = Path(dir_path).resolve()
        files: list[Path] = []
        for p in resolved_dir.glob(pattern):
            resolved = p.resolve()
            if not resolved.is_file():
                continue
            try:
                resolved.relative_to(resolved_dir)
            except ValueError:
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


def create_local_fetcher(settings: RAGSettings) -> LocalFetcher:
    """Settings から Real / Fake のいずれかを選択して返すファクトリ."""
    if settings.rag_local_fake_mode:
        from rag.config import PROJECT_ROOT
        from rag.pipeline.ingesters._fake.local import FakeLocalFetcher

        fixture_dir = Path(settings.rag_local_fake_fixture_dir)
        if not fixture_dir.is_absolute():
            fixture_dir = PROJECT_ROOT / fixture_dir
        if not fixture_dir.exists():
            raise FileNotFoundError(
                f"Local fake fixture ディレクトリが見つかりません: {fixture_dir}。"
                f"RAG_LOCAL_FAKE_FIXTURE_DIR を確認してください"
            )
        return FakeLocalFetcher(fixture_dir=fixture_dir)
    return RealLocalFetcher()

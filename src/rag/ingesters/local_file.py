"""ローカルファイルインジェスター: ローカルファイルの取り込み

仕様: docs/specs/rag-knowledge.md
Issue: #71
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import BaseIngester, IngestedContent

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".md", ".txt"})


class LocalFileIngester(BaseIngester):
    """ローカルファイル取り込み用インジェスター.

    Markdown (.md) やテキスト (.txt) ファイルを読み込み、
    IngestedContent に変換する。
    """

    def validate_identifier(self, identifier: str) -> str:
        """ファイルパスを検証し、正規化済みの絶対パスを返す.

        Args:
            identifier: 検証するファイルパス

        Returns:
            正規化済みの絶対パス文字列

        Raises:
            ValueError: パスが存在しない、ファイルでない、
                        または未対応の拡張子の場合
        """
        path = Path(identifier).resolve()

        if not path.exists():
            raise ValueError(f"ファイルが存在しません: {path}")

        if not path.is_file():
            raise ValueError(f"ファイルではありません: {path}")

        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"未対応の拡張子です: {path.suffix} "
                f"(対応: {', '.join(sorted(SUPPORTED_EXTENSIONS))})"
            )

        return str(path)

    async def fetch_single(self, identifier: str) -> IngestedContent | None:
        """単一ファイルを読み込み IngestedContent を返す.

        1. パス検証
        2. ファイル読み込み
        3. タイトル抽出（Markdown の場合は最初の # 見出し）
        4. IngestedContent に変換

        Args:
            identifier: 読み込むファイルパス

        Returns:
            IngestedContent、または読み込み失敗時は None
        """
        try:
            validated_path = self.validate_identifier(identifier)
        except ValueError:
            logger.warning("Invalid file path: %s", identifier)
            return None

        path = Path(validated_path)

        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Failed to read file: %s", validated_path)
            return None

        title = self._extract_title(text, path)

        return IngestedContent(
            source_id=validated_path,
            title=title,
            text=text,
            ingested_at=IngestedContent.now_iso(),
            source_type="file",
            metadata={
                "file_extension": path.suffix.lower(),
                "file_name": path.name,
            },
        )

    async def discover(self, source: str, **kwargs: object) -> list[str]:
        """ディレクトリ内のファイル一覧を返す.

        Args:
            source: 検索対象のディレクトリパス
            **kwargs: pattern (str) — glob パターン（デフォルト: 全対応拡張子）

        Returns:
            発見されたファイルパスのリスト
        """
        dir_path = Path(source).resolve()

        if not dir_path.exists():
            raise ValueError(f"ディレクトリが存在しません: {dir_path}")

        if not dir_path.is_dir():
            raise ValueError(f"ディレクトリではありません: {dir_path}")

        pattern = kwargs.get("pattern")
        if isinstance(pattern, str):
            paths = sorted(dir_path.glob(pattern))
        else:
            paths = sorted(
                p
                for ext in SUPPORTED_EXTENSIONS
                for p in dir_path.glob(f"**/*{ext}")
            )

        return [
            str(p.resolve())
            for p in paths
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        ]

    @staticmethod
    def _extract_title(text: str, path: Path) -> str:
        """テキストからタイトルを抽出する.

        Markdown の場合、最初の ``# 見出し`` をタイトルとして使用する。
        見出しがない場合やテキストファイルの場合、ファイル名（拡張子なし）を返す。

        Args:
            text: ファイルの内容
            path: ファイルパス

        Returns:
            抽出されたタイトル
        """
        if path.suffix.lower() == ".md":
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("# ") and not stripped.startswith("## "):
                    return stripped[2:].strip()

        return path.stem

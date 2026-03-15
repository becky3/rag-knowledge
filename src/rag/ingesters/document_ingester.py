"""ドキュメントインジェスター: テキストドキュメントを読み取りナレッジベースに取り込む

仕様: docs/specs/document-ingester.md
Issue: #184, #198
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base_ingester import BaseIngester, IngestedContent

logger = logging.getLogger(__name__)

# --- ハードリミット（コード内定数。設定・引数・環境変数で緩和不可） ---
MAX_FILES_HARD_LIMIT = 100
"""ディレクトリ一括取り込み時のファイル数上限"""


class DocumentIngester(BaseIngester):
    """ドキュメント取り込み用インジェスター.

    仕様: docs/specs/document-ingester.md

    テキストドキュメント（Markdown、プレーンテキスト、PDF、AsciiDoc）を
    読み取り、IngestedContent に変換する。BaseIngester を継承する。
    """

    def __init__(
        self,
        *,
        supported_extensions: list[str] | None = None,
    ) -> None:
        """DocumentIngester を初期化する.

        Args:
            supported_extensions: 対応ファイル拡張子のリスト
                （デフォルト: [".md", ".txt", ".pdf", ".adoc"]）
        """
        if supported_extensions is None:
            supported_extensions = [".md", ".txt", ".pdf", ".adoc"]
        self._supported_extensions = [
            ext.lower() for ext in supported_extensions
        ]

    def validate_identifier(self, identifier: str) -> str:
        """ファイルパスを検証し、正規化済みの絶対パスを返す.

        Args:
            identifier: 検証するファイルパス

        Returns:
            正規化済み絶対パス文字列

        Raises:
            ValueError: パスが空、ファイルが存在しない、
                拡張子が未対応、ディレクトリが指定された場合
        """
        if not identifier or not identifier.strip():
            raise ValueError("file_path must not be empty")

        path = Path(identifier.strip()).resolve()

        if not path.exists():
            raise ValueError(f"File not found: {path}")

        if path.is_dir():
            raise ValueError(f"Path is a directory, not a file: {path}")

        ext = path.suffix.lower()
        if ext not in self._supported_extensions:
            raise ValueError(
                f"Unsupported file extension: {ext!r}. "
                f"Supported: {', '.join(self._supported_extensions)}"
            )

        return str(path)

    async def fetch_single(self, identifier: str) -> IngestedContent | None:
        """単一ファイルのテキストを抽出し、IngestedContent を返す.

        Args:
            identifier: ファイルパス

        Returns:
            IngestedContent、または取得失敗時は None

        Raises:
            ValueError: パスが空、ファイルが存在しない、
                拡張子が未対応、ディレクトリが指定された場合
        """
        resolved_path_str = self.validate_identifier(identifier)

        path = Path(resolved_path_str)

        # 0 バイトファイルはスキップ
        try:
            file_size = path.stat().st_size
        except OSError:
            logger.exception("Failed to stat file: %s", path)
            return None

        if file_size == 0:
            logger.warning("Skipping empty file (0 bytes): %s", path)
            return None

        # テキスト抽出
        text = self._extract_text(path)
        if text is None:
            return None

        if not text.strip():
            logger.warning("No text extracted from file: %s", path)
            return None

        return IngestedContent(
            source_id=path.as_uri(),
            title=path.stem,
            text=text,
            ingested_at=IngestedContent.now_iso(),
            source_type="document",
            metadata={
                "file_extension": path.suffix.lower(),
                "file_size_bytes": file_size,
                "file_path": identifier.strip(),
            },
        )

    def _extract_text(self, path: Path) -> str | None:
        """ファイル形式に応じてテキストを抽出する.

        Args:
            path: ファイルパス（resolve 済み）

        Returns:
            抽出テキスト、または失敗時は None
        """
        ext = path.suffix.lower()

        if ext == ".pdf":
            return self._extract_pdf(path)

        # .md, .txt, .adoc およびその他の設定追加分: UTF-8 テキストとして読み取り
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.exception(
                "UTF-8 decode error for file: %s", path
            )
            return None
        except OSError:
            logger.exception("Failed to read file: %s", path)
            return None

    def _extract_pdf(self, path: Path) -> str | None:
        """PDF からテキストを抽出する.

        pymupdf4llm を使用して Markdown に変換する。

        Args:
            path: PDF ファイルパス

        Returns:
            抽出テキスト、または失敗時は None
        """
        try:
            import pymupdf4llm  # type: ignore[import-untyped]
        except ImportError:
            logger.error(
                "pymupdf4llm is not installed. "
                "Install it with: uv add pymupdf4llm"
            )
            return None

        try:
            return pymupdf4llm.to_markdown(str(path))  # type: ignore[no-any-return]
        except Exception:
            logger.exception("Failed to convert PDF to markdown: %s", path)
            return None

    def validate_pattern(self, pattern: str) -> str:
        """glob パターンをバリデーションする.

        Args:
            pattern: glob パターン

        Returns:
            バリデーション済みパターン

        Raises:
            ValueError: パターンに '..' が含まれる、
                または絶対パスの場合
        """
        if ".." in pattern:
            raise ValueError(
                f"Pattern must not contain '..': {pattern!r}"
            )
        if pattern.startswith("/") or Path(pattern).is_absolute():
            raise ValueError(
                f"Pattern must not be an absolute path: {pattern!r}"
            )
        return pattern

    def collect_files(
        self,
        dir_path: str,
        pattern: str = "**/*",
        *,
        max_files: int = MAX_FILES_HARD_LIMIT,
    ) -> list[Path]:
        """ディレクトリから対象ファイルを収集する.

        Args:
            dir_path: ディレクトリパス
            pattern: glob パターン（デフォルト: ``**/*``）
            max_files: ファイル数上限（デフォルト: MAX_FILES_HARD_LIMIT）

        Returns:
            収集されたファイルパスのリスト（辞書順ソート済み、上限適用済み）

        Raises:
            ValueError: dir_path が空、存在しない、
                ディレクトリでない、pattern が不正な場合
        """
        if not dir_path or not dir_path.strip():
            raise ValueError("dir_path must not be empty")

        resolved_dir = Path(dir_path.strip()).resolve()

        if not resolved_dir.exists():
            raise ValueError(f"Directory not found: {resolved_dir}")

        if not resolved_dir.is_dir():
            raise ValueError(
                f"Path is not a directory: {resolved_dir}"
            )

        # パターンバリデーション
        validated_pattern = self.validate_pattern(pattern)

        # ハードリミット適用（max_files が 0 以下の場合は 1 にクランプ）
        effective_limit = min(max(max_files, 1), MAX_FILES_HARD_LIMIT)

        # glob を遅延イテレーションし、候補ファイルを収集する。
        # メモリ使用量を抑えるため、候補数の上限（scan_cap）を設ける。
        # ソートが必要なため effective_limit より多めに収集する。
        scan_cap = effective_limit * 10
        files: list[Path] = []
        for p in resolved_dir.glob(validated_pattern):
            resolved = p.resolve()

            # ファイルでない場合はスキップ
            if not resolved.is_file():
                continue

            # dir_path 配下チェック
            try:
                resolved.relative_to(resolved_dir)
            except ValueError:
                logger.warning(
                    "File outside dir_path excluded: %s", resolved
                )
                continue

            # 対応拡張子チェック
            if resolved.suffix.lower() not in self._supported_extensions:
                continue

            files.append(resolved)

            # スキャン上限に達したら打ち切り
            if len(files) >= scan_cap:
                logger.warning(
                    "Glob scan reached cap (%d candidates), "
                    "stopping enumeration early",
                    scan_cap,
                )
                break

        # パスの辞書順でソート
        files.sort(key=lambda p: str(p))

        # ファイル数上限適用
        if len(files) > effective_limit:
            logger.warning(
                "File count %d exceeds limit %d, clamping to %d",
                len(files),
                effective_limit,
                effective_limit,
            )
            files = files[:effective_limit]

        return files

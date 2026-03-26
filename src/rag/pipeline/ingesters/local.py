"""Local インジェスター — ローカルファイルを source_store に配置.

仕様: docs/specs/ingesters/local.md
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from rag.pipeline.ingesters._common import IngestResult

if TYPE_CHECKING:
    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)

MAX_FILES_HARD_LIMIT = 100
DEFAULT_SUPPORTED_EXTENSIONS: list[str] = [".md", ".txt", ".pdf", ".adoc"]

# MCP ツール経由アップロードのベースディレクトリ
_UPLOAD_DIR = ".upload"

UploadMode = Literal["fail", "replace"]


class LocalIngester:
    """ローカルファイル取り込み用インジェスター.

    仕様: docs/specs/ingesters/local.md
    """

    def __init__(self, source_store: SourceStore, *, supported_extensions: list[str] | None = None, http_mode_enabled: bool = False, allowed_dirs: list[str] | None = None) -> None:
        self._store = source_store
        self._extensions = [ext.lower() for ext in (supported_extensions or DEFAULT_SUPPORTED_EXTENSIONS)]
        self._http_mode_enabled = http_mode_enabled
        self._allowed_dirs = [Path(d.strip()).resolve() for d in (allowed_dirs or []) if d.strip()]

    def add_document(
        self,
        data: bytes,
        filename: str,
        *,
        upload_mode: UploadMode = "fail",
    ) -> IngestResult:
        """単一ドキュメントのバイト列を source_store に配置する.

        Args:
            data: ファイルのバイト列（MCP 経由はデコード済み、CLI 経由は直接読み込み）
            filename: 配置先命名に使用するファイル名（サニタイズ済みであること）
            upload_mode: 同名ファイル存在時の動作
                ``fail`` — エラー（デフォルト）、``replace`` — 上書き
        """
        result = IngestResult()
        try:
            if len(data) == 0:
                raise ValueError(f"ファイルが空です (0 バイト): {filename}")

            rel_path = self._upload_rel_path(filename)

            if upload_mode == "fail" and self._file_exists(rel_path):
                raise FileExistsError(
                    f"同名ファイルが既に存在します: {rel_path}"
                )

            self._store.place_file(source_type="local", data=data, rel_path=rel_path)
            result.placed = 1
        except FileExistsError:
            raise
        except ValueError as e:
            result.errors = 1
            result.error_details.append(str(e))
        except OSError as e:
            logger.exception("Failed to place file: %s", filename)
            result.errors = 1
            result.error_details.append(str(e))
        return result

    def crawl_documents(
        self,
        dir_path: str,
        pattern: str = "**/*",
        *,
        upload_mode: UploadMode = "fail",
    ) -> IngestResult:
        """ディレクトリ内のドキュメントファイルを一括配置する.

        Args:
            dir_path: 取り込み対象ディレクトリのパス
            pattern: glob パターン
            upload_mode: 同名ファイル存在時の動作
                ``fail`` — スキップ（個別ファイル）、``replace`` — 上書き
        """
        result = IngestResult()
        try:
            files = self._collect_files(dir_path, pattern)
        except ValueError as e:
            result.errors = 1
            result.error_details.append(str(e))
            return result
        if not files:
            return result
        resolved_dir = Path(dir_path.strip()).resolve()
        dir_basename = resolved_dir.name
        date_prefix = self._upload_date_prefix()
        for fp in files:
            try:
                if fp.stat().st_size == 0:
                    logger.warning("Skipping empty file (0 bytes): %s", fp)
                    result.skipped += 1
                    continue
                relative = fp.relative_to(resolved_dir)
                rel_path = (
                    f"local/{_UPLOAD_DIR}/{date_prefix}"
                    f"/{dir_basename}/{relative.as_posix()}"
                )

                if upload_mode == "fail" and self._file_exists(rel_path):
                    logger.warning(
                        "File already exists, skipping: %s", rel_path,
                    )
                    result.skipped += 1
                    continue

                data = fp.read_bytes()
                self._store.place_file(source_type="local", data=data, rel_path=rel_path)
                result.placed += 1
            except OSError:
                logger.exception("Failed to copy file: %s", fp)
                result.errors += 1
                result.error_details.append(str(fp))
        return result

    @staticmethod
    def _upload_date_prefix() -> str:
        """アップロード日に基づくディレクトリプレフィックスを返す."""
        today = datetime.date.today()
        return f"{today.year}/{today.month:02d}/{today.day:02d}"

    def _upload_rel_path(self, filename: str) -> str:
        """MCP ツール経由アップロード用の source_store 相対パスを返す."""
        date_prefix = self._upload_date_prefix()
        return f"local/{_UPLOAD_DIR}/{date_prefix}/{filename}"

    def _file_exists(self, rel_path: str) -> bool:
        """source_store 内にファイルが存在するか確認する."""
        return (self._store.root_dir / rel_path).exists()

    def _validate_single_file(self, file_path: str) -> Path:
        if not file_path or not file_path.strip():
            raise ValueError("file_path must not be empty")
        resolved = Path(file_path.strip()).resolve()
        if not resolved.exists():
            raise ValueError(f"File not found: {resolved}")
        if resolved.is_dir():
            raise ValueError(f"Path is a directory, not a file: {resolved}")
        ext = resolved.suffix.lower()
        if ext not in self._extensions:
            raise ValueError(f"Unsupported file extension: {ext!r}. Supported: {', '.join(self._extensions)}")
        if resolved.stat().st_size == 0:
            raise ValueError(f"File is empty (0 bytes): {resolved}")
        self._check_http_mode_access(resolved)
        return resolved

    def _check_http_mode_access(self, path: Path) -> None:
        if not self._http_mode_enabled:
            return
        if not self._allowed_dirs:
            raise ValueError("HTTP モードでは rag_document_allowed_dirs の設定が必要です")
        resolved = path.resolve()
        for allowed in self._allowed_dirs:
            try:
                resolved.relative_to(allowed)
                return
            except ValueError:
                continue
        raise ValueError(f"HTTP モードでは許可ディレクトリ外のファイルにアクセスできません: {path}")

    def _collect_files(self, dir_path: str, pattern: str = "**/*") -> list[Path]:
        if not dir_path or not dir_path.strip():
            raise ValueError("dir_path must not be empty")
        resolved_dir = Path(dir_path.strip()).resolve()
        if not resolved_dir.exists():
            raise ValueError(f"Directory not found: {resolved_dir}")
        if not resolved_dir.is_dir():
            raise ValueError(f"Path is not a directory: {resolved_dir}")
        if resolved_dir == Path(resolved_dir.anchor):
            raise ValueError(f"Root directory is not allowed: {resolved_dir}")
        if ".." in pattern:
            raise ValueError(f"Pattern must not contain '..': {pattern!r}")
        if pattern.startswith("/") or Path(pattern).is_absolute():
            raise ValueError(f"Pattern must not be an absolute path: {pattern!r}")
        self._check_http_mode_access(resolved_dir)
        files: list[Path] = []
        for p in resolved_dir.glob(pattern):
            resolved = p.resolve()
            if not resolved.is_file():
                continue
            try:
                resolved.relative_to(resolved_dir)
            except ValueError:
                logger.warning("File outside dir_path excluded: %s", resolved)
                continue
            if resolved.suffix.lower() not in self._extensions:
                continue
            files.append(resolved)
        files.sort(key=lambda p: str(p))
        if len(files) > MAX_FILES_HARD_LIMIT:
            logger.warning("File count %d exceeds limit %d, clamping to %d", len(files), MAX_FILES_HARD_LIMIT, MAX_FILES_HARD_LIMIT)
            files = files[:MAX_FILES_HARD_LIMIT]
        return files

"""コンバーター: source_store → converted_store のテキスト変換.

仕様: docs/specs/converter.md
Issue: #249

ConverterProtocol を実装し、パイプライン制御から呼び出される。
ファイル拡張子に応じた変換方式を選択し、
インデクサーが処理可能な統一テキスト形式を出力する。
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from rag.converter.handlers import (
    convert_html,
    convert_json_bluesky,
    convert_json_zenn_scrap,
    convert_pdf,
    passthrough_copy,
)
from rag.converter.normalize import normalize_text
from rag.ingesters.document_ingester import DocumentIngester, PdfBackendConfig
from rag.pipeline.models import detect_source_type
from rag.store.models import SourceType

logger = logging.getLogger(__name__)

RegenOption = Literal["skip", "if_modified", "force"]

# 拡張子 → 出力拡張子のマッピング（変換対象）
_EXTENSION_OUTPUT_MAP: dict[str, str] = {
    ".html": ".md",
    ".pdf": ".md",
    ".json": ".md",
}

# パススルー対象の拡張子
_PASSTHROUGH_EXTENSIONS: frozenset[str] = frozenset({".md", ".txt", ".adoc"})

# 変換対象外のファイル名
_EXCLUDED_FILES: frozenset[str] = frozenset({"metadata.db"})


class ConversionSkippedError(Exception):
    """変換がスキップされた場合の例外."""


@dataclass
class ConvertBatchResult:
    """一括変換の結果サマリ."""

    success: int = 0
    skipped: int = 0
    errors: int = 0
    error_files: list[str] = field(default_factory=list)


class Converter:
    """コンバーター: source_store → converted_store のテキスト変換.

    ConverterProtocol を満たす実装。
    ファイル拡張子に応じた変換方式を選択し、
    インデクサーが処理可能な統一テキスト形式を出力する。
    """

    def __init__(
        self,
        *,
        regen_option: RegenOption = "skip",
        pdf_config: PdfBackendConfig | None = None,
    ) -> None:
        """Converter を初期化する.

        Args:
            regen_option: 再生成オプション（デフォルト: skip）
            pdf_config: PDF バックエンド設定
        """
        self._regen_option = regen_option
        self._doc_ingester = DocumentIngester(
            pdf_config=pdf_config or PdfBackendConfig(),
        )

    # --- ConverterProtocol 実装 ---

    def convert(
        self,
        file_path: str,
        source_store_dir: Path,
        converted_store_dir: Path,
    ) -> Path:
        """ソースファイルをテキストに変換する.

        Args:
            file_path: source_store 内の相対パス
            source_store_dir: source_store のルートディレクトリ
            converted_store_dir: converted_store のルートディレクトリ

        Returns:
            converted_store 内の変換済みファイルの絶対パス

        Raises:
            ConversionSkippedError: 変換がスキップされた場合
        """
        # 変換対象外チェック
        file_name = PurePosixPath(file_path).name
        if file_name in _EXCLUDED_FILES:
            raise ConversionSkippedError(f"Excluded file: {file_name}")
        if file_path.endswith(".meta"):
            raise ConversionSkippedError(f"Meta file excluded: {file_path}")

        source_path = source_store_dir / file_path
        ext = PurePosixPath(file_path).suffix.lower()

        # ソースファイル存在チェック
        if not source_path.exists():
            logger.warning("Source file not found: %s", file_path)
            raise ConversionSkippedError(f"Source not found: {file_path}")

        # 0 バイトチェック
        if source_path.stat().st_size == 0:
            logger.warning("Skipping 0-byte file: %s", file_path)
            raise ConversionSkippedError(f"0-byte file: {file_path}")

        # サポートされている拡張子かチェック
        if ext not in _EXTENSION_OUTPUT_MAP and ext not in _PASSTHROUGH_EXTENSIONS:
            logger.warning("Unsupported extension: %s (%s)", ext, file_path)
            raise ConversionSkippedError(f"Unsupported extension: {ext}")

        # 出力パス算出
        converted_rel_path = _get_converted_rel_path(file_path)
        converted_path = converted_store_dir / converted_rel_path

        # パススルー判定
        is_passthrough = ext in _PASSTHROUGH_EXTENSIONS

        # 再生成オプションのチェック
        if converted_path.exists():
            if self._regen_option == "skip":
                return converted_path
            if self._regen_option == "if_modified":
                source_mtime = source_path.stat().st_mtime
                converted_mtime = converted_path.stat().st_mtime
                if source_mtime <= converted_mtime:
                    return converted_path

        # パススルー
        if is_passthrough:
            try:
                passthrough_copy(source_path, converted_path)
                return converted_path
            except OSError:
                logger.exception("Passthrough copy failed: %s", file_path)
                raise ConversionSkippedError(
                    f"Copy failed: {file_path}",
                ) from None

        # 変換処理
        text = self._dispatch_conversion(ext, source_path, file_path)

        if text is None or not text.strip():
            # 空結果: 既存ファイルを削除（Zenn スクラップ空コメント等）
            if converted_path.exists():
                converted_path.unlink()
            logger.warning("No text extracted: %s", file_path)
            raise ConversionSkippedError(
                f"Empty conversion result: {file_path}",
            )

        # テキスト正規化
        normalized = normalize_text(text)

        # 出力
        converted_path.parent.mkdir(parents=True, exist_ok=True)
        converted_path.write_text(normalized, encoding="utf-8")
        return converted_path

    def delete(
        self,
        file_path: str,
        converted_store_dir: Path,
    ) -> None:
        """converted_store から変換済みファイルを削除する.

        Args:
            file_path: source_store 内の相対パス
            converted_store_dir: converted_store のルートディレクトリ
        """
        converted_rel_path = _get_converted_rel_path(file_path)
        converted_path = converted_store_dir / converted_rel_path
        if converted_path.exists():
            converted_path.unlink()
            logger.debug("Deleted converted file: %s", converted_path)

    def clear(
        self,
        converted_store_dir: Path,
        source_type: SourceType | None = None,
    ) -> None:
        """converted_store をクリアする.

        Args:
            converted_store_dir: converted_store のルートディレクトリ
            source_type: 指定時はその媒体のディレクトリのみクリア
        """
        if source_type is not None:
            target_dir = converted_store_dir / source_type
            if target_dir.exists():
                shutil.rmtree(target_dir)
                logger.info("Cleared converted_store: %s", target_dir)
        else:
            if converted_store_dir.exists():
                shutil.rmtree(converted_store_dir)
                logger.info(
                    "Cleared entire converted_store: %s",
                    converted_store_dir,
                )

    # --- 一括変換（プロトコル外） ---

    def convert_batch(
        self,
        file_paths: list[str],
        source_store_dir: Path,
        converted_store_dir: Path,
        *,
        regen_option: RegenOption | None = None,
    ) -> ConvertBatchResult:
        """複数ファイルを一括変換する.

        Args:
            file_paths: source_store 内の相対パスのリスト
            source_store_dir: source_store のルートディレクトリ
            converted_store_dir: converted_store のルートディレクトリ
            regen_option: 再生成オプション（None 時はインスタンスのデフォルトを使用）

        Returns:
            変換結果サマリ
        """
        saved_option = self._regen_option
        if regen_option is not None:
            self._regen_option = regen_option

        result = ConvertBatchResult()
        try:
            for file_path in file_paths:
                try:
                    self.convert(
                        file_path, source_store_dir, converted_store_dir,
                    )
                    result.success += 1
                except ConversionSkippedError:
                    result.skipped += 1
                except Exception:
                    logger.exception("Conversion error: %s", file_path)
                    result.errors += 1
                    result.error_files.append(file_path)
        finally:
            self._regen_option = saved_option

        return result

    # --- 内部メソッド ---

    def _dispatch_conversion(
        self,
        ext: str,
        source_path: Path,
        file_path: str,
    ) -> str | None:
        """拡張子に応じた変換処理を実行する.

        Args:
            ext: ファイル拡張子（小文字、ドット付き）
            source_path: ソースファイルの絶対パス
            file_path: source_store 内の相対パス

        Returns:
            変換後テキスト、または失敗時は None
        """
        if ext == ".html":
            return convert_html(source_path)

        if ext == ".pdf":
            return convert_pdf(source_path, self._doc_ingester)

        if ext == ".json":
            return self._convert_json(source_path, file_path)

        return None

    def _convert_json(
        self,
        source_path: Path,
        file_path: str,
    ) -> str | None:
        """JSON ファイルを source_type に応じて変換する.

        Args:
            source_path: JSON ファイルの絶対パス
            file_path: source_store 内の相対パス

        Returns:
            抽出テキスト、または変換不可時は None
        """
        source_type = detect_source_type(file_path)

        try:
            raw = source_path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            logger.exception("Failed to read JSON: %s", file_path)
            return None

        if not isinstance(parsed, dict):
            logger.warning(
                "JSON root is not an object: %s (%s)",
                type(parsed).__name__,
                file_path,
            )
            return None

        data: dict[str, object] = parsed

        if source_type == "bluesky":
            return convert_json_bluesky(data)

        if source_type == "zenn" and "/scraps/" in file_path:
            return convert_json_zenn_scrap(data)

        # 不明な JSON source_type
        logger.warning(
            "Unknown JSON source_type for conversion: %s (%s)",
            source_type,
            file_path,
        )
        return None


def _get_converted_rel_path(file_path: str) -> str:
    """source_store 相対パスから converted_store の相対パスを算出する.

    Args:
        file_path: source_store 内の相対パス

    Returns:
        converted_store 内の相対パス
    """
    p = PurePosixPath(file_path)
    ext = p.suffix.lower()
    new_ext = _EXTENSION_OUTPUT_MAP.get(ext)
    if new_ext is not None:
        return str(p.with_suffix(new_ext))
    return file_path

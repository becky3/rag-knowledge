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
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from rag.media.analyzer import MediaAnalyzer

from rag.converter.handlers import (
    convert_html,
    convert_json_bluesky,
    convert_json_youtube,
    convert_json_zenn_article,
    convert_json_zenn_scrap,
    passthrough_copy,
)
from rag.converter.normalize import normalize_text
from rag.converter.pdf_extractor import PdfBackendConfig, extract_pdf
from rag.pipeline.models import detect_source_type
from rag.store.meta import read_meta
from rag.store.models import SourceType

logger = logging.getLogger(__name__)

RegenOption = Literal["skip", "if_modified", "force"]

# 拡張子 → 出力拡張子のマッピング（変換対象）
_EXTENSION_OUTPUT_MAP: dict[str, str] = {
    ".html": ".md",
    ".htm": ".md",
    ".pdf": ".md",
    ".json": ".md",
    # メディア解析対象（画像）
    ".webp": ".md",
    ".jpg": ".md",
    ".jpeg": ".md",
    ".png": ".md",
    # メディア解析対象（動画）
    ".ts": ".md",  # MPEG-TS（HLS セグメント）

    ".mp4": ".md",
}

# パススルー対象の拡張子
_PASSTHROUGH_EXTENSIONS: frozenset[str] = frozenset({".md", ".txt", ".adoc"})

# メディア解析対象の拡張子（handlers.py からも参照される public 定数）
IMAGE_EXTENSIONS: frozenset[str] = frozenset({".webp", ".jpg", ".jpeg", ".png"})
VIDEO_EXTENSIONS: frozenset[str] = frozenset({".ts", ".mp4"})
MEDIA_EXTENSIONS: frozenset[str] = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

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
        regen_option: RegenOption,
        pdf_config: PdfBackendConfig,
        youtube_merge_gap_sec: float,
        youtube_merge_max_chars: int,
        media_analyzer: MediaAnalyzer | None,
    ) -> None:
        """Converter を初期化する.

        Args:
            regen_option: 再生成オプション
            pdf_config: PDF バックエンド設定
            youtube_merge_gap_sec: YouTube スニペット結合の間隔閾値（秒）
            youtube_merge_max_chars: YouTube スニペット結合の最大文字数
            media_analyzer: メディア解析モジュール（None の場合メディア解析スキップ）
        """
        self._regen_option = regen_option
        self._pdf_config = pdf_config
        self._youtube_merge_gap_sec = youtube_merge_gap_sec
        self._youtube_merge_max_chars = youtube_merge_max_chars
        self._media_analyzer = media_analyzer

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
        converted_rel_path = get_converted_rel_path(file_path)
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
        text = self._dispatch_conversion(
            ext, source_path, file_path, source_store_dir,
        )

        # Zenn 記事: body_html にタイトルが含まれないため .meta から取得して先頭付与
        if text is not None:
            text = self._prepend_title_from_meta(
                text, file_path, source_store_dir,
            )

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
        converted_rel_path = get_converted_rel_path(file_path)
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

        # バッチ開始時に Vision API の利用可否をキャッシュ
        if self._media_analyzer is not None:
            self._media_analyzer.check_and_cache_availability()

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
            if self._media_analyzer is not None:
                self._media_analyzer.clear_availability_cache()

        return result

    # --- 内部メソッド ---

    def _prepend_title_from_meta(
        self,
        text: str,
        file_path: str,
        source_store_dir: Path,
    ) -> str:
        """タイトルがコンテンツに含まれないソースに .meta のタイトルを先頭付与する.

        Zenn 記事の body_html にはタイトルが含まれないため、
        .meta の title フィールドを H1 見出しとして先頭に付与する。

        Args:
            text: 変換後テキスト
            file_path: source_store 内の相対パス
            source_store_dir: source_store のルートディレクトリ

        Returns:
            タイトル付与済みテキスト（対象外の場合はそのまま返す）
        """
        source_type = detect_source_type(file_path)
        if source_type != "zenn" or "/articles/" not in file_path:
            return text

        source_path = source_store_dir / file_path
        try:
            meta = read_meta(source_path)
            title = meta.get("title", "")
            if title:
                return f"# {title}\n\n{text}"
        except (FileNotFoundError, ValueError, OSError):
            logger.debug("No .meta for title prepend: %s", file_path)

        return text

    def _dispatch_conversion(
        self,
        ext: str,
        source_path: Path,
        file_path: str,
        source_store_dir: Path,
    ) -> str | None:
        """拡張子に応じた変換処理を実行する.

        Args:
            ext: ファイル拡張子（小文字、ドット付き）
            source_path: ソースファイルの絶対パス
            file_path: source_store 内の相対パス
            source_store_dir: source_store のルートディレクトリ

        Returns:
            変換後テキスト、または失敗時は None
        """
        if ext in (".html", ".htm"):
            return convert_html(source_path)

        if ext == ".pdf":
            return extract_pdf(source_path, self._pdf_config)

        if ext == ".json":
            return self._convert_json(source_path, file_path, source_store_dir)

        if ext in MEDIA_EXTENSIONS:
            return self._convert_media(ext, source_path)

        return None

    def _convert_media(
        self,
        ext: str,
        source_path: Path,
    ) -> str | None:
        """メディアファイルを Vision モデルでテキスト化する.

        Args:
            ext: ファイル拡張子（小文字、ドット付き）
            source_path: メディアファイルの絶対パス

        Returns:
            解析テキスト。メディア解析を実行できない場合は
            ConversionSkippedError を送出する。
        """
        if self._media_analyzer is None:
            raise ConversionSkippedError(
                f"メディア解析モジュール未設定のためスキップ: {source_path}",
            )

        if not self._media_analyzer.is_available():
            raise ConversionSkippedError(
                f"LM Studio が利用不可のためメディア解析をスキップ: {source_path}",
            )

        if ext in IMAGE_EXTENSIONS:
            text = self._media_analyzer.analyze_image(source_path)
        elif ext in VIDEO_EXTENSIONS:
            text = self._media_analyzer.analyze_video(source_path)
        else:
            return None

        return text if text else None

    def _convert_json(
        self,
        source_path: Path,
        file_path: str,
        source_store_dir: Path,
    ) -> str | None:
        """JSON ファイルを source_type に応じて変換する.

        Args:
            source_path: JSON ファイルの絶対パス
            file_path: source_store 内の相対パス
            source_store_dir: source_store のルートディレクトリ

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
            return convert_json_bluesky(
                data,
                media_analyzer=self._media_analyzer,
                source_store_dir=source_store_dir,
                file_path=file_path,
            )

        if source_type == "youtube":
            return convert_json_youtube(
                data,
                merge_gap_sec=self._youtube_merge_gap_sec,
                merge_max_chars=self._youtube_merge_max_chars,
            )

        if source_type == "zenn" and "/articles/" in file_path:
            return convert_json_zenn_article(data)

        if source_type == "zenn" and "/scraps/" in file_path:
            return convert_json_zenn_scrap(data)

        # 不明な JSON source_type
        logger.warning(
            "Unknown JSON source_type for conversion: %s (%s)",
            source_type,
            file_path,
        )
        return None


def get_converted_rel_path(file_path: str) -> str:
    """source_store 相対パスから converted_store の相対パスを算出する.

    拡張子マッピング（_EXTENSION_OUTPUT_MAP）に基づいて、
    source_store の拡張子を converted_store の拡張子に変換する。

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

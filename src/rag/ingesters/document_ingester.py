"""ドキュメントインジェスター: テキストドキュメントを読み取りナレッジベースに取り込む

仕様: docs/specs/document-ingester.md
Issue: #184, #198, #221
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .base_ingester import BaseIngester, IngestedContent

logger = logging.getLogger(__name__)

# --- ハードリミット（コード内定数。設定・引数・環境変数で緩和不可） ---
MAX_FILES_HARD_LIMIT = 100
"""ディレクトリ一括取り込み時のファイル数上限"""

# --- PDF 事前判定用の定数 ---
_TEX_KEYWORDS = ("tex", "latex", "pdflatex", "xelatex", "lualatex", "dvips", "dvipdfm")
_MATH_FONT_KEYWORDS = (
    "cmmi", "cmsy", "cmex", "msam", "msbm", "stix",
    "cambria math", "latin modern math",
)


@dataclass(frozen=True)
class PdfBackendConfig:
    """PDF バックエンド関連の設定."""

    backend: Literal["auto", "mineru", "pymupdf4llm"] = "auto"
    mineru_mfd_conf_thres: float = 0.6
    quality_ufffd_threshold: float = 0.10
    quality_greek_threshold: float = 0.15
    quality_cjk_min_threshold: float = 0.05
    quality_min_chars_per_page: int = 10
    quality_sample_pages: int = 10


@dataclass(frozen=True)
class _PdfAssessment:
    """PDF 事前判定の結果."""

    backend: Literal["mineru", "pymupdf4llm"]
    mineru_mode: str | None = None  # "ocr" or "txt"
    reason: str = ""


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
        pdf_config: PdfBackendConfig | None = None,
    ) -> None:
        """DocumentIngester を初期化する.

        Args:
            supported_extensions: 対応ファイル拡張子のリスト
                （デフォルト: [".md", ".txt", ".pdf", ".adoc"]）
            pdf_config: PDF バックエンド設定（デフォルト: PdfBackendConfig()）
        """
        if supported_extensions is None:
            supported_extensions = [".md", ".txt", ".pdf", ".adoc"]
        self._supported_extensions = [
            ext.lower() for ext in supported_extensions
        ]
        self._pdf_config = pdf_config or PdfBackendConfig()

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

        仕様: docs/specs/document-ingester.md#pdf-抽出バックエンドの自動選択

        PDF バックエンド設定に応じて pymupdf4llm または MinerU を使用する。

        Args:
            path: PDF ファイルパス

        Returns:
            抽出テキスト、または失敗時は None
        """
        cfg = self._pdf_config
        backend = cfg.backend

        if backend == "pymupdf4llm":
            return self._extract_pdf_pymupdf4llm(path)

        if backend == "mineru":
            if not self._is_mineru_available():
                logger.error(
                    "MinerU is not installed but RAG_PDF_BACKEND=mineru. "
                    "Install with: uv sync --extra mineru"
                )
                return None
            return self._extract_pdf_mineru(path, mode="ocr")

        # backend == "auto": 事前判定フロー
        assessment = self._assess_pdf(path)
        if assessment.backend == "pymupdf4llm":
            logger.info("PDF backend: pymupdf4llm (%s)", assessment.reason)
            return self._extract_pdf_pymupdf4llm(path)

        # MinerU が必要だが未インストール → フォールバック
        if not self._is_mineru_available():
            logger.warning(
                "PDF assessment recommends MinerU (%s) but it is not installed. "
                "Falling back to pymupdf4llm. Install with: uv sync --extra mineru",
                assessment.reason,
            )
            return self._extract_pdf_pymupdf4llm(path)

        logger.info(
            "PDF backend: MinerU mode=%s (%s)",
            assessment.mineru_mode,
            assessment.reason,
        )
        return self._extract_pdf_mineru(path, mode=assessment.mineru_mode or "ocr")

    def _assess_pdf(self, path: Path) -> _PdfAssessment:
        """PDF を軽量検査し、最適なバックエンドを判定する.

        3 フェーズの検査を実行し、判定マトリクスに基づいてバックエンドを選択する。

        Args:
            path: PDF ファイルパス

        Returns:
            判定結果
        """
        try:
            import pymupdf
        except ImportError:
            logger.warning("pymupdf not available for PDF assessment, using pymupdf4llm")
            return _PdfAssessment(backend="pymupdf4llm", reason="pymupdf not available")

        try:
            doc = pymupdf.open(str(path))  # type: ignore[no-untyped-call]
        except Exception:
            logger.warning("Failed to open PDF for assessment: %s", path)
            return _PdfAssessment(backend="pymupdf4llm", reason="assessment failed")

        try:
            return self._run_assessment(doc)
        finally:
            doc.close()  # type: ignore[no-untyped-call]

    def _run_assessment(self, doc: Any) -> _PdfAssessment:
        """PDF ドキュメントに対して 3 フェーズ検査を実行する.

        Args:
            doc: pymupdf.Document オブジェクト

        Returns:
            判定結果
        """
        cfg = self._pdf_config
        tex_origin = False
        math_font_detected = False
        tounicode_missing = False

        # --- Phase 1: メタデータ検査 ---
        metadata = doc.metadata
        if metadata:
            producer = (metadata.get("producer") or "").lower()
            creator = (metadata.get("creator") or "").lower()
            combined = producer + " " + creator
            if any(kw in combined for kw in _TEX_KEYWORDS):
                tex_origin = True

        # --- Phase 2: フォント検査 ---
        page_count: int = doc.page_count
        for page_idx in range(page_count):
            page = doc[page_idx]
            fonts = page.get_fonts()
            for font_info in fonts:
                font_name = (font_info[3] if len(font_info) > 3 else "").lower()
                # 数式フォント検出
                if any(kw in font_name for kw in _MATH_FONT_KEYWORDS):
                    math_font_detected = True

                # Type0 (CID) フォントの ToUnicode チェック
                font_type = font_info[1] if len(font_info) > 1 else ""
                if font_type == "Type0":
                    xref = font_info[0] if len(font_info) > 0 else 0
                    if xref:
                        try:
                            key_val = doc.xref_get_key(xref, "ToUnicode")
                            if key_val[0] == "null" or not key_val[1]:
                                tounicode_missing = True
                        except Exception:
                            logger.debug("ToUnicode check failed for xref %d", xref)

        # --- Phase 3: サンプルテキスト品質検査 ---
        max_samples = min(cfg.quality_sample_pages, page_count)
        if max_samples > 0 and page_count > 0:
            step = page_count / max_samples
            sample_indices = [int(step * i + step / 2) for i in range(max_samples)]
        else:
            sample_indices = []

        total_chars = 0
        ufffd_count = 0
        cjk_count = 0
        greek_count = 0
        pages_sampled = 0

        for idx in sample_indices:
            if idx >= page_count:
                continue
            page = doc[idx]
            try:
                text: str = page.get_text()
            except Exception:
                continue
            pages_sampled += 1
            total_chars += len(text)
            for ch in text:
                cp = ord(ch)
                if cp == 0xFFFD:
                    ufffd_count += 1
                elif 0x4E00 <= cp <= 0x9FFF or 0x3040 <= cp <= 0x30FF or 0x3400 <= cp <= 0x4DBF:
                    cjk_count += 1
                elif 0x0370 <= cp <= 0x03FF or 0x1F00 <= cp <= 0x1FFF:
                    greek_count += 1

        # 品質指標の算出
        ufffd_ratio = ufffd_count / total_chars if total_chars > 0 else 0.0
        cjk_ratio = cjk_count / total_chars if total_chars > 0 else 0.0
        greek_ratio = greek_count / total_chars if total_chars > 0 else 0.0
        avg_chars_per_page = total_chars / pages_sampled if pages_sampled > 0 else 0.0

        # --- 判定マトリクス（上から順に評価） ---
        if tounicode_missing:
            return _PdfAssessment(
                backend="mineru", mineru_mode="ocr", reason="ToUnicode CMap missing"
            )
        if ufffd_ratio > cfg.quality_ufffd_threshold:
            return _PdfAssessment(
                backend="mineru", mineru_mode="ocr",
                reason=f"ufffd ratio {ufffd_ratio:.2%} > {cfg.quality_ufffd_threshold:.0%}",
            )
        if cjk_ratio < cfg.quality_cjk_min_threshold and greek_ratio > cfg.quality_greek_threshold:
            return _PdfAssessment(
                backend="mineru", mineru_mode="ocr",
                reason=f"CJK {cjk_ratio:.2%} < {cfg.quality_cjk_min_threshold:.0%} "
                       f"and Greek {greek_ratio:.2%} > {cfg.quality_greek_threshold:.0%}",
            )
        if avg_chars_per_page < cfg.quality_min_chars_per_page:
            return _PdfAssessment(
                backend="mineru", mineru_mode="ocr",
                reason=f"avg chars/page {avg_chars_per_page:.0f} < {cfg.quality_min_chars_per_page}",
            )
        if math_font_detected:
            return _PdfAssessment(
                backend="mineru", mineru_mode="txt", reason="math font detected"
            )
        if tex_origin:
            return _PdfAssessment(
                backend="mineru", mineru_mode="txt", reason="TeX-origin metadata"
            )

        return _PdfAssessment(backend="pymupdf4llm", reason="normal PDF")

    @staticmethod
    def _is_mineru_available() -> bool:
        """MinerU がインストールされているか確認する."""
        try:
            import mineru  # noqa: F401
            return True
        except ImportError:
            return False

    def _extract_pdf_pymupdf4llm(self, path: Path) -> str | None:
        """pymupdf4llm で PDF からテキストを抽出する.

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

    def _extract_pdf_mineru(self, path: Path, *, mode: str = "ocr") -> str | None:
        """MinerU で PDF からテキストを抽出する.

        Args:
            path: PDF ファイルパス
            mode: MinerU の解析モード（"ocr" or "txt"）

        Returns:
            抽出テキスト（Markdown 形式）、または失敗時は None
        """
        try:
            from mineru.pdf_parser import PDFParser
        except ImportError:
            logger.error(
                "MinerU is not installed. Install with: uv sync --extra mineru"
            )
            return None

        # デバイス選択
        device = "cpu"
        try:
            import torch
            if torch.cuda.is_available():
                device = "cuda"
        except ImportError:
            pass

        # MFD conf_thres のモンキーパッチ
        target_conf = self._pdf_config.mineru_mfd_conf_thres
        self._apply_mfd_conf_thres_patch(target_conf)

        try:
            parser = PDFParser(
                str(path),
                method=mode,
                device=device,
            )
            result = parser.parse()
            # MinerU の parse() は (content_list, images) のタプルを返す
            if isinstance(result, tuple):
                content_list = result[0]
            else:
                content_list = result

            # content_list から Markdown テキストを組み立てる
            md_parts: list[str] = []
            for item in content_list:
                if isinstance(item, dict):
                    text = item.get("text", "")
                    if text:
                        md_parts.append(text)
                elif isinstance(item, str):
                    md_parts.append(item)
            return "\n\n".join(md_parts)
        except Exception:
            logger.exception("Failed to extract PDF with MinerU: %s", path)
            return None

    @staticmethod
    def _apply_mfd_conf_thres_patch(target_conf: float) -> None:
        """MinerU の MFD 信頼度閾値をモンキーパッチで適用する.

        初回のみパッチを適用し、以降は target_conf が変更された場合のみ再適用する。

        Args:
            target_conf: 適用する信頼度閾値
        """
        try:
            import mineru.model.mfd.yolo_v8 as mfd_module
            cls = mfd_module.YOLOv8MFDModel

            # 既にパッチ済みかつ同じ閾値なら何もしない
            current_conf = getattr(cls, "_mfd_patched_conf", None)
            if current_conf == target_conf:
                return

            # 元の __init__ を保存（初回のみ）
            if not hasattr(cls, "_mfd_original_init"):
                cls._mfd_original_init = cls.__init__

            original_init = cls._mfd_original_init

            def patched_init(
                self: object,
                weight: str,
                device: str = "cpu",
                imgsz: int = 1888,
                conf: float = 0.25,
                iou: float = 0.45,
            ) -> None:
                original_init(self, weight, device, imgsz, target_conf, iou)

            cls.__init__ = patched_init
            cls._mfd_patched_conf = target_conf
        except (ImportError, AttributeError):
            logger.debug("MFD conf_thres patch skipped (module not available)")

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

"""PDF テキスト抽出モジュール.

仕様: docs/specs/converter.md

PDF バックエンド設定に応じて pymupdf4llm または MinerU を使用し、
PDF ファイルからテキスト（Markdown 形式）を抽出する。
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

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


def extract_pdf(path: Path, config: PdfBackendConfig) -> str | None:
    """PDF からテキストを抽出する.

    PDF バックエンド設定に応じて pymupdf4llm または MinerU を使用する。

    Args:
        path: PDF ファイルパス
        config: PDF バックエンド設定

    Returns:
        抽出テキスト、または失敗時は None
    """
    backend = config.backend

    if backend == "pymupdf4llm":
        return _extract_pdf_pymupdf4llm(path)

    if backend == "mineru":
        if not _is_mineru_available():
            logger.error(
                "MinerU is not installed but rag_pdf_backend=mineru. "
                "Install with: uv sync --extra mineru"
            )
            return None
        return _extract_pdf_mineru(path, config, mode="ocr")

    # backend == "auto": 事前判定フロー
    assessment = _assess_pdf(path, config)
    if assessment.backend == "pymupdf4llm":
        logger.info("PDF backend: pymupdf4llm (%s)", assessment.reason)
        return _extract_pdf_pymupdf4llm(path)

    # MinerU が必要だが未インストール → フォールバック
    if not _is_mineru_available():
        logger.warning(
            "PDF assessment recommends MinerU (%s) but it is not installed. "
            "Falling back to pymupdf4llm. Install with: uv sync --extra mineru",
            assessment.reason,
        )
        return _extract_pdf_pymupdf4llm(path)

    logger.info(
        "PDF backend: MinerU mode=%s (%s)",
        assessment.mineru_mode,
        assessment.reason,
    )
    return _extract_pdf_mineru(path, config, mode=assessment.mineru_mode or "ocr")


def _assess_pdf(path: Path, config: PdfBackendConfig) -> _PdfAssessment:
    """PDF を軽量検査し、最適なバックエンドを判定する.

    3 フェーズの検査を実行し、判定マトリクスに基づいてバックエンドを選択する。

    Args:
        path: PDF ファイルパス
        config: PDF バックエンド設定

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
        return _run_assessment(doc, config)
    except Exception:
        logger.warning("PDF assessment failed, falling back to pymupdf4llm: %s", path)
        return _PdfAssessment(backend="pymupdf4llm", reason="assessment failed")
    finally:
        doc.close()  # type: ignore[no-untyped-call]


def _run_assessment(doc: Any, config: PdfBackendConfig) -> _PdfAssessment:
    """PDF ドキュメントに対して 3 フェーズ検査を実行する.

    Args:
        doc: pymupdf.Document オブジェクト
        config: PDF バックエンド設定

    Returns:
        判定結果
    """
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
        # 両方検出済みなら早期終了
        if tounicode_missing and math_font_detected:
            break

        try:
            page = doc[page_idx]
            fonts = page.get_fonts()
        except Exception:
            logger.debug("Failed to get fonts for page %d", page_idx)
            continue

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
    max_samples = min(config.quality_sample_pages, page_count)
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
    if ufffd_ratio > config.quality_ufffd_threshold:
        return _PdfAssessment(
            backend="mineru", mineru_mode="ocr",
            reason=f"ufffd ratio {ufffd_ratio:.2%} > {config.quality_ufffd_threshold:.0%}",
        )
    if cjk_ratio < config.quality_cjk_min_threshold and greek_ratio > config.quality_greek_threshold:
        return _PdfAssessment(
            backend="mineru", mineru_mode="ocr",
            reason=f"CJK {cjk_ratio:.2%} < {config.quality_cjk_min_threshold:.0%} "
                   f"and Greek {greek_ratio:.2%} > {config.quality_greek_threshold:.0%}",
        )
    if avg_chars_per_page < config.quality_min_chars_per_page:
        return _PdfAssessment(
            backend="mineru", mineru_mode="ocr",
            reason=f"avg chars/page {avg_chars_per_page:.0f} < {config.quality_min_chars_per_page}",
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


def _is_mineru_available() -> bool:
    """MinerU pipeline がインストールされているか確認する."""
    try:
        from mineru.backend.pipeline import pipeline_analyze  # noqa: F401
        return True
    except (ImportError, RuntimeError, OSError):
        return False


def _extract_pdf_pymupdf4llm(path: Path) -> str | None:
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


def _extract_pdf_mineru(
    path: Path, config: PdfBackendConfig, *, mode: str = "ocr",
) -> str | None:
    """MinerU で PDF からテキストを抽出する.

    MinerU pipeline API を使用:
    1. doc_analyze: PDF bytes → モデル推論結果
    2. result_to_middle_json: 推論結果 → 中間 JSON
    3. union_make: 中間 JSON → Markdown 文字列

    Args:
        path: PDF ファイルパス
        config: PDF バックエンド設定
        mode: MinerU の解析モード（"ocr" or "txt"）

    Returns:
        抽出テキスト（Markdown 形式）、または失敗時は None
    """
    try:
        from mineru.backend.pipeline.pipeline_analyze import (
            doc_analyze as pipeline_doc_analyze,
        )
        from mineru.backend.pipeline.model_json_to_middle_json import (
            result_to_middle_json as pipeline_result_to_middle_json,
        )
        from mineru.backend.pipeline.pipeline_middle_json_mkcontent import (
            union_make as pipeline_union_make,
        )
        from mineru.data.data_reader_writer import FileBasedDataWriter
        from mineru.utils.enum_class import MakeMode
    except ImportError:
        logger.error(
            "MinerU is not installed. Install with: uv sync --extra mineru"
        )
        return None
    except (RuntimeError, OSError):
        logger.exception("MinerU module failed to load (GPU driver or DLL issue)")
        return None

    # デバイス選択（環境変数で MinerU に伝達）
    if "MINERU_DEVICE_MODE" not in os.environ:
        try:
            import torch
            if torch.cuda.is_available():
                os.environ["MINERU_DEVICE_MODE"] = "cuda"
            else:
                os.environ["MINERU_DEVICE_MODE"] = "cpu"
        except ImportError:
            os.environ["MINERU_DEVICE_MODE"] = "cpu"

    # MFD conf_thres のモンキーパッチ（モデル初期化前に適用）
    target_conf = config.mineru_mfd_conf_thres
    _apply_mfd_conf_thres_patch(target_conf)

    try:
        # PDF bytes を読み込み
        pdf_bytes = path.read_bytes()

        # Step 1: モデル推論
        infer_results, all_image_lists, all_pdf_docs, lang_list, ocr_enabled_list = (
            pipeline_doc_analyze(
                [pdf_bytes],
                ["japan"],  # MinerU の日本語コード（PaddleOCR 準拠）
                parse_method=mode,
                formula_enable=True,
                table_enable=True,
            )
        )

        # Step 2: 中間 JSON に変換（画像は一時ディレクトリに書き出し）
        with tempfile.TemporaryDirectory() as tmpdir:
            image_writer = FileBasedDataWriter(str(Path(tmpdir) / "images"))
            middle_json: dict[str, Any] = pipeline_result_to_middle_json(
                infer_results[0],
                all_image_lists[0],
                all_pdf_docs[0],
                image_writer,
                lang_list[0],
                ocr_enabled_list[0],
                True,  # formula_enable
            )

            # Step 3: Markdown 文字列を生成（NLP モード: 画像参照なし）
            pdf_info = middle_json["pdf_info"]
            md_content: str = pipeline_union_make(pdf_info, MakeMode.NLP_MD, "")

        return md_content if md_content.strip() else None
    except Exception:
        logger.exception("Failed to extract PDF with MinerU: %s", path)
        return None


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

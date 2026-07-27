"""PDF メディア削減モジュール.

仕様: docs/specs/infrastructure/pdf-media-reduction.md

PDF から削減対象（高解像度画像・埋め込みファイルストリーム）の実体を除去した
軽量コピーを生成する。source_store 配置前の事前処理であり、取り込みパイプラインの
外側に位置する。入力は読み取り専用で開き、結果は別ファイルとして出力する。

削減は 2 系統:

- 画像: 解像度が閾値を超えるものを目標 DPI へ縮小・再エンコードする。画像単位の
  取り出し・縮小・差し替えを自前で行う（pymupdf の ``Document.rewrite_images`` は
  同一入力でも非決定的にプロセスごとクラッシュするため使用しない。詳細は
  ``_downscale_images`` を参照）
- 埋め込みファイルストリーム: ``/Type /Filespec`` の ``/EF`` が参照するストリームの
  実体を空に置換する。埋め込み動画（RichMedia アノテーションが参照するアセット）と
  添付ファイルは同じ機構で格納されるため、両者をまとめて扱う

いずれもテキスト抽出（[../converter.md] の PDF テキスト抽出）の入力にならないため、
削減しても抽出内容は保持される。ただし画像の有無・寸法は pymupdf4llm のレイアウト
解析に影響するため、生成される Markdown の整形（見出し判定・行の結合位置）は変化しうる。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

# PDF の拡張子
PDF_EXTENSIONS: frozenset[str] = frozenset({".pdf"})

# 画像再圧縮の既定値（本モジュールが SSoT）。
# 発表資料は元データが縮小されないまま埋め込まれることが多く、表示に必要な解像度を
# 大きく超える。閾値・目標値ともスライド表示に十分な水準に置き、CLI で上書きできる。
DEFAULT_DPI_THRESHOLD = 200
DEFAULT_DPI_TARGET = 150
DEFAULT_JPEG_QUALITY = 75

# スキャン PDF 判定のサンプルページ数と 1 ページあたり文字数の下限。
# PDF テキスト抽出の事前判定（rag_pdf_quality_sample_pages /
# rag_pdf_quality_min_chars_per_page）と同じ観点で、テキスト層の有無を見る。
DEFAULT_SCAN_SAMPLE_PAGES = 10
DEFAULT_SCAN_MIN_CHARS_PER_PAGE = 10

# 再エンコードを試みる最小サイズ。これ未満の画像はファイルサイズへの寄与が小さく、
# デコード・再エンコードのコストに見合わない
MIN_RECOMPRESS_BYTES = 16 * 1024

# /Type /Filespec の /EF エントリ（埋め込みファイルストリームの参照）
_FILESPEC_MARKER = "/Filespec"
_EF_PATTERN = re.compile(r"/EF\s*<<(.*?)>>", re.DOTALL)
_INDIRECT_REF_PATTERN = re.compile(r"(\d+) 0 R")


class PdfReductionError(Exception):
    """PDF をドキュメントとして開けない・削減コピーを生成できない場合の例外."""


@dataclass(frozen=True)
class PdfMediaReport:
    """PDF の削減対象（画像・埋め込みファイル）の占有量レポート.

    画像は再圧縮による削減のため、除去量ではなく現在の占有量を示す。
    ``oversized_image_*`` は解像度閾値を超えて縮小対象になる画像の規模を表す。
    """

    total_bytes: int
    page_count: int
    image_count: int
    image_bytes: int
    oversized_image_count: int
    oversized_image_bytes: int
    embedded_count: int
    embedded_bytes: int
    image_ext_counts: dict[str, int] = field(default_factory=dict)
    is_low_text_layer: bool = False

    @property
    def reducible_bytes(self) -> int:
        """削減対象の合計サイズ（埋め込みファイル全量 + 画像の現在の占有量）.

        削減後のサイズは予測しない。画像の圧縮率は内容に依存し、PDF 構造の再構築でも
        サイズが変動するため、予測値を出すと実測と乖離して判断を誤らせる。
        """
        return self.embedded_bytes + self.image_bytes


def _iter_embedded_stream_xrefs(doc: Any) -> Iterator[int]:
    """``/Type /Filespec`` の ``/EF`` が参照するストリーム xref を列挙する.

    埋め込み動画（RichMedia アセット）・添付ファイルの実体がここに格納される。
    ドキュメント全体の xref を走査するのは、RichMedia アセットが文書カタログの
    ``/Names /EmbeddedFiles`` に登録されず、pymupdf の添付ファイル API から
    見えないため。
    """
    seen: set[int] = set()
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref, compressed=False)
        except Exception:
            continue
        if _FILESPEC_MARKER not in obj:
            continue
        ef_match = _EF_PATTERN.search(obj)
        if ef_match is None:
            continue
        for ref in _INDIRECT_REF_PATTERN.findall(ef_match.group(1)):
            target = int(ref)
            if target in seen:
                continue
            try:
                if not doc.xref_is_stream(target):
                    continue
            except Exception:
                continue
            seen.add(target)
            yield target


def _stream_size(doc: Any, xref: int) -> int:
    """ストリームの圧縮後サイズ（ファイルサイズへの寄与分）を返す."""
    try:
        raw = doc.xref_stream_raw(xref)
    except Exception:
        return 0
    return len(raw) if raw else 0


def _page_dpi(width_px: int, height_px: int, rect: Any) -> float:
    """ページ上の配置サイズから画像の実効 DPI を推定する.

    配置矩形が取得できない場合は 0.0 を返す（閾値判定では非超過として扱う）。
    """
    if rect is None:
        return 0.0
    width_pt = abs(getattr(rect, "width", 0.0))
    height_pt = abs(getattr(rect, "height", 0.0))
    if width_pt <= 0 or height_pt <= 0:
        return 0.0
    return float(max(width_px / (width_pt / 72.0), height_px / (height_pt / 72.0)))


def _is_low_text_layer(
    doc: Any,
    *,
    sample_pages: int = DEFAULT_SCAN_SAMPLE_PAGES,
    min_chars_per_page: int = DEFAULT_SCAN_MIN_CHARS_PER_PAGE,
) -> bool:
    """テキスト層の乏しい PDF（スキャン文書等）かを判定する.

    ページ画像自体がテキスト抽出（OCR）の入力になるため、画像を縮小すると
    抽出品質が劣化しうる。該当する PDF は既定で削減対象から除外する。
    """
    page_count = doc.page_count
    if page_count == 0:
        return False
    max_samples = min(sample_pages, page_count)
    step = page_count / max_samples
    indices = [int(step * i + step / 2) for i in range(max_samples)]

    total_chars = 0
    sampled = 0
    for idx in indices:
        if idx >= page_count:
            continue
        try:
            text: str = doc[idx].get_text()
        except Exception:
            continue
        sampled += 1
        total_chars += len(text.strip())
    if sampled == 0:
        return False
    return (total_chars / sampled) < min_chars_per_page


def analyze_pdf_media(
    path: Path,
    *,
    dpi_threshold: int = DEFAULT_DPI_THRESHOLD,
) -> PdfMediaReport:
    """PDF の削減対象（画像・埋め込みファイル）の占有量を解析する.

    Args:
        path: 対象ファイルパス
        dpi_threshold: この実効 DPI を超える画像を再圧縮対象として計上する

    Returns:
        削減対象の占有量レポート

    Raises:
        PdfReductionError: PDF として開けない場合
    """
    import pymupdf

    total_bytes = path.stat().st_size
    try:
        doc = pymupdf.open(str(path))  # type: ignore[no-untyped-call]
    except Exception as e:
        msg = f"PDF を開けません: {path.name} ({e})"
        raise PdfReductionError(msg) from e

    try:
        image_count = 0
        image_bytes = 0
        oversized_count = 0
        oversized_bytes = 0
        ext_counts: dict[str, int] = {}
        seen_xrefs: set[int] = set()

        for pno in range(doc.page_count):
            page = doc[pno]
            for info in page.get_images(full=True):  # type: ignore[no-untyped-call]
                xref = info[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    image = doc.extract_image(xref)  # type: ignore[no-untyped-call]
                except Exception:
                    continue
                size = len(image["image"])
                image_count += 1
                image_bytes += size
                ext = str(image.get("ext") or "(none)").lower()
                ext_counts[ext] = ext_counts.get(ext, 0) + 1

                rects = page.get_image_rects(xref)
                rect = rects[0] if rects else None
                dpi = _page_dpi(int(image["width"]), int(image["height"]), rect)
                if dpi > dpi_threshold:
                    oversized_count += 1
                    oversized_bytes += size

        embedded_xrefs = list(_iter_embedded_stream_xrefs(doc))
        embedded_bytes = sum(_stream_size(doc, x) for x in embedded_xrefs)

        return PdfMediaReport(
            total_bytes=total_bytes,
            page_count=doc.page_count,
            image_count=image_count,
            image_bytes=image_bytes,
            oversized_image_count=oversized_count,
            oversized_image_bytes=oversized_bytes,
            embedded_count=len(embedded_xrefs),
            embedded_bytes=embedded_bytes,
            image_ext_counts=ext_counts,
            is_low_text_layer=_is_low_text_layer(doc),
        )
    finally:
        doc.close()  # type: ignore[no-untyped-call]


@dataclass(frozen=True)
class _ImagePlacement:
    """画像の配置情報（縮小率の決定と差し替えに必要な最小情報）."""

    page_no: int
    rect: Any
    smask_xref: int


def _collect_image_placements(doc: Any) -> dict[int, _ImagePlacement]:
    """画像 xref ごとに、最大の配置矩形・ページ番号・透過マスクの xref を集める.

    同じ画像が複数ページ・複数箇所に配置される場合は、最も大きく表示される箇所を
    基準に縮小率を決める（小さい配置に合わせると、大きく表示される箇所が粗くなるため）。
    """
    placements: dict[int, _ImagePlacement] = {}
    for pno in range(doc.page_count):
        page = doc[pno]
        for info in page.get_images(full=True):
            xref = info[0]
            smask_xref = int(info[1]) if len(info) > 1 else 0
            rects = page.get_image_rects(xref)
            if not rects:
                continue
            biggest = max(rects, key=lambda r: abs(r.width) * abs(r.height))
            current = placements.get(xref)
            if current is not None and abs(biggest.width) * abs(biggest.height) <= abs(
                current.rect.width,
            ) * abs(current.rect.height):
                continue
            placements[xref] = _ImagePlacement(
                page_no=pno, rect=biggest, smask_xref=smask_xref,
            )
    return placements


def _has_unsupported_colorspace(doc: Any, xref: int) -> bool:
    """差し替えに対応しない画像か判定する.

    ``/ColorSpace`` を持たない画像（ステンシルマスク・JBIG2 等）は差し替えると
    エラーを出さずに表示が崩れるため、対象から除外する。
    """
    try:
        kind, _ = doc.xref_get_key(xref, "ColorSpace")
    except Exception:
        return True
    return bool(kind == "null")


def _recompress_image(
    payload: bytes,
    mask_payload: bytes | None,
    *,
    scale: float,
    quality: int,
) -> bytes | None:
    """画像バイト列を縮小・再エンコードする（失敗時は None）.

    PDF では透過情報が本体とは別のソフトマスク画像として格納される。差し替え時に
    本体だけを置き換えるとマスクへの参照が失われ、透過部分が不透明になって表示が
    崩れるため、マスクを合成してから 1 枚の画像として書き出す。

    透過を持つ画像は PNG（可逆・アルファ保持）、持たない画像は JPEG で出力する。

    Args:
        payload: 画像本体のバイト列
        mask_payload: ソフトマスク画像のバイト列（存在しない場合は None）
        scale: 縮小率（1.0 なら寸法を変えずに再エンコードのみ行う）
        quality: JPEG 品質

    Returns:
        再エンコード後のバイト列、または失敗時は None
    """
    import io

    from PIL import Image

    resample = Image.Resampling.LANCZOS
    try:
        opened = Image.open(io.BytesIO(payload))
        has_alpha = opened.mode in ("RGBA", "LA") or (
            opened.mode == "P" and "transparency" in opened.info
        )
        image: Image.Image = opened.convert("RGBA" if has_alpha else "RGB")

        if mask_payload is not None:
            mask = Image.open(io.BytesIO(mask_payload)).convert("L")
            if mask.size != image.size:
                mask = mask.resize(image.size, resample)
            # 全面不透明なマスクは意味を持たないため合成しない（JPEG のまま扱える）
            darkest = mask.getextrema()[0]
            if isinstance(darkest, (int, float)) and darkest < 255:
                image = image.convert("RGBA")
                image.putalpha(mask)
                has_alpha = True

        new_size = (
            max(1, int(image.width * scale)),
            max(1, int(image.height * scale)),
        )
        if new_size != image.size:
            image = image.resize(new_size, resample)

        buf = io.BytesIO()
        if has_alpha:
            image.save(buf, format="PNG", optimize=True)
        else:
            image.save(buf, format="JPEG", quality=quality, optimize=True)
    except Exception:
        return None
    return buf.getvalue()


def _downscale_images(
    doc: Any,
    *,
    dpi_threshold: int,
    dpi_target: int,
    quality: int,
    label: str,
) -> int:
    """実効解像度が閾値を超える画像を縮小・再エンコードして差し替える.

    pymupdf の ``Document.rewrite_images`` は同一入力でも非決定的に
    プロセスごとクラッシュする（MuPDF の C 層で segmentation fault）ため使用しない。
    画像単位の取り出し・縮小・差し替えを自前で行うことで、Python 例外として
    扱える範囲に閉じ込め、1 枚の失敗が処理全体を落とさないようにする。

    Args:
        doc: 対象ドキュメント
        dpi_threshold: この実効 DPI を超える画像を縮小対象とする
        dpi_target: 縮小後の目標 DPI
        quality: 再エンコード時の JPEG 品質
        label: ログ出力に使う識別名

    Returns:
        差し替えた画像の枚数
    """
    replaced = 0
    for xref, placement in _collect_image_placements(doc).items():
        if _has_unsupported_colorspace(doc, xref):
            continue
        try:
            info = doc.extract_image(xref)
        except Exception:
            logger.debug("Failed to extract image xref=%d: %s", xref, label)
            continue

        payload = info["image"]
        mask_payload: bytes | None = None
        if placement.smask_xref:
            try:
                mask_payload = doc.extract_image(placement.smask_xref)["image"]
            except Exception:
                # マスクを取り出せない画像は透過を保証できないため差し替えない
                logger.debug("Failed to extract smask for xref=%d: %s", xref, label)
                continue

        # 判定はマスクの分を含めた実サイズで行う（本体だけでは小さくても、
        # マスクと合わせるとファイルサイズへの寄与が大きい画像があるため）
        original_bytes = len(payload) + len(mask_payload or b"")
        if original_bytes < MIN_RECOMPRESS_BYTES:
            continue

        # 解像度が閾値を超える場合は縮小する。閾値以下でも再エンコードは試みる
        # （可逆圧縮のまま埋め込まれた画像は、縮小せずとも再エンコードで大きく縮む）
        dpi = _page_dpi(int(info["width"]), int(info["height"]), placement.rect)
        scale = dpi_target / dpi if dpi > dpi_threshold else 1.0

        new_payload = _recompress_image(
            payload, mask_payload, scale=scale, quality=quality,
        )
        if new_payload is None:
            logger.debug("Failed to recompress image xref=%d: %s", xref, label)
            continue
        # 再エンコードで大きくなる場合は元のまま残す（縮小が逆効果になるのを防ぐ）
        if len(new_payload) >= original_bytes:
            continue
        try:
            doc[placement.page_no].replace_image(xref, stream=new_payload)
            replaced += 1
        except Exception:
            logger.debug("Failed to replace image xref=%d: %s", xref, label)
    return replaced


def reduce_pdf(
    src: Path,
    dst: Path,
    *,
    dpi_threshold: int = DEFAULT_DPI_THRESHOLD,
    dpi_target: int = DEFAULT_DPI_TARGET,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> None:
    """削減対象の実体を除去した削減コピーを生成する（非破壊）.

    入力は読み取り専用で開き、一切変更しない。埋め込みファイルストリームは
    エントリを残したまま実体を空に置換するため、参照構造が壊れず削減後も
    正当な PDF として開ける（動画の再生・添付の取り出しはできなくなる）。

    Args:
        src: 入力 PDF のパス
        dst: 削減コピーの出力先パス
        dpi_threshold: この実効 DPI を超える画像を再圧縮する
        dpi_target: 再圧縮後の目標 DPI
        quality: 再圧縮時の JPEG 品質

    Raises:
        PdfReductionError: 入力を開けない場合・出力の書き出しに失敗した場合
    """
    import pymupdf

    try:
        doc = pymupdf.open(str(src))  # type: ignore[no-untyped-call]
    except Exception as e:
        msg = f"PDF を開けません: {src.name} ({e})"
        raise PdfReductionError(msg) from e

    try:
        for xref in _iter_embedded_stream_xrefs(doc):
            try:
                doc.update_stream(  # type: ignore[no-untyped-call]
                    xref, b"", new=False, compress=False,
                )
            except Exception:
                logger.debug("Failed to empty embedded stream xref=%d: %s", xref, src.name)

        _downscale_images(
            doc,
            dpi_threshold=dpi_threshold,
            dpi_target=dpi_target,
            quality=quality,
            label=src.name,
        )

        # garbage=4: 参照されなくなったオブジェクトの回収と重複の統合
        # clean=True: コンテンツストリームを再構築する。これを省くと画像差し替え後の
        # 残骸が残り、画像が縮んでもファイル全体が元より大きくなる
        # use_objstms=1: オブジェクトストリームで再構築する。これを省くと削減対象の
        # 少ない PDF で構造が展開されて元より大きくなる
        doc.save(  # type: ignore[no-untyped-call]
            str(dst), garbage=4, deflate=True, clean=True, use_objstms=1,
        )
    except Exception as e:
        dst.unlink(missing_ok=True)
        msg = f"PDF の削減コピー生成に失敗しました: {src.name} -> {dst} ({e})"
        raise PdfReductionError(msg) from e
    finally:
        doc.close()  # type: ignore[no-untyped-call]

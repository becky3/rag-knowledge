"""reduce-pdf CLI コマンドのテスト.

仕様: docs/specs/infrastructure/pdf-media-reduction.md

テスト方針（aidlc-docs/plan-work/issue-802.md より転記）:
- レポートのみモード・削減実行・既存出力のスキップ・出力先未指定エラーを検証する
- --alongside 別名出力（<元名>.reduced.pdf）・--output-dir との排他・
  .reduced サフィックス付きファイルの対象除外を検証する
- 高解像度画像の再圧縮によるサイズ削減と、埋め込みファイルストリームの実体除去を検証する
- テキスト層の乏しい PDF が既定で除外され、--include-scanned で削減されることを検証する
- 削減前後でテキスト抽出の内容が保持されること（Markdown 整形差は許容）を検証する
- 透過画像の透過が削減後も保持されること（ソフトマスクの合成）を検証する
- フィクスチャ PDF は pymupdf でテスト内生成する
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import pymupdf
import pytest

from rag.cli import run_reduce_pdf
from rag.converter.pdf_media_reducer import (
    DEFAULT_DPI_TARGET,
    DEFAULT_DPI_THRESHOLD,
    DEFAULT_JPEG_QUALITY,
    PdfReductionError,
    analyze_pdf_media,
    reduce_pdf,
)


def _high_res_image_bytes(width: int = 2400, height: int = 1800) -> bytes:
    """再圧縮で明確に縮む高解像度画像（ノイズ入り PNG）を生成する."""
    from PIL import Image

    # 単色だと PNG 圧縮が効きすぎて再圧縮の効果が測れないため、階調を持たせる
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    assert pixels is not None
    for y in range(0, height, 4):
        for x in range(0, width, 4):
            color = ((x * 7) % 256, (y * 13) % 256, ((x + y) * 3) % 256)
            for dy in range(4):
                for dx in range(4):
                    if x + dx < width and y + dy < height:
                        pixels[x + dx, y + dy] = color
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _make_pdf(
    path: Path,
    *,
    with_image: bool = True,
    text: str = "PDF reduction test page with enough text to look like a normal document.",
    pages: int = 1,
) -> Path:
    """テキスト（と任意で高解像度画像）を持つ PDF を生成する."""
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text, fontsize=11)
        if with_image:
            # ページのごく一部に配置し、実効 DPI を閾値超にする
            page.insert_image(pymupdf.Rect(72, 120, 272, 270), stream=_high_res_image_bytes())
    doc.save(str(path))
    doc.close()
    return path


def _make_pdf_with_embedded_file(path: Path, payload_size: int = 400_000) -> Path:
    """埋め込みファイル（動画アセットと同じ Filespec 機構）を持つ PDF を生成する."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Deck with an embedded media asset.", fontsize=11)
    # 圧縮が効きにくいペイロード（サイズ寄与を確実にするため）
    payload = bytes((i * 37 + i // 251) % 256 for i in range(payload_size))
    doc.embfile_add("asset.mp4", payload, filename="asset.mp4")
    doc.save(str(path))
    doc.close()
    return path


def _transparent_image_bytes(width: int = 2000, height: int = 1500) -> bytes:
    """半透明のグラデーションを持つ RGBA PNG を生成する."""
    from PIL import Image

    image = Image.new("RGBA", (width, height))
    pixels = image.load()
    assert pixels is not None
    for y in range(0, height, 4):
        alpha = int(255 * (y / height))
        for x in range(0, width, 4):
            color = ((x * 5) % 256, (y * 11) % 256, 128, alpha)
            for dy in range(4):
                for dx in range(4):
                    if x + dx < width and y + dy < height:
                        pixels[x + dx, y + dy] = color
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _make_pdf_with_transparency(path: Path) -> Path:
    """透過画像（ソフトマスク付き）を持つ PDF を生成する."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Deck containing a transparent image.", fontsize=11)
    page.insert_image(
        pymupdf.Rect(72, 120, 272, 270), stream=_transparent_image_bytes(),
    )
    doc.save(str(path))
    doc.close()
    return path


def _soft_mask_count(path: Path) -> int:
    """ソフトマスクを持つ画像の件数を数える."""
    doc = pymupdf.open(str(path))
    try:
        seen: set[int] = set()
        count = 0
        for pno in range(doc.page_count):
            for info in doc[pno].get_images(full=True):
                if info[0] in seen:
                    continue
                seen.add(info[0])
                if info[1]:
                    count += 1
        return count
    finally:
        doc.close()


def _make_scanned_pdf(path: Path) -> Path:
    """テキスト層を持たない（画像のみの）PDF を生成する."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=_high_res_image_bytes())
    doc.save(str(path))
    doc.close()
    return path


def _args(
    paths: list[str],
    *,
    output_dir: str | None = None,
    alongside: bool = False,
    report_only: bool = False,
    dpi_threshold: int = DEFAULT_DPI_THRESHOLD,
    dpi_target: int = DEFAULT_DPI_TARGET,
    quality: int = DEFAULT_JPEG_QUALITY,
    include_scanned: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        paths=paths,
        output_dir=output_dir,
        alongside=alongside,
        report_only=report_only,
        dpi_threshold=dpi_threshold,
        dpi_target=dpi_target,
        quality=quality,
        include_scanned=include_scanned,
    )


def _page_text(path: Path) -> str:
    doc = pymupdf.open(str(path))
    try:
        return "".join(doc[i].get_text() for i in range(doc.page_count))
    finally:
        doc.close()


class TestRunReducePdf:
    """run_reduce_pdf の振る舞いテスト."""

    def test_report_only_shows_summary_without_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        pdf = _make_pdf(tmp_path / "deck.pdf")

        run_reduce_pdf(_args([str(pdf)], report_only=True))

        out = capsys.readouterr().out
        assert "画像: 1 件" in out
        assert "削減対象合計:" in out
        assert "レポート完了: 対象 1 件 / エラー 0 件" in out
        assert not list(tmp_path.glob("*.reduced.pdf"))

    def test_reduction_creates_copy_in_output_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        pdf = _make_pdf(tmp_path / "deck.pdf")
        out_dir = tmp_path / "reduced"

        run_reduce_pdf(_args([str(pdf)], output_dir=str(out_dir)))

        out = capsys.readouterr().out
        assert (out_dir / "deck.pdf").exists()
        assert "削減完了: 生成 1 件 / スキップ 0 件 / エラー 0 件" in out

    def test_alongside_writes_reduced_suffix_next_to_source(
        self, tmp_path: Path,
    ) -> None:
        pdf = _make_pdf(tmp_path / "deck.pdf")

        run_reduce_pdf(_args([str(pdf)], alongside=True))

        assert (tmp_path / "deck.reduced.pdf").exists()

    def test_reduced_copy_is_excluded_from_targets(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _make_pdf(tmp_path / "deck.pdf")
        _make_pdf(tmp_path / "deck.reduced.pdf")

        run_reduce_pdf(_args([str(tmp_path)], report_only=True))

        out = capsys.readouterr().out
        assert "レポート完了: 対象 1 件" in out

    def test_high_resolution_image_is_recompressed(self, tmp_path: Path) -> None:
        pdf = _make_pdf(tmp_path / "deck.pdf")
        out_dir = tmp_path / "reduced"

        run_reduce_pdf(_args([str(pdf)], output_dir=str(out_dir)))

        assert (out_dir / "deck.pdf").stat().st_size < pdf.stat().st_size

    def test_text_content_is_preserved(self, tmp_path: Path) -> None:
        pdf = _make_pdf(tmp_path / "deck.pdf")
        out_dir = tmp_path / "reduced"

        run_reduce_pdf(_args([str(pdf)], output_dir=str(out_dir)))

        assert _page_text(out_dir / "deck.pdf").strip() == _page_text(pdf).strip()

    def test_source_file_is_not_modified(self, tmp_path: Path) -> None:
        pdf = _make_pdf(tmp_path / "deck.pdf")
        before = pdf.read_bytes()

        run_reduce_pdf(_args([str(pdf)], output_dir=str(tmp_path / "reduced")))

        assert pdf.read_bytes() == before

    def test_embedded_file_payload_is_emptied(self, tmp_path: Path) -> None:
        pdf = _make_pdf_with_embedded_file(tmp_path / "media.pdf")
        out_dir = tmp_path / "reduced"

        run_reduce_pdf(_args([str(pdf)], output_dir=str(out_dir)))

        reduced = out_dir / "media.pdf"
        assert reduced.stat().st_size < pdf.stat().st_size
        doc = pymupdf.open(str(reduced))
        try:
            assert doc.page_count == 1
            assert doc.embfile_get(0) == b""
        finally:
            doc.close()

    def test_transparency_is_preserved(self, tmp_path: Path) -> None:
        pdf = _make_pdf_with_transparency(tmp_path / "alpha.pdf")
        out_dir = tmp_path / "reduced"
        assert _soft_mask_count(pdf) == 1

        run_reduce_pdf(_args([str(pdf)], output_dir=str(out_dir)))

        # 透過画像を差し替える際にマスクを合成しないと、透過が失われて
        # 不透明な矩形になる。削減後もソフトマスクが残っていることを確認する
        assert _soft_mask_count(out_dir / "alpha.pdf") == 1

    def test_low_text_layer_pdf_is_skipped_by_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        pdf = _make_scanned_pdf(tmp_path / "scanned.pdf")
        out_dir = tmp_path / "reduced"

        run_reduce_pdf(_args([str(pdf)], output_dir=str(out_dir)))

        out = capsys.readouterr().out
        assert "テキスト層が乏しいため削減対象から除外" in out
        assert not (out_dir / "scanned.pdf").exists()
        assert "スキップ 1 件" in out

    def test_low_text_layer_pdf_is_reduced_with_include_scanned(
        self, tmp_path: Path,
    ) -> None:
        pdf = _make_scanned_pdf(tmp_path / "scanned.pdf")
        out_dir = tmp_path / "reduced"

        run_reduce_pdf(_args(
            [str(pdf)], output_dir=str(out_dir), include_scanned=True,
        ))

        assert (out_dir / "scanned.pdf").exists()

    def test_directory_input_collects_pdf_recursively(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        src_dir = tmp_path / "corpus"
        (src_dir / "nested").mkdir(parents=True)
        _make_pdf(src_dir / "a.pdf")
        _make_pdf(src_dir / "nested" / "b.pdf")
        (src_dir / "note.txt").write_text("not pdf", encoding="utf-8")
        out_dir = tmp_path / "reduced"

        run_reduce_pdf(_args([str(src_dir)], output_dir=str(out_dir)))

        out = capsys.readouterr().out
        assert (out_dir / "a.pdf").exists()
        assert (out_dir / "b.pdf").exists()
        assert "生成 2 件" in out

    def test_existing_output_is_skipped_not_overwritten(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        pdf = _make_pdf(tmp_path / "deck.pdf")
        out_dir = tmp_path / "reduced"
        out_dir.mkdir()
        existing = out_dir / "deck.pdf"
        existing.write_bytes(b"existing content")

        run_reduce_pdf(_args([str(pdf)], output_dir=str(out_dir)))

        out = capsys.readouterr().out
        assert existing.read_bytes() == b"existing content"
        assert "スキップ 1 件" in out

    def test_same_name_in_single_run_skips_second(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "x").mkdir()
        (tmp_path / "y").mkdir()
        _make_pdf(tmp_path / "x" / "same.pdf")
        _make_pdf(tmp_path / "y" / "same.pdf")
        out_dir = tmp_path / "reduced"

        run_reduce_pdf(_args(
            [str(tmp_path / "x"), str(tmp_path / "y")], output_dir=str(out_dir),
        ))

        out = capsys.readouterr().out
        assert "出力名が衝突" in out
        assert "生成 1 件 / スキップ 1 件" in out

    def test_missing_output_dir_without_report_only_exits(
        self, tmp_path: Path,
    ) -> None:
        pdf = _make_pdf(tmp_path / "deck.pdf")

        with pytest.raises(SystemExit) as exc_info:
            run_reduce_pdf(_args([str(pdf)]))

        assert exc_info.value.code == 1

    def test_output_dir_and_alongside_are_mutually_exclusive(
        self, tmp_path: Path,
    ) -> None:
        pdf = _make_pdf(tmp_path / "deck.pdf")

        with pytest.raises(SystemExit) as exc_info:
            run_reduce_pdf(_args(
                [str(pdf)], output_dir=str(tmp_path / "out"), alongside=True,
            ))

        assert exc_info.value.code == 1

    def test_no_targets_returns_without_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_reduce_pdf(_args([str(tmp_path)], report_only=True))

        assert "対象の PDF ファイルがありません" in capsys.readouterr().out

    def test_missing_path_is_reported_and_exits(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc_info:
            run_reduce_pdf(_args([str(tmp_path / "nope.pdf")], report_only=True))

        assert exc_info.value.code == 1

    def test_broken_pdf_is_counted_as_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"not a pdf at all")

        with pytest.raises(SystemExit) as exc_info:
            run_reduce_pdf(_args([str(broken)], report_only=True))

        assert exc_info.value.code == 1
        assert "エラー" in capsys.readouterr().err


class TestPdfMediaReducerModule:
    """pdf_media_reducer モジュール単体の振る舞いテスト."""

    def test_analyze_counts_embedded_streams(self, tmp_path: Path) -> None:
        pdf = _make_pdf_with_embedded_file(tmp_path / "media.pdf")

        report = analyze_pdf_media(pdf)

        assert report.embedded_count >= 1
        assert report.embedded_bytes > 0
        assert report.reducible_bytes >= report.embedded_bytes

    def test_analyze_flags_low_text_layer(self, tmp_path: Path) -> None:
        assert analyze_pdf_media(_make_scanned_pdf(tmp_path / "s.pdf")).is_low_text_layer
        assert not analyze_pdf_media(_make_pdf(tmp_path / "n.pdf")).is_low_text_layer

    def test_analyze_raises_for_broken_pdf(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"not a pdf at all")

        with pytest.raises(PdfReductionError):
            analyze_pdf_media(broken)

    def test_reduce_raises_and_removes_partial_output_for_broken_pdf(
        self, tmp_path: Path,
    ) -> None:
        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"not a pdf at all")
        dst = tmp_path / "out.pdf"

        with pytest.raises(PdfReductionError):
            reduce_pdf(broken, dst)

        assert not dst.exists()

    def test_image_without_colorspace_is_left_untouched(self, tmp_path: Path) -> None:
        """ColorSpace を持たない画像（ステンシルマスク等）は差し替えない.

        差し替えるとエラーを出さずに表示が崩れるため、対象から除外する。
        """
        from rag.converter.pdf_media_reducer import _has_unsupported_colorspace

        pdf = _make_pdf(tmp_path / "deck.pdf")
        doc = pymupdf.open(str(pdf))
        try:
            xref = doc[0].get_images(full=True)[0][0]
            assert not _has_unsupported_colorspace(doc, xref)
            # ColorSpace を落とした状態を再現する
            doc.xref_set_key(xref, "ColorSpace", "null")
            assert _has_unsupported_colorspace(doc, xref)
        finally:
            doc.close()

    def test_reduce_does_not_inflate_pdf_without_media(self, tmp_path: Path) -> None:
        pdf = _make_pdf(tmp_path / "textonly.pdf", with_image=False, pages=3)
        dst = tmp_path / "textonly.reduced.pdf"

        reduce_pdf(pdf, dst)

        assert dst.stat().st_size <= pdf.stat().st_size

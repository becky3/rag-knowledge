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


# テキスト層判定の閾値（本番では設定値を CLI から渡す）
_SCAN = {"scan_sample_pages": 10, "scan_min_chars_per_page": 10}


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


def _make_encrypted_pdf(path: Path) -> Path:
    """パスワード保護された PDF を生成する."""
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "protected document", fontsize=11)
    doc.save(
        str(path),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner",
        user_pw="user",
    )
    doc.close()
    return path


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
    min_size_mb: int = 0,
) -> argparse.Namespace:
    # min_size_mb はテストでは既定 0（無効）とする。フィクスチャ PDF は
    # すべて実運用の既定下限より小さく、既定値のままでは全テストが空振りするため
    return argparse.Namespace(
        paths=paths,
        output_dir=output_dir,
        alongside=alongside,
        report_only=report_only,
        dpi_threshold=dpi_threshold,
        dpi_target=dpi_target,
        quality=quality,
        include_scanned=include_scanned,
        min_size_mb=min_size_mb,
    )


def _image_shape(path: Path, index: int = 0) -> tuple[int, int, str]:
    """PDF 内の index 番目の画像の (幅, 高さ, 形式) を返す.

    差し替えの有無はストリームの生バイト列では判定できない（保存時に
    コンテナが再圧縮されるため）。差し替えられていれば寸法か形式が変わる。
    """
    doc = pymupdf.open(str(path))
    try:
        xref = doc[0].get_images(full=True)[index][0]
        info = doc.extract_image(xref)
        return int(info["width"]), int(info["height"]), str(info["ext"])
    finally:
        doc.close()


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

    def test_small_files_are_excluded_by_min_size(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """サイズ下限未満のファイルは対象から除外される.

        削減は非可逆な再エンコードを伴い、小さいファイルでは画質を落とす割に
        削減量がわずかなため、既定で除外する。
        """
        pdf = _make_pdf(tmp_path / "small.pdf")
        out_dir = tmp_path / "reduced"

        run_reduce_pdf(_args([str(pdf)], output_dir=str(out_dir), min_size_mb=20))

        out = capsys.readouterr().out
        assert "対象外にしました" in out
        assert "対象の PDF ファイルがありません" in out
        assert not (out_dir / "small.pdf").exists()

    def test_cli_parser_defaults_min_size_to_module_constant(self) -> None:
        """CLI の既定値が削減モジュールの定数（SSoT）と一致している."""
        from rag.cli import _build_parser
        from rag.converter.pdf_media_reducer import DEFAULT_MIN_FILE_SIZE_MB

        args = _build_parser().parse_args(["reduce-pdf", "x.pdf", "--report-only"])

        assert args.min_size_mb == DEFAULT_MIN_FILE_SIZE_MB
        assert DEFAULT_MIN_FILE_SIZE_MB > 0  # 既定で「全件対象」にならないこと

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

    def test_password_protected_pdf_does_not_abort_the_batch(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """パスワード保護 PDF はエラー計上のうえスキップし、他のファイルを処理し続ける.

        パスワード保護 PDF は open 自体は成功し、ページアクセス時に例外が出る。
        これを捕捉し損ねるとバッチ全体が中断する。
        """
        _make_encrypted_pdf(tmp_path / "locked.pdf")
        _make_pdf(tmp_path / "normal.pdf")
        out_dir = tmp_path / "reduced"

        run_reduce_pdf(_args([str(tmp_path)], output_dir=str(out_dir)))

        captured = capsys.readouterr()
        assert (out_dir / "normal.pdf").exists()
        assert "生成 1 件" in captured.out
        assert "エラー 1 件" in captured.out
        assert "パスワード" in captured.err

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

        report = analyze_pdf_media(pdf, **_SCAN)

        assert report.embedded_count >= 1
        assert report.embedded_bytes > 0
        assert report.reducible_bytes >= report.embedded_bytes

    def test_analyze_flags_low_text_layer(self, tmp_path: Path) -> None:
        assert analyze_pdf_media(_make_scanned_pdf(tmp_path / "s.pdf"), **_SCAN).is_low_text_layer
        assert not analyze_pdf_media(_make_pdf(tmp_path / "n.pdf"), **_SCAN).is_low_text_layer

    def test_analyze_raises_for_broken_pdf(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"not a pdf at all")

        with pytest.raises(PdfReductionError):
            analyze_pdf_media(broken, **_SCAN)

    def test_reduce_raises_for_broken_pdf(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"not a pdf at all")
        dst = tmp_path / "out.pdf"

        with pytest.raises(PdfReductionError):
            reduce_pdf(broken, dst, **_SCAN)

        assert not dst.exists()

    def test_reduce_rejects_writing_over_the_source(self, tmp_path: Path) -> None:
        """入力と同じパスへの出力を拒否する（非破壊の担保）.

        後始末で出力先を削除する経路があるため、入力と同一パスを許すと
        原本を削除しうる。処理前に弾く。
        """
        pdf = _make_pdf(tmp_path / "deck.pdf")
        before = pdf.read_bytes()

        with pytest.raises(PdfReductionError):
            reduce_pdf(pdf, pdf, **_SCAN)

        assert pdf.read_bytes() == before

    def test_reduce_keeps_existing_output_when_saving_fails(
        self, tmp_path: Path,
    ) -> None:
        """書き出しに失敗しても、既存の出力先ファイルを削除しない.

        後始末の対象は本呼び出しが作ったファイルに限る。
        """
        pdf = _make_pdf(tmp_path / "deck.pdf")
        existing = tmp_path / "existing.pdf"
        existing.write_bytes(b"existing content")

        # 書き込めないパス（ディレクトリを出力先に指定）で save を失敗させる
        with pytest.raises(PdfReductionError):
            reduce_pdf(pdf, tmp_path, **_SCAN)

        assert existing.read_bytes() == b"existing content"

    def test_image_without_colorspace_is_left_untouched(self, tmp_path: Path) -> None:
        """ColorSpace を持たない画像（ステンシルマスク等）は差し替えない.

        差し替えるとエラーを出さずに表示が崩れるため、対象から除外する。
        """
        pdf = _make_pdf(tmp_path / "deck.pdf")
        stripped = tmp_path / "stripped.pdf"

        # ColorSpace を落とした PDF を用意する（ステンシルマスク相当の状態）
        doc = pymupdf.open(str(pdf))
        try:
            xref = doc[0].get_images(full=True)[0][0]
            doc.xref_set_key(xref, "ColorSpace", "null")
            doc.save(str(stripped))
        finally:
            doc.close()

        before = _image_shape(stripped)
        reduce_pdf(stripped, tmp_path / "out.pdf", **_SCAN)

        # 差し替えられていれば縮小されて寸法が変わる。変わらない = 対象外にできている
        assert _image_shape(tmp_path / "out.pdf") == before

    def test_recompression_keeps_original_when_result_is_larger(
        self, tmp_path: Path,
    ) -> None:
        """再エンコードで大きくなる画像は元のまま残す（縮小が逆効果になるのを防ぐ）."""
        from PIL import Image

        # 低品質で圧縮済みの JPEG は、既定品質での再エンコードでかえって大きくなる
        image = Image.new("RGB", (900, 700))
        pixels = image.load()
        assert pixels is not None
        for y in range(0, 700, 3):
            for x in range(0, 900, 3):
                color = ((x * 3) % 256, (y * 7) % 256, ((x + y) * 5) % 256)
                for dy in range(3):
                    for dx in range(3):
                        if x + dx < 900 and y + dy < 700:
                            pixels[x + dx, y + dy] = color
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=20)

        pdf = tmp_path / "lowq.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "low quality jpeg deck", fontsize=11)
        # ページ全面に配置して解像度を閾値以下にし、縮小ではなく再エンコードのみを走らせる
        page.insert_image(page.rect, stream=buf.getvalue())
        doc.save(str(pdf))
        doc.close()

        before = _image_shape(pdf)
        reduce_pdf(pdf, tmp_path / "out.pdf", **_SCAN)

        assert _image_shape(tmp_path / "out.pdf") == before

    def test_unreadable_soft_mask_leaves_image_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ソフトマスクを取り出せない画像は透過を保証できないため差し替えない."""
        pdf = _make_pdf_with_transparency(tmp_path / "alpha.pdf")

        doc = pymupdf.open(str(pdf))
        try:
            smask_xref = doc[0].get_images(full=True)[0][1]
        finally:
            doc.close()
        assert smask_xref

        # マスクの取り出しだけが失敗する状態を作る
        original = pymupdf.Document.extract_image

        def failing_extract(self: pymupdf.Document, xref: int) -> object:
            if xref == smask_xref:
                msg = "mask unreadable"
                raise RuntimeError(msg)
            return original(self, xref)

        before = _image_shape(pdf)
        monkeypatch.setattr(pymupdf.Document, "extract_image", failing_extract)
        reduce_pdf(pdf, tmp_path / "out.pdf", **_SCAN)
        monkeypatch.undo()

        assert _image_shape(tmp_path / "out.pdf") == before

    def test_reduce_does_not_inflate_pdf_without_media(self, tmp_path: Path) -> None:
        pdf = _make_pdf(tmp_path / "textonly.pdf", with_image=False, pages=3)
        dst = tmp_path / "textonly.reduced.pdf"

        reduce_pdf(pdf, dst, **_SCAN)

        assert dst.stat().st_size <= pdf.stat().st_size

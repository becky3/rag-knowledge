"""ドキュメントインジェスターのテスト

仕様: docs/specs/document-ingester.md
Issue: #184, #198

テスト方針:
- 単体テスト: DocumentIngester の fetch_single / validate_identifier / collect_files
- 正常系: .md / .txt / .adoc ファイルの取り込み、ディレクトリ一括取り込み
- 異常系: 存在しないファイル、空パス、未対応拡張子、空ファイル（0バイト）、UTF-8以外
- バリデーション: パストラバーサル対策、pattern の '..' / 絶対パス拒否
- クランプ: ファイル数上限超過時のクランプ動作（テスト時は 5 件で実行）
- PDF テスト: pymupdf4llm をモックしてテスト（実 PDF ファイルは不要）
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from rag.ingesters.document_ingester import (
    MAX_FILES_HARD_LIMIT,
    DocumentIngester,
)


# --- ヘルパー ---


def _create_text_file(tmp_path: Path, name: str, content: str) -> Path:
    """テスト用テキストファイルを作成する."""
    file_path = tmp_path / name
    file_path.write_text(content, encoding="utf-8")
    return file_path


def _create_empty_file(tmp_path: Path, name: str) -> Path:
    """テスト用の空ファイルを作成する."""
    file_path = tmp_path / name
    file_path.write_bytes(b"")
    return file_path


def _create_binary_file(tmp_path: Path, name: str, data: bytes) -> Path:
    """テスト用バイナリファイルを作成する."""
    file_path = tmp_path / name
    file_path.write_bytes(data)
    return file_path


# --- ハードリミット定数テスト ---


class TestHardLimits:
    """ハードリミット定数の値テスト."""

    def test_max_files_hard_limit(self) -> None:
        """ファイル数上限が仕様通り 100 件であること."""
        assert MAX_FILES_HARD_LIMIT == 100


# --- DocumentIngester.validate_identifier テスト ---


class TestDocumentIngesterValidate:
    """DocumentIngester.validate_identifier のテスト."""

    @pytest.fixture()
    def ingester(self) -> DocumentIngester:
        return DocumentIngester()

    def test_valid_md_file(self, ingester: DocumentIngester, tmp_path: Path) -> None:
        """正しい .md ファイルのパスが正規化されて返ること."""
        f = _create_text_file(tmp_path, "test.md", "# Hello")
        result = ingester.validate_identifier(str(f))
        assert result == str(f.resolve())

    def test_valid_txt_file(self, ingester: DocumentIngester, tmp_path: Path) -> None:
        """正しい .txt ファイルのパスが正規化されて返ること."""
        f = _create_text_file(tmp_path, "test.txt", "Hello")
        result = ingester.validate_identifier(str(f))
        assert result == str(f.resolve())

    def test_valid_adoc_file(self, ingester: DocumentIngester, tmp_path: Path) -> None:
        """正しい .adoc ファイルのパスが正規化されて返ること."""
        f = _create_text_file(tmp_path, "test.adoc", "= Hello")
        result = ingester.validate_identifier(str(f))
        assert result == str(f.resolve())

    def test_valid_pdf_file(self, ingester: DocumentIngester, tmp_path: Path) -> None:
        """正しい .pdf ファイルのパスが正規化されて返ること."""
        f = _create_binary_file(tmp_path, "test.pdf", b"%PDF-1.4 dummy")
        result = ingester.validate_identifier(str(f))
        assert result == str(f.resolve())

    def test_empty_path(self, ingester: DocumentIngester) -> None:
        """空文字列がバリデーションエラーになること."""
        with pytest.raises(ValueError, match="must not be empty"):
            ingester.validate_identifier("")
        with pytest.raises(ValueError, match="must not be empty"):
            ingester.validate_identifier("   ")

    def test_nonexistent_file(self, ingester: DocumentIngester) -> None:
        """存在しないファイルがバリデーションエラーになること."""
        with pytest.raises(ValueError, match="File not found"):
            ingester.validate_identifier("/nonexistent/path/file.md")

    def test_unsupported_extension(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """未対応拡張子がバリデーションエラーになること."""
        f = _create_text_file(tmp_path, "test.docx", "Hello")
        with pytest.raises(ValueError, match="Unsupported file extension"):
            ingester.validate_identifier(str(f))

    def test_directory_path(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """ディレクトリパスがバリデーションエラーになること."""
        with pytest.raises(ValueError, match="Path is a directory"):
            ingester.validate_identifier(str(tmp_path))

    def test_whitespace_trimmed(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """前後の空白がトリムされること."""
        f = _create_text_file(tmp_path, "test.md", "# Hello")
        result = ingester.validate_identifier(f"  {f}  ")
        assert result == str(f.resolve())

    def test_custom_extensions(self, tmp_path: Path) -> None:
        """カスタム拡張子が受け入れられること."""
        ingester = DocumentIngester(supported_extensions=[".md", ".rst"])
        f = _create_text_file(tmp_path, "test.rst", "Hello")
        result = ingester.validate_identifier(str(f))
        assert result == str(f.resolve())

        # .txt は対応外
        f2 = _create_text_file(tmp_path, "test.txt", "Hello")
        with pytest.raises(ValueError, match="Unsupported file extension"):
            ingester.validate_identifier(str(f2))


# --- DocumentIngester.fetch_single テスト ---


class TestDocumentIngesterFetchSingle:
    """DocumentIngester.fetch_single のテスト."""

    @pytest.fixture()
    def ingester(self) -> DocumentIngester:
        return DocumentIngester()

    async def test_fetch_md_file(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """Markdown ファイルが正常に取り込まれること."""
        f = _create_text_file(tmp_path, "document.md", "# Hello World\n\nThis is content.")
        result = await ingester.fetch_single(str(f))

        assert result is not None
        assert result.source_id == f.resolve().as_uri()
        assert result.title == "document"
        assert result.text == "# Hello World\n\nThis is content."
        assert result.source_type == "document"
        assert result.metadata["file_extension"] == ".md"
        assert result.metadata["file_size_bytes"] > 0
        assert result.metadata["file_path"] == str(f)

    async def test_fetch_txt_file(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """テキストファイルが正常に取り込まれること."""
        f = _create_text_file(tmp_path, "notes.txt", "Some plain text notes.")
        result = await ingester.fetch_single(str(f))

        assert result is not None
        assert result.title == "notes"
        assert result.text == "Some plain text notes."

    async def test_fetch_adoc_file(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """AsciiDoc ファイルが正常に取り込まれること."""
        f = _create_text_file(tmp_path, "guide.adoc", "= Guide Title\n\nContent here.")
        result = await ingester.fetch_single(str(f))

        assert result is not None
        assert result.title == "guide"
        assert result.text == "= Guide Title\n\nContent here."

    async def test_fetch_empty_file(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """空ファイル（0バイト）がスキップされること."""
        f = _create_empty_file(tmp_path, "empty.md")
        result = await ingester.fetch_single(str(f))

        assert result is None

    async def test_fetch_nonexistent_file(
        self, ingester: DocumentIngester
    ) -> None:
        """存在しないファイルで ValueError を送出すること."""
        with pytest.raises(ValueError, match="File not found"):
            await ingester.fetch_single("/nonexistent/file.md")

    async def test_fetch_unsupported_extension(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """未対応拡張子で ValueError を送出すること."""
        f = _create_text_file(tmp_path, "test.docx", "Hello")
        with pytest.raises(ValueError, match="Unsupported file extension"):
            await ingester.fetch_single(str(f))

    async def test_fetch_non_utf8_file(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """UTF-8 以外のエンコーディングのファイルで None を返すこと."""
        f = tmp_path / "shift_jis.txt"
        f.write_bytes("日本語テスト".encode("shift_jis"))
        result = await ingester.fetch_single(str(f))

        assert result is None

    async def test_fetch_pdf_with_mock(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """PDF ファイルが pymupdf4llm 経由で取り込まれること（モック）."""
        f = _create_binary_file(tmp_path, "report.pdf", b"%PDF-1.4 dummy content")

        with patch(
            "rag.ingesters.document_ingester.DocumentIngester._extract_pdf",
            return_value="# Extracted PDF Content\n\nParagraph from PDF.",
        ):
            result = await ingester.fetch_single(str(f))

        assert result is not None
        assert result.title == "report"
        assert "Extracted PDF Content" in result.text
        assert result.metadata["file_extension"] == ".pdf"

    async def test_fetch_pdf_extraction_failure(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """PDF 変換失敗時に None を返すこと."""
        f = _create_binary_file(tmp_path, "bad.pdf", b"%PDF-1.4 corrupted")

        with patch(
            "rag.ingesters.document_ingester.DocumentIngester._extract_pdf",
            return_value=None,
        ):
            result = await ingester.fetch_single(str(f))

        assert result is None

    async def test_fetch_whitespace_only_content(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """空白のみのファイルで None を返すこと."""
        f = _create_text_file(tmp_path, "whitespace.md", "   \n\n   ")
        result = await ingester.fetch_single(str(f))

        assert result is None

    async def test_source_id_is_file_uri(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """source_id が file URI であること."""
        f = _create_text_file(tmp_path, "doc.md", "Content")
        result = await ingester.fetch_single(str(f))

        assert result is not None
        assert result.source_id.startswith("file:///")

    async def test_ingested_at_is_set(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """ingested_at がセットされること."""
        f = _create_text_file(tmp_path, "doc.md", "Content")
        result = await ingester.fetch_single(str(f))

        assert result is not None
        assert result.ingested_at  # 空でない


# --- DocumentIngester.validate_pattern テスト ---


class TestDocumentIngesterValidatePattern:
    """DocumentIngester.validate_pattern のテスト."""

    @pytest.fixture()
    def ingester(self) -> DocumentIngester:
        return DocumentIngester()

    def test_valid_pattern(self, ingester: DocumentIngester) -> None:
        """正しいパターンがそのまま返ること."""
        assert ingester.validate_pattern("**/*.md") == "**/*.md"
        assert ingester.validate_pattern("*.txt") == "*.txt"
        assert ingester.validate_pattern("docs/**/*") == "docs/**/*"

    def test_pattern_with_dotdot(self, ingester: DocumentIngester) -> None:
        """'..' を含むパターンがバリデーションエラーになること."""
        with pytest.raises(ValueError, match="must not contain '..'"):
            ingester.validate_pattern("../**/*.md")
        with pytest.raises(ValueError, match="must not contain '..'"):
            ingester.validate_pattern("docs/../../etc/passwd")

    def test_absolute_pattern(self, ingester: DocumentIngester) -> None:
        """絶対パスのパターンがバリデーションエラーになること."""
        with pytest.raises(ValueError, match="must not be an absolute path"):
            ingester.validate_pattern("/etc/**/*.md")


# --- DocumentIngester.collect_files テスト ---


class TestDocumentIngesterCollectFiles:
    """DocumentIngester.collect_files のテスト."""

    @pytest.fixture()
    def ingester(self) -> DocumentIngester:
        return DocumentIngester()

    def test_collect_from_directory(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """ディレクトリからファイルが収集されること."""
        _create_text_file(tmp_path, "a.md", "Content A")
        _create_text_file(tmp_path, "b.txt", "Content B")
        _create_text_file(tmp_path, "c.py", "print('hello')")  # 未対応

        files = ingester.collect_files(str(tmp_path))

        assert len(files) == 2
        names = [f.name for f in files]
        assert "a.md" in names
        assert "b.txt" in names
        assert "c.py" not in names

    def test_collect_recursive(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """サブディレクトリ内のファイルも再帰的に収集されること."""
        sub = tmp_path / "sub"
        sub.mkdir()
        _create_text_file(tmp_path, "root.md", "Root")
        _create_text_file(sub, "nested.md", "Nested")

        files = ingester.collect_files(str(tmp_path))

        assert len(files) == 2

    def test_collect_with_pattern(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """glob パターンでフィルタされること."""
        _create_text_file(tmp_path, "a.md", "Content A")
        _create_text_file(tmp_path, "b.txt", "Content B")

        files = ingester.collect_files(str(tmp_path), pattern="*.md")

        assert len(files) == 1
        assert files[0].name == "a.md"

    def test_collect_sorted(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """ファイルがパスの辞書順でソートされること."""
        _create_text_file(tmp_path, "c.md", "C")
        _create_text_file(tmp_path, "a.md", "A")
        _create_text_file(tmp_path, "b.md", "B")

        files = ingester.collect_files(str(tmp_path))

        names = [f.name for f in files]
        assert names == sorted(names)

    def test_collect_empty_directory(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """空ディレクトリで空リストを返すこと."""
        files = ingester.collect_files(str(tmp_path))

        assert files == []

    def test_collect_no_matching_files(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """対応拡張子に一致するファイルがない場合に空リストを返すこと."""
        _create_text_file(tmp_path, "script.py", "print('hello')")

        files = ingester.collect_files(str(tmp_path))

        assert files == []

    def test_collect_empty_dir_path(self, ingester: DocumentIngester) -> None:
        """空の dir_path がバリデーションエラーになること."""
        with pytest.raises(ValueError, match="dir_path must not be empty"):
            ingester.collect_files("")
        with pytest.raises(ValueError, match="dir_path must not be empty"):
            ingester.collect_files("   ")

    def test_collect_nonexistent_directory(
        self, ingester: DocumentIngester
    ) -> None:
        """存在しないディレクトリがバリデーションエラーになること."""
        with pytest.raises(ValueError, match="Directory not found"):
            ingester.collect_files("/nonexistent/directory")

    def test_collect_file_instead_of_directory(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """ファイルパスがバリデーションエラーになること."""
        f = _create_text_file(tmp_path, "file.md", "Content")
        with pytest.raises(ValueError, match="Path is not a directory"):
            ingester.collect_files(str(f))

    def test_collect_pattern_with_dotdot(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """'..' を含むパターンがバリデーションエラーになること."""
        with pytest.raises(ValueError, match="must not contain '..'"):
            ingester.collect_files(str(tmp_path), pattern="../**/*.md")

    def test_collect_absolute_pattern(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """絶対パスのパターンがバリデーションエラーになること."""
        with pytest.raises(ValueError, match="must not be an absolute path"):
            ingester.collect_files(str(tmp_path), pattern="/etc/**/*.md")

    def test_collect_clamp_at_limit(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """ファイル数上限超過時にクランプされること（テスト安全値: 5件）."""
        for i in range(8):
            _create_text_file(tmp_path, f"file_{i:02d}.md", f"Content {i}")

        files = ingester.collect_files(str(tmp_path), max_files=5)

        assert len(files) == 5
        # 辞書順で先頭5件
        names = [f.name for f in files]
        assert names == ["file_00.md", "file_01.md", "file_02.md", "file_03.md", "file_04.md"]

    def test_collect_within_limit(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """ファイル数上限以内の場合にクランプされないこと."""
        for i in range(3):
            _create_text_file(tmp_path, f"file_{i}.md", f"Content {i}")

        files = ingester.collect_files(str(tmp_path), max_files=5)

        assert len(files) == 3

    def test_collect_max_files_capped_by_hard_limit(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """max_files が MAX_FILES_HARD_LIMIT を超えないこと."""
        _create_text_file(tmp_path, "a.md", "Content")

        # max_files=200 を指定しても MAX_FILES_HARD_LIMIT でキャップされる
        files = ingester.collect_files(str(tmp_path), max_files=200)
        assert len(files) == 1  # ファイル数 < 上限なので全件返る


# --- PDF 抽出テスト（モック） ---


class TestDocumentIngesterPdf:
    """PDF テキスト抽出のテスト（pymupdf4llm をモック）."""

    @pytest.fixture()
    def ingester(self) -> DocumentIngester:
        return DocumentIngester()

    def test_extract_pdf_success(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """pymupdf4llm で正常にテキスト抽出されること."""
        f = _create_binary_file(tmp_path, "doc.pdf", b"%PDF-1.4 dummy")

        with patch.dict("sys.modules", {"pymupdf4llm": __import__("unittest.mock", fromlist=["MagicMock"])}):
            with patch(
                "pymupdf4llm.to_markdown",
                return_value="# PDF Title\n\nPDF content here.",
                create=True,
            ):
                result = ingester._extract_pdf(f)

        assert result == "# PDF Title\n\nPDF content here."

    def test_extract_pdf_import_error(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """pymupdf4llm が未インストールの場合に None を返すこと."""
        f = _create_binary_file(tmp_path, "doc.pdf", b"%PDF-1.4 dummy")

        with patch.dict("sys.modules", {"pymupdf4llm": None}):
            result = ingester._extract_pdf(f)

        assert result is None

    def test_extract_pdf_conversion_error(
        self, ingester: DocumentIngester, tmp_path: Path
    ) -> None:
        """PDF 変換エラー時に None を返すこと."""
        f = _create_binary_file(tmp_path, "doc.pdf", b"%PDF-1.4 dummy")

        with patch.dict("sys.modules", {"pymupdf4llm": __import__("unittest.mock", fromlist=["MagicMock"])}):
            with patch(
                "pymupdf4llm.to_markdown",
                side_effect=Exception("Conversion failed"),
                create=True,
            ):
                result = ingester._extract_pdf(f)

        assert result is None

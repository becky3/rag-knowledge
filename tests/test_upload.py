"""コンテンツアップロード層のテスト.

仕様: docs/specs/infrastructure/content-upload.md
"""

from __future__ import annotations

import base64

import pytest

from rag.upload import decode_upload_content, sanitize_filename


class TestDecodeUploadContent:
    def test_text_encoding_decodes_to_utf8_bytes(self) -> None:
        result = decode_upload_content("hello world", "text")
        assert result == b"hello world"

    def test_text_encoding_with_japanese(self) -> None:
        result = decode_upload_content("日本語テキスト", "text")
        assert result == "日本語テキスト".encode("utf-8")

    def test_base64_encoding_decodes_correctly(self) -> None:
        original = b"binary content \x00\x01\x02"
        encoded = base64.b64encode(original).decode("ascii")
        result = decode_upload_content(encoded, "base64")
        assert result == original

    def test_base64_encoding_with_pdf_like_content(self) -> None:
        pdf_like = b"%PDF-1.4 some content"
        encoded = base64.b64encode(pdf_like).decode("ascii")
        result = decode_upload_content(encoded, "base64")
        assert result == pdf_like

    def test_invalid_encoding_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="encoding"):
            decode_upload_content("content", "utf-8")

    def test_invalid_encoding_hex_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="encoding"):
            decode_upload_content("content", "hex")

    def test_invalid_base64_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="base64"):
            decode_upload_content("not-valid-base64!!!", "base64")

    def test_base64_without_padding_raises_value_error(self) -> None:
        # 正しいパディングなし（validate=True で拒否される）
        no_padding = base64.b64encode(b"test").decode("ascii").rstrip("=")
        with pytest.raises(ValueError, match="base64"):
            decode_upload_content(no_padding, "base64")

    def test_empty_content_text_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="空"):
            decode_upload_content("", "text")

    def test_empty_content_base64_raises_value_error(self) -> None:
        # base64 で空バイト列になるケース: base64.b64encode(b"") == b""
        with pytest.raises(ValueError, match="空"):
            decode_upload_content("", "base64")

    def test_base64_single_missing_padding_raises_value_error(self) -> None:
        # b"ab" → "YWI=" → パディング1つ除去で "YWI" → Incorrect padding
        no_padding = "YWI"
        with pytest.raises(ValueError, match="base64"):
            decode_upload_content(no_padding, "base64")


class TestSanitizeFilename:
    def test_plain_filename_is_returned_as_is(self) -> None:
        assert sanitize_filename("resume.pdf") == "resume.pdf"

    def test_filename_with_extension_is_returned(self) -> None:
        assert sanitize_filename("notes.md") == "notes.md"

    def test_path_with_directory_separator_extracts_filename(self) -> None:
        assert sanitize_filename("/home/user/docs/resume.pdf") == "resume.pdf"

    def test_windows_path_extracts_filename(self) -> None:
        assert sanitize_filename("C:\\Users\\user\\docs\\resume.pdf") == "resume.pdf"

    def test_relative_path_extracts_filename(self) -> None:
        assert sanitize_filename("docs/notes.md") == "notes.md"

    def test_dotdot_path_extracts_filename_part(self) -> None:
        # ../../etc/passwd → basename は "passwd"
        assert sanitize_filename("../../etc/passwd") == "passwd"

    def test_dotdot_alone_raises_value_error(self) -> None:
        # ".." 単体はファイル名部分も ".." になるため拒否
        with pytest.raises(ValueError, match="filename"):
            sanitize_filename("..")

    def test_empty_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="空"):
            sanitize_filename("")

    def test_root_path_raises_value_error(self) -> None:
        # "/" → Path("/").name == "" → 拒否
        with pytest.raises(ValueError, match="filename"):
            sanitize_filename("/")

    def test_windows_drive_root_raises_value_error(self) -> None:
        # "C:\" → Path("C:\\").name == "" → 拒否
        with pytest.raises(ValueError, match="filename"):
            sanitize_filename("C:\\")

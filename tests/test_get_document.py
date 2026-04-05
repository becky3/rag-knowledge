"""get_document 共通ロジックのテスト.

仕様: docs/specs/search-response.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.rag_knowledge import (
    DocumentResult,
    format_document_response,
    get_document,
)


@pytest.fixture
def stores(tmp_path: Path) -> tuple[Path, Path]:
    """テスト用 source_store と converted_store を作成する."""
    source = tmp_path / "source_store"
    converted = tmp_path / "converted_store"
    source.mkdir()
    converted.mkdir()
    return source, converted


class TestGetDocumentFormatText:
    """format=text のテスト."""

    def test_text_format_reads_from_converted_store(
        self, stores: tuple[Path, Path],
    ) -> None:
        """format=text で converted_store からテキストを読み取ること."""
        source_dir, converted_dir = stores

        # source_store にファイルを配置
        _setup_source_with_db(
            source_dir,
            source_id="web/https/example.com/page.html",
            source_type="web",
            file_path="web/https/example.com/page.html",
            title="Test Page",
            content=b"<html>original</html>",
        )

        # converted_store に変換済みファイルを配置
        conv_file = converted_dir / "web/https/example.com/page.md"
        conv_file.parent.mkdir(parents=True, exist_ok=True)
        conv_file.write_text("# Converted content", encoding="utf-8")

        result = get_document(
            source_id="web/https/example.com/page.html",
            format="text",
            source_store_dir=str(source_dir),
            converted_store_dir=str(converted_dir),
        )

        assert result.error is None
        assert result.source_id == "web/https/example.com/page.html"
        assert result.title == "Test Page"
        assert result.source_type == "web"
        assert result.format == "text"
        assert result.content == "# Converted content"
        assert not result.is_binary

    def test_text_format_missing_converted_file(
        self, stores: tuple[Path, Path],
    ) -> None:
        """format=text で converted_store にファイルがない場合、エラーを返すこと."""
        source_dir, converted_dir = stores

        _setup_source_with_db(
            source_dir,
            source_id="web/https/example.com/page.html",
            source_type="web",
            file_path="web/https/example.com/page.html",
            title="Test Page",
            content=b"<html>original</html>",
        )

        result = get_document(
            source_id="web/https/example.com/page.html",
            format="text",
            source_store_dir=str(source_dir),
            converted_store_dir=str(converted_dir),
        )

        assert result.error is not None
        assert "変換済みファイルが見つかりません" in result.error
        assert "format=original" in result.error


class TestGetDocumentFormatOriginal:
    """format=original のテスト."""

    def test_original_format_reads_text_file(
        self, stores: tuple[Path, Path],
    ) -> None:
        """format=original でテキストファイルの内容を返すこと."""
        source_dir, converted_dir = stores

        _setup_source_with_db(
            source_dir,
            source_id="local/notes/memo.md",
            source_type="local",
            file_path="local/notes/memo.md",
            title="memo",
            content=b"# My Memo\nHello World",
        )

        result = get_document(
            source_id="local/notes/memo.md",
            format="original",
            source_store_dir=str(source_dir),
            converted_store_dir=str(converted_dir),
        )

        assert result.error is None
        assert result.content == "# My Memo\nHello World"
        assert not result.is_binary

    def test_original_format_binary_returns_info(
        self, stores: tuple[Path, Path],
    ) -> None:
        """format=original でバイナリファイルのとき、MIME情報を返すこと."""
        source_dir, converted_dir = stores

        _setup_source_with_db(
            source_dir,
            source_id="web/https/example.com/doc.pdf",
            source_type="web",
            file_path="web/https/example.com/doc.pdf",
            title="PDF Document",
            content=b"%PDF-1.4 fake content",
        )

        result = get_document(
            source_id="web/https/example.com/doc.pdf",
            format="original",
            source_store_dir=str(source_dir),
            converted_store_dir=str(converted_dir),
        )

        assert result.error is None
        assert result.is_binary
        assert "application/pdf" in result.content
        assert "format=text" in result.content


class TestGetDocumentEdgeCases:
    """エッジケースのテスト."""

    def test_invalid_format_returns_error(
        self, stores: tuple[Path, Path],
    ) -> None:
        """無効な format でエラーを返すこと."""
        source_dir, converted_dir = stores

        result = get_document(
            source_id="any",
            format="invalid",
            source_store_dir=str(source_dir),
            converted_store_dir=str(converted_dir),
        )

        assert result.error is not None
        assert "無効な format" in result.error

    def test_nonexistent_source_returns_error(
        self, stores: tuple[Path, Path],
    ) -> None:
        """存在しない source_id でエラーを返すこと."""
        source_dir, converted_dir = stores

        # metadata.db を初期化（空）
        from rag.store.source_store import SourceStore
        with SourceStore(root_dir=source_dir) as store:
            store.initialize()

        result = get_document(
            source_id="nonexistent",
            format="text",
            source_store_dir=str(source_dir),
            converted_store_dir=str(converted_dir),
        )

        assert result.error is not None
        assert "ソースが見つかりません" in result.error

    def test_deleted_source_still_accessible(
        self, stores: tuple[Path, Path],
    ) -> None:
        """論理削除済みのソースも取得できること."""
        source_dir, converted_dir = stores

        _setup_source_with_db(
            source_dir,
            source_id="local/old/doc.txt",
            source_type="local",
            file_path="local/old/doc.txt",
            title="Old Doc",
            content=b"Old content",
        )

        # 論理削除
        from rag.store.source_store import SourceStore
        with SourceStore(root_dir=source_dir) as store:
            store.soft_delete("local/old/doc.txt")

        result = get_document(
            source_id="local/old/doc.txt",
            format="original",
            source_store_dir=str(source_dir),
            converted_store_dir=str(converted_dir),
        )

        assert result.error is None
        assert result.content == "Old content"


class TestFormatDocumentResponse:
    """format_document_response のテスト."""

    def test_formats_successful_result(self) -> None:
        """正常な結果をフォーマットできること."""
        result = DocumentResult(
            source_id="web/https/example.com/page.html",
            title="Test Page",
            source_type="web",
            format="text",
            content="Document content here.",
        )

        response = format_document_response(result)

        assert "Source: web/https/example.com/page.html" in response
        assert "Title: Test Page" in response
        assert "Type: web" in response
        assert "Format: text" in response
        assert "Document content here." in response

    def test_formats_error_result(self) -> None:
        """エラー結果をフォーマットできること."""
        result = DocumentResult(
            source_id="missing",
            title="",
            source_type="",
            format="text",
            content="",
            error="ソースが見つかりません: missing",
        )

        response = format_document_response(result)

        assert response.startswith("エラー:")
        assert "ソースが見つかりません" in response


# --- ヘルパー関数 ---


def _setup_source_with_db(
    source_dir: Path,
    *,
    source_id: str,
    source_type: str,
    file_path: str,
    title: str,
    content: bytes,
) -> None:
    """source_store にファイルと metadata.db レコードをセットアップする."""
    from rag.store.source_store import SourceStore

    with SourceStore(root_dir=source_dir) as store:
        store.initialize()

        # ファイルを直接配置
        full_path = source_dir / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(content)

        # metadata.db に登録
        import hashlib
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        content_hash = hashlib.sha256(content).hexdigest()
        store.db.register_source(
            source_id=source_id,
            source_type=source_type,
            title=title,
            content_hash=content_hash,
            file_size=len(content),
            collected_at=now,
            updated_at=now,
        )

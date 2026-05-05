"""ソース管理 Port + Real Adapter.

仕様: docs/specs/rag-knowledge.md / docs/specs/search-response.md

ソースの存在確認・全文取得・削除・ドキュメント取得を抽象化する。
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urldefrag

from rag.admin.models import DocumentResult
from rag.converter.converter import get_converted_rel_path

if TYPE_CHECKING:
    from rag.bm25_index import BM25Index
    from rag.vector_store import VectorStore

logger = logging.getLogger(__name__)

# テキストファイルとして扱う拡張子
_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {".md", ".txt", ".adoc", ".html", ".htm", ".json"},
)

_VALID_FORMATS: frozenset[str] = frozenset({"text", "original"})


class SourceManagementPort(Protocol):
    """ソース管理 Port.

    ベクトルストア / BM25 / source_store / converted_store にまたがる
    ソース単位の CRUD 系操作を抽象化する。
    """

    async def source_exists(self, source_url: str) -> bool:
        """ソース URL に対応するチャンクが存在するか確認する."""
        ...

    async def get_full_page_text(self, source_url: str) -> str:
        """ソース URL のページ全文を返す."""
        ...

    async def delete_source(self, source_url: str) -> int:
        """ソース URL 指定で知識を削除する."""
        ...

    def get_document(self, source_id: str, format: str) -> DocumentResult:
        """ドキュメント全文を取得する.

        format=\"text\" は converted_store、format=\"original\" は source_store から取得。
        """
        ...


class RealSourceManagementAdapter:
    """SourceManagementPort の本番実装."""

    def __init__(
        self,
        vector_store: VectorStore,
        *,
        bm25_index: BM25Index | None,
        source_store_dir: Path | None = None,
        converted_store_dir: Path | None = None,
    ) -> None:
        """RealSourceManagementAdapter を初期化する.

        Args:
            vector_store: ベクトルストア
            bm25_index: BM25 インデックス（オプション）
            source_store_dir: source_store ルートディレクトリ（get_document で使用）
            converted_store_dir: converted_store ルートディレクトリ（get_document で使用）
        """
        self._vector_store = vector_store
        self._bm25_index = bm25_index
        self._source_store_dir = source_store_dir
        self._converted_store_dir = converted_store_dir

    async def source_exists(self, source_url: str) -> bool:
        """ソース URL に対応するチャンクが存在するか確認する（軽量版）."""
        return await self._vector_store.source_exists(source_url)

    async def get_full_page_text(self, source_url: str) -> str:
        """ソース URL のページ全文を返す.

        VectorStore から全チャンクを取得し、chunk_index 昇順で結合する。
        """
        chunks = await self._vector_store.get_chunks_by_source(source_url)
        return "\n".join(chunk.text for chunk in chunks)

    async def delete_source(self, source_url: str) -> int:
        """ソース URL 指定で知識を削除."""
        normalized_url, fragment = urldefrag(source_url)

        total_deleted = await self._vector_store.delete_by_source(normalized_url)

        if fragment:
            legacy_deleted = await self._vector_store.delete_by_source(source_url)
            total_deleted += legacy_deleted
            logger.info(
                "Deleted %d chunks from sources: %s (normalized), %s (with fragment)",
                total_deleted, normalized_url, source_url,
            )
        else:
            logger.info(
                "Deleted %d chunks from source: %s",
                total_deleted, normalized_url,
            )

        if self._bm25_index is not None:
            try:
                bm25_deleted = self._bm25_index.delete_by_source(normalized_url)
                if fragment:
                    bm25_deleted += self._bm25_index.delete_by_source(source_url)
                logger.debug("Deleted %d documents from BM25 index", bm25_deleted)
            except Exception:
                logger.warning(
                    "Failed to delete from BM25 index for %s", normalized_url,
                    exc_info=True,
                )

        return total_deleted

    def get_document(self, source_id: str, format: str) -> DocumentResult:
        """ドキュメント全文を取得する（MCP/CLI 共通ロジック）."""
        if self._source_store_dir is None or self._converted_store_dir is None:
            raise RuntimeError(
                "get_document requires source_store_dir and converted_store_dir; "
                "RealSourceManagementAdapter must be constructed with both",
            )
        return get_document(
            source_id=source_id,
            format=format,
            source_store_dir=str(self._source_store_dir),
            converted_store_dir=str(self._converted_store_dir),
        )


def get_document(
    source_id: str,
    format: str,
    source_store_dir: str,
    converted_store_dir: str,
) -> DocumentResult:
    """ドキュメント全文を取得する（MCP/CLI 共通ロジック）.

    Adapter 経由とスタンドアロン経由の両方から利用される。

    Args:
        source_id: ソース識別子
        format: 取得形式（"text" または "original"）
        source_store_dir: source_store のルートディレクトリパス
        converted_store_dir: converted_store のルートディレクトリパス

    Returns:
        DocumentResult
    """
    if format not in _VALID_FORMATS:
        valid = ", ".join(sorted(_VALID_FORMATS))
        return DocumentResult(
            source_id=source_id,
            title="",
            source_type="",
            format=format,
            content="",
            error=f"無効な format: {format!r}（有効値: {valid}）",
        )

    from rag.store.source_store import SourceStore

    with SourceStore(root_dir=Path(source_store_dir)) as store:
        record = store.db.get_source(source_id)

        if record is None:
            return DocumentResult(
                source_id=source_id,
                title="",
                source_type="",
                format=format,
                content="",
                error=f"ソースが見つかりません: {source_id}",
            )

        title = record.title
        source_type = record.source_type
        file_path = record.source_id

        source_meta = store.get_metadata(source_id, record=record)
        collected_at = source_meta.collected_at if source_meta else ""
        extra = source_meta.extra if source_meta else {}

        if format == "original":
            ext = PurePosixPath(file_path).suffix.lower()
            if ext not in _TEXT_EXTENSIONS:
                mime_type = (
                    mimetypes.guess_type(file_path)[0]
                    or "application/octet-stream"
                )
                file_size = record.file_size
                info = (
                    f"バイナリファイルです（MIME: {mime_type}, サイズ: "
                    f"{file_size:,} bytes）。\n"
                    "テキスト形式で取得するには format=text を指定してください。"
                )
                return DocumentResult(
                    source_id=source_id,
                    title=title,
                    source_type=source_type,
                    format=format,
                    content=info,
                    is_binary=True,
                    collected_at=collected_at,
                    extra=extra,
                )

            file_data = store.get_file(source_id)
            if file_data is None:
                return DocumentResult(
                    source_id=source_id,
                    title=title,
                    source_type=source_type,
                    format=format,
                    content="",
                    error=f"ファイルが見つかりません: {file_path}",
                )

            try:
                content = file_data.content.decode("utf-8")
            except UnicodeDecodeError:
                content = file_data.content.decode("utf-8", errors="replace")

            return DocumentResult(
                source_id=source_id,
                title=title,
                source_type=source_type,
                format=format,
                content=content,
                collected_at=collected_at,
                extra=extra,
            )

    # format == "text": converted_store から読み取り
    converted_rel = get_converted_rel_path(file_path)
    converted_path = Path(converted_store_dir) / converted_rel

    if not converted_path.exists():
        return DocumentResult(
            source_id=source_id,
            title=title,
            source_type=source_type,
            format=format,
            content="",
            error=(
                f"変換済みファイルが見つかりません: {converted_rel}\n"
                "source_store にオリジナルが存在します。"
                "format=original で取得できます。"
            ),
        )

    try:
        content = converted_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = converted_path.read_bytes().decode("utf-8", errors="replace")

    return DocumentResult(
        source_id=source_id,
        title=title,
        source_type=source_type,
        format=format,
        content=content,
        collected_at=collected_at,
        extra=extra,
    )

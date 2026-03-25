"""インデクサー — メインモジュール.

仕様: docs/specs/indexer.md

converted_store のテキストファイルからチャンキング・Embedding・インデックス構築を行う。
パイプライン制御から IndexerProtocol 経由で呼び出される。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from pathlib import Path
from typing import Any

from rag.bm25_index import BM25Index
from rag.chunker import chunk_text
from rag.content_detector import ContentType, detect_content_type
from rag.heading_chunker import chunk_by_headings
from rag.indexer.chunk_id import generate_chunk_id, parse_chunk_index
from rag.indexer.metadata import build_chunk_metadata
from rag.store.metadata_db import MetadataDB
from rag.store.models import SourceMetadata, SourceType
from rag.table_chunker import chunk_table_data
from rag.vector_store import DocumentChunk, VectorStore

logger = logging.getLogger(__name__)


class Indexer:
    """インデクサー実装.

    IndexerProtocol を満たし、パイプライン制御から呼び出される。
    """

    # Embedding プレフィックス "search_document: " の文字数
    _EMBEDDING_PREFIX_LEN = 17

    def __init__(
        self,
        vector_store: VectorStore,
        bm25_index: BM25Index,
        metadata_db: MetadataDB,
        chunk_size: int = 200,
        chunk_overlap: int = 30,
        embedding_prefix_enabled: bool = True,
        embedding_context_length: int = 512,
        worst_token_char_ratio: float = 0.7,
        heading_overhead: int = 100,
    ) -> None:
        """Indexer を初期化する.

        Args:
            vector_store: ChromaDB ベクトルストア
            bm25_index: BM25 キーワード検索インデックス
            metadata_db: metadata.db 操作クラス
            chunk_size: チャンクの最大文字数
            chunk_overlap: チャンク間のオーバーラップ文字数
            embedding_prefix_enabled: Embedding プレフィックスの付与有無
            embedding_context_length: Embedding モデルのコンテキスト長（トークン数）
            worst_token_char_ratio: 最悪ケーストークン/文字比率
            heading_overhead: 見出しチャンカーの breadcrumb + heading オーバーヘッド目安
        """
        self._vector_store = vector_store
        self._bm25 = bm25_index
        self._metadata_db = metadata_db
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._embedding_checked = False

        # 実効サイズの算出: min(chunk_size, 安全上限 - overhead)
        # 仕様: docs/specs/indexer.md「チャンカー別オーバーヘッドと実効サイズ」
        token_safe_limit = int(embedding_context_length / worst_token_char_ratio)
        prefix_overhead = self._EMBEDDING_PREFIX_LEN if embedding_prefix_enabled else 0
        safe_base = token_safe_limit - prefix_overhead
        self._effective_size_prose = max(1, min(chunk_size, safe_base))
        self._effective_size_heading = max(1, min(chunk_size, safe_base - heading_overhead))
        # テーブルは行単位分割のため chunk_size を適用しない（仕様参照）
        self._effective_size_table = max(1, safe_base)
        # overlap が実効サイズ以上だと chunk_text が ValueError になるためクランプ
        self._effective_overlap = min(chunk_overlap, self._effective_size_prose - 1)

    def add(
        self,
        source_id: str,
        converted_path: Path,
        metadata: SourceMetadata,
    ) -> None:
        """変換済みファイルをインデックスに追加する.

        Args:
            source_id: ソース識別子
            converted_path: converted_store 内の変換済みファイルパス
            metadata: ソースメタデータ

        Raises:
            ValueError: source_id と metadata.source_id が一致しない場合
            ConnectionError: Embedding プロバイダーに接続できない場合
        """
        _validate_source_id(source_id, metadata)
        self._check_embedding_available()
        text = self._read_file(converted_path)
        if not text.strip():
            logger.info("空ファイルのためスキップ: %s", converted_path)
            return

        chunk_texts = self._chunk_text(text)
        if not chunk_texts:
            return

        self._add_to_indices(source_id, chunk_texts, metadata)
        logger.info(
            "インデックスに追加: %s (%d チャンク)", source_id, len(chunk_texts),
        )

    def update(
        self,
        source_id: str,
        converted_path: Path,
        metadata: SourceMetadata,
    ) -> None:
        """インデックスを更新する.

        再チャンキング・再 Embedding で新チャンクを upsert し、
        stale チャンクを削除する。

        Args:
            source_id: ソース識別子
            converted_path: converted_store 内の変換済みファイルパス
            metadata: ソースメタデータ

        Raises:
            ValueError: source_id と metadata.source_id が一致しない場合
            ConnectionError: Embedding プロバイダーに接続できない場合
        """
        _validate_source_id(source_id, metadata)
        self._check_embedding_available()
        text = self._read_file(converted_path)

        if not text.strip():
            self.delete(source_id)
            return

        chunk_texts = self._chunk_text(text)
        if not chunk_texts:
            self.delete(source_id)
            return

        # 新チャンク ID を算出
        new_chunk_ids = {
            generate_chunk_id(source_id, i) for i in range(len(chunk_texts))
        }

        # 新チャンクを upsert
        self._add_to_indices(source_id, chunk_texts, metadata)

        # stale チャンクを削除
        _run_async(
            self._vector_store.delete_stale_chunks(source_id, new_chunk_ids),
        )
        self._bm25.delete_stale_docs(source_id, new_chunk_ids)

        logger.info(
            "インデックスを更新: %s (%d チャンク)", source_id, len(chunk_texts),
        )

    def delete(self, source_id: str) -> None:
        """インデックスから削除する.

        Args:
            source_id: ソース識別子
        """
        _run_async(self._vector_store.delete_by_source(source_id))
        self._bm25.delete_by_source(source_id)
        logger.info("インデックスから削除: %s", source_id)

    def upsert_metadata(
        self,
        source_id: str,
        metadata: SourceMetadata,
    ) -> None:
        """メタデータのみ更新する（チャンク再生成なし）.

        Args:
            source_id: ソース識別子
            metadata: ソースメタデータ
        """
        chunk_ids = _run_async(
            self._vector_store.get_chunk_ids_for_source(source_id),
        )
        if not chunk_ids:
            logger.warning(
                "メタデータ更新対象のチャンクが存在しません: %s", source_id,
            )
            return

        total_chunks = len(chunk_ids)
        metadatas = []
        for chunk_id in chunk_ids:
            chunk_index = parse_chunk_index(chunk_id)
            meta = build_chunk_metadata(metadata, chunk_index, total_chunks)
            metadatas.append(meta)

        _run_async(self._vector_store.update_metadata(chunk_ids, metadatas))

        logger.info(
            "メタデータを更新: %s (%d チャンク)", source_id, total_chunks,
        )

    def clear(self, source_type: SourceType | None = None) -> None:
        """インデックスをクリアする.

        Args:
            source_type: 指定時はその媒体のみクリア
        """
        if source_type is None:
            self._clear_all()
        else:
            self._clear_by_source_type(source_type)

    # --- 内部メソッド ---

    def _check_embedding_available(self) -> None:
        """Embedding プロバイダーの疎通を確認する.

        初回呼び出し時のみ実際にチェックし、結果をキャッシュする。
        バッチ処理（run_index_only 等）での重複チェックを回避する。

        Raises:
            ConnectionError: プロバイダーに接続できない場合
        """
        if self._embedding_checked:
            return
        available = _run_async(self._vector_store.is_embedding_available())
        if not available:
            msg = "Embedding プロバイダーに接続できません"
            raise ConnectionError(msg)
        self._embedding_checked = True

    def _read_file(self, path: Path) -> str:
        """ファイルの内容を読み取る."""
        return path.read_text(encoding="utf-8")

    def _chunk_text(self, text: str) -> list[str]:
        """コンテンツタイプに応じたチャンキング戦略でテキストを分割する.

        各チャンカーにはトークン安全上限から算出した実効サイズを渡す。
        仕様: docs/specs/indexer.md「チャンカー別オーバーヘッドと実効サイズ」
        """
        content_type = detect_content_type(text)

        if content_type == ContentType.TABLE:
            table_chunks = chunk_table_data(
                text, max_chunk_size=self._effective_size_table,
            )
            return [c.formatted_text for c in table_chunks] if table_chunks else []

        if content_type in (ContentType.HEADING, ContentType.MIXED):
            heading_chunks = chunk_by_headings(
                text,
                max_chunk_size=self._effective_size_heading,
                min_chunk_size=max(1, self._effective_size_heading // 4),
            )
            return [c.formatted_text for c in heading_chunks] if heading_chunks else []

        # PROSE
        return chunk_text(
            text,
            chunk_size=self._effective_size_prose,
            chunk_overlap=self._effective_overlap,
        )

    def _add_to_indices(
        self,
        source_id: str,
        chunk_texts: list[str],
        metadata: SourceMetadata,
    ) -> None:
        """チャンクを ChromaDB と BM25 に追加する."""
        total_chunks = len(chunk_texts)

        doc_chunks: list[DocumentChunk] = []
        bm25_docs: list[tuple[str, str, str, str]] = []
        bm25_metadata_list: list[dict[str, str | int | float | bool]] = []

        for i, text in enumerate(chunk_texts):
            chunk_id = generate_chunk_id(source_id, i)
            meta = build_chunk_metadata(metadata, i, total_chunks)

            doc_chunks.append(DocumentChunk(
                id=chunk_id,
                text=text,
                metadata=meta,
            ))
            bm25_docs.append((
                chunk_id, text, source_id, metadata.source_type,
            ))
            bm25_metadata_list.append(meta)

        # ChromaDB に upsert（async → sync ブリッジ）
        _run_async(self._vector_store.add_documents(doc_chunks))

        # BM25 に追加
        self._bm25.add_documents(bm25_docs, metadata_list=bm25_metadata_list)

    def _clear_all(self) -> None:
        """全インデックスをクリアする."""
        _run_async(self._vector_store.clear())
        self._bm25.clear()
        logger.info("全インデックスをクリアしました")

    def _clear_by_source_type(self, source_type: SourceType) -> None:
        """指定 source_type のインデックスをクリアする."""
        records = self._metadata_db.search_sources(source_type=source_type)
        for record in records:
            self.delete(record.source_id)

        logger.info(
            "source_type=%s のインデックスをクリア (%d 件)",
            source_type, len(records),
        )


def _validate_source_id(source_id: str, metadata: SourceMetadata) -> None:
    """source_id と metadata.source_id の一致を検証する."""
    if source_id != metadata.source_id:
        msg = (
            f"source_id の不一致: 引数={source_id}, "
            f"metadata.source_id={metadata.source_id}"
        )
        raise ValueError(msg)


def _run_async(coro: Any) -> Any:
    """async コルーチンを sync コンテキストから実行する.

    パイプライン制御は sync で呼び出すため通常は asyncio.run() パスを使用する。
    既存ループ内からの呼び出し時は新スレッドで実行する（MCP サーバー等）。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # 既存ループ内からの呼び出し（MCP サーバー等）
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()

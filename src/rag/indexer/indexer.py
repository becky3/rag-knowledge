"""インデクサー — メインモジュール.

仕様: docs/specs/indexer.md

converted_store のテキストファイルからチャンキング・Embedding・インデックス構築を行う。
パイプライン制御から IndexerProtocol 経由で呼び出される。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from pathlib import Path

from rag.bm25_index import BM25Index
from rag.chunker import chunk_text
from rag.content_detector import ContentType, detect_content_type
from rag.heading_chunker import chunk_by_headings
from rag.indexer.chunk_id import generate_chunk_id, parse_chunk_index
from rag.indexer.metadata import build_chunk_metadata
from rag.indexer.write_strategy import BM25WriteStrategy, IndexWriteStrategy
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
        chunk_size: int,
        chunk_overlap: int,
        embedding_prefix_enabled: bool,
        embedding_context_length: int,
        worst_token_char_ratio: float,
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
        """
        self._vector_store = vector_store
        self._bm25 = bm25_index
        self._metadata_db = metadata_db
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._embedding_checked = False
        self._embedding_lock = asyncio.Lock()
        self._write_strategies: list[IndexWriteStrategy] = [
            BM25WriteStrategy(bm25_index),
        ]

        # 実効サイズの算出: min(chunk_size, 安全上限 - overhead)
        # 仕様: docs/specs/indexer.md「オーバーヘッドと実効サイズ」
        token_safe_limit = int(embedding_context_length / worst_token_char_ratio)
        prefix_overhead = self._EMBEDDING_PREFIX_LEN if embedding_prefix_enabled else 0
        safe_base = token_safe_limit - prefix_overhead
        self._effective_size_prose = max(1, min(chunk_size, safe_base))
        # テーブルは行単位分割のため chunk_size を適用しない（仕様参照）
        self._effective_size_table = max(1, safe_base)
        # overlap が実効サイズ以上だと chunk_text が ValueError になるためクランプ
        self._effective_overlap = min(chunk_overlap, self._effective_size_prose - 1)

    async def add(
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
        await self._check_embedding_available()
        text, chunk_texts = await asyncio.to_thread(
            self._read_and_chunk, converted_path,
        )
        if not text.strip():
            logger.info("空ファイルのためスキップ: %s", converted_path)
            return

        if not chunk_texts:
            return

        await self._add_to_indices(source_id, chunk_texts, metadata)
        logger.info(
            "インデックスに追加: %s (%d チャンク)", source_id, len(chunk_texts),
        )

    async def update(
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
        await self._check_embedding_available()
        text, chunk_texts = await asyncio.to_thread(
            self._read_and_chunk, converted_path,
        )

        if not text.strip():
            await self.delete(source_id)
            return

        if not chunk_texts:
            await self.delete(source_id)
            return

        # 新チャンク ID を算出
        new_chunk_ids = {
            generate_chunk_id(source_id, i) for i in range(len(chunk_texts))
        }

        # 新チャンクを upsert
        await self._add_to_indices(source_id, chunk_texts, metadata)

        # stale チャンクを削除
        await self._vector_store.delete_stale_chunks(source_id, new_chunk_ids)
        self._bm25.delete_stale_docs(source_id, new_chunk_ids)

        logger.info(
            "インデックスを更新: %s (%d チャンク)", source_id, len(chunk_texts),
        )

    async def delete(self, source_id: str) -> None:
        """インデックスから削除する.

        Args:
            source_id: ソース識別子
        """
        await self._vector_store.delete_by_source(source_id)
        self._bm25.delete_by_source(source_id)
        logger.info("インデックスから削除: %s", source_id)

    async def upsert_metadata(
        self,
        source_id: str,
        metadata: SourceMetadata,
    ) -> None:
        """メタデータのみ更新する（チャンク再生成なし）.

        Args:
            source_id: ソース識別子
            metadata: ソースメタデータ
        """
        chunk_ids = await self._vector_store.get_chunk_ids_for_source(source_id)
        if not chunk_ids:
            logger.warning(
                "メタデータ更新対象のチャンクが存在しません: %s", source_id,
            )
            return

        total_chunks = len(chunk_ids)

        # 既存チャンクの section_path を保持する（メタデータのみ更新時に消さない）
        existing_meta_map = await self._vector_store.get_metadata_by_ids(
            chunk_ids,
        )

        metadatas = []
        for chunk_id in chunk_ids:
            chunk_index = parse_chunk_index(chunk_id)
            existing_sp = str(
                existing_meta_map.get(chunk_id, {}).get("section_path", ""),
            )
            meta = build_chunk_metadata(
                metadata, chunk_index, total_chunks, section_path=existing_sp,
            )
            metadatas.append(meta)

        await self._vector_store.update_metadata(chunk_ids, metadatas)

        logger.info(
            "メタデータを更新: %s (%d チャンク)", source_id, total_chunks,
        )

    @contextlib.contextmanager
    def batch_writes(self) -> Iterator[None]:
        """バッチ書き込みコンテキスト.

        保有する全 ``IndexWriteStrategy`` を ``ExitStack`` で nest し、enter/exit する。
        各 strategy がインデックス実装ごとの戦略（BM25 は deferred save → flush 等）を
        適用する。``with`` ブロック内で例外が発生した場合も、すでに enter 済みの
        strategy は LIFO 順で確実に exit される（``ExitStack`` の保証）。

        strategy の ``__exit__`` 自体が例外を投げた場合は呼び出し側に伝播し、
        パイプラインを中断する。with ブロック内のソース処理エラーは
        ``_run_processing_loop`` 側で ``PipelineSummary.errors`` に集約される。
        """
        with contextlib.ExitStack() as stack:
            for strategy in self._write_strategies:
                stack.enter_context(strategy)
            yield

    async def clear(
        self,
        source_type: SourceType | None = None,
        *,
        path: str | None = None,
    ) -> None:
        """インデックスをクリアする.

        Args:
            source_type: 指定時はその媒体のみクリア
            path: 指定時はそのディレクトリ配下（再帰的）のみクリア
                （source_type と排他）
        """
        if source_type is not None and path is not None:
            msg = "source_type と path は同時に指定できません"
            raise ValueError(msg)
        if path is not None:
            await self._clear_by_path(path)
        elif source_type is None:
            await self._clear_all()
        else:
            await self._clear_by_source_type(source_type)

    # --- 内部メソッド ---

    async def _check_embedding_available(self) -> None:
        """Embedding プロバイダーの疎通を確認する.

        初回呼び出し時のみ実際にチェックし、結果をキャッシュする。
        バッチ処理（run_index_only 等）での重複チェックを回避する。
        asyncio.Lock で並列呼び出し時のレースコンディションを防止する。

        Raises:
            ConnectionError: プロバイダーに接続できない場合
        """
        if self._embedding_checked:
            return
        async with self._embedding_lock:
            if self._embedding_checked:
                return
            available = await self._vector_store.is_embedding_available()
            if not available:
                msg = "Embedding プロバイダーに接続できません"
                raise ConnectionError(msg)
            self._embedding_checked = True

    def _read_file(self, path: Path) -> str:
        """ファイルの内容を読み取る."""
        return path.read_text(encoding="utf-8")

    def _read_and_chunk(
        self, converted_path: Path,
    ) -> tuple[str, list[tuple[str, str]]]:
        """ファイル読み込みとチャンキングを同期実行する (to_thread 用)."""
        text = self._read_file(converted_path)
        if not text.strip():
            return text, []
        return text, self._chunk_text(text)

    def _chunk_text(self, text: str) -> list[tuple[str, str]]:
        """コンテンツタイプに応じたチャンキング戦略でテキストを分割する.

        各チャンカーにはトークン安全上限から算出した実効サイズを渡す。
        仕様: docs/specs/indexer.md「オーバーヘッドと実効サイズ」

        Returns:
            (content, section_path) のタプルリスト
        """
        content_type = detect_content_type(text)

        if content_type == ContentType.TABLE:
            table_chunks = chunk_table_data(
                text, row_context_size=1, max_chunk_size=self._effective_size_table,
            )
            return [
                (c.content, c.section_path) for c in table_chunks
            ] if table_chunks else []

        if content_type in (ContentType.HEADING, ContentType.MIXED):
            heading_chunks = chunk_by_headings(
                text,
                max_chunk_size=self._effective_size_prose,
                min_chunk_size=max(1, self._effective_size_prose // 4),
            )
            return [
                (c.content, c.section_path) for c in heading_chunks
            ] if heading_chunks else []

        # PROSE
        prose_chunks = chunk_text(
            text,
            chunk_size=self._effective_size_prose,
            chunk_overlap=self._effective_overlap,
        )
        return [(c, "") for c in prose_chunks]

    async def _add_to_indices(
        self,
        source_id: str,
        chunk_texts: list[tuple[str, str]],
        metadata: SourceMetadata,
    ) -> None:
        """チャンクを ChromaDB と BM25 に追加する.

        Args:
            source_id: ソース識別子
            chunk_texts: (content, section_path) のタプルリスト
            metadata: ソースメタデータ
        """
        total_chunks = len(chunk_texts)

        doc_chunks: list[DocumentChunk] = []
        bm25_docs: list[tuple[str, str, str, str]] = []
        bm25_metadata_list: list[dict[str, str | int | float | bool]] = []

        for i, (content, section_path) in enumerate(chunk_texts):
            chunk_id = generate_chunk_id(source_id, i)
            meta = build_chunk_metadata(metadata, i, total_chunks, section_path)

            # ChromaDB: Embedding は本文のみ（section_path を含めない）
            doc_chunks.append(DocumentChunk(
                id=chunk_id,
                text=content,
                metadata=meta,
            ))

            # BM25: 本文のみ（section_path はメタデータで保持）
            bm25_docs.append((
                chunk_id, content, source_id, metadata.source_type,
            ))
            bm25_metadata_list.append(meta)

        # ChromaDB に upsert
        await self._vector_store.add_documents(doc_chunks)

        # BM25 に追加
        self._bm25.add_documents(bm25_docs, metadata_list=bm25_metadata_list)

    async def _clear_all(self) -> None:
        """全インデックスをクリアする."""
        await self._vector_store.clear()
        self._bm25.clear()
        logger.info("全インデックスをクリアしました")

    async def _clear_by_source_type(self, source_type: SourceType) -> None:
        """指定 source_type のインデックスを一括クリアする."""
        vector_count = await self._vector_store.delete_by_source_type(source_type)
        bm25_count = self._bm25.delete_by_source_type(source_type)

        logger.info(
            "source_type=%s のインデックスをクリア (vector: %d, bm25: %d)",
            source_type, vector_count, bm25_count,
        )

    async def _clear_by_path(self, path: str) -> None:
        """指定パス配下のインデックスを一括クリアする."""
        vector_count = await self._vector_store.delete_by_source_id_prefix(path)
        bm25_count = self._bm25.delete_by_source_id_prefix(path)

        logger.info(
            "path=%s 配下のインデックスをクリア (vector: %d, bm25: %d)",
            path, vector_count, bm25_count,
        )


def _validate_source_id(source_id: str, metadata: SourceMetadata) -> None:
    """source_id と metadata.source_id の一致を検証する."""
    if source_id != metadata.source_id:
        msg = (
            f"source_id の不一致: 引数={source_id}, "
            f"metadata.source_id={metadata.source_id}"
        )
        raise ValueError(msg)

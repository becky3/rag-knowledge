"""ChromaDBベクトルストアモジュール

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlparse

import chromadb
from chromadb.api.shared_system_client import SharedSystemClient
from chromadb.api.types import Embeddings, Metadatas
from chromadb.config import Settings as ChromaSettings

from .embedding.base import EmbeddingProvider

logger = logging.getLogger(__name__)


@dataclass
class DocumentChunk:
    """ベクトルストアに格納するチャンク."""

    id: str  # ユニークID（URLハッシュ + chunk_index）
    text: str  # チャンク本文
    metadata: dict[str, str | int | float | bool]  # source_id, title, chunk_index, crawled_at / collected_at, source_type, custom:*


@dataclass
class RetrievalResult:
    """検索結果."""

    text: str
    metadata: dict[str, str | int | float | bool]
    distance: float  # 小さいほど類似度が高い


class VectorStore:
    """ChromaDBベースのベクトルストア.

    仕様: docs/specs/rag-knowledge.md
    """

    # HNSW パラメータのデフォルト値
    _DEFAULT_HNSW_M = 48
    _DEFAULT_HNSW_CONSTRUCTION_EF = 400
    _DEFAULT_HNSW_SEARCH_EF = 300

    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        persist_directory: str = "./chroma_db",
        collection_name: str = "knowledge",
        *,
        hnsw_m: int | None = None,
        hnsw_construction_ef: int | None = None,
        hnsw_search_ef: int | None = None,
    ) -> None:
        """VectorStoreを初期化する.

        Args:
            embedding_provider: Embedding生成プロバイダー
            persist_directory: ChromaDBの永続化ディレクトリ
            collection_name: コレクション名
            hnsw_m: HNSW グラフの最大接続数
            hnsw_construction_ef: HNSW 構築時の探索候補数
            hnsw_search_ef: HNSW 検索時の探索候補数
        """
        self._embedding = embedding_provider
        self._persist_directory = persist_directory
        self._collection_name = collection_name
        self._hnsw_metadata = self._build_hnsw_metadata(
            hnsw_m, hnsw_construction_ef, hnsw_search_ef,
        )
        # テレメトリを無効化
        chroma_settings = ChromaSettings(anonymized_telemetry=False)
        self._client = chromadb.PersistentClient(
            path=persist_directory, settings=chroma_settings
        )
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata=self._hnsw_metadata,
        )

    @classmethod
    def _build_hnsw_metadata(
        cls,
        hnsw_m: int | None,
        hnsw_construction_ef: int | None,
        hnsw_search_ef: int | None,
    ) -> dict[str, str | int]:
        """HNSW パラメータからコレクション metadata を構築する."""
        return {
            "hnsw:space": "cosine",
            "hnsw:M": hnsw_m if hnsw_m is not None else cls._DEFAULT_HNSW_M,
            "hnsw:construction_ef": hnsw_construction_ef if hnsw_construction_ef is not None else cls._DEFAULT_HNSW_CONSTRUCTION_EF,
            "hnsw:search_ef": hnsw_search_ef if hnsw_search_ef is not None else cls._DEFAULT_HNSW_SEARCH_EF,
        }

    @classmethod
    def create_http(
        cls,
        embedding_provider: EmbeddingProvider,
        host: str = "localhost",
        port: int = 8000,
        collection_name: str = "knowledge",
        *,
        hnsw_m: int | None = None,
        hnsw_construction_ef: int | None = None,
        hnsw_search_ef: int | None = None,
    ) -> "VectorStore":
        """ChromaDB サーバーに HttpClient で接続する VectorStore を作成する.

        Args:
            embedding_provider: Embedding生成プロバイダー
            host: ChromaDB サーバーのホスト
            port: ChromaDB サーバーのポート
            collection_name: コレクション名
            hnsw_m: HNSW グラフの最大接続数
            hnsw_construction_ef: HNSW 構築時の探索候補数
            hnsw_search_ef: HNSW 検索時の探索候補数

        Returns:
            HttpClient ベースの VectorStore インスタンス
        """
        instance = cls.__new__(cls)
        instance._embedding = embedding_provider
        instance._persist_directory = ""
        instance._collection_name = collection_name
        instance._hnsw_metadata = cls._build_hnsw_metadata(
            hnsw_m, hnsw_construction_ef, hnsw_search_ef,
        )
        chroma_settings = ChromaSettings(anonymized_telemetry=False)
        instance._client = chromadb.HttpClient(
            host=host, port=port, settings=chroma_settings,
        )
        try:
            instance._client.heartbeat()
        except Exception as exc:
            msg = (
                f"ChromaDB サーバー ({host}:{port}) への接続に失敗しました: {exc}. "
                "'chroma run' でサーバーを起動してください。"
            )
            raise ConnectionError(msg) from exc
        instance._collection = instance._client.get_or_create_collection(
            name=collection_name,
            metadata=instance._hnsw_metadata,
        )
        return instance

    @classmethod
    def create_ephemeral(
        cls,
        embedding_provider: EmbeddingProvider,
        collection_name: str = "knowledge",
        *,
        hnsw_m: int | None = None,
        hnsw_construction_ef: int | None = None,
        hnsw_search_ef: int | None = None,
    ) -> "VectorStore":
        """テスト用のインメモリVectorStoreを作成する.

        Args:
            embedding_provider: Embedding生成プロバイダー
            collection_name: コレクション名
            hnsw_m: HNSW グラフの最大接続数
            hnsw_construction_ef: HNSW 構築時の探索候補数
            hnsw_search_ef: HNSW 検索時の探索候補数

        Returns:
            インメモリのVectorStoreインスタンス
        """
        instance = cls.__new__(cls)
        instance._embedding = embedding_provider
        instance._persist_directory = ""
        instance._collection_name = collection_name
        instance._hnsw_metadata = cls._build_hnsw_metadata(
            hnsw_m, hnsw_construction_ef, hnsw_search_ef,
        )
        # テレメトリを無効化
        chroma_settings = ChromaSettings(anonymized_telemetry=False)
        instance._client = chromadb.EphemeralClient(settings=chroma_settings)
        instance._collection = instance._client.get_or_create_collection(
            name=collection_name,
            metadata=instance._hnsw_metadata,
        )
        return instance

    async def add_documents(self, chunks: list[DocumentChunk]) -> int:
        """チャンクをEmbedding→ベクトルストアに追加（upsert動作）.

        同じIDのドキュメントが既に存在する場合は上書きする。

        Args:
            chunks: 追加するチャンクのリスト

        Returns:
            追加件数
        """
        if not chunks:
            return 0

        # テキストをEmbeddingに変換
        texts = [chunk.text for chunk in chunks]
        raw_embeddings = await self._embedding.embed_documents(texts)
        embeddings: Embeddings = cast(Embeddings, raw_embeddings)

        # ChromaDBにupsert（同期APIなのでto_threadでラップ）
        ids = [chunk.id for chunk in chunks]
        metadatas = [chunk.metadata for chunk in chunks]

        await asyncio.to_thread(
            self._collection.upsert,
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas,  # type: ignore[arg-type]
        )

        logger.info("Upserted %d documents to vector store", len(chunks))
        return len(chunks)

    async def search(
        self,
        query: str,
        n_results: int = 5,
        similarity_threshold: float | None = None,
        where: dict[str, str | int | float | bool] | None = None,
    ) -> list[RetrievalResult]:
        """クエリに類似するチャンクを検索する.

        Args:
            query: 検索クエリ
            n_results: 返却する結果の最大数
            similarity_threshold: 類似度閾値（cosine距離）。指定時、この値より大きいdistanceの結果を除外
            where: メタデータフィルタ（例: {"source_type": "bluesky"}）

        Returns:
            検索結果のリスト（類似度の高い順）
        """
        # クエリをEmbeddingに変換
        raw_query_embedding = await self._embedding.embed_query(query)
        query_embeddings: Embeddings = cast(Embeddings, [raw_query_embedding])

        # 閾値フィルタリングを行う場合、多めに取得してからフィルタリング
        fetch_count = n_results
        if similarity_threshold is not None:
            # 閾値フィルタリング後にn_results件返すため、多めに取得
            # 3倍（最低20件）: 閾値で除外される可能性を考慮し余裕を持って取得
            fetch_count = max(n_results * 3, 20)

        # コレクションサイズを超えないように制限（ChromaDBバージョンによる例外を防止）
        collection_count = await asyncio.to_thread(self._collection.count)
        if collection_count > 0:
            fetch_count = min(fetch_count, collection_count)
        else:
            # コレクションが空の場合は空リストを返す
            return []

        # ChromaDBで検索（同期APIなのでto_threadでラップ）
        query_kwargs: dict[str, object] = {
            "query_embeddings": query_embeddings,
            "n_results": fetch_count,
            "include": ["documents", "metadatas", "distances"],
        }
        if where is not None:
            query_kwargs["where"] = where

        results = await asyncio.to_thread(
            self._collection.query,
            **query_kwargs,  # type: ignore[arg-type]
        )

        # 結果を変換
        retrieval_results: list[RetrievalResult] = []
        excluded_count = 0
        if results["documents"] and results["documents"][0]:
            documents = results["documents"][0]
            metadatas = results["metadatas"][0] if results["metadatas"] else [{}] * len(documents)
            distances = results["distances"][0] if results["distances"] else [0.0] * len(documents)

            for doc, meta, dist in zip(documents, metadatas, distances):
                # 閾値フィルタリング
                if similarity_threshold is not None and dist > similarity_threshold:
                    excluded_count += 1
                    continue

                retrieval_results.append(
                    RetrievalResult(
                        text=doc,
                        metadata=meta or {},  # type: ignore[arg-type]
                        distance=dist,
                    )
                )

                # n_results件に達したら終了
                if len(retrieval_results) >= n_results:
                    break

        if excluded_count > 0:
            logger.debug(
                "Similarity threshold filtering: excluded %d results (threshold=%.3f)",
                excluded_count,
                similarity_threshold,
            )

        return retrieval_results

    async def get_chunks_by_source(self, source_id: str) -> list[RetrievalResult]:
        """ソース識別子指定で全チャンクを取得する（chunk_index 昇順）.

        Args:
            source_id: ソース識別子

        Returns:
            チャンクのリスト（chunk_index 昇順）
        """
        results = await asyncio.to_thread(
            self._collection.get,
            where={"source_id": source_id},
            include=["documents", "metadatas"],
        )

        if not results["ids"]:
            return []

        chunks: list[RetrievalResult] = []
        documents = results["documents"] or []
        metadatas = results["metadatas"] or []

        for doc, meta in zip(documents, metadatas):
            chunks.append(
                RetrievalResult(
                    text=doc or "",
                    metadata=meta or {},  # type: ignore[arg-type]
                    distance=0.0,
                )
            )

        # chunk_index 昇順でソート
        chunks.sort(key=lambda c: int(c.metadata.get("chunk_index", 0)))
        return chunks

    async def get_metadata_by_ids(
        self,
        ids: list[str],
    ) -> dict[str, dict[str, str | int | float | bool]]:
        """チャンクIDのリストからメタデータを一括取得する.

        Args:
            ids: チャンクIDのリスト

        Returns:
            チャンクID → メタデータ辞書のマッピング
        """
        if not ids:
            return {}

        results = await asyncio.to_thread(
            self._collection.get,
            ids=ids,
            include=["metadatas"],
        )

        meta_map: dict[str, dict[str, str | int | float | bool]] = {}
        metadatas = results["metadatas"] or []
        for chunk_id, meta in zip(results["ids"], metadatas):
            meta_map[chunk_id] = meta or {}  # type: ignore[assignment]

        return meta_map

    async def source_exists(self, source_id: str) -> bool:
        """ソース識別子に対応するチャンクが存在するか確認する（軽量版）.

        documents / metadatas を取得せず、IDs の有無のみで判定する。

        Args:
            source_id: ソース識別子

        Returns:
            チャンクが 1 件以上存在すれば True
        """
        results = await asyncio.to_thread(
            self._collection.get,
            where={"source_id": source_id},
            include=[],
        )
        return bool(results["ids"])

    async def delete_by_source(self, source_id: str) -> int:
        """ソース識別子指定でチャンクを削除.

        Args:
            source_id: ソース識別子

        Returns:
            削除件数
        """
        # まず該当するドキュメントを検索
        results = await asyncio.to_thread(
            self._collection.get,
            where={"source_id": source_id},
            include=["metadatas"],
        )

        if not results["ids"]:
            return 0

        ids_to_delete = results["ids"]
        count = len(ids_to_delete)

        # 削除実行
        await asyncio.to_thread(
            self._collection.delete,
            ids=ids_to_delete,
        )

        logger.info("Deleted %d documents from vector store (source: %s)", count, source_id)
        return count

    async def delete_stale_chunks(self, source_id: str, valid_ids: set[str]) -> int:
        """ソースのチャンクのうち、valid_idsに含まれないものを削除.

        upsert後に古いチャンクを削除するために使用。

        Args:
            source_id: ソース識別子
            valid_ids: 保持するID（これ以外のIDを削除）

        Returns:
            削除件数
        """
        # ソースの全チャンクを取得
        results = await asyncio.to_thread(
            self._collection.get,
            where={"source_id": source_id},
            include=["metadatas"],
        )

        if not results["ids"]:
            return 0

        # valid_idsに含まれないIDを特定
        stale_ids = [id_ for id_ in results["ids"] if id_ not in valid_ids]

        if not stale_ids:
            return 0

        # 削除実行
        await asyncio.to_thread(
            self._collection.delete,
            ids=stale_ids,
        )

        logger.info(
            "Deleted %d stale chunks from vector store (source: %s)", len(stale_ids), source_id
        )
        return len(stale_ids)

    def get_stats(self) -> dict[str, object]:
        """ナレッジベース統計（総チャンク数等）とソース一覧を返す.

        Note:
            この関数は全チャンクのメタデータを走査するため O(N) のコストがかかる。
            チャンク数が多い場合は頻繁な呼び出しを避けること。

        Returns:
            統計情報の辞書。キー:
            - total_chunks: 総チャンク数
            - source_count: ユニークソースURL数
            - sources: ドメイン別ソース一覧
        """
        count = self._collection.count()

        # ユニークなソースURL数とソース詳細を取得
        all_docs = self._collection.get(include=["metadatas"])
        source_urls: set[str] = set()
        # url -> {"title": str, "chunks": int}
        source_details: dict[str, dict[str, str | int]] = {}
        if all_docs["metadatas"]:
            for meta in all_docs["metadatas"]:
                if meta and "source_id" in meta:
                    url = str(meta["source_id"])
                    source_urls.add(url)
                    if url not in source_details:
                        source_details[url] = {
                            "title": str(meta.get("title", "")),
                            "chunks": 0,
                        }
                    source_details[url]["chunks"] = int(source_details[url]["chunks"]) + 1

        # ドメイン別にグルーピング
        domain_groups: dict[str, list[dict[str, str | int]]] = {}
        for url, detail in source_details.items():
            domain = urlparse(url).netloc or "unknown"
            if domain not in domain_groups:
                domain_groups[domain] = []
            domain_groups[domain].append({
                "url": url,
                "title": detail["title"],
                "chunks": detail["chunks"],
            })

        # ドメイン名でソート、各ドメイン内はタイトルでソート
        sources: list[dict[str, object]] = []
        for domain in sorted(domain_groups.keys()):
            pages = sorted(domain_groups[domain], key=lambda p: str(p.get("title", "")))
            sources.append({
                "domain": domain,
                "pages": pages,
            })

        return {
            "total_chunks": count,
            "source_count": len(source_urls),
            "sources": sources,
        }

    def close(self) -> None:
        """リソースを解放し、SharedSystemClient キャッシュをクリアする.

        ChromaDB の SharedSystemClient は persist_directory をキーにして
        System インスタンスをキャッシュする。サブプロセスが DB を更新した後、
        このキャッシュが残っていると古い HNSW インメモリ状態が再利用され、
        where フィルタ付き query() が失敗する。

        close() 後に新しい PersistentClient を作成すると、ディスクから
        最新の HNSW インデックスがロードされる。
        """
        identifier = self._persist_directory
        if not identifier:
            return

        try:
            systems = getattr(SharedSystemClient, "_identifier_to_system", None)
            refcounts = getattr(SharedSystemClient, "_identifier_to_refcount", None)
            if systems is None or not isinstance(systems, dict):
                logger.warning(
                    "SharedSystemClient internals changed; "
                    "cannot clear cache for: %s",
                    identifier,
                )
                return

            system = systems.pop(identifier, None)
            if system is not None:
                system.stop()
                if isinstance(refcounts, dict):
                    refcounts.pop(identifier, None)
                logger.info(
                    "Cleared SharedSystemClient cache for: %s", identifier,
                )
        except Exception:
            logger.warning(
                "Failed to clear SharedSystemClient cache for: %s",
                identifier,
                exc_info=True,
            )

    async def get_chunk_ids_for_source(self, source_id: str) -> list[str]:
        """source_id に紐づく全チャンク ID を取得する.

        Args:
            source_id: ソース識別子

        Returns:
            チャンク ID のリスト
        """
        result = await asyncio.to_thread(
            self._collection.get,
            where={"source_id": source_id},
            include=[],
        )
        return list(result["ids"]) if result["ids"] else []

    async def update_metadata(
        self,
        chunk_ids: list[str],
        metadatas: list[dict[str, str | int | float | bool]],
    ) -> None:
        """チャンクのメタデータのみ更新する（Embedding/ドキュメントは維持）.

        Args:
            chunk_ids: 更新対象のチャンク ID リスト
            metadatas: 対応するメタデータのリスト
        """
        if len(chunk_ids) != len(metadatas):
            raise ValueError(
                f"update_metadata: length mismatch: "
                f"chunk_ids={len(chunk_ids)}, metadatas={len(metadatas)}"
            )

        if not chunk_ids:
            return

        await asyncio.to_thread(
            self._collection.update,
            ids=chunk_ids,
            metadatas=cast(Metadatas, metadatas),
        )

    async def clear(self) -> None:
        """コレクションを削除して再作成する（全データクリア）."""
        collection_name = self._collection_name

        def _clear_sync() -> None:
            self._client.delete_collection(collection_name)
            self._collection = self._client.get_or_create_collection(
                name=collection_name,
                metadata=self._hnsw_metadata,
            )

        await asyncio.to_thread(_clear_sync)

    async def is_embedding_available(self) -> bool:
        """Embedding プロバイダーの疎通を確認する.

        Returns:
            接続可能なら True
        """
        return await self._embedding.is_available()

"""インデクサーのテスト.

仕様: docs/specs/indexer.md
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from rag.bm25_index import BM25Index
from rag.embedding.base import EmbeddingProvider
from rag.indexer.chunk_id import generate_chunk_id
from rag.indexer.indexer import Indexer
from rag.store.metadata_db import MetadataDB
from rag.store.models import SourceMetadata
from rag.vector_store import VectorStore


# --- テストヘルパー ---


class MockEmbeddingProvider(EmbeddingProvider):
    """テスト用のモック Embedding プロバイダー."""

    def __init__(self, dimension: int = 3) -> None:
        self._dimension = dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            [float(i + len(text)) for i in range(self._dimension)]
            for text in texts
        ]

    async def is_available(self) -> bool:
        return True


def _make_metadata(**overrides: object) -> SourceMetadata:
    """テスト用の SourceMetadata を生成する."""
    defaults: dict[str, object] = {
        "source_id": "https://example.com/page",
        "source_type": "web",
        "title": "Test Page",
        "collected_at": "2025-01-01T00:00:00Z",
        "extra": {},
    }
    defaults.update(overrides)
    return SourceMetadata(**defaults)  # type: ignore[arg-type]


# --- フィクスチャ ---


@pytest.fixture
def mock_embedding() -> MockEmbeddingProvider:
    return MockEmbeddingProvider(dimension=3)


@pytest.fixture
def vector_store(mock_embedding: MockEmbeddingProvider) -> VectorStore:
    """各テストで独立したインメモリ VectorStore."""
    collection_name = f"test_{uuid.uuid4().hex[:8]}"
    return VectorStore.create_ephemeral(mock_embedding, collection_name)


@pytest.fixture
def bm25_index() -> BM25Index:
    """インメモリ BM25 インデックス."""
    return BM25Index(k1=1.5, b=0.75)


@pytest.fixture
def metadata_db(tmp_path: Path) -> MetadataDB:
    """テスト用の MetadataDB."""
    db = MetadataDB(tmp_path / "metadata.db")
    db.initialize()
    return db


@pytest.fixture
def indexer(
    vector_store: VectorStore,
    bm25_index: BM25Index,
    metadata_db: MetadataDB,
) -> Indexer:
    """テスト用の Indexer."""
    return Indexer(
        vector_store=vector_store,
        bm25_index=bm25_index,
        metadata_db=metadata_db,
        chunk_size=200,
        chunk_overlap=30,
    )


def _write_text_file(tmp_path: Path, filename: str, content: str) -> Path:
    """テスト用のテキストファイルを作成する."""
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    return path


# --- テスト: add ---


class TestAdd:
    """Indexer.add のテスト."""

    def test_adds_prose_to_chromadb(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "doc.txt", "Sample prose text for testing.")
        meta = _make_metadata(source_id="src-1")

        indexer.add("src-1", path, meta)

        # ChromaDB にチャンクが追加されたか確認
        result = vector_store._collection.get(
            where={"source_id": "src-1"}, include=["metadatas"],
        )
        assert len(result["ids"]) >= 1

    def test_adds_prose_to_bm25(
        self, indexer: Indexer, bm25_index: BM25Index, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "doc.txt", "Sample prose text for testing.")
        meta = _make_metadata(source_id="src-1")

        indexer.add("src-1", path, meta)

        assert bm25_index.get_document_count() >= 1

    def test_skips_empty_file(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "empty.txt", "   \n  ")
        meta = _make_metadata(source_id="src-empty")

        indexer.add("src-empty", path, meta)

        result = vector_store._collection.get(
            where={"source_id": "src-empty"}, include=[],
        )
        assert len(result["ids"]) == 0

    def test_chunk_metadata_has_required_fields(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "doc.txt", "Short text for metadata check.")
        meta = _make_metadata(
            source_id="src-meta",
            source_type="web",
            title="Meta Test",
            collected_at="2025-06-01T00:00:00Z",
        )

        indexer.add("src-meta", path, meta)

        result = vector_store._collection.get(
            where={"source_id": "src-meta"}, include=["metadatas"],
        )
        assert result["metadatas"]
        chunk_meta = result["metadatas"][0]
        assert chunk_meta["source_id"] == "src-meta"
        assert chunk_meta["source_type"] == "web"
        assert chunk_meta["title"] == "Meta Test"
        assert chunk_meta["chunk_index"] == 0
        assert chunk_meta["total_chunks"] >= 1
        assert chunk_meta["collected_at"] == "2025-06-01T00:00:00Z"

    def test_custom_metadata_fields(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "doc.txt", "Text with custom metadata.")
        meta = _make_metadata(
            source_id="src-custom",
            extra={"author": "Alice", "category": "tech"},
        )

        indexer.add("src-custom", path, meta)

        result = vector_store._collection.get(
            where={"source_id": "src-custom"}, include=["metadatas"],
        )
        chunk_meta = result["metadatas"][0]
        assert chunk_meta["custom:author"] == "Alice"
        assert chunk_meta["custom:category"] == "tech"

    def test_heading_text_uses_heading_chunker(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        text = "# Section A\n\nContent of section A.\n\n# Section B\n\nContent of section B."
        path = _write_text_file(tmp_path, "heading.txt", text)
        meta = _make_metadata(source_id="src-heading")

        indexer.add("src-heading", path, meta)

        result = vector_store._collection.get(
            where={"source_id": "src-heading"}, include=["documents"],
        )
        assert len(result["ids"]) >= 2

    def test_section_path_in_metadata(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        """section_path がメタデータに含まれること."""
        text = "# Parent\n\n## Child\n\nContent under child."
        path = _write_text_file(tmp_path, "heading.txt", text)
        meta = _make_metadata(source_id="src-sp")

        indexer.add("src-sp", path, meta)

        result = vector_store._collection.get(
            where={"source_id": "src-sp"}, include=["metadatas"],
        )
        # いずれかのチャンクに section_path が設定されている
        section_paths = [m.get("section_path", "") for m in result["metadatas"]]
        assert any("Parent" in sp for sp in section_paths)

    def test_embedding_receives_content_only(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        """Embedding（ChromaDB の documents）には本文のみが格納されること."""
        text = "# UniqueHeading\n\nBody text only."
        path = _write_text_file(tmp_path, "heading.txt", text)
        meta = _make_metadata(source_id="src-emb")

        indexer.add("src-emb", path, meta)

        result = vector_store._collection.get(
            where={"source_id": "src-emb"},
            include=["documents", "metadatas"],
        )
        # section_path はメタデータに格納されている
        assert any(
            m.get("section_path") == "UniqueHeading"
            for m in result["metadatas"]
        )
        # documents（Embedding 入力）には見出しテキストが含まれない
        for doc in result["documents"]:
            assert "UniqueHeading" not in doc
            assert "[" not in doc  # 旧形式の breadcrumb
            assert "# " not in doc  # 旧形式の見出しプレフィックス

    def test_bm25_receives_section_path_plus_content(
        self, indexer: Indexer, bm25_index: BM25Index, tmp_path: Path,
    ) -> None:
        """BM25 には section_path + 本文が渡されること."""
        text = "# MyHeading\n\nBody text for BM25."
        path = _write_text_file(tmp_path, "heading.txt", text)
        meta = _make_metadata(source_id="src-bm25-sp")

        indexer.add("src-bm25-sp", path, meta)

        # BM25 で見出しキーワードで検索できる
        results = bm25_index.search("MyHeading", n_results=5)
        assert len(results) > 0

    def test_table_text_uses_table_chunker(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        text = (
            "| Name | HP | MP |\n"
            "| --- | --- | --- |\n"
            "| Alice | 100 | 50 |\n"
            "| Bob | 80 | 60 |\n"
            "| Carol | 120 | 40 |"
        )
        path = _write_text_file(tmp_path, "table.txt", text)
        meta = _make_metadata(source_id="src-table")

        indexer.add("src-table", path, meta)

        result = vector_store._collection.get(
            where={"source_id": "src-table"}, include=[],
        )
        assert len(result["ids"]) >= 1

    def test_chunk_ids_follow_spec_format(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "doc.txt", "Text for ID format check.")
        meta = _make_metadata(source_id="src-id-fmt")

        indexer.add("src-id-fmt", path, meta)

        result = vector_store._collection.get(
            where={"source_id": "src-id-fmt"}, include=[],
        )
        expected_id = generate_chunk_id("src-id-fmt", 0)
        assert expected_id in result["ids"]


# --- テスト: update ---


class TestUpdate:
    """Indexer.update のテスト."""

    def test_updates_existing_chunks(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "doc.txt", "Original content.")
        meta = _make_metadata(source_id="src-upd")

        indexer.add("src-upd", path, meta)

        # 更新
        path.write_text("Updated content with new information.", encoding="utf-8")
        indexer.update("src-upd", path, meta)

        result = vector_store._collection.get(
            where={"source_id": "src-upd"}, include=["documents"],
        )
        assert len(result["ids"]) >= 1
        assert any("Updated" in doc for doc in result["documents"])

    def test_stale_chunks_deleted_when_count_decreases(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        """複数チャンク → 少数チャンクで stale チャンクが削除される."""
        # 長いテキスト（複数チャンク）
        long_text = " ".join(["Long paragraph content."] * 200)
        path = _write_text_file(tmp_path, "doc.txt", long_text)
        meta = _make_metadata(source_id="src-stale")

        indexer.add("src-stale", path, meta)
        initial_result = vector_store._collection.get(
            where={"source_id": "src-stale"}, include=[],
        )
        initial_count = len(initial_result["ids"])
        assert initial_count > 1

        # 短いテキスト（1チャンク）に更新
        path.write_text("Short updated text.", encoding="utf-8")
        indexer.update("src-stale", path, meta)

        result = vector_store._collection.get(
            where={"source_id": "src-stale"}, include=[],
        )
        assert len(result["ids"]) < initial_count

    def test_stale_chunks_deleted_from_bm25(
        self, indexer: Indexer, bm25_index: BM25Index, tmp_path: Path,
    ) -> None:
        long_text = " ".join(["Long paragraph content."] * 200)
        path = _write_text_file(tmp_path, "doc.txt", long_text)
        meta = _make_metadata(source_id="src-bm25-stale")

        indexer.add("src-bm25-stale", path, meta)
        initial_count = bm25_index.get_document_count()
        assert initial_count > 1

        # 短いテキスト
        path.write_text("Short.", encoding="utf-8")
        indexer.update("src-bm25-stale", path, meta)

        assert bm25_index.get_document_count() < initial_count

    def test_update_empty_file_deletes_all(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "doc.txt", "Some content.")
        meta = _make_metadata(source_id="src-empty-upd")

        indexer.add("src-empty-upd", path, meta)

        # 空ファイルに更新
        path.write_text("", encoding="utf-8")
        indexer.update("src-empty-upd", path, meta)

        result = vector_store._collection.get(
            where={"source_id": "src-empty-upd"}, include=[],
        )
        assert len(result["ids"]) == 0


# --- テスト: delete ---


class TestDelete:
    """Indexer.delete のテスト."""

    def test_deletes_from_chromadb(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "doc.txt", "Content to be deleted.")
        meta = _make_metadata(source_id="src-del")

        indexer.add("src-del", path, meta)
        indexer.delete("src-del")

        result = vector_store._collection.get(
            where={"source_id": "src-del"}, include=[],
        )
        assert len(result["ids"]) == 0

    def test_deletes_from_bm25(
        self, indexer: Indexer, bm25_index: BM25Index, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "doc.txt", "Content to be deleted.")
        meta = _make_metadata(source_id="src-del-bm25")

        indexer.add("src-del-bm25", path, meta)
        assert bm25_index.get_document_count() >= 1

        indexer.delete("src-del-bm25")
        assert bm25_index.get_document_count() == 0

    def test_delete_nonexistent_source_no_error(
        self, indexer: Indexer,
    ) -> None:
        """存在しない source_id の削除はエラーにならない."""
        indexer.delete("nonexistent-source")

    def test_delete_only_affects_target_source(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        path_a = _write_text_file(tmp_path, "a.txt", "Content A.")
        path_b = _write_text_file(tmp_path, "b.txt", "Content B.")
        meta_a = _make_metadata(source_id="src-a")
        meta_b = _make_metadata(source_id="src-b")

        indexer.add("src-a", path_a, meta_a)
        indexer.add("src-b", path_b, meta_b)

        indexer.delete("src-a")

        result_a = vector_store._collection.get(
            where={"source_id": "src-a"}, include=[],
        )
        result_b = vector_store._collection.get(
            where={"source_id": "src-b"}, include=[],
        )
        assert len(result_a["ids"]) == 0
        assert len(result_b["ids"]) >= 1


# --- テスト: upsert_metadata ---


class TestUpsertMetadata:
    """Indexer.upsert_metadata のテスト."""

    def test_updates_metadata_without_changing_text(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "doc.txt", "Content for metadata upsert.")
        original_meta = _make_metadata(source_id="src-meta-up", title="Old Title")

        indexer.add("src-meta-up", path, original_meta)

        # メタデータのみ更新
        updated_meta = _make_metadata(source_id="src-meta-up", title="New Title")
        indexer.upsert_metadata("src-meta-up", updated_meta)

        result = vector_store._collection.get(
            where={"source_id": "src-meta-up"},
            include=["metadatas", "documents"],
        )
        assert result["metadatas"][0]["title"] == "New Title"
        # テキストは維持される
        assert result["documents"][0] is not None

    def test_updates_custom_fields(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "doc.txt", "Content with custom meta.")
        original_meta = _make_metadata(
            source_id="src-custom-up",
            extra={"author": "Alice"},
        )

        indexer.add("src-custom-up", path, original_meta)

        updated_meta = _make_metadata(
            source_id="src-custom-up",
            extra={"author": "Bob", "version": 2},
        )
        indexer.upsert_metadata("src-custom-up", updated_meta)

        result = vector_store._collection.get(
            where={"source_id": "src-custom-up"},
            include=["metadatas"],
        )
        meta = result["metadatas"][0]
        assert meta["custom:author"] == "Bob"
        assert meta["custom:version"] == 2

    def test_nonexistent_source_logs_warning(
        self, indexer: Indexer,
    ) -> None:
        """存在しない source_id のメタデータ更新はエラーにならない."""
        meta = _make_metadata(source_id="nonexistent")
        indexer.upsert_metadata("nonexistent", meta)


# --- テスト: clear ---


class TestClear:
    """Indexer.clear のテスト."""

    def test_clear_all(
        self, indexer: Indexer, vector_store: VectorStore,
        bm25_index: BM25Index, tmp_path: Path,
    ) -> None:
        path_a = _write_text_file(tmp_path, "a.txt", "Content A.")
        path_b = _write_text_file(tmp_path, "b.txt", "Content B.")
        meta_a = _make_metadata(source_id="src-a", source_type="web")
        meta_b = _make_metadata(source_id="src-b", source_type="zenn")

        indexer.add("src-a", path_a, meta_a)
        indexer.add("src-b", path_b, meta_b)

        indexer.clear()

        assert vector_store._collection.count() == 0
        assert bm25_index.get_document_count() == 0

    def test_clear_by_source_type(
        self, indexer: Indexer, vector_store: VectorStore,
        metadata_db: MetadataDB, tmp_path: Path,
    ) -> None:
        # metadata.db にレコードを登録
        metadata_db.register_source(
            source_id="src-web",
            source_type="web",
            file_path="web/page.md",
            title="Web Page",
            content_hash="abc",
            file_size=100,
            collected_at="2025-01-01T00:00:00Z",
            updated_at="2025-01-01T00:00:00Z",
        )
        metadata_db.register_source(
            source_id="src-zenn",
            source_type="zenn",
            file_path="zenn/article.md",
            title="Zenn Article",
            content_hash="def",
            file_size=200,
            collected_at="2025-01-01T00:00:00Z",
            updated_at="2025-01-01T00:00:00Z",
        )

        path_web = _write_text_file(tmp_path, "web.txt", "Web content.")
        path_zenn = _write_text_file(tmp_path, "zenn.txt", "Zenn content.")
        meta_web = _make_metadata(source_id="src-web", source_type="web")
        meta_zenn = _make_metadata(source_id="src-zenn", source_type="zenn")

        indexer.add("src-web", path_web, meta_web)
        indexer.add("src-zenn", path_zenn, meta_zenn)

        # web のみクリア
        indexer.clear("web")

        result_web = vector_store._collection.get(
            where={"source_id": "src-web"}, include=[],
        )
        result_zenn = vector_store._collection.get(
            where={"source_id": "src-zenn"}, include=[],
        )
        assert len(result_web["ids"]) == 0
        assert len(result_zenn["ids"]) >= 1

    def test_clear_empty_index_no_error(
        self, indexer: Indexer,
    ) -> None:
        """空インデックスの clear はエラーにならない."""
        indexer.clear()

    def test_clear_all_then_add(
        self, indexer: Indexer, vector_store: VectorStore, tmp_path: Path,
    ) -> None:
        """clear 後に再追加できる."""
        path = _write_text_file(tmp_path, "doc.txt", "Content.")
        meta = _make_metadata(source_id="src-re")

        indexer.add("src-re", path, meta)
        indexer.clear()
        indexer.add("src-re", path, meta)

        result = vector_store._collection.get(
            where={"source_id": "src-re"}, include=[],
        )
        assert len(result["ids"]) >= 1


# --- テスト: source_id バリデーション ---


class TestSourceIdValidation:
    """source_id と metadata.source_id の一致バリデーションテスト."""

    def test_add_raises_on_mismatch(
        self, indexer: Indexer, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "doc.txt", "Content.")
        meta = _make_metadata(source_id="different-id")

        with pytest.raises(ValueError, match="source_id の不一致"):
            indexer.add("src-mismatch", path, meta)

    def test_update_raises_on_mismatch(
        self, indexer: Indexer, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "doc.txt", "Content.")
        meta = _make_metadata(source_id="different-id")

        with pytest.raises(ValueError, match="source_id の不一致"):
            indexer.update("src-mismatch", path, meta)

    def test_matching_ids_no_error(
        self, indexer: Indexer, tmp_path: Path,
    ) -> None:
        path = _write_text_file(tmp_path, "doc.txt", "Content.")
        meta = _make_metadata(source_id="src-ok")
        indexer.add("src-ok", path, meta)


# --- テスト: Embedding 疎通確認 ---


class UnavailableEmbeddingProvider(EmbeddingProvider):
    """疎通不可のモック Embedding プロバイダー."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 3 for _ in texts]

    async def is_available(self) -> bool:
        return False


class TestEmbeddingAvailability:
    """Embedding プロバイダー疎通確認のテスト."""

    def test_add_raises_when_embedding_unavailable(
        self, bm25_index: BM25Index, metadata_db: MetadataDB, tmp_path: Path,
    ) -> None:
        """add 時に Embedding プロバイダーが接続不可ならエラーになること."""
        provider = UnavailableEmbeddingProvider()
        collection_name = f"test_{uuid.uuid4().hex[:8]}"
        vs = VectorStore.create_ephemeral(provider, collection_name)
        idx = Indexer(
            vector_store=vs,
            bm25_index=bm25_index,
            metadata_db=metadata_db,
        )
        path = _write_text_file(tmp_path, "doc.txt", "Some content.")
        meta = _make_metadata(source_id="src-unavail")

        with pytest.raises(ConnectionError, match="Embedding"):
            idx.add("src-unavail", path, meta)

    def test_update_raises_when_embedding_unavailable(
        self, bm25_index: BM25Index, metadata_db: MetadataDB, tmp_path: Path,
    ) -> None:
        """update 時に Embedding プロバイダーが接続不可ならエラーになること."""
        provider = UnavailableEmbeddingProvider()
        collection_name = f"test_{uuid.uuid4().hex[:8]}"
        vs = VectorStore.create_ephemeral(provider, collection_name)
        idx = Indexer(
            vector_store=vs,
            bm25_index=bm25_index,
            metadata_db=metadata_db,
        )
        path = _write_text_file(tmp_path, "doc.txt", "Some content.")
        meta = _make_metadata(source_id="src-unavail-upd")

        with pytest.raises(ConnectionError, match="Embedding"):
            idx.update("src-unavail-upd", path, meta)

    def test_delete_does_not_check_embedding(
        self, indexer: Indexer,
    ) -> None:
        """delete は Embedding チェックを行わないこと（エラーにならない）."""
        indexer.delete("nonexistent-source")

    def test_clear_does_not_check_embedding(
        self, indexer: Indexer,
    ) -> None:
        """clear は Embedding チェックを行わないこと（エラーにならない）."""
        indexer.clear()

    def test_check_cached_after_first_success(
        self, indexer: Indexer, tmp_path: Path,
    ) -> None:
        """疎通確認は初回成功後にキャッシュされ、2回目以降はスキップされること."""
        assert indexer._embedding_checked is False
        path = _write_text_file(tmp_path, "doc.txt", "Content for cache test.")
        meta = _make_metadata(source_id="src-cache")

        indexer.add("src-cache", path, meta)

        # 初回 add 後にキャッシュされている
        assert indexer._embedding_checked is True


# --- テスト: IndexerProtocol 適合性 ---


class TestProtocolConformance:
    """IndexerProtocol へのプロトコル適合性テスト."""

    def test_conforms_to_protocol(self, indexer: Indexer) -> None:
        """Indexer が IndexerProtocol のメソッドシグネチャを満たす."""
        from rag.pipeline.protocols import IndexerProtocol

        def _accept_protocol(obj: IndexerProtocol) -> None:
            """型チェッカー（mypy）でプロトコル適合性を検証する."""

        # ランタイムでも呼び出し可能であることを確認
        _accept_protocol(indexer)

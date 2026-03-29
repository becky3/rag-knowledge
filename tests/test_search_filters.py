"""検索フィルタ機能のテスト

FR-4: 汎用カスタムメタデータフィルタ（rag_search 拡張）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.bm25_index import BM25Index
from rag.filter_parser import parse_filters

from factories import make_bm25_index


class TestParseFilters:
    """parse_filters のテスト."""

    def test_single_key_value(self) -> None:
        """単一 key=value のパース."""
        result = parse_filters("repository=rag-knowledge")
        assert result == {"repository": "rag-knowledge"}

    def test_multiple_key_values(self) -> None:
        """複数 key=value のパース."""
        result = parse_filters("repository=rag-knowledge,tag=dev")
        assert result == {"repository": "rag-knowledge", "tag": "dev"}

    def test_whitespace_trimmed(self) -> None:
        """前後の空白がトリムされる."""
        result = parse_filters(" repository = rag-knowledge , tag = dev ")
        assert result == {"repository": "rag-knowledge", "tag": "dev"}

    def test_empty_string_returns_empty_dict(self) -> None:
        """空文字列は空 dict."""
        result = parse_filters("")
        assert result == {}

    def test_value_with_equals(self) -> None:
        """値に = が含まれる場合（最初の = で分割）."""
        result = parse_filters("expr=a=b")
        assert result == {"expr": "a=b"}

    def test_missing_equals_raises(self) -> None:
        """= を含まないペアはエラー."""
        with pytest.raises(ValueError, match="不正なフィルタ形式"):
            parse_filters("invalid-filter")

    def test_empty_key_raises(self) -> None:
        """キーが空の場合はエラー."""
        with pytest.raises(ValueError, match="フィルタのキーが空です"):
            parse_filters("=value")

    def test_empty_value_allowed(self) -> None:
        """値が空は許容."""
        result = parse_filters("key=")
        assert result == {"key": ""}

    def test_trailing_comma_ignored(self) -> None:
        """末尾カンマは無視."""
        result = parse_filters("repository=rag-knowledge,")
        assert result == {"repository": "rag-knowledge"}


class TestBM25MetadataMap:
    """BM25Index の _doc_metadata_map 関連テスト."""

    def test_add_documents_with_metadata(self) -> None:
        """add_documents に metadata_list を渡すとメタデータが保持される."""
        index = make_bm25_index()
        docs = [
            ("doc1", "テスト文書1", "source1", "journal"),
            ("doc2", "テスト文書2", "source2", "journal"),
        ]
        metadata_list = [
            {"source_type": "journal", "custom:repository": "repo-a"},
            {"source_type": "journal", "custom:repository": "repo-b"},
        ]
        added = index.add_documents(docs, metadata_list=metadata_list)
        assert added == 2
        assert index._doc_metadata_map["doc1"]["custom:repository"] == "repo-a"
        assert index._doc_metadata_map["doc2"]["custom:repository"] == "repo-b"

    def test_add_documents_without_metadata(self) -> None:
        """metadata_list なしでも従来通り動作する."""
        index = make_bm25_index()
        docs = [
            ("doc1", "テスト文書1", "source1", "web"),
        ]
        added = index.add_documents(docs)
        assert added == 1
        assert "doc1" not in index._doc_metadata_map

    def test_update_document_metadata(self) -> None:
        """既存ドキュメントを更新するとメタデータも更新される."""
        index = make_bm25_index()
        docs = [("doc1", "テスト文書1", "source1", "journal")]
        meta = [{"custom:repository": "repo-a"}]
        index.add_documents(docs, metadata_list=meta)

        # 更新
        docs2 = [("doc1", "テスト文書1更新版", "source1", "journal")]
        meta2 = [{"custom:repository": "repo-b"}]
        index.add_documents(docs2, metadata_list=meta2)

        assert index._doc_metadata_map["doc1"]["custom:repository"] == "repo-b"

    def test_delete_by_source_cleans_metadata(self) -> None:
        """delete_by_source でメタデータも削除される."""
        index = make_bm25_index()
        docs = [
            ("doc1", "文書1", "source1", "journal"),
            ("doc2", "文書2", "source2", "journal"),
        ]
        meta = [
            {"custom:repository": "repo-a"},
            {"custom:repository": "repo-b"},
        ]
        index.add_documents(docs, metadata_list=meta)

        index.delete_by_source("source1")
        assert "doc1" not in index._doc_metadata_map
        assert "doc2" in index._doc_metadata_map

    def test_delete_stale_docs_cleans_metadata(self) -> None:
        """delete_stale_docs でメタデータも削除される."""
        index = make_bm25_index()
        docs = [
            ("doc1", "文書1", "source1", "journal"),
            ("doc2", "文書2", "source1", "journal"),
        ]
        meta = [
            {"custom:repository": "repo-a"},
            {"custom:repository": "repo-a"},
        ]
        index.add_documents(docs, metadata_list=meta)

        deleted = index.delete_stale_docs("source1", valid_ids={"doc1"})
        assert deleted == 1
        assert "doc1" in index._doc_metadata_map
        assert "doc2" not in index._doc_metadata_map

    def test_clear_cleans_metadata(self) -> None:
        """clear でメタデータも全削除される."""
        index = make_bm25_index()
        docs = [("doc1", "文書1", "source1", "journal")]
        meta = [{"custom:repository": "repo-a"}]
        index.add_documents(docs, metadata_list=meta)

        index.clear()
        assert len(index._doc_metadata_map) == 0


class TestBM25SearchWithFilters:
    """BM25 search の filters パラメータテスト."""

    @pytest.fixture()
    def index_with_metadata(self) -> BM25Index:
        """メタデータ付きドキュメントを持つ BM25Index."""
        index = make_bm25_index()
        docs = [
            ("doc1", "パイプライン移行の作業記録", "source1", "journal"),
            ("doc2", "検索精度の改善に関するメモ", "source2", "journal"),
            ("doc3", "パイプライン設計の技術文書", "source3", "web"),
        ]
        metadata_list = [
            {"source_type": "journal", "custom:repository": "rag-knowledge"},
            {"source_type": "journal", "custom:repository": "ai-assistant"},
            {"source_type": "web"},
        ]
        index.add_documents(docs, metadata_list=metadata_list)
        return index

    def test_filter_by_custom_field(self, index_with_metadata: BM25Index) -> None:
        """custom: フィールドでフィルタできる."""
        results = index_with_metadata.search(
            "パイプライン", n_results=10,
            filters={"custom:repository": "rag-knowledge"},
        )
        assert len(results) > 0
        assert all(r.doc_id == "doc1" for r in results)

    def test_filter_excludes_non_matching(self, index_with_metadata: BM25Index) -> None:
        """フィルタ条件に合わないドキュメントは除外される."""
        results = index_with_metadata.search(
            "パイプライン", n_results=10,
            filters={"custom:repository": "nonexistent"},
        )
        assert results == []

    def test_filter_none_returns_all(self, index_with_metadata: BM25Index) -> None:
        """filters=None は全件対象."""
        results = index_with_metadata.search(
            "パイプライン", n_results=10, filters=None,
        )
        assert len(results) > 0

    def test_filter_with_source_type(self, index_with_metadata: BM25Index) -> None:
        """source_type と filters の併用."""
        results = index_with_metadata.search(
            "パイプライン", n_results=10,
            source_type="journal",
            filters={"custom:repository": "rag-knowledge"},
        )
        assert len(results) > 0
        assert all(r.doc_id == "doc1" for r in results)

    def test_no_metadata_doc_excluded_by_filter(self) -> None:
        """メタデータを持たないドキュメントはフィルタで除外される."""
        index = make_bm25_index()
        docs = [
            ("doc1", "テスト文書", "source1", "web"),
        ]
        # metadata_list なしで追加
        index.add_documents(docs)

        results = index.search(
            "テスト", n_results=10,
            filters={"custom:repository": "any"},
        )
        assert results == []


class TestBM25MatchesFilters:
    """BM25Index._matches_filters の単体テスト."""

    def test_empty_filters_matches_all(self) -> None:
        """空の filters は常に True."""
        assert BM25Index._matches_filters({"key": "val"}, {}) is True

    def test_single_key_match(self) -> None:
        """単一キー一致."""
        assert BM25Index._matches_filters(
            {"custom:repository": "rag-knowledge"},
            {"custom:repository": "rag-knowledge"},
        ) is True

    def test_single_key_mismatch(self) -> None:
        """単一キー不一致."""
        assert BM25Index._matches_filters(
            {"custom:repository": "rag-knowledge"},
            {"custom:repository": "other"},
        ) is False

    def test_missing_key_mismatch(self) -> None:
        """メタデータにキーがない場合は不一致."""
        assert BM25Index._matches_filters(
            {},
            {"custom:repository": "rag-knowledge"},
        ) is False

    def test_multiple_keys_all_match(self) -> None:
        """複数キーが全て一致."""
        assert BM25Index._matches_filters(
            {"custom:repository": "rag-knowledge", "custom:author": "alice"},
            {"custom:repository": "rag-knowledge", "custom:author": "alice"},
        ) is True

    def test_multiple_keys_partial_mismatch(self) -> None:
        """複数キーのうち1つが不一致."""
        assert BM25Index._matches_filters(
            {"custom:repository": "rag-knowledge", "custom:author": "alice"},
            {"custom:repository": "rag-knowledge", "custom:author": "bob"},
        ) is False

    def test_case_insensitive_match(self) -> None:
        """大文字小文字を区別しない比較."""
        assert BM25Index._matches_filters(
            {"custom:repository": "Rag-Knowledge"},
            {"custom:repository": "rag-knowledge"},
        ) is True

    def test_int_metadata_matches_string_filter(self) -> None:
        """int メタデータが文字列フィルタにマッチする."""
        assert BM25Index._matches_filters(
            {"custom:duration": 42},
            {"custom:duration": "42"},
        ) is True

    def test_bool_metadata_matches_string_filter(self) -> None:
        """bool メタデータが文字列フィルタにマッチする（大文字小文字不問）."""
        assert BM25Index._matches_filters(
            {"custom:closed": True},
            {"custom:closed": "true"},
        ) is True

    def test_bool_false_matches(self) -> None:
        """bool False が 'false' にマッチする."""
        assert BM25Index._matches_filters(
            {"custom:closed": False},
            {"custom:closed": "false"},
        ) is True


class TestBM25MetadataPersistence:
    """BM25Index のメタデータ永続化テスト."""

    @pytest.fixture()
    def persist_dir(self, tmp_path: Path) -> str:
        return str(tmp_path / "bm25_filter_test")

    def test_metadata_map_persisted_and_restored(self, persist_dir: str) -> None:
        """_doc_metadata_map が永続化・復元される."""
        index = make_bm25_index(persist_dir=persist_dir)
        docs = [
            ("doc1", "テスト文書", "source1", "journal"),
        ]
        meta = [{"source_type": "journal", "custom:repository": "rag-knowledge"}]
        index.add_documents(docs, metadata_list=meta)

        # 新インスタンスで復元
        index2 = make_bm25_index(persist_dir=persist_dir)
        assert index2._doc_metadata_map["doc1"]["custom:repository"] == "rag-knowledge"

    def test_filter_works_after_persistence(self, persist_dir: str) -> None:
        """永続化→復元後もフィルタ検索が動作する."""
        index = make_bm25_index(persist_dir=persist_dir)
        docs = [
            ("doc1", "パイプライン移行の作業記録", "source1", "journal"),
            ("doc2", "検索精度の改善メモ", "source2", "journal"),
        ]
        meta = [
            {"source_type": "journal", "custom:repository": "rag-knowledge"},
            {"source_type": "journal", "custom:repository": "ai-assistant"},
        ]
        index.add_documents(docs, metadata_list=meta)

        # 永続化→復元
        index2 = make_bm25_index(persist_dir=persist_dir)
        results = index2.search(
            "パイプライン", n_results=10,
            filters={"custom:repository": "rag-knowledge"},
        )
        assert len(results) > 0
        assert all(r.doc_id == "doc1" for r in results)


class TestBuildWhereClause:
    """RAGKnowledgeService._build_where_clause のテスト."""

    def _build(
        self,
        source_type: str | None = None,
        filters: dict[str, str] | None = None,
    ) -> dict | None:
        from rag.rag_knowledge import RAGKnowledgeService
        return RAGKnowledgeService._build_where_clause(source_type, filters)

    def test_both_none_returns_none(self) -> None:
        """source_type=None, filters=None → None."""
        assert self._build() is None

    def test_source_type_only(self) -> None:
        """source_type のみ → 単純な dict."""
        result = self._build(source_type="journal")
        assert result == {"source_type": "journal"}

    def test_filters_only(self) -> None:
        """filters のみ → custom: プレフィックス付き."""
        result = self._build(filters={"repository": "rag-knowledge"})
        assert result == {"custom:repository": "rag-knowledge"}

    def test_source_type_and_filters_combined(self) -> None:
        """source_type + filters → $and 結合."""
        result = self._build(
            source_type="journal",
            filters={"repository": "rag-knowledge"},
        )
        assert "$and" in result
        and_conditions = result["$and"]
        assert {"source_type": "journal"} in and_conditions
        assert {"custom:repository": "rag-knowledge"} in and_conditions

    def test_multiple_filters(self) -> None:
        """複数 filters → $and 結合."""
        result = self._build(
            filters={"repository": "rag-knowledge", "author": "alice"},
        )
        assert "$and" in result
        and_conditions = result["$and"]
        assert len(and_conditions) == 2

    def test_empty_filters_treated_as_none(self) -> None:
        """空 dict の filters → None."""
        assert self._build(filters={}) is None


class TestBuildBM25Filters:
    """RAGKnowledgeService._build_bm25_filters のテスト."""

    def _build(
        self, filters: dict[str, str],
    ) -> dict[str, str]:
        from rag.rag_knowledge import RAGKnowledgeService
        return RAGKnowledgeService._build_bm25_filters(filters)

    def test_adds_custom_prefix(self) -> None:
        """キーに custom: プレフィックスが付く."""
        result = self._build({"repository": "rag-knowledge"})
        assert result == {"custom:repository": "rag-knowledge"}

    def test_multiple_keys(self) -> None:
        """複数キーに custom: プレフィックスが付く."""
        result = self._build({"repository": "rag-knowledge", "author": "alice"})
        assert result == {
            "custom:repository": "rag-knowledge",
            "custom:author": "alice",
        }

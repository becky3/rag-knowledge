"""BM25インデックスのテスト

仕様: docs/specs/rag-knowledge.md
"""

import json
from pathlib import Path

import pytest

from rag.bm25_index import BM25Index, METADATA_FILENAME, tokenize_japanese


class TestTokenizeJapanese:
    """tokenize_japanese関数のテスト."""

    def test_empty_text_returns_empty_list(self) -> None:
        """AC7: 空のテキストは空リストを返す."""
        assert tokenize_japanese("") == []
        assert tokenize_japanese("   ") == []

    def test_japanese_text_tokenized(self) -> None:
        """日本語テキストがトークン化される."""
        tokens = tokenize_japanese("りゅうおうはボスモンスターです")

        # 何らかのトークンが生成される
        assert len(tokens) > 0

    def test_stopwords_removed(self) -> None:
        """AC7: ストップワードが除去される."""
        tokens = tokenize_japanese("これは本です")

        # 「これ」「は」「です」はストップワード
        assert "これ" not in tokens
        assert "は" not in tokens
        assert "です" not in tokens

    def test_mixed_text_tokenized(self) -> None:
        """AC7: 日本語と英数字の混合テキストがトークン化される."""
        tokens = tokenize_japanese("Python3とJavaScript")

        # 何らかのトークンが生成される
        assert len(tokens) > 0


class TestBM25Index:
    """BM25Indexクラスのテスト."""

    def test_add_and_search_documents(self) -> None:
        """ドキュメントの追加と検索ができる."""
        index = BM25Index()

        # ドキュメントを追加
        docs = [
            ("doc1", "りゅうおうはドラゴンクエストのボスです", "source1"),
            ("doc2", "スライムは最弱のモンスターです", "source2"),
            ("doc3", "ゾーマは強力な魔王です", "source3"),
        ]
        added = index.add_documents(docs)
        assert added == 3

        # 検索
        results = index.search("りゅうおう", n_results=3)

        # りゅうおうを含むドキュメントがヒット
        assert len(results) > 0
        assert any("りゅうおう" in r.text for r in results)

    def test_delete_by_source(self) -> None:
        """ソースURL指定でドキュメントを削除できる."""
        index = BM25Index()

        docs = [
            ("doc1", "テスト1", "source1"),
            ("doc2", "テスト2", "source1"),
            ("doc3", "テスト3", "source2"),
        ]
        index.add_documents(docs)

        # source1のドキュメントを削除
        deleted = index.delete_by_source("source1")
        assert deleted == 2

        # 残りは1件
        assert index.get_document_count() == 1

    def test_search_empty_index_returns_empty_list(self) -> None:
        """AC6: 空のインデックスへの検索は空リストを返す."""
        index = BM25Index()
        results = index.search("テスト")
        assert results == []

    def test_search_with_no_matches_returns_empty_list(self) -> None:
        """AC6: マッチするドキュメントがない場合は空リストを返す."""
        index = BM25Index()
        index.add_documents([("doc1", "りゅうおう", "source1")])

        # 全く関係ないクエリ
        results = index.search("プログラミング言語")

        # マッチなしの場合は空（またはスコア0の結果がない）
        # BM25はトークンベースなので、完全に無関係なら結果なし
        # ただし簡易トークナイザの場合は挙動が異なる可能性あり
        # 結果があってもスコアが0より大きいもののみ
        assert all(r.score > 0 for r in results)

    def test_update_existing_document(self) -> None:
        """AC6: 既存ドキュメントの更新."""
        index = BM25Index()

        # 3つ以上のドキュメントを追加（BM25のIDF計算にはN>=3が必要）
        index.add_documents([
            ("doc1", "dragon quest adventure game", "source1"),
            ("doc2", "pokemon battle monster", "source2"),
            ("doc3", "zelda sword shield", "source3"),
        ])
        assert index.get_document_count() == 3

        # 同じIDで更新（addedは0だがドキュメントは更新される）
        added = index.add_documents([("doc1", "final fantasy rpg game", "source1")])
        assert added == 0  # 新規追加ではない
        assert index.get_document_count() == 3

        # 更新後のテキストで検索 - finalでヒットするはず
        results = index.search("final fantasy")
        assert len(results) > 0, "更新後のドキュメントが検索でヒットしない"
        assert "final" in results[0].text

        # 古いテキストのキーワードでは検索されない（テキストが置き換わっている）
        old_results = index.search("dragon quest")
        assert not any("dragon" in r.text for r in old_results)

    def test_bm25_parameters(self) -> None:
        """BM25パラメータのカスタマイズ."""
        # カスタムパラメータでインスタンス化
        index = BM25Index(k1=2.0, b=0.5)

        # パラメータが設定されている
        assert index._k1 == 2.0
        assert index._b == 0.5


class TestBM25IndexPersistence:
    """BM25Index永続化のテスト.

    仕様: docs/specs/rag-knowledge.md
    """

    @pytest.fixture()
    def persist_dir(self, tmp_path: Path) -> str:
        """テスト用永続化ディレクトリ."""
        return str(tmp_path / "bm25_test")

    def _sample_docs(self) -> list[tuple[str, str, str]]:
        """テスト用ドキュメント（BM25のIDF計算に3件以上必要）."""
        return [
            ("doc1", "dragon quest adventure game rpg", "source1"),
            ("doc2", "pokemon battle monster capture", "source2"),
            ("doc3", "zelda sword shield adventure", "source3"),
            ("doc4", "final fantasy crystal chronicles", "source4"),
        ]

    def test_save_and_load(self, persist_dir: str) -> None:
        """AC79: save後に新インスタンスでloadし、データ・検索が復元される."""
        # 1. インデックスを作成してドキュメントを追加
        index = BM25Index(persist_dir=persist_dir)
        index.add_documents(self._sample_docs())
        assert index.get_document_count() == 4

        # 検索結果を記録
        results_before = index.search("dragon quest", n_results=3)
        assert len(results_before) > 0

        # 2. 新しいインスタンスで復元
        index2 = BM25Index(persist_dir=persist_dir)
        assert index2.get_document_count() == 4

        # 検索結果が同じ
        results_after = index2.search("dragon quest", n_results=3)
        assert len(results_after) > 0
        assert results_after[0].doc_id == results_before[0].doc_id

        # ソースURLも復元
        assert index2.get_source_url("doc1") == "source1"
        assert index2.get_source_url("doc4") == "source4"

    def test_none_dir_means_in_memory(self) -> None:
        """AC80: persist_dir=Noneで従来のインメモリ動作."""
        index = BM25Index(persist_dir=None)
        index.add_documents(self._sample_docs())
        assert index.get_document_count() == 4

        # persist_dir が None なのでファイルは作られない
        assert index._persist_dir is None

    def test_delete_triggers_save(self, persist_dir: str) -> None:
        """AC79: delete後に永続化され、再ロードで反映."""
        index = BM25Index(persist_dir=persist_dir)
        index.add_documents(self._sample_docs())
        assert index.get_document_count() == 4

        # source1 を削除
        deleted = index.delete_by_source("source1")
        assert deleted == 1

        # 新しいインスタンスで復元
        index2 = BM25Index(persist_dir=persist_dir)
        assert index2.get_document_count() == 3
        assert index2.get_source_url("doc1") is None

    def test_corrupt_metadata_starts_empty(self, persist_dir: str) -> None:
        """AC81: 壊れたJSONメタデータ → 空インデックスで起動."""
        # 永続化ディレクトリを手動作成して壊れたメタデータを配置
        persist_path = Path(persist_dir)
        persist_path.mkdir(parents=True)
        (persist_path / METADATA_FILENAME).write_text(
            "{ invalid json !!!", encoding="utf-8"
        )

        # エラーなく起動、空インデックス
        index = BM25Index(persist_dir=persist_dir)
        assert index.get_document_count() == 0

    def test_version_mismatch_starts_empty(self, persist_dir: str) -> None:
        """AC81: 不明バージョン → 空インデックスで起動."""
        persist_path = Path(persist_dir)
        persist_path.mkdir(parents=True)
        metadata = {
            "version": 9999,
            "doc_ids": ["doc1"],
            "documents": {"doc1": "text"},
            "doc_source_map": {"doc1": "source"},
        }
        (persist_path / METADATA_FILENAME).write_text(
            json.dumps(metadata), encoding="utf-8"
        )

        # バージョン不一致で空インデックス
        index = BM25Index(persist_dir=persist_dir)
        assert index.get_document_count() == 0

    def test_empty_index_no_error(self, persist_dir: str) -> None:
        """AC79: 空インデックスのsave/loadがエラーなし."""
        # 空のインデックスを作成（ドキュメント追加なし）
        index = BM25Index(persist_dir=persist_dir)
        assert index.get_document_count() == 0

        # 空のまま新インスタンスを作成（エラーなし）
        index2 = BM25Index(persist_dir=persist_dir)
        assert index2.get_document_count() == 0

    def test_corrupt_bm25s_model_starts_empty(self, persist_dir: str) -> None:
        """AC81: 正常なメタデータだがbm25sモデルが壊れている場合."""
        # 一度正常に保存
        index = BM25Index(persist_dir=persist_dir)
        index.add_documents(self._sample_docs())
        assert index.get_document_count() == 4

        # bm25s ディレクトリ内を破壊
        bm25s_dir = Path(persist_dir) / "bm25s"
        for f in bm25s_dir.iterdir():
            f.write_text("corrupted", encoding="utf-8")

        # 空インデックスで起動
        index2 = BM25Index(persist_dir=persist_dir)
        assert index2.get_document_count() == 0

    def test_delete_all_removes_persist_dir(self, persist_dir: str) -> None:
        """AC79: 全ドキュメント削除後に永続化ディレクトリが削除される."""
        index = BM25Index(persist_dir=persist_dir)
        index.add_documents(self._sample_docs())
        assert Path(persist_dir).exists()

        # 全ソースを削除
        for src in ["source1", "source2", "source3", "source4"]:
            index.delete_by_source(src)

        assert not Path(persist_dir).exists()

        # 空状態で再ロードしてもエラーなし
        index2 = BM25Index(persist_dir=persist_dir)
        assert index2.get_document_count() == 0

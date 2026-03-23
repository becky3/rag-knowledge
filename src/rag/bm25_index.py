"""BM25キーワード検索インデックスモジュール

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import bm25s
    import fugashi

logger = logging.getLogger(__name__)

# 永続化メタデータ
METADATA_FILENAME = "metadata.json"
BM25S_SUBDIR = "bm25s"
METADATA_VERSION = 1

# fugashiのインポートを遅延させる（オプショナル依存）
_fugashi_available: bool | None = None
_tagger: "fugashi.Tagger | None" = None


def _get_fugashi_tagger() -> "fugashi.Tagger | None":
    """fugashiのTaggerをシングルトンで取得する."""
    global _fugashi_available, _tagger

    if _fugashi_available is False:
        return None

    if _tagger is not None:
        return _tagger

    try:
        import fugashi

        _tagger = fugashi.Tagger()
        _fugashi_available = True
        logger.info("fugashi tokenizer initialized successfully")
        return _tagger
    except (ImportError, RuntimeError) as e:
        _fugashi_available = False
        logger.warning("fugashi not available, falling back to simple tokenizer: %s", e)
        return None


@dataclass
class BM25Result:
    """BM25検索結果."""

    doc_id: str
    score: float
    text: str


class BM25Index:
    """BM25ベースのキーワード検索インデックス.

    仕様: docs/specs/rag-knowledge.md
    """

    def __init__(
        self,
        k1: float = 1.5,
        b: float = 0.75,
        persist_dir: str | None = None,
    ) -> None:
        """BM25Indexを初期化する.

        Args:
            k1: 用語頻度の飽和パラメータ（デフォルト: 1.5）
            b: 文書長の正規化パラメータ（デフォルト: 0.75）
            persist_dir: 永続化ディレクトリ（Noneの場合はインメモリのみ）
        """
        self._k1 = k1
        self._b = b
        self._persist_dir = Path(persist_dir) if persist_dir else None

        # ドキュメントストレージ
        self._documents: dict[str, str] = {}  # id -> text
        self._doc_source_map: dict[str, str] = {}  # id -> source_url
        self._doc_source_type_map: dict[str, str] = {}  # id -> source_type
        self._doc_metadata_map: dict[str, dict[str, str | int | float | bool]] = {}  # id -> chunk metadata

        # BM25インデックス（遅延初期化）
        self._bm25: "bm25s.BM25 | None" = None
        self._doc_ids: list[str] = []  # インデックス順序を保持

        # 再構築フラグ
        self._needs_rebuild = True

        # 永続化ディレクトリからロード
        if self._persist_dir is not None:
            self._load()

    def add_documents(
        self,
        documents: list[tuple[str, str, str, str]],
        metadata_list: list[dict[str, str | int | float | bool]] | None = None,
    ) -> int:
        """ドキュメントをインデックスに追加する.

        Args:
            documents: (id, text, source_url, source_type) のリスト
            metadata_list: 各ドキュメントに対応するメタデータ辞書のリスト（任意）

        Returns:
            追加されたドキュメント数
        """
        if metadata_list is not None and len(metadata_list) != len(documents):
            raise ValueError(
                f"add_documents: length mismatch: "
                f"documents={len(documents)}, metadata_list={len(metadata_list)}"
            )

        added = 0
        updated = 0
        for i, (doc_id, text, source_url, source_type) in enumerate(documents):
            if doc_id in self._documents:
                self._documents[doc_id] = text
                self._doc_source_map[doc_id] = source_url
                self._doc_source_type_map[doc_id] = source_type
                updated += 1
            else:
                self._documents[doc_id] = text
                self._doc_source_map[doc_id] = source_url
                self._doc_source_type_map[doc_id] = source_type
                added += 1
            if metadata_list is not None:
                self._doc_metadata_map[doc_id] = metadata_list[i]

        # 新規追加または更新があった場合はインデックス再構築が必要
        if added > 0 or updated > 0:
            self._needs_rebuild = True
            logger.debug(
                "BM25 index: added %d, updated %d documents", added, updated
            )
            self._save()

        return added

    def search(
        self,
        query: str,
        n_results: int = 10,
        source_type: str | None = None,
        filters: dict[str, str] | None = None,
    ) -> list[BM25Result]:
        """クエリでキーワード検索を実行する.

        Args:
            query: 検索クエリ
            n_results: 返却する結果の最大数
            source_type: ソース種別フィルタ（指定時はそのソース種別のみ返す）
            filters: カスタムメタデータフィルタ（完全一致。キーは custom:{key} 形式）

        Returns:
            BM25Resultのリスト（スコア降順）
        """
        if not self._documents:
            return []

        # 必要に応じてインデックスを再構築
        if self._needs_rebuild:
            self._rebuild_index()

        if self._bm25 is None:
            return []

        # クエリをトークナイズ
        query_tokens = tokenize_japanese(query)
        if not query_tokens:
            return []

        # BM25検索（k は corpus サイズ以下に制限）
        # source_type / filters フィルタで除外される可能性を考慮し、3倍（最低20件）を取得
        fetch_count = n_results
        has_filter = source_type is not None or filters
        if has_filter:
            fetch_count = max(n_results * 3, 20)
        k = min(fetch_count, len(self._doc_ids))
        if k == 0:
            return []

        doc_indices, scores = self._bm25.retrieve(
            [query_tokens], k=k, show_progress=False
        )

        # スコア > 0 の結果のみ抽出（source_type + filters フィルタ適用）
        results: list[BM25Result] = []
        for idx, score in zip(doc_indices[0], scores[0]):
            if score <= 0:
                continue
            doc_id = self._doc_ids[int(idx)]
            if source_type is not None:
                if self._doc_source_type_map.get(doc_id) != source_type:
                    continue
            if filters:
                doc_meta = self._doc_metadata_map.get(doc_id, {})
                if not self._matches_filters(doc_meta, filters):
                    continue
            results.append(
                BM25Result(
                    doc_id=doc_id,
                    score=float(score),
                    text=self._documents[doc_id],
                )
            )
            if len(results) >= n_results:
                break

        return results

    @staticmethod
    def _matches_filters(
        metadata: dict[str, str | int | float | bool],
        filters: dict[str, str],
    ) -> bool:
        """メタデータがフィルタ条件に一致するか判定する.

        フィルタ値（常に文字列）とメタデータ値を大文字変換して比較する。
        これにより、メタデータの型（int/bool 等）や大文字小文字の違いを吸収する。
        """
        for key, value in filters.items():
            meta_value = metadata.get(key)
            if meta_value is None:
                return False
            if str(meta_value).upper() != value.upper():
                return False
        return True

    def delete_by_source(self, source_url: str) -> int:
        """ソースURL指定でドキュメントを削除する.

        Args:
            source_url: 削除するソースURL

        Returns:
            削除されたドキュメント数
        """
        to_delete = [
            doc_id
            for doc_id, url in self._doc_source_map.items()
            if url == source_url
        ]

        for doc_id in to_delete:
            del self._documents[doc_id]
            del self._doc_source_map[doc_id]
            self._doc_source_type_map.pop(doc_id, None)  # 旧データに source_type がない場合の互換性
            self._doc_metadata_map.pop(doc_id, None)

        if to_delete:
            self._needs_rebuild = True
            logger.debug(
                "Deleted %d documents from BM25 index (source: %s)",
                len(to_delete),
                source_url,
            )
            self._save()

        return len(to_delete)

    def get_document_count(self) -> int:
        """インデックス内のドキュメント数を返す."""
        return len(self._documents)

    def get_source_url(self, doc_id: str) -> str | None:
        """ドキュメントIDからソースURLを取得する.

        Args:
            doc_id: ドキュメントID

        Returns:
            ソースURL、見つからない場合はNone
        """
        return self._doc_source_map.get(doc_id)

    def get_source_type(self, doc_id: str) -> str | None:
        """ドキュメントIDからソース種別を取得する.

        Args:
            doc_id: ドキュメントID

        Returns:
            ソース種別、見つからない場合はNone
        """
        return self._doc_source_type_map.get(doc_id)

    def delete_stale_docs(self, source_id: str, valid_ids: set[str]) -> int:
        """ソースのドキュメントのうち、valid_ids に含まれないものを削除する.

        Args:
            source_id: ソース識別子
            valid_ids: 保持する ID セット（これ以外を削除）

        Returns:
            削除件数
        """
        stale_ids = [
            doc_id
            for doc_id, src in list(self._doc_source_map.items())
            if src == source_id and doc_id not in valid_ids
        ]
        for doc_id in stale_ids:
            self._documents.pop(doc_id, None)
            self._doc_source_map.pop(doc_id, None)
            self._doc_source_type_map.pop(doc_id, None)
            self._doc_metadata_map.pop(doc_id, None)

        if stale_ids:
            self._needs_rebuild = True
            self._save()

        return len(stale_ids)

    def clear(self) -> None:
        """全データをクリアして永続化する."""
        self._documents.clear()
        self._doc_source_map.clear()
        self._doc_source_type_map.clear()
        self._doc_metadata_map.clear()
        self._doc_ids.clear()
        self._bm25 = None
        self._needs_rebuild = True
        self._save()

    def _rebuild_index(self) -> None:
        """BM25インデックスを再構築する."""
        try:
            import bm25s
        except ImportError:
            logger.warning("bm25s not installed, BM25 search disabled")
            self._bm25 = None
            self._needs_rebuild = False
            return

        self._doc_ids = list(self._documents.keys())
        tokenized_corpus = [
            tokenize_japanese(self._documents[doc_id]) for doc_id in self._doc_ids
        ]

        if tokenized_corpus:
            self._bm25 = bm25s.BM25(k1=self._k1, b=self._b)
            self._bm25.index(tokenized_corpus, show_progress=False)
        else:
            self._bm25 = None

        self._needs_rebuild = False
        logger.debug("Rebuilt BM25 index with %d documents", len(self._doc_ids))

    def _save(self) -> None:
        """インデックスをディスクに永続化する."""
        if self._persist_dir is None:
            return

        if not self._documents:
            # 空インデックスの場合、ディレクトリがあれば削除
            if self._persist_dir.exists():
                shutil.rmtree(self._persist_dir)
                logger.debug("Removed empty BM25 persist dir: %s", self._persist_dir)
            return

        # インデックスが未構築なら構築
        if self._needs_rebuild:
            self._rebuild_index()

        if self._bm25 is None:
            return

        try:
            self._persist_dir.parent.mkdir(parents=True, exist_ok=True)

            # アトミックスワップ用のディレクトリ名を事前定義
            old_dir = self._persist_dir.with_name(
                self._persist_dir.name + "_old"
            )

            # 一時ディレクトリに書き出し（アトミック書き込み）
            tmp_dir = Path(
                tempfile.mkdtemp(
                    dir=self._persist_dir.parent,
                    prefix=f"{self._persist_dir.name}_tmp_",
                )
            )
            try:
                # metadata.json
                metadata = {
                    "version": METADATA_VERSION,
                    "doc_ids": self._doc_ids,
                    "documents": self._documents,
                    "doc_source_map": self._doc_source_map,
                    "doc_source_type_map": self._doc_source_type_map,
                    "doc_metadata_map": self._doc_metadata_map,
                }
                metadata_path = tmp_dir / METADATA_FILENAME
                metadata_path.write_text(
                    json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
                )

                # bm25s ネイティブファイル
                bm25s_dir = tmp_dir / BM25S_SUBDIR
                bm25s_dir.mkdir()
                self._bm25.save(str(bm25s_dir))

                # アトミックスワップ: old → .old, tmp → 本体, .old 削除
                if old_dir.exists():
                    shutil.rmtree(old_dir)

                if self._persist_dir.exists():
                    self._persist_dir.rename(old_dir)

                tmp_dir.rename(self._persist_dir)

                if old_dir.exists():
                    shutil.rmtree(old_dir)

                logger.debug(
                    "BM25 index saved to %s (%d documents)",
                    self._persist_dir,
                    len(self._doc_ids),
                )
            except Exception:
                # リカバリ: .old があれば復元
                if old_dir.exists() and not self._persist_dir.exists():
                    old_dir.rename(self._persist_dir)
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir)
                raise
        except Exception:
            logger.warning("Failed to save BM25 index", exc_info=True)

    def _load(self) -> None:
        """ディスクからインデックスをロードする."""
        if self._persist_dir is None:
            return

        # クラッシュリカバリ: _old が残っていて本体がなければ復元
        old_dir = self._persist_dir.with_name(self._persist_dir.name + "_old")
        if old_dir.exists() and not self._persist_dir.exists():
            old_dir.rename(self._persist_dir)
            logger.warning("Recovered BM25 index from _old directory")

        if not self._persist_dir.exists():
            return

        metadata_path = self._persist_dir / METADATA_FILENAME
        if not metadata_path.exists():
            logger.warning(
                "BM25 metadata not found at %s, starting with empty index",
                metadata_path,
            )
            return

        try:
            raw = metadata_path.read_text(encoding="utf-8")
            metadata = json.loads(raw)

            # バージョンチェック
            version = metadata.get("version")
            if version != METADATA_VERSION:
                logger.warning(
                    "BM25 metadata version mismatch (expected %d, got %s), "
                    "starting with empty index",
                    METADATA_VERSION,
                    version,
                )
                return

            # 必須キー検証
            required_keys = ("doc_ids", "documents", "doc_source_map")
            if not all(k in metadata for k in required_keys):
                logger.warning(
                    "BM25 metadata missing required keys at %s, "
                    "starting with empty index",
                    metadata_path,
                )
                return

            # ドキュメントデータ復元
            self._doc_ids = metadata["doc_ids"]
            self._documents = metadata["documents"]
            self._doc_source_map = metadata["doc_source_map"]
            self._doc_source_type_map = metadata.get("doc_source_type_map", {})
            self._doc_metadata_map = metadata["doc_metadata_map"]

            # bm25s モデル復元
            bm25s_dir = self._persist_dir / BM25S_SUBDIR
            if not bm25s_dir.exists():
                logger.warning(
                    "BM25 model directory not found at %s, starting with empty index",
                    bm25s_dir,
                )
                self._doc_ids = []
                self._documents = {}
                self._doc_source_map = {}
                self._doc_source_type_map = {}
                self._doc_metadata_map = {}
                return

            import bm25s as bm25s_lib

            self._bm25 = bm25s_lib.BM25.load(str(bm25s_dir))
            self._needs_rebuild = False

            logger.info(
                "BM25 index loaded from %s (%d documents)",
                self._persist_dir,
                len(self._doc_ids),
            )
        except Exception:
            logger.warning(
                "Failed to load BM25 index from %s, starting with empty index",
                self._persist_dir,
                exc_info=True,
            )
            self._documents = {}
            self._doc_source_map = {}
            self._doc_source_type_map = {}
            self._doc_metadata_map = {}
            self._doc_ids = []
            self._bm25 = None
            self._needs_rebuild = True


# ストップワード（日本語の一般的な助詞・助動詞など）
JAPANESE_STOPWORDS = frozenset(
    [
        "の",
        "に",
        "は",
        "を",
        "た",
        "が",
        "で",
        "て",
        "と",
        "し",
        "れ",
        "さ",
        "ある",
        "いる",
        "も",
        "する",
        "から",
        "な",
        "こと",
        "として",
        "い",
        "や",
        "れる",
        "など",
        "なっ",
        "ない",
        "この",
        "ため",
        "その",
        "あっ",
        "よう",
        "また",
        "もの",
        "という",
        "あり",
        "まで",
        "られ",
        "なる",
        "へ",
        "か",
        "だ",
        "これ",
        "によって",
        "により",
        "おり",
        "より",
        "による",
        "ず",
        "なり",
        "られる",
        "において",
        "ば",
        "なかっ",
        "なく",
        "しかし",
        "について",
        "せ",
        "だっ",
        "その他",
        "できる",
        "それ",
        "う",
        "ので",
        "なお",
        "のみ",
        "でき",
        "き",
        "つ",
        "における",
        "および",
        "いう",
        "さらに",
        "でも",
        "ら",
        "たり",
        "その後",
        "ほか",
        "ほど",
        "ます",
        "です",
        "ました",
        "でした",
    ]
)

# 名詞・動詞・形容詞の品詞タグ
INCLUDE_POS = frozenset(["名詞", "動詞", "形容詞", "固有名詞"])


@lru_cache(maxsize=10000)
def tokenize_japanese(text: str) -> list[str]:
    """日本語テキストをトークン化する.

    仕様: docs/specs/rag-knowledge.md

    - 形態素解析（fugashi/MeCab）を使用（利用可能な場合）
    - 名詞・動詞・形容詞のみ抽出
    - ストップワード除去

    Args:
        text: トークン化するテキスト

    Returns:
        トークンのリスト
    """
    if not text or not text.strip():
        return []

    text = text.strip().lower()

    # fugashiが利用可能な場合は形態素解析を使用
    tagger = _get_fugashi_tagger()
    if tagger is not None:
        return _tokenize_with_fugashi(text, tagger)

    # フォールバック: 簡易トークナイザ
    return _tokenize_simple(text)


def _tokenize_with_fugashi(
    text: str,
    tagger: "fugashi.Tagger",
) -> list[str]:
    """fugashiを使って形態素解析でトークン化する."""
    tokens: list[str] = []

    for word in tagger(text):
        # 品詞情報を取得
        if not word.feature.pos1:
            continue

        pos = word.feature.pos1
        surface = word.surface

        # 名詞・動詞・形容詞のみ抽出
        if pos not in INCLUDE_POS:
            continue

        # ストップワード除去
        if surface in JAPANESE_STOPWORDS:
            continue

        # 1文字の助詞・記号をスキップ
        if len(surface) == 1 and not surface.isalnum():
            continue

        tokens.append(surface)

    return tokens


def _tokenize_simple(text: str) -> list[str]:
    """簡易トークナイザ（fugashiが使えない場合のフォールバック）."""
    # 記号で分割
    tokens = re.split(r"[\s\.,!?、。！？\n\t]+", text)

    # 空トークンとストップワードを除去
    tokens = [
        t.strip()
        for t in tokens
        if t.strip() and t.strip() not in JAPANESE_STOPWORDS and len(t.strip()) > 1
    ]

    return tokens

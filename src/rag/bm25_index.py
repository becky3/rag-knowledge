"""BM25キーワード検索インデックスモジュール

仕様: docs/specs/infrastructure/bm25-scalability.md
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import sqlite3
import tempfile
from contextlib import redirect_stdout
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import bm25s
    import fugashi

logger = logging.getLogger(__name__)

BM25S_SUBDIR = "bm25s"
STORE_DB_FILENAME = "bm25_store.db"
_REBUILD_MEMORY_WARNING_BYTES = 256 * 1024 * 1024  # 256 MB

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


_STORE_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS chunks (
    doc_id      TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    source_type TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_chunks_source_id ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source_type ON chunks(source_type);

CREATE TABLE IF NOT EXISTS token_cache (
    doc_id    TEXT PRIMARY KEY,
    text_hash TEXT NOT NULL,
    tokens    TEXT NOT NULL
);
"""


def _text_hash(text: str) -> str:
    """テキストの SHA-256 ハッシュ先頭 16 文字を返す."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class BM25Result:
    """BM25検索結果."""

    doc_id: str
    score: float
    text: str


class _BM25Store:
    """BM25 チャンクデータの SQLite ストア.

    persist_dir が None の場合は :memory: で動作する。
    """

    def __init__(self, persist_dir: Path | None) -> None:
        self._persist_dir = persist_dir
        if persist_dir is not None:
            persist_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(persist_dir / STORE_DB_FILENAME)
        else:
            db_path = ":memory:"

        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_STORE_SCHEMA_SQL)
        except sqlite3.DatabaseError:
            if conn is not None:
                conn.close()
            if persist_dir is not None:
                corrupt_path = persist_dir / STORE_DB_FILENAME
                if corrupt_path.exists():
                    corrupt_path.unlink()
                    logger.warning(
                        "Corrupt BM25 store DB removed: %s", corrupt_path,
                    )
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_STORE_SCHEMA_SQL)
        self._conn = conn

    def close(self) -> None:
        self._conn.close()

    def upsert_chunks(
        self,
        documents: list[tuple[str, str, str, str]],
        metadata_list: list[dict[str, str | int | float | bool]] | None,
    ) -> tuple[int, int]:
        """チャンクを INSERT OR REPLACE する.

        Returns:
            (added, updated) のタプル
        """
        existing: set[str] = set()
        doc_ids = [d[0] for d in documents]
        for batch_start in range(0, len(doc_ids), 900):
            batch = doc_ids[batch_start:batch_start + 900]
            placeholders = ",".join("?" * len(batch))
            rows = self._conn.execute(
                f"SELECT doc_id FROM chunks WHERE doc_id IN ({placeholders})",
                batch,
            ).fetchall()
            existing.update(r[0] for r in rows)

        added = 0
        updated = 0
        params = []
        for i, (doc_id, text, source_id, source_type) in enumerate(documents):
            meta_json = json.dumps(
                metadata_list[i], ensure_ascii=False,
            ) if metadata_list is not None else "{}"
            params.append((doc_id, text, source_id, source_type, meta_json))
            if doc_id in existing:
                updated += 1
            else:
                added += 1

        self._conn.executemany(
            "INSERT OR REPLACE INTO chunks (doc_id, text, source_id, source_type, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            params,
        )
        self._conn.commit()
        return added, updated

    def delete_by_source(self, source_id: str) -> list[str]:
        """source_id で削除し、削除された doc_id リストを返す."""
        rows = self._conn.execute(
            "SELECT doc_id FROM chunks WHERE source_id = ?", (source_id,),
        ).fetchall()
        deleted_ids = [r[0] for r in rows]
        if deleted_ids:
            self._conn.execute(
                "DELETE FROM chunks WHERE source_id = ?", (source_id,),
            )
            self._delete_token_cache(deleted_ids)
            self._conn.commit()
        return deleted_ids

    def delete_by_source_type(self, source_type: str) -> list[str]:
        """source_type で削除し、削除された doc_id リストを返す."""
        rows = self._conn.execute(
            "SELECT doc_id FROM chunks WHERE source_type = ?", (source_type,),
        ).fetchall()
        deleted_ids = [r[0] for r in rows]
        if deleted_ids:
            self._conn.execute(
                "DELETE FROM chunks WHERE source_type = ?", (source_type,),
            )
            self._delete_token_cache(deleted_ids)
            self._conn.commit()
        return deleted_ids

    def delete_stale(self, source_id: str, valid_ids: set[str]) -> list[str]:
        """source_id のチャンクのうち valid_ids に含まれないものを削除する."""
        rows = self._conn.execute(
            "SELECT doc_id FROM chunks WHERE source_id = ?", (source_id,),
        ).fetchall()
        stale_ids = [r[0] for r in rows if r[0] not in valid_ids]
        if stale_ids:
            for batch_start in range(0, len(stale_ids), 900):
                batch = stale_ids[batch_start:batch_start + 900]
                placeholders = ",".join("?" * len(batch))
                self._conn.execute(
                    f"DELETE FROM chunks WHERE doc_id IN ({placeholders})", batch,
                )
            self._delete_token_cache(stale_ids)
            self._conn.commit()
        return stale_ids

    def get_document_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        return row[0] if row else 0

    def get_source_url(self, doc_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT source_id FROM chunks WHERE doc_id = ?", (doc_id,),
        ).fetchone()
        return row[0] if row else None

    def get_source_type(self, doc_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT source_type FROM chunks WHERE doc_id = ?", (doc_id,),
        ).fetchone()
        return row[0] if row else None

    def get_metadata(self, doc_id: str) -> dict[str, str | int | float | bool]:
        row = self._conn.execute(
            "SELECT metadata FROM chunks WHERE doc_id = ?", (doc_id,),
        ).fetchone()
        if row is None:
            return {}
        return json.loads(row[0])  # type: ignore[no-any-return]

    def get_texts_by_ids(self, doc_ids: list[str]) -> dict[str, str]:
        """doc_id リストに対応するテキストを取得する."""
        result: dict[str, str] = {}
        for batch_start in range(0, len(doc_ids), 900):
            batch = doc_ids[batch_start:batch_start + 900]
            placeholders = ",".join("?" * len(batch))
            rows = self._conn.execute(
                f"SELECT doc_id, text FROM chunks WHERE doc_id IN ({placeholders})",
                batch,
            ).fetchall()
            for r in rows:
                result[r[0]] = r[1]
        return result

    def get_source_types_by_ids(self, doc_ids: list[str]) -> dict[str, str]:
        """doc_id リストに対応する source_type を取得する."""
        result: dict[str, str] = {}
        for batch_start in range(0, len(doc_ids), 900):
            batch = doc_ids[batch_start:batch_start + 900]
            placeholders = ",".join("?" * len(batch))
            rows = self._conn.execute(
                f"SELECT doc_id, source_type FROM chunks WHERE doc_id IN ({placeholders})",
                batch,
            ).fetchall()
            for r in rows:
                result[r[0]] = r[1]
        return result

    def get_metadatas_by_ids(
        self, doc_ids: list[str],
    ) -> dict[str, dict[str, str | int | float | bool]]:
        """doc_id リストに対応するメタデータを取得する."""
        result: dict[str, dict[str, str | int | float | bool]] = {}
        for batch_start in range(0, len(doc_ids), 900):
            batch = doc_ids[batch_start:batch_start + 900]
            placeholders = ",".join("?" * len(batch))
            rows = self._conn.execute(
                f"SELECT doc_id, metadata FROM chunks WHERE doc_id IN ({placeholders})",
                batch,
            ).fetchall()
            for r in rows:
                result[r[0]] = json.loads(r[1])
        return result

    def estimate_text_memory_bytes(self) -> tuple[int, int]:
        """チャンク数とテキスト合計バイト数を返す（rebuild 前の見積もり用）."""
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(text)), 0) FROM chunks",
        ).fetchone()
        return (row[0], row[1]) if row else (0, 0)

    def get_all_for_rebuild(self) -> list[tuple[str, str]]:
        """rebuild 用に全 (doc_id, text) を doc_id 順で返す."""
        return self._conn.execute(
            "SELECT doc_id, text FROM chunks ORDER BY doc_id",
        ).fetchall()

    def has_documents(self) -> bool:
        row = self._conn.execute("SELECT 1 FROM chunks LIMIT 1").fetchone()
        return row is not None

    def clear(self) -> None:
        self._conn.execute("DELETE FROM chunks")
        self._conn.execute("DELETE FROM token_cache")
        self._conn.commit()

    # --- token cache ---

    def get_cached_tokens(
        self, doc_ids_with_hashes: list[tuple[str, str]],
    ) -> dict[str, list[str]]:
        """キャッシュヒットしたトークンを返す."""
        result: dict[str, list[str]] = {}
        for batch_start in range(0, len(doc_ids_with_hashes), 900):
            batch = doc_ids_with_hashes[batch_start:batch_start + 900]
            ids = [b[0] for b in batch]
            placeholders = ",".join("?" * len(ids))
            rows = self._conn.execute(
                f"SELECT doc_id, text_hash, tokens FROM token_cache "
                f"WHERE doc_id IN ({placeholders})",
                ids,
            ).fetchall()
            hash_map = {b[0]: b[1] for b in batch}
            for doc_id, cached_hash, tokens_json in rows:
                if cached_hash == hash_map.get(doc_id):
                    result[doc_id] = json.loads(tokens_json)
        return result

    def upsert_token_cache(
        self, entries: list[tuple[str, str, list[str]]],
    ) -> None:
        """トークンキャッシュを一括更新する."""
        if not entries:
            return
        params = [
            (doc_id, text_hash, json.dumps(tokens, ensure_ascii=False))
            for doc_id, text_hash, tokens in entries
        ]
        self._conn.executemany(
            "INSERT OR REPLACE INTO token_cache (doc_id, text_hash, tokens) "
            "VALUES (?, ?, ?)",
            params,
        )
        self._conn.commit()

    def _delete_token_cache(self, doc_ids: list[str]) -> None:
        for batch_start in range(0, len(doc_ids), 900):
            batch = doc_ids[batch_start:batch_start + 900]
            placeholders = ",".join("?" * len(batch))
            self._conn.execute(
                f"DELETE FROM token_cache WHERE doc_id IN ({placeholders})", batch,
            )


class BM25Index:
    """BM25ベースのキーワード検索インデックス.

    仕様: docs/specs/infrastructure/bm25-scalability.md
    """

    def __init__(
        self,
        k1: float,
        b: float,
        persist_dir: str | None,
    ) -> None:
        """BM25Indexを初期化する.

        Args:
            k1: 用語頻度の飽和パラメータ
            b: 文書長の正規化パラメータ
            persist_dir: 永続化ディレクトリ（Noneの場合はインメモリのみ）
        """
        self._k1 = k1
        self._b = b
        self._persist_dir = Path(persist_dir) if persist_dir else None

        self._store = _BM25Store(self._persist_dir)

        # BM25インデックス（遅延初期化）
        self._bm25: "bm25s.BM25 | None" = None
        self._doc_ids: list[str] = []

        # 再構築フラグ
        self._needs_rebuild = True

        # 遅延 save モード
        self._deferred_save = False

        # 永続化ディレクトリからbm25sモデルをロード
        if self._persist_dir is not None:
            self._load_bm25s()

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

        added, updated = self._store.upsert_chunks(documents, metadata_list)

        if added > 0 or updated > 0:
            self._needs_rebuild = True
            logger.debug(
                "BM25 index: added %d, updated %d documents", added, updated,
            )
            if not self._deferred_save:
                self._save_bm25s()

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
        if not self._store.has_documents():
            return []

        if self._needs_rebuild:
            self._rebuild_index()

        if self._bm25 is None:
            return []

        query_tokens = tokenize_japanese(query)
        if not query_tokens:
            return []

        fetch_count = n_results
        has_filter = source_type is not None or filters
        if has_filter:
            fetch_count = max(n_results * 3, 20)
        k = min(fetch_count, len(self._doc_ids))
        if k == 0:
            return []

        doc_indices, scores = self._bm25.retrieve(
            [query_tokens], k=k, show_progress=False,
        )

        hit_doc_ids: list[tuple[str, float]] = []
        for idx, score in zip(doc_indices[0], scores[0]):
            if score <= 0:
                continue
            hit_doc_ids.append((self._doc_ids[int(idx)], float(score)))

        if not hit_doc_ids:
            return []

        all_hit_ids = [d[0] for d in hit_doc_ids]
        texts = self._store.get_texts_by_ids(all_hit_ids)

        if source_type is not None:
            source_types = self._store.get_source_types_by_ids(all_hit_ids)
        else:
            source_types = {}

        if filters:
            metadatas = self._store.get_metadatas_by_ids(all_hit_ids)
        else:
            metadatas = {}

        results: list[BM25Result] = []
        for doc_id, score in hit_doc_ids:
            if source_type is not None:
                if source_types.get(doc_id) != source_type:
                    continue
            if filters:
                doc_meta = metadatas.get(doc_id, {})
                if not self._matches_filters(doc_meta, filters):
                    continue
            text = texts.get(doc_id, "")
            results.append(BM25Result(doc_id=doc_id, score=score, text=text))
            if len(results) >= n_results:
                break

        return results

    @staticmethod
    def _matches_filters(
        metadata: dict[str, str | int | float | bool],
        filters: dict[str, str],
    ) -> bool:
        """メタデータがフィルタ条件に一致するか判定する."""
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
        deleted_ids = self._store.delete_by_source(source_url)

        if deleted_ids:
            self._needs_rebuild = True
            logger.debug(
                "Deleted %d documents from BM25 index (source: %s)",
                len(deleted_ids), source_url,
            )
            if not self._deferred_save:
                self._save_bm25s()

        return len(deleted_ids)

    def delete_by_source_type(self, source_type: str) -> int:
        """source_type 指定でドキュメントを一括削除する.

        Args:
            source_type: 削除対象の source_type

        Returns:
            削除されたドキュメント数
        """
        deleted_ids = self._store.delete_by_source_type(source_type)

        if deleted_ids:
            self._needs_rebuild = True
            logger.debug(
                "Deleted %d documents from BM25 index (source_type: %s)",
                len(deleted_ids), source_type,
            )
            if not self._deferred_save:
                self._save_bm25s()

        return len(deleted_ids)

    def close(self) -> None:
        """SQLite コネクションをクローズする."""
        self._store.close()

    def get_document_count(self) -> int:
        """インデックス内のドキュメント数を返す."""
        return self._store.get_document_count()

    def get_source_url(self, doc_id: str) -> str | None:
        """ドキュメントIDからソースURLを取得する."""
        return self._store.get_source_url(doc_id)

    def get_source_type(self, doc_id: str) -> str | None:
        """ドキュメントIDからソース種別を取得する."""
        return self._store.get_source_type(doc_id)

    def get_metadata(self, doc_id: str) -> dict[str, str | int | float | bool]:
        """ドキュメントIDからチャンクメタデータを取得する."""
        return self._store.get_metadata(doc_id)

    def delete_stale_docs(self, source_id: str, valid_ids: set[str]) -> int:
        """ソースのドキュメントのうち、valid_ids に含まれないものを削除する."""
        stale_ids = self._store.delete_stale(source_id, valid_ids)

        if stale_ids:
            self._needs_rebuild = True
            if not self._deferred_save:
                self._save_bm25s()

        return len(stale_ids)

    def clear(self) -> None:
        """全データをクリアして永続化する."""
        self._store.clear()
        self._doc_ids.clear()
        self._bm25 = None
        self._needs_rebuild = True
        self._save_bm25s()

    def set_deferred_save(self, enabled: bool) -> None:
        """遅延 save モードの有効/無効を切り替える."""
        self._deferred_save = enabled

    def flush(self) -> None:
        """未保存の変更を rebuild + 永続化する."""
        if not self._needs_rebuild:
            return
        self._rebuild_index()
        self._persist_bm25s()

    def _rebuild_index(self) -> None:
        """BM25インデックスを再構築する（トークンキャッシュ使用）."""
        try:
            with redirect_stdout(io.StringIO()):
                import bm25s
        except ImportError:
            logger.warning("bm25s not installed, BM25 search disabled")
            self._bm25 = None
            self._needs_rebuild = False
            return

        chunk_count, text_bytes = self._store.estimate_text_memory_bytes()
        if text_bytes > _REBUILD_MEMORY_WARNING_BYTES:
            logger.warning(
                "BM25 rebuild: text data is large (%d chunks, %d MB). "
                "Peak memory usage will increase during rebuild.",
                chunk_count, text_bytes // (1024 * 1024),
            )

        all_docs = self._store.get_all_for_rebuild()
        if not all_docs:
            self._bm25 = None
            self._doc_ids = []
            self._needs_rebuild = False
            return

        self._doc_ids = [doc_id for doc_id, _ in all_docs]
        doc_hashes = [(doc_id, _text_hash(text)) for doc_id, text in all_docs]
        text_map = {doc_id: text for doc_id, text in all_docs}

        cached = self._store.get_cached_tokens(doc_hashes)

        new_cache_entries: list[tuple[str, str, list[str]]] = []
        tokenized_corpus: list[list[str]] = []
        cache_hits = 0

        for doc_id, text_h in doc_hashes:
            if doc_id in cached:
                tokenized_corpus.append(cached[doc_id])
                cache_hits += 1
            else:
                tokens = tokenize_japanese(text_map[doc_id])
                tokenized_corpus.append(tokens)
                new_cache_entries.append((doc_id, text_h, tokens))

        if new_cache_entries:
            self._store.upsert_token_cache(new_cache_entries)

        self._bm25 = bm25s.BM25(k1=self._k1, b=self._b)
        self._bm25.index(tokenized_corpus, show_progress=False)
        self._needs_rebuild = False

        logger.debug(
            "Rebuilt BM25 index with %d documents (cache hits: %d, misses: %d)",
            len(self._doc_ids), cache_hits, len(new_cache_entries),
        )

    def _save_bm25s(self) -> None:
        """rebuild + bm25s 永続化を実行する."""
        if not self._store.has_documents():
            if self._persist_dir is not None and self._persist_dir.exists():
                bm25s_dir = self._persist_dir / BM25S_SUBDIR
                if bm25s_dir.exists():
                    shutil.rmtree(bm25s_dir)
                logger.debug("Removed BM25S subdir (empty index): %s", self._persist_dir)
            self._bm25 = None
            self._doc_ids = []
            self._needs_rebuild = False
            return

        if self._needs_rebuild:
            self._rebuild_index()

        self._persist_bm25s()

    def _persist_bm25s(self) -> None:
        """bm25s モデルをディスクに保存する（アトミックスワップ）."""
        if self._persist_dir is None or self._bm25 is None:
            return

        try:
            bm25s_dir = self._persist_dir / BM25S_SUBDIR
            old_dir = self._persist_dir / (BM25S_SUBDIR + "_old")

            tmp_dir = Path(
                tempfile.mkdtemp(
                    dir=self._persist_dir,
                    prefix=f"{BM25S_SUBDIR}_tmp_",
                ),
            )
            try:
                self._bm25.save(str(tmp_dir))

                if old_dir.exists():
                    shutil.rmtree(old_dir)
                if bm25s_dir.exists():
                    bm25s_dir.rename(old_dir)
                tmp_dir.rename(bm25s_dir)
                if old_dir.exists():
                    shutil.rmtree(old_dir)

                logger.debug(
                    "BM25 model saved to %s (%d documents)",
                    bm25s_dir, len(self._doc_ids),
                )
            except Exception:
                if old_dir.exists() and not bm25s_dir.exists():
                    old_dir.rename(bm25s_dir)
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir)
                raise
        except Exception:
            logger.warning("Failed to save BM25 model", exc_info=True)

    def _load_bm25s(self) -> None:
        """bm25s モデルをディスクからロードする."""
        if self._persist_dir is None:
            return

        bm25s_dir = self._persist_dir / BM25S_SUBDIR

        # クラッシュリカバリ
        old_dir = self._persist_dir / (BM25S_SUBDIR + "_old")
        if old_dir.exists() and not bm25s_dir.exists():
            old_dir.rename(bm25s_dir)
            logger.warning("Recovered BM25 model from _old directory")

        if not bm25s_dir.exists():
            return

        if not self._store.has_documents():
            return

        try:
            with redirect_stdout(io.StringIO()):
                import bm25s as bm25s_lib

            self._bm25 = bm25s_lib.BM25.load(str(bm25s_dir))

            all_docs = self._store.get_all_for_rebuild()
            self._doc_ids = [doc_id for doc_id, _ in all_docs]
            self._needs_rebuild = False

            logger.info(
                "BM25 index loaded from %s (%d documents)",
                self._persist_dir, len(self._doc_ids),
            )
        except Exception:
            logger.warning(
                "Failed to load BM25 model from %s, will rebuild on next search",
                bm25s_dir, exc_info=True,
            )
            self._bm25 = None
            self._doc_ids = []
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

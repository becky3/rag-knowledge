"""青空文庫インジェスター.

仕様: docs/specs/ingesters/aozora.md

青空文庫の作品カタログ CSV を管理し、
作品 XHTML を GitHub Raw URL 経由で取得して source_store に配置する。
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import httpx

from rag.pipeline.ingesters._common import (
    IngestResult,
    ProgressCallback,
    fetch_get,
    now_iso,
)

if TYPE_CHECKING:

    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)

# ハードリミット: 作品取得上限
MAX_WORKS_HARD_LIMIT = 500

# カタログ CSV ZIP の URL
CATALOG_ZIP_URL = (
    "https://www.aozora.gr.jp/index_pages/list_person_all_extended_utf8.zip"
)

# GitHub Raw URL のベース
GITHUB_RAW_BASE = (
    "https://raw.githubusercontent.com/aozorabunko/aozorabunko/master"
)

# カタログの source_store 内の相対パス
CATALOG_REL_PATH = "aozora/catalog.csv"

# 検索結果の最大表示件数（ハードリミット）
MAX_SEARCH_LIMIT = 2000

# CSV カラム名（list_person_all_extended_utf8.csv の主要カラム）
COL_BOOK_ID = "作品ID"
COL_TITLE = "作品名"
COL_PERSON_ID = "人物ID"
COL_LAST_NAME = "姓"
COL_FIRST_NAME = "名"
COL_LAST_NAME_KANA = "姓読み"
COL_FIRST_NAME_KANA = "名読み"
COL_COPYRIGHT = "作品著作権フラグ"
COL_XHTML_URL = "XHTML/HTMLファイルURL"


class AozoraIngester:
    """青空文庫インジェスター.

    青空文庫の作品カタログを管理し、
    作品 XHTML を取得して source_store に配置する。
    """

    def __init__(
        self,
        source_store: SourceStore,
        *,
        max_works: int,
    ) -> None:
        self._store = source_store
        self._max_works = max_works

    # ------------------------------------------------------------------
    # カタログ更新
    # ------------------------------------------------------------------

    async def update_catalog(
        self,
        *,
        client: Any | None = None,
    ) -> str:
        """カタログ CSV をダウンロードし source_store に配置する.

        Args:
            client: ConstrainedClient インスタンス

        Returns:
            更新結果のサマリーテキスト
        """
        if client is None:
            raise ValueError("client (ConstrainedClient) が必要です")

        # CSV ZIP ダウンロード
        try:
            resp = await fetch_get(client, CATALOG_ZIP_URL)
        except httpx.HTTPStatusError as e:
            raise ValueError(
                f"カタログ ZIP のダウンロードに失敗しました（status={e.response.status_code}）"
            ) from e
        zip_bytes = resp.content

        # ZIP 展開 + CSV 読み取り
        csv_text = self._extract_csv_from_zip(zip_bytes)

        # 前回カタログとの差分検出
        prev_records = self._load_catalog()
        new_records = self._parse_csv(csv_text)

        new_count = 0
        updated_count = 0
        if prev_records is not None:
            prev_ids = {r[COL_BOOK_ID] for r in prev_records}
            new_ids = {r[COL_BOOK_ID] for r in new_records}
            new_count = len(new_ids - prev_ids)
            # 更新検出: 同一作品IDで内容が異なるものをカウント
            prev_by_id = {r[COL_BOOK_ID]: r for r in prev_records}
            for record in new_records:
                book_id = record[COL_BOOK_ID]
                if book_id in prev_by_id and record != prev_by_id[book_id]:
                    updated_count += 1

        # source_store に配置
        csv_bytes = csv_text.encode("utf-8")
        metadata = {
            "source_type": "aozora",
            "title": "Aozora Bunko Catalog",
            "collected_at": now_iso(),
        }
        self._store.place_file(
            source_type="aozora",
            data=csv_bytes,
            rel_path=CATALOG_REL_PATH,
            metadata=metadata,
        )

        total = len(new_records)
        if prev_records is None:
            return (
                f"カタログを初回ダウンロードしました: {total}作品"
            )
        return (
            f"カタログを更新しました: 総作品数{total} / "
            f"新着{new_count}件 / 更新{updated_count}件"
        )

    # ------------------------------------------------------------------
    # 作品検索
    # ------------------------------------------------------------------

    def search(
        self,
        *,
        author: str | None = None,
        title: str | None = None,
        limit: int,
    ) -> list[dict[str, str]]:
        """ローカルカタログから著者名・作品名で検索する.

        Args:
            author: 著者名（部分一致）
            title: 作品タイトル（部分一致）
            limit: 最大表示件数

        Returns:
            検索結果のリスト

        Raises:
            ValueError: カタログ未ダウンロード、または検索条件が空
        """
        if not author and not title:
            raise ValueError(
                "author または title のいずれかを指定してください"
            )

        # limit のバリデーション
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise TypeError(
                f"limit は整数で指定してください（受け取った値: {limit!r}）"
            )
        if limit <= 0:
            raise ValueError(
                f"limit は 1 以上で指定してください（受け取った値: {limit}）"
            )
        if limit > MAX_SEARCH_LIMIT:
            limit = MAX_SEARCH_LIMIT

        records = self._load_catalog()
        if records is None:
            raise ValueError(
                "カタログが未ダウンロードです。"
                "先に rag_update_aozora_catalog でカタログを更新してください"
            )

        results: list[dict[str, str]] = []
        for record in records:
            author_name = self._get_author_name(record)
            title_name = record.get(COL_TITLE, "")

            if author and author not in author_name:
                continue
            if title and title not in title_name:
                continue

            copyright_flag = record.get(COL_COPYRIGHT, "")
            results.append({
                "book_id": record.get(COL_BOOK_ID, ""),
                "title": title_name,
                "person_id": record.get(COL_PERSON_ID, ""),
                "author": author_name,
                "copyright": "フリー" if copyright_flag == "なし" else "あり",
            })

        results.sort(key=lambda r: r["book_id"])
        return results[:limit]

    # ------------------------------------------------------------------
    # 単一作品取り込み
    # ------------------------------------------------------------------

    async def add_work(
        self,
        book_id: str,
        *,
        client: Any | None = None,
    ) -> IngestResult:
        """指定作品を取得し source_store に配置する.

        Args:
            book_id: 青空文庫の作品 ID
            client: ConstrainedClient インスタンス

        Returns:
            配置結果
        """
        if client is None:
            raise ValueError("client (ConstrainedClient) が必要です")
        if not book_id or not book_id.strip():
            raise ValueError("book_id が空です")

        records = self._load_catalog()
        if records is None:
            raise ValueError(
                "カタログが未ダウンロードです。"
                "先に rag_update_aozora_catalog でカタログを更新してください"
            )

        # 作品を検索
        record = self._find_record_by_book_id(records, book_id)
        if record is None:
            raise ValueError(
                f"作品 ID '{book_id}' がカタログに見つかりません"
            )

        # 著作権チェック
        copyright_flag = record.get(COL_COPYRIGHT, "")
        if copyright_flag != "なし":
            raise ValueError(
                f"作品 ID '{book_id}' は著作権ありのため取り込みできません"
            )

        result = IngestResult()
        await self._ingest_work(record, client, result)
        return result

    # ------------------------------------------------------------------
    # 著者一括取り込み
    # ------------------------------------------------------------------

    async def crawl_author(
        self,
        person_id: str,
        *,
        max_works: int | None = None,
        client: Any | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> IngestResult:
        """指定著者の著作権フリー作品を一括取り込みする.

        Args:
            person_id: 著者の人物 ID
            max_works: 最大取り込み数
            client: ConstrainedClient インスタンス
            progress_callback: 進捗コールバック (processed, total, current)

        Returns:
            配置結果
        """
        logger.info(
            "Aozora author crawl started: person_id=%s, max_works=%s",
            person_id,
            max_works if max_works is not None else self._max_works,
        )

        if client is None:
            raise ValueError("client (ConstrainedClient) が必要です")
        if not person_id or not person_id.strip():
            raise ValueError("person_id が空です")

        effective_max = self._validate_max_works(
            max_works if max_works is not None else self._max_works
        )

        records = self._load_catalog()
        if records is None:
            raise ValueError(
                "カタログが未ダウンロードです。"
                "先に rag_update_aozora_catalog でカタログを更新してください"
            )

        # 人物 ID で検索 + 著作権フリーフィルタ
        targets: list[dict[str, str]] = []
        for record in records:
            if record.get(COL_PERSON_ID, "") != person_id:
                continue
            if record.get(COL_COPYRIGHT, "") != "なし":
                continue
            xhtml_url = record.get(COL_XHTML_URL, "")
            if not xhtml_url:
                continue
            targets.append(record)
            if len(targets) >= effective_max:
                break

        logger.info("Author %s: %d target works", person_id, len(targets))

        if not targets:
            return IngestResult()

        result = IngestResult()
        consecutive_failures = 0

        for work_idx, record in enumerate(targets):
            book_id = record.get(COL_BOOK_ID, "?")
            try:
                placed = await self._ingest_work(record, client, result)
                if placed:
                    consecutive_failures = 0
                else:
                    # スキップ（重複）の場合はリセット
                    consecutive_failures = 0
            except Exception:
                logger.exception("作品の取得・配置に失敗しました: %s", book_id)
                result.errors += 1
                result.error_details.append(f"book_id={book_id}")
                consecutive_failures += 1

                if consecutive_failures >= 5:
                    logger.warning(
                        "5回連続失敗のためサーキットブレーカー発動。"
                        "操作を中断します"
                    )
                    break

            if progress_callback is not None:
                progress_callback(work_idx + 1, len(targets), f"book_id={book_id}")

        logger.info(
            "Aozora author crawl completed: placed=%d, skipped=%d, errors=%d",
            result.placed, result.skipped, result.errors,
        )
        return result

    # ------------------------------------------------------------------
    # 内部メソッド
    # ------------------------------------------------------------------

    async def _ingest_work(
        self,
        record: dict[str, str],
        client: Any,
        result: IngestResult,
    ) -> bool:
        """1作品を取得し配置する.

        Returns:
            True if placed, False if skipped.
        """
        book_id = record.get(COL_BOOK_ID, "")
        person_id = record.get(COL_PERSON_ID, "")
        xhtml_url = record.get(COL_XHTML_URL, "")

        if not xhtml_url:
            logger.warning("XHTML URL が欠落: book_id=%s", book_id)
            result.errors += 1
            result.error_details.append(f"XHTML URL 欠落: book_id={book_id}")
            return False

        # ファイルパス導出
        rel_path = f"aozora/{person_id}/{book_id}.html"

        # 重複チェック（スキップ方式）
        full_path = self._store.root_dir / rel_path
        if full_path.exists():
            result.skipped += 1
            return False

        # GitHub Raw URL に変換
        github_url = self._to_github_raw_url(xhtml_url)

        # ダウンロード（生データをそのまま保存、エンコーディング変換はコンバーターの責務）
        try:
            resp = await fetch_get(client, github_url)
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            logger.error(
                "XHTML ダウンロード失敗: book_id=%s status=%s url=%s",
                book_id, status_code, github_url,
            )
            result.errors += 1
            result.error_details.append(
                f"XHTML ダウンロード失敗: book_id={book_id}, status={status_code}"
            )
            return False
        raw_bytes: bytes = resp.content

        # 元 URL の正規化（http → https）
        url = xhtml_url
        if url.startswith("http://"):
            url = url.replace("http://", "https://", 1)

        # .meta 生成
        metadata: dict[str, Any] = {
            "url": url,
            "source_type": "aozora",
            "title": record.get(COL_TITLE, ""),
            "collected_at": now_iso(),
            "book_id": book_id,
            "person_id": person_id,
            "author": self._get_author_name(record),
            "author_kana": self._get_author_kana(record),
            "copyright_expired": True,
        }

        # source_store に配置
        self._store.place_file(
            source_type="aozora",
            data=raw_bytes,
            rel_path=rel_path,
            metadata=metadata,
        )
        result.placed += 1
        return True

    def _load_catalog(self) -> list[dict[str, str]] | None:
        """source_store からカタログ CSV を読み込む.

        Returns:
            CSV レコードのリスト、またはカタログ未ダウンロードの場合 None
        """
        catalog_path = self._store.root_dir / CATALOG_REL_PATH
        if not catalog_path.exists():
            return None

        text = catalog_path.read_text(encoding="utf-8")
        return self._parse_csv(text)

    @staticmethod
    def _parse_csv(text: str) -> list[dict[str, str]]:
        """CSV テキストをパースする."""
        reader = csv.DictReader(io.StringIO(text))
        return list(reader)

    @staticmethod
    def _extract_csv_from_zip(zip_bytes: bytes) -> str:
        """ZIP バイナリから CSV テキストを抽出する."""
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            # ZIP 内の最初の CSV ファイルを取得
            csv_names = [n for n in zf.namelist() if n.endswith(".csv")]
            if not csv_names:
                raise ValueError("ZIP 内に CSV ファイルが見つかりません")
            raw = zf.read(csv_names[0])
            # UTF-8 BOM 付き CSV（list_person_all_extended_utf8.zip）
            return raw.decode("utf-8-sig", errors="replace")

    @staticmethod
    def _find_record_by_book_id(
        records: list[dict[str, str]],
        book_id: str,
    ) -> dict[str, str] | None:
        """作品 ID でレコードを検索する."""
        for record in records:
            if record.get(COL_BOOK_ID, "") == book_id:
                return record
        return None

    @staticmethod
    def _get_author_name(record: dict[str, str]) -> str:
        """レコードから著者名を構成する."""
        last = record.get(COL_LAST_NAME, "")
        first = record.get(COL_FIRST_NAME, "")
        if last and first:
            return f"{last} {first}"
        return last or first

    @staticmethod
    def _get_author_kana(record: dict[str, str]) -> str:
        """レコードから著者名カナを構成する."""
        last = record.get(COL_LAST_NAME_KANA, "")
        first = record.get(COL_FIRST_NAME_KANA, "")
        if last and first:
            return f"{last} {first}"
        return last or first

    @staticmethod
    def _to_github_raw_url(aozora_url: str) -> str:
        """青空文庫 URL を GitHub Raw URL に変換する.

        例:
            https://www.aozora.gr.jp/cards/000035/files/1567_14913.html
            → https://raw.githubusercontent.com/aozorabunko/aozorabunko/master/cards/000035/files/1567_14913.html
        """
        # URL から /cards/ 以降を抽出
        for prefix in (
            "https://www.aozora.gr.jp/",
            "http://www.aozora.gr.jp/",
        ):
            if aozora_url.startswith(prefix):
                path = aozora_url[len(prefix):]
                return f"{GITHUB_RAW_BASE}/{path}"

        # フォールバック: URL のパス部分を使用
        parsed = PurePosixPath(aozora_url)
        # cards/ を含むパスを探す
        parts = parsed.parts
        for i, part in enumerate(parts):
            if part == "cards":
                rel = "/".join(parts[i:])
                return f"{GITHUB_RAW_BASE}/{rel}"

        raise ValueError(
            f"GitHub Raw URL に変換できません: {aozora_url}"
        )

    def _validate_max_works(self, max_works: object) -> int:
        """max_works のバリデーション."""
        if isinstance(max_works, bool) or not isinstance(max_works, int):
            raise TypeError(
                f"max_works は整数で指定してください（受け取った値: {max_works!r}）"
            )
        if max_works <= 0:
            raise ValueError(
                f"max_works は 1 以上で指定してください（受け取った値: {max_works}）"
            )
        if max_works > MAX_WORKS_HARD_LIMIT:
            logger.warning(
                "max_works が上限 %d を超えています（%d）。%d にクランプします",
                MAX_WORKS_HARD_LIMIT,
                max_works,
                MAX_WORKS_HARD_LIMIT,
            )
            return MAX_WORKS_HARD_LIMIT
        return max_works

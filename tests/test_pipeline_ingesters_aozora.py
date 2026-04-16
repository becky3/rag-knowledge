"""青空文庫インジェスターのテスト.

仕様: docs/specs/ingesters/aozora.md

テスト方針:
- 入力バリデーション（book_id, person_id, max_works, limit）
- max_works のクランプ（ハードリミット超過時）
- 著作権チェック（著作権ありの作品を拒否）
- 重複スキップ（ファイル存在時）
- カタログ未ダウンロード時のエラー
- GitHub Raw URL 変換
- HTTP エラー時のハンドリング
- 検索機能（著者名・タイトル部分一致）
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from rag.store.source_store import SourceStore

from factories import make_aozora_ingester


@pytest.fixture()
def source_store(tmp_path: Path) -> SourceStore:
    """テスト用 SourceStore を生成する."""
    store = SourceStore(tmp_path / "source_store")
    store.initialize()
    return store


def _write_catalog(store: SourceStore, records: list[dict[str, str]]) -> None:
    """テスト用カタログ CSV を source_store に書き込む."""
    if not records:
        return
    fieldnames = list(records[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(records)
    csv_bytes = buf.getvalue().encode("utf-8")
    store.place_file(
        source_type="aozora",
        data=csv_bytes,
        rel_path="aozora/catalog.csv",
        metadata={
            "source_type": "aozora",
            "title": "Aozora Bunko Catalog",
            "collected_at": "2026-01-01T00:00:00+09:00",
        },
    )


def _make_record(
    book_id: str = "001567",
    title: str = "Sample Title",
    person_id: str = "000035",
    last_name: str = "Alice",
    first_name: str = "Bob",
    copyright_flag: str = "なし",
    xhtml_url: str = "https://www.aozora.gr.jp/cards/000035/files/1567_14913.html",
) -> dict[str, str]:
    """テスト用カタログレコードを生成する."""
    return {
        "作品ID": book_id,
        "作品名": title,
        "人物ID": person_id,
        "姓": last_name,
        "名": first_name,
        "姓読み": "アリス",
        "名読み": "ボブ",
        "作品著作権フラグ": copyright_flag,
        "XHTML/HTMLファイルURL": xhtml_url,
    }


def _mock_client(
    status_code: int = 200,
    content: bytes = b"<html><body>test</body></html>",
) -> AsyncMock:
    """ConstrainedClient のモックを生成する.

    fetch_get の raise_for_status() が正しく動作するよう、
    実 httpx.Response を返す。
    """
    client = AsyncMock()
    request = httpx.Request("GET", "https://example.com")
    resp = httpx.Response(status_code, content=content, request=request)
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# === バリデーションテスト ===


class TestValidation:
    """入力バリデーションのテスト."""

    @pytest.mark.asyncio()
    async def test_add_work_empty_book_id(
        self, source_store: SourceStore
    ) -> None:
        """book_id が空文字列の場合 ValueError."""
        ingester = make_aozora_ingester(source_store)
        client = _mock_client()
        with pytest.raises(ValueError, match="book_id が空です"):
            await ingester.add_work("", client=client)

    @pytest.mark.asyncio()
    async def test_add_work_no_client(
        self, source_store: SourceStore
    ) -> None:
        """client が None の場合 ValueError."""
        ingester = make_aozora_ingester(source_store)
        with pytest.raises(ValueError, match="client"):
            await ingester.add_work("001567", client=None)

    @pytest.mark.asyncio()
    async def test_crawl_author_empty_person_id(
        self, source_store: SourceStore
    ) -> None:
        """person_id が空文字列の場合 ValueError."""
        ingester = make_aozora_ingester(source_store)
        client = _mock_client()
        with pytest.raises(ValueError, match="person_id が空です"):
            await ingester.crawl_author("", client=client)

    def test_search_no_criteria(self, source_store: SourceStore) -> None:
        """author も title も未指定の場合 ValueError."""
        _write_catalog(source_store, [_make_record()])
        ingester = make_aozora_ingester(source_store)
        with pytest.raises(ValueError, match="author または title"):
            ingester.search(limit=20)

    def test_search_invalid_limit_type(
        self, source_store: SourceStore
    ) -> None:
        """limit が整数でない場合 TypeError."""
        _write_catalog(source_store, [_make_record()])
        ingester = make_aozora_ingester(source_store)
        with pytest.raises(TypeError, match="limit は整数"):
            ingester.search(author="test", limit="10")  # type: ignore[arg-type]

    def test_search_negative_limit(
        self, source_store: SourceStore
    ) -> None:
        """limit が 0 以下の場合 ValueError."""
        _write_catalog(source_store, [_make_record()])
        ingester = make_aozora_ingester(source_store)
        with pytest.raises(ValueError, match="1 以上"):
            ingester.search(author="test", limit=0)


# === max_works クランプテスト ===


class TestMaxWorksClamp:
    """max_works のバリデーションとクランプ."""

    @pytest.mark.asyncio()
    async def test_max_works_exceeds_hard_limit_clamped(
        self, source_store: SourceStore
    ) -> None:
        """ハードリミット超過時はクランプされる."""
        _write_catalog(source_store, [_make_record()])
        ingester = make_aozora_ingester(source_store, max_works=9999)
        client = _mock_client()
        # crawl_author は内部で _validate_max_works を呼ぶ
        result = await ingester.crawl_author(
            "000035", max_works=9999, client=client
        )
        # クランプされてエラーにならず正常終了すること
        assert result.errors == 0

    @pytest.mark.asyncio()
    async def test_max_works_zero_rejected(
        self, source_store: SourceStore
    ) -> None:
        """max_works=0 は ValueError."""
        _write_catalog(source_store, [_make_record()])
        ingester = make_aozora_ingester(source_store)
        client = _mock_client()
        with pytest.raises(ValueError, match="1 以上"):
            await ingester.crawl_author(
                "000035", max_works=0, client=client
            )

    @pytest.mark.asyncio()
    async def test_max_works_negative_rejected(
        self, source_store: SourceStore
    ) -> None:
        """max_works が負数は ValueError."""
        _write_catalog(source_store, [_make_record()])
        ingester = make_aozora_ingester(source_store)
        client = _mock_client()
        with pytest.raises(ValueError, match="1 以上"):
            await ingester.crawl_author(
                "000035", max_works=-1, client=client
            )


# === 著作権チェックテスト ===


class TestCopyrightCheck:
    """著作権チェックのテスト."""

    @pytest.mark.asyncio()
    async def test_copyrighted_work_rejected(
        self, source_store: SourceStore
    ) -> None:
        """著作権ありの作品は取り込み拒否."""
        _write_catalog(
            source_store,
            [_make_record(copyright_flag="あり")],
        )
        ingester = make_aozora_ingester(source_store)
        client = _mock_client()
        with pytest.raises(ValueError, match="著作権あり"):
            await ingester.add_work("001567", client=client)

    @pytest.mark.asyncio()
    async def test_copyrighted_work_skipped_in_crawl(
        self, source_store: SourceStore
    ) -> None:
        """crawl_author で著作権ありの作品はフィルタされる."""
        _write_catalog(
            source_store,
            [_make_record(copyright_flag="あり")],
        )
        ingester = make_aozora_ingester(source_store)
        client = _mock_client()
        result = await ingester.crawl_author("000035", client=client)
        assert result.placed == 0
        assert result.errors == 0


# === カタログ未ダウンロードテスト ===


class TestCatalogNotFound:
    """カタログ未ダウンロード時のエラー."""

    @pytest.mark.asyncio()
    async def test_add_work_no_catalog(
        self, source_store: SourceStore
    ) -> None:
        """カタログなしで add_work するとエラー."""
        ingester = make_aozora_ingester(source_store)
        client = _mock_client()
        with pytest.raises(ValueError, match="カタログが未ダウンロード"):
            await ingester.add_work("001567", client=client)

    def test_search_no_catalog(self, source_store: SourceStore) -> None:
        """カタログなしで search するとエラー."""
        ingester = make_aozora_ingester(source_store)
        with pytest.raises(ValueError, match="カタログが未ダウンロード"):
            ingester.search(author="test", limit=20)

    @pytest.mark.asyncio()
    async def test_crawl_author_no_catalog(
        self, source_store: SourceStore
    ) -> None:
        """カタログなしで crawl_author するとエラー."""
        ingester = make_aozora_ingester(source_store)
        client = _mock_client()
        with pytest.raises(ValueError, match="カタログが未ダウンロード"):
            await ingester.crawl_author("000035", client=client)


# === 重複スキップテスト ===


class TestDuplicateSkip:
    """重複ファイルのスキップ."""

    @pytest.mark.asyncio()
    async def test_existing_file_skipped(
        self, source_store: SourceStore
    ) -> None:
        """既にファイルが存在する場合はスキップ."""
        _write_catalog(source_store, [_make_record()])
        # 先にファイルを配置
        source_store.place_file(
            source_type="aozora",
            data=b"<html>existing</html>",
            rel_path="aozora/000035/001567.html",
            metadata={
                "url": "https://www.aozora.gr.jp/cards/000035/files/1567_14913.html",
                "source_type": "aozora",
                "title": "Sample Title",
                "collected_at": "2026-01-01T00:00:00+09:00",
            },
        )
        ingester = make_aozora_ingester(source_store)
        client = _mock_client()
        result = await ingester.add_work("001567", client=client)
        assert result.placed == 0
        assert result.skipped == 1
        # HTTP リクエストは発生しない
        client.get.assert_not_called()


# === GitHub Raw URL 変換テスト ===


class TestGithubRawUrl:
    """GitHub Raw URL 変換のテスト."""

    def test_aozora_url_to_github_raw(
        self, source_store: SourceStore
    ) -> None:
        """aozora.gr.jp の URL が GitHub Raw URL に変換される."""
        ingester = make_aozora_ingester(source_store)
        url = "https://www.aozora.gr.jp/cards/000035/files/1567_14913.html"
        result = ingester._to_github_raw_url(url)
        assert result == (
            "https://raw.githubusercontent.com/"
            "aozorabunko/aozorabunko/master/"
            "cards/000035/files/1567_14913.html"
        )


# === HTTP エラーテスト ===


class TestHttpError:
    """HTTP エラー時のハンドリング."""

    @pytest.mark.asyncio()
    async def test_xhtml_download_404_counted_as_error(
        self, source_store: SourceStore
    ) -> None:
        """XHTML DL で 404 の場合はエラーカウント."""
        _write_catalog(source_store, [_make_record()])
        ingester = make_aozora_ingester(source_store)
        client = _mock_client(status_code=404)
        result = await ingester.add_work("001567", client=client)
        assert result.placed == 0
        assert result.errors == 1
        assert "ダウンロード失敗" in result.error_details[0]

    @pytest.mark.asyncio()
    async def test_catalog_download_error(
        self, source_store: SourceStore
    ) -> None:
        """カタログ DL で HTTP エラーの場合は ValueError."""
        ingester = make_aozora_ingester(source_store)
        client = _mock_client(status_code=500)
        with pytest.raises(ValueError, match="ダウンロードに失敗"):
            await ingester.update_catalog(client=client)


# === 検索テスト ===


class TestSearch:
    """検索機能のテスト."""

    def test_search_by_author(self, source_store: SourceStore) -> None:
        """著者名で部分一致検索."""
        _write_catalog(
            source_store,
            [
                _make_record(book_id="001", last_name="Alice", first_name="Bob"),
                _make_record(book_id="002", last_name="Carol", first_name="Dave"),
            ],
        )
        ingester = make_aozora_ingester(source_store)
        results = ingester.search(author="Alice", limit=20)
        assert len(results) == 1
        assert results[0]["book_id"] == "001"

    def test_search_by_title(self, source_store: SourceStore) -> None:
        """タイトルで部分一致検索."""
        _write_catalog(
            source_store,
            [
                _make_record(book_id="001", title="Sample Title A"),
                _make_record(book_id="002", title="Another Work"),
            ],
        )
        ingester = make_aozora_ingester(source_store)
        results = ingester.search(title="Sample", limit=20)
        assert len(results) == 1
        assert results[0]["book_id"] == "001"

    def test_search_includes_person_id(
        self, source_store: SourceStore
    ) -> None:
        """検索結果に person_id が含まれる."""
        _write_catalog(
            source_store,
            [_make_record(person_id="000999")],
        )
        ingester = make_aozora_ingester(source_store)
        results = ingester.search(author="Alice", limit=20)
        assert results[0]["person_id"] == "000999"

    def test_search_limit_clamp(self, source_store: SourceStore) -> None:
        """limit が結果を制限する."""
        records = [
            _make_record(book_id=f"{i:06d}", last_name="Alice")
            for i in range(10)
        ]
        _write_catalog(source_store, records)
        ingester = make_aozora_ingester(source_store)
        results = ingester.search(author="Alice", limit=3)
        assert len(results) == 3

    def test_search_limit_clamp_at_max(
        self, source_store: SourceStore
    ) -> None:
        """limit が MAX_SEARCH_LIMIT を超える場合にクランプされる."""
        from rag.pipeline.ingesters.aozora import MAX_SEARCH_LIMIT

        records = [
            _make_record(book_id=f"{i:06d}", last_name="Alice")
            for i in range(5)
        ]
        _write_catalog(source_store, records)
        ingester = make_aozora_ingester(source_store)
        results = ingester.search(author="Alice", limit=MAX_SEARCH_LIMIT + 1)
        assert len(results) == 5  # 全5件 < MAX_SEARCH_LIMIT なので全件返る

    def test_search_results_sorted_by_book_id_asc(
        self, source_store: SourceStore
    ) -> None:
        """検索結果が book_id 昇順でソートされる."""
        records = [
            _make_record(book_id="000003", last_name="Alice"),
            _make_record(book_id="000001", last_name="Alice"),
            _make_record(book_id="000005", last_name="Alice"),
            _make_record(book_id="000002", last_name="Alice"),
        ]
        _write_catalog(source_store, records)
        ingester = make_aozora_ingester(source_store)
        results = ingester.search(author="Alice", limit=20)
        book_ids = [r["book_id"] for r in results]
        assert book_ids == ["000001", "000002", "000003", "000005"]


# === 正常系取り込みテスト ===


class TestIngestWork:
    """正常系の作品取り込みテスト."""

    @pytest.mark.asyncio()
    async def test_add_work_success(
        self, source_store: SourceStore
    ) -> None:
        """正常に作品を取得・配置できる."""
        _write_catalog(source_store, [_make_record()])
        ingester = make_aozora_ingester(source_store)
        client = _mock_client(content=b"<html><body>content</body></html>")
        result = await ingester.add_work("001567", client=client)
        assert result.placed == 1
        assert result.errors == 0
        # ファイルが配置されている
        placed_file = source_store.root_dir / "aozora" / "000035" / "001567.html"
        assert placed_file.exists()
        # .meta が配置されている
        meta_file = source_store.root_dir / "aozora" / "000035" / "001567.html.meta"
        assert meta_file.exists()

    @pytest.mark.asyncio()
    async def test_crawl_author_success(
        self, source_store: SourceStore
    ) -> None:
        """person_id で著者の作品を一括取り込みできる."""
        _write_catalog(
            source_store,
            [
                _make_record(book_id="001", person_id="000035"),
                _make_record(
                    book_id="002",
                    person_id="000035",
                    xhtml_url="https://www.aozora.gr.jp/cards/000035/files/2_100.html",
                ),
                _make_record(book_id="003", person_id="999999"),
            ],
        )
        ingester = make_aozora_ingester(source_store)
        client = _mock_client()
        result = await ingester.crawl_author("000035", client=client)
        # person_id=000035 の作品 2 件のみ取り込み
        assert result.placed == 2
        assert result.errors == 0

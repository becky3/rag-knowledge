"""Scrapy Bridge のユニットテスト.

仕様: docs/specs/site-ingest.md

テスト方針:
- JSONL パースの正常系・異常系（不正 JSON、必須フィールド不足、空行）
- .meta 生成の正確性（仕様書のマッピングテーブルに準拠）
- source_store への配置（place_file_from_url 経由、.meta サイドカー生成）
- 拡張子判定（_needs_html_extension による .html 付与）
- エッジケース: HTML ファイル欠損、非200ステータス、filepath 未指定
- 複数レコードの処理と結果集計
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from rag.scrapy.bridge import (
    JsonlRecord,
    _build_meta,
    _parse_jsonl_line,
    _resolve_html_path,
    import_to_source_store,
)
from rag.store.source_store import SourceStore


# --- ヘルパー ---


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """JSONL ファイルを書き出す."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _write_html(html_dir: Path, filepath: str, content: str = "<html><body>Test</body></html>") -> None:
    """HTML ファイルを配置する."""
    dest = html_dir / filepath
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")


def _make_record(
    url: str = "https://example.com/page",
    title: str = "Test Page",
    status: int = 200,
    depth: int = 0,
    collected_at: str = "2026-03-21T03:00:00+00:00",
    filepath: str = "page.html",
) -> dict:
    """テスト用 JSONL レコード辞書を生成する."""
    return {
        "url": url,
        "title": title,
        "status": status,
        "depth": depth,
        "collected_at": collected_at,
        "filepath": filepath,
    }


# --- フィクスチャ ---


@pytest.fixture()
def source_store(tmp_path: Path) -> SourceStore:
    """テスト用 SourceStore を生成する."""
    store = SourceStore(tmp_path / "source_store")
    store.initialize()
    return store


@pytest.fixture()
def html_dir(tmp_path: Path) -> Path:
    """テスト用 HTML 一時保存ディレクトリ."""
    d = tmp_path / "html"
    d.mkdir()
    return d


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    """テスト用 JSONL ファイルパス."""
    return tmp_path / "metadata.jsonl"


# --- _parse_jsonl_line テスト ---


class TestParseJsonlLine:
    """JSONL 行パースのテスト."""

    def test_valid_record(self) -> None:
        """正常なレコードがパースされること."""
        line = json.dumps(_make_record())
        result = _parse_jsonl_line(line, 1)

        assert result is not None
        assert result.url == "https://example.com/page"
        assert result.title == "Test Page"
        assert result.status == 200
        assert result.depth == 0
        assert result.collected_at == "2026-03-21T03:00:00+00:00"
        assert result.filepath == "page.html"

    def test_empty_line_returns_none(self) -> None:
        """空行は None を返すこと."""
        assert _parse_jsonl_line("", 1) is None
        assert _parse_jsonl_line("   ", 1) is None
        assert _parse_jsonl_line("\n", 1) is None

    def test_invalid_json_returns_none(self) -> None:
        """不正な JSON は None を返すこと."""
        assert _parse_jsonl_line("{invalid json}", 1) is None

    def test_non_dict_returns_none(self) -> None:
        """辞書型でない JSON は None を返すこと."""
        assert _parse_jsonl_line("[1, 2, 3]", 1) is None
        assert _parse_jsonl_line('"string"', 1) is None

    def test_missing_url_returns_none(self) -> None:
        """url フィールドが欠けている場合は None を返すこと."""
        rec = _make_record()
        del rec["url"]
        assert _parse_jsonl_line(json.dumps(rec), 1) is None

    def test_missing_title_returns_none(self) -> None:
        """title フィールドが欠けている場合は None を返すこと."""
        rec = _make_record()
        del rec["title"]
        assert _parse_jsonl_line(json.dumps(rec), 1) is None

    def test_missing_collected_at_returns_none(self) -> None:
        """collected_at フィールドが欠けている場合は None を返すこと."""
        rec = _make_record()
        del rec["collected_at"]
        assert _parse_jsonl_line(json.dumps(rec), 1) is None

    def test_optional_status_defaults_to_200(self) -> None:
        """status がない場合はデフォルト 200 になること."""
        rec = _make_record()
        del rec["status"]
        result = _parse_jsonl_line(json.dumps(rec), 1)
        assert result is not None
        assert result.status == 200

    def test_optional_depth_defaults_to_0(self) -> None:
        """depth がない場合はデフォルト 0 になること."""
        rec = _make_record()
        del rec["depth"]
        result = _parse_jsonl_line(json.dumps(rec), 1)
        assert result is not None
        assert result.depth == 0

    def test_optional_filepath_defaults_to_empty(self) -> None:
        """filepath がない場合はデフォルト空文字になること."""
        rec = _make_record()
        del rec["filepath"]
        result = _parse_jsonl_line(json.dumps(rec), 1)
        assert result is not None
        assert result.filepath == ""


# --- _build_meta テスト ---


class TestBuildMeta:
    """JSONL → .meta 変換のテスト."""

    def test_meta_fields_match_spec(self) -> None:
        """仕様書の .meta マッピングに準拠すること."""
        record = JsonlRecord(
            url="https://example.com/docs/guide",
            title="Guide Title",
            status=200,
            depth=1,
            collected_at="2026-01-15T10:30:00+09:00",
            filepath="docs_guide.html",
        )
        meta = _build_meta(record)

        assert meta["source_id"] == "https://example.com/docs/guide"
        assert meta["source_type"] == "web"
        assert meta["title"] == "Guide Title"
        assert meta["collected_at"] == "2026-01-15T10:30:00+09:00"
        assert meta["url"] == "https://example.com/docs/guide"

    def test_meta_has_exactly_five_fields(self) -> None:
        """仕様書で定義された5フィールドのみ含むこと."""
        record = JsonlRecord(
            url="https://example.com/page",
            title="Test",
            status=200,
            depth=0,
            collected_at="2026-03-21T00:00:00Z",
            filepath="page.html",
        )
        meta = _build_meta(record)
        assert set(meta.keys()) == {
            "source_id", "source_type", "title", "collected_at", "url",
        }

    def test_empty_title_preserved(self) -> None:
        """タイトルが空文字でもそのまま保持されること."""
        record = JsonlRecord(
            url="https://example.com/page",
            title="",
            status=200,
            depth=0,
            collected_at="2026-03-21T00:00:00Z",
            filepath="page.html",
        )
        meta = _build_meta(record)
        assert meta["title"] == ""


# --- _resolve_html_path テスト ---


class TestResolveHtmlPath:
    """HTML ファイルパス解決のテスト."""

    def test_with_filepath(self, html_dir: Path) -> None:
        """filepath がある場合はパスが解決されること."""
        record = JsonlRecord(
            url="https://example.com/page",
            title="Test",
            status=200,
            depth=0,
            collected_at="2026-03-21T00:00:00Z",
            filepath="page.html",
        )
        path = _resolve_html_path(record, html_dir)
        assert path == (html_dir / "page.html").resolve()

    def test_without_filepath_returns_none(self, html_dir: Path) -> None:
        """filepath が空の場合は None を返すこと."""
        record = JsonlRecord(
            url="https://example.com/page",
            title="Test",
            status=200,
            depth=0,
            collected_at="2026-03-21T00:00:00Z",
            filepath="",
        )
        path = _resolve_html_path(record, html_dir)
        assert path is None

    def test_path_traversal_blocked(self, html_dir: Path) -> None:
        """パストラバーサルを含む filepath がブロックされること."""
        record = JsonlRecord(
            url="https://example.com/page",
            title="Test",
            status=200,
            depth=0,
            collected_at="2026-03-21T00:00:00Z",
            filepath="../../../etc/passwd",
        )
        path = _resolve_html_path(record, html_dir)
        assert path is None

    def test_path_traversal_relative_blocked(self, html_dir: Path) -> None:
        """相対パスによるディレクトリ脱出がブロックされること."""
        record = JsonlRecord(
            url="https://example.com/page",
            title="Test",
            status=200,
            depth=0,
            collected_at="2026-03-21T00:00:00Z",
            filepath="../sibling/file.html",
        )
        path = _resolve_html_path(record, html_dir)
        assert path is None


# --- import_to_source_store テスト ---


class TestImportToSourceStore:
    """source_store 配置の統合テスト."""

    def test_single_page_placement(
        self,
        source_store: SourceStore,
        html_dir: Path,
        jsonl_path: Path,
    ) -> None:
        """1ページが正しく source_store に配置されること."""
        _write_jsonl(jsonl_path, [_make_record()])
        _write_html(html_dir, "page.html", "<html><title>Test Page</title><body>Content</body></html>")

        result = import_to_source_store(
            jsonl_path=jsonl_path,
            html_dir=html_dir,
            source_store=source_store,
        )

        assert result.total_lines == 1
        assert result.ingest.placed == 1
        assert result.ingest.skipped == 0
        assert result.ingest.errors == 0

        # source_store にファイルが配置されている
        record = source_store.db.get_source("https://example.com/page")
        assert record is not None
        assert record.source_type == "web"
        assert record.title == "Test Page"

    def test_meta_file_generated(
        self,
        source_store: SourceStore,
        html_dir: Path,
        jsonl_path: Path,
    ) -> None:
        """.meta サイドカーファイルが生成されること."""
        _write_jsonl(jsonl_path, [_make_record(
            url="https://example.com/docs/guide",
            title="Guide Title",
            collected_at="2026-01-15T10:30:00+09:00",
            filepath="guide.html",
        )])
        _write_html(html_dir, "guide.html")

        import_to_source_store(
            jsonl_path=jsonl_path,
            html_dir=html_dir,
            source_store=source_store,
        )

        # .meta ファイルが存在するか
        files = list(source_store.root_dir.rglob("*.meta"))
        assert len(files) == 1

        meta = yaml.safe_load(files[0].read_text(encoding="utf-8"))
        assert meta["source_id"] == "https://example.com/docs/guide"
        assert meta["source_type"] == "web"
        assert meta["title"] == "Guide Title"
        assert meta["collected_at"] == "2026-01-15T10:30:00+09:00"
        assert meta["url"] == "https://example.com/docs/guide"

    def test_html_extension_added_for_extensionless_url(
        self,
        source_store: SourceStore,
        html_dir: Path,
        jsonl_path: Path,
    ) -> None:
        """拡張子なし URL に .html が付与されること."""
        _write_jsonl(jsonl_path, [_make_record(
            url="https://example.com/page",
            filepath="page.html",
        )])
        _write_html(html_dir, "page.html")

        import_to_source_store(
            jsonl_path=jsonl_path,
            html_dir=html_dir,
            source_store=source_store,
        )

        # source_store 内のファイルパスに .html が含まれる
        files = source_store.list_files(source_type="web")
        assert len(files) == 1
        assert str(files[0]).endswith(".html")

    def test_html_extension_not_added_for_html_url(
        self,
        source_store: SourceStore,
        html_dir: Path,
        jsonl_path: Path,
    ) -> None:
        """既に .html 拡張子がある URL には追加しないこと."""
        _write_jsonl(jsonl_path, [_make_record(
            url="https://example.com/page.html",
            filepath="page.html",
        )])
        _write_html(html_dir, "page.html")

        import_to_source_store(
            jsonl_path=jsonl_path,
            html_dir=html_dir,
            source_store=source_store,
        )

        files = source_store.list_files(source_type="web")
        assert len(files) == 1
        # .html.html にはならない
        assert not str(files[0]).endswith(".html.html")

    def test_multiple_pages(
        self,
        source_store: SourceStore,
        html_dir: Path,
        jsonl_path: Path,
    ) -> None:
        """複数ページが正しく処理されること."""
        records = [
            _make_record(url=f"https://example.com/page{i}", filepath=f"page{i}.html")
            for i in range(5)
        ]
        _write_jsonl(jsonl_path, records)
        for i in range(5):
            _write_html(html_dir, f"page{i}.html", f"<html><body>Page {i}</body></html>")

        result = import_to_source_store(
            jsonl_path=jsonl_path,
            html_dir=html_dir,
            source_store=source_store,
        )

        assert result.total_lines == 5
        assert result.ingest.placed == 5
        assert result.ingest.errors == 0

    def test_non_200_status_skipped(
        self,
        source_store: SourceStore,
        html_dir: Path,
        jsonl_path: Path,
    ) -> None:
        """非200ステータスのレコードがスキップされること."""
        records = [
            _make_record(url="https://example.com/ok", filepath="ok.html", status=200),
            _make_record(url="https://example.com/not-found", filepath="nf.html", status=404),
            _make_record(url="https://example.com/error", filepath="err.html", status=500),
        ]
        _write_jsonl(jsonl_path, records)
        _write_html(html_dir, "ok.html")
        _write_html(html_dir, "nf.html")
        _write_html(html_dir, "err.html")

        result = import_to_source_store(
            jsonl_path=jsonl_path,
            html_dir=html_dir,
            source_store=source_store,
        )

        assert result.ingest.placed == 1
        assert result.ingest.skipped == 2

    def test_missing_html_file(
        self,
        source_store: SourceStore,
        html_dir: Path,
        jsonl_path: Path,
    ) -> None:
        """HTML ファイルが存在しない場合はエラーとして記録されること."""
        _write_jsonl(jsonl_path, [_make_record(filepath="nonexistent.html")])

        result = import_to_source_store(
            jsonl_path=jsonl_path,
            html_dir=html_dir,
            source_store=source_store,
        )

        assert result.ingest.placed == 0
        assert result.ingest.errors == 1
        assert any("HTML not found" in d for d in result.ingest.error_details)

    def test_missing_filepath_field(
        self,
        source_store: SourceStore,
        html_dir: Path,
        jsonl_path: Path,
    ) -> None:
        """filepath フィールドがない場合はエラーとして記録されること."""
        rec = _make_record()
        del rec["filepath"]
        _write_jsonl(jsonl_path, [rec])

        result = import_to_source_store(
            jsonl_path=jsonl_path,
            html_dir=html_dir,
            source_store=source_store,
        )

        assert result.ingest.placed == 0
        assert result.ingest.errors == 1

    def test_empty_jsonl_file(
        self,
        source_store: SourceStore,
        html_dir: Path,
        jsonl_path: Path,
    ) -> None:
        """空の JSONL ファイルで結果が 0 件になること."""
        jsonl_path.write_text("", encoding="utf-8")

        result = import_to_source_store(
            jsonl_path=jsonl_path,
            html_dir=html_dir,
            source_store=source_store,
        )

        assert result.total_lines == 0
        assert result.ingest.placed == 0

    def test_nonexistent_jsonl_file(
        self,
        source_store: SourceStore,
        html_dir: Path,
        tmp_path: Path,
    ) -> None:
        """JSONL ファイルが存在しない場合は空の結果を返すこと."""
        result = import_to_source_store(
            jsonl_path=tmp_path / "nonexistent.jsonl",
            html_dir=html_dir,
            source_store=source_store,
        )

        assert result.total_lines == 0
        assert result.ingest.placed == 0

    def test_invalid_jsonl_lines_counted(
        self,
        source_store: SourceStore,
        html_dir: Path,
        jsonl_path: Path,
    ) -> None:
        """不正な JSONL 行がパースエラーとしてカウントされること."""
        jsonl_path.write_text(
            "{invalid json}\n"
            "[1, 2, 3]\n"
            '{"url": "https://example.com/ok", "title": "OK", "collected_at": "2026-03-21T00:00:00Z", "filepath": "ok.html"}\n',
            encoding="utf-8",
        )
        _write_html(html_dir, "ok.html")

        result = import_to_source_store(
            jsonl_path=jsonl_path,
            html_dir=html_dir,
            source_store=source_store,
        )

        assert result.total_lines == 3
        assert result.parse_errors == 2
        assert result.ingest.placed == 1

    def test_mixed_success_and_errors(
        self,
        source_store: SourceStore,
        html_dir: Path,
        jsonl_path: Path,
    ) -> None:
        """成功・スキップ・エラーが混在するケースの集計が正しいこと."""
        records = [
            _make_record(url="https://example.com/ok1", filepath="ok1.html"),
            _make_record(url="https://example.com/ok2", filepath="ok2.html"),
            _make_record(url="https://example.com/skip", filepath="skip.html", status=404),
            _make_record(url="https://example.com/missing", filepath="missing.html"),
        ]
        _write_jsonl(jsonl_path, records)
        _write_html(html_dir, "ok1.html")
        _write_html(html_dir, "ok2.html")
        _write_html(html_dir, "skip.html")
        # missing.html は配置しない

        result = import_to_source_store(
            jsonl_path=jsonl_path,
            html_dir=html_dir,
            source_store=source_store,
        )

        assert result.total_lines == 4
        assert result.ingest.placed == 2
        assert result.ingest.skipped == 1
        assert result.ingest.errors == 1

    def test_duplicate_url_overwrites(
        self,
        source_store: SourceStore,
        html_dir: Path,
        jsonl_path: Path,
    ) -> None:
        """同一 URL の重複レコードで上書き配置されること."""
        records = [
            _make_record(url="https://example.com/page", filepath="page_v1.html", title="V1"),
            _make_record(url="https://example.com/page", filepath="page_v2.html", title="V2"),
        ]
        _write_jsonl(jsonl_path, records)
        _write_html(html_dir, "page_v1.html", "<html><body>V1</body></html>")
        _write_html(html_dir, "page_v2.html", "<html><body>V2</body></html>")

        result = import_to_source_store(
            jsonl_path=jsonl_path,
            html_dir=html_dir,
            source_store=source_store,
        )

        # 1件目は新規配置、2件目は既存上書き（skipped）
        assert result.ingest.placed == 1
        assert result.ingest.skipped == 1

        # 最後の配置が有効
        record = source_store.db.get_source("https://example.com/page")
        assert record is not None
        assert record.title == "V2"

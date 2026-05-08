"""Scrapy Runner のユニットテスト.

仕様: docs/specs/site-ingest.md

テスト方針:
- subprocess ラッパーのモックテスト（Scrapy プロセスの起動・終了・エラーハンドリング）
- _build_spider_script の設定値埋め込み（JSON ファイル経由）
- ディレクトリ構造の準備（crawl_dir, html_dir, jobdir）
- --force オプションによるクロールディレクトリ削除
- クロールキーによる JOBDIR 分離
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import pytest

from factories import make_scrapy_runner_args
from rag.scrapy.runner import CrawlResult, RealScrapyRunner, _crawl_key


async def _async_lines_iter(lines: list[bytes]):
    """AsyncMock の stderr 用の非同期イテレータ."""
    for line in lines:
        yield line


def _expected_crawl_dir(
    tmp_path: Path, start_url: str, url_pattern: str = "",
) -> Path:
    """テスト用: runner.run() と同じロジックでクロールディレクトリを計算する."""
    parsed = urlparse(start_url)
    effective_pattern = url_pattern
    if not effective_pattern:
        path = parsed.path.rstrip("/")
        if path and path != "/":
            base_prefix = f"{parsed.scheme}://{parsed.hostname}{path}"
            effective_pattern = f"^{re.escape(base_prefix)}(?:/|$)"
    domain = parsed.hostname or "unknown"
    key = _crawl_key(start_url, effective_pattern)
    return tmp_path / domain / key


# --- _crawl_key テスト ---


class TestCrawlKey:
    """クロールキー生成のテスト."""

    def test_same_params_same_key(self) -> None:
        """同じパラメータは同じキーを返す."""
        key1 = _crawl_key("https://example.com/a", "/a/.*")
        key2 = _crawl_key("https://example.com/a", "/a/.*")
        assert key1 == key2

    def test_different_start_url_different_key(self) -> None:
        """異なる start_url は異なるキーを返す."""
        key1 = _crawl_key("https://example.com/a", "")
        key2 = _crawl_key("https://example.com/b", "")
        assert key1 != key2

    def test_different_url_pattern_different_key(self) -> None:
        """異なる url_pattern は異なるキーを返す."""
        key1 = _crawl_key("https://example.com", "/a/.*")
        key2 = _crawl_key("https://example.com", "/b/.*")
        assert key1 != key2

    def test_key_length(self) -> None:
        """キーは16文字."""
        key = _crawl_key("https://example.com", "")
        assert len(key) == 16

    def test_key_is_hex(self) -> None:
        """キーは16進文字列."""
        key = _crawl_key("https://example.com", "")
        int(key, 16)  # hex でなければ ValueError


# --- CrawlResult テスト ---


class TestCrawlResult:
    """CrawlResult データクラスのテスト."""

    def test_success_result(self, tmp_path: Path) -> None:
        """正常終了の CrawlResult."""
        result = CrawlResult(
            exit_code=0,
            output_dir=tmp_path / "html",
            jsonl_path=tmp_path / "metadata.jsonl",
            success=True,
        )
        assert result.success is True
        assert result.exit_code == 0
        assert result.stderr_tail == ""
        assert result.crawl_dir is None

    def test_failure_result(self, tmp_path: Path) -> None:
        """異常終了の CrawlResult."""
        result = CrawlResult(
            exit_code=1,
            output_dir=tmp_path / "html",
            jsonl_path=tmp_path / "metadata.jsonl",
            success=False,
            stderr_tail="Error occurred",
        )
        assert result.success is False
        assert result.exit_code == 1
        assert result.stderr_tail == "Error occurred"

    def test_crawl_dir_field(self, tmp_path: Path) -> None:
        """crawl_dir フィールドが設定できること."""
        crawl_dir = tmp_path / "domain" / "key123"
        result = CrawlResult(
            exit_code=0,
            output_dir=crawl_dir / "html",
            jsonl_path=crawl_dir / "metadata.jsonl",
            success=True,
            crawl_dir=crawl_dir,
        )
        assert result.crawl_dir == crawl_dir

    def test_cleanup_deletes_crawl_dir(self, tmp_path: Path) -> None:
        """cleanup() がクロールディレクトリを削除すること."""
        domain_dir = tmp_path / "domain"
        crawl_dir = domain_dir / "key123"
        crawl_dir.mkdir(parents=True)
        (crawl_dir / "dummy.txt").write_text("data", encoding="utf-8")

        result = CrawlResult(
            exit_code=0, output_dir=crawl_dir / "html",
            jsonl_path=crawl_dir / "metadata.jsonl",
            success=True, crawl_dir=crawl_dir,
        )
        result.cleanup()
        assert not crawl_dir.exists()
        # 空の親ディレクトリも削除される
        assert not domain_dir.exists()

    def test_cleanup_noop_when_crawl_dir_none(self) -> None:
        """crawl_dir が None の場合 cleanup() は何もしないこと."""
        result = CrawlResult(
            exit_code=0, output_dir=Path("/tmp/html"),
            jsonl_path=Path("/tmp/metadata.jsonl"),
            success=True, crawl_dir=None,
        )
        result.cleanup()  # 例外が発生しないこと

    def test_cleanup_warns_on_oserror(self, tmp_path: Path) -> None:
        """削除失敗時に警告ログを出力し例外を送出しないこと."""
        crawl_dir = tmp_path / "domain" / "key123"
        # crawl_dir を作成しない（rmtree が失敗する）
        result = CrawlResult(
            exit_code=0, output_dir=crawl_dir / "html",
            jsonl_path=crawl_dir / "metadata.jsonl",
            success=True, crawl_dir=crawl_dir,
        )
        # 例外が発生しないこと
        result.cleanup()


# --- _build_spider_script テスト ---


def _write_params(tmp_path: Path, **overrides: object) -> Path:
    """テスト用パラメータ JSON ファイルを作成して返す."""
    params = {
        "start_url": "https://example.com",
        "allowed_domains": "example.com",
        "url_pattern": "",
        "output_dir": str(tmp_path / "html"),
        "jsonl_path": str(tmp_path / "metadata.jsonl"),
        "jobdir": str(tmp_path / "jobdir"),
        "max_pages": 100,
        "delay_sec": 0.5,
        "download_timeout": 60,
        "timeout_sec": 0,
        "error_count": 0,
    }
    params.update(overrides)
    params_path = tmp_path / "spider_params.json"
    params_path.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    return params_path


class TestBuildSpiderScript:
    """Spider スクリプト構築のテスト."""

    def test_script_contains_settings(self, tmp_path: Path) -> None:
        """スクリプトに Scrapy 設定が JSON から読み込まれる構造を含むこと."""
        runner = RealScrapyRunner(
            **make_scrapy_runner_args(
                temp_dir=tmp_path,
                delay_sec=0.5,
                max_pages=100,
                download_timeout=60,
            ),
        )
        params_path = _write_params(tmp_path)
        script = runner._build_spider_script(params_path=params_path)

        # JSON から設定を読み込む構造
        assert "params['delay_sec']" in script
        assert "params['download_timeout']" in script
        assert "'LOG_LEVEL': 'INFO'" in script
        # max_pages は Spider パラメータとして渡される（CLOSESPIDER_PAGECOUNT ではない）
        assert "max_pages=params.get('max_pages', 0)" in script

    def test_script_contains_spider_args(self, tmp_path: Path) -> None:
        """スクリプトに Spider 引数が JSON 経由で渡される構造を含むこと."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))
        params_path = _write_params(
            tmp_path,
            start_url="https://docs.example.com/guide",
            allowed_domains="docs.example.com",
            url_pattern=r"/guide/.*",
        )
        script = runner._build_spider_script(params_path=params_path)

        # Spider 引数が params から渡される
        assert "params.get('start_url', '')" in script
        assert "params['allowed_domains']" in script
        assert "params.get('url_pattern', '')" in script

    def test_script_imports(self, tmp_path: Path) -> None:
        """スクリプトに必要な import が含まれること."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))
        params_path = _write_params(tmp_path)
        script = runner._build_spider_script(params_path=params_path)

        assert "from scrapy.crawler import CrawlerProcess" in script
        assert "from rag.scrapy.spider import SiteSpider" in script
        assert "process.crawl(" in script
        assert "process.start()" in script

    def test_script_adds_src_to_path(self, tmp_path: Path) -> None:
        """スクリプトが src/ を sys.path に追加すること."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))
        params_path = _write_params(tmp_path)
        script = runner._build_spider_script(params_path=params_path)

        assert "sys.path.insert(0, src_dir)" in script

    def test_script_reads_json_params(self, tmp_path: Path) -> None:
        """スクリプトが JSON ファイルからパラメータを読み込むこと."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))
        params_path = _write_params(tmp_path)
        script = runner._build_spider_script(params_path=params_path)

        assert "json.load(f)" in script
        assert str(params_path).replace("\\", "/") in script

    def test_script_contains_bfs_settings(self, tmp_path: Path) -> None:
        """スクリプトに BFS 設定が含まれること."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))
        params_path = _write_params(tmp_path)
        script = runner._build_spider_script(params_path=params_path)

        assert "'DEPTH_PRIORITY': 1" in script
        assert "PickleFifoDiskQueue" in script
        assert "FifoMemoryQueue" in script

    def test_script_does_not_contain_closespider_pagecount(self, tmp_path: Path) -> None:
        """スクリプトに CLOSESPIDER_PAGECOUNT が含まれないこと."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))
        params_path = _write_params(tmp_path)
        script = runner._build_spider_script(params_path=params_path)

        assert "CLOSESPIDER_PAGECOUNT" not in script

    def test_script_contains_closespider_timeout_conditional(self, tmp_path: Path) -> None:
        """スクリプトに CLOSESPIDER_TIMEOUT の条件分岐が含まれること."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))
        params_path = _write_params(tmp_path, timeout_sec=300)
        script = runner._build_spider_script(params_path=params_path)

        assert "CLOSESPIDER_TIMEOUT" in script
        assert "params.get('timeout_sec')" in script

    def test_script_contains_closespider_errorcount_conditional(self, tmp_path: Path) -> None:
        """スクリプトに CLOSESPIDER_ERRORCOUNT の条件分岐が含まれること."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))
        params_path = _write_params(tmp_path, error_count=10)
        script = runner._build_spider_script(params_path=params_path)

        assert "CLOSESPIDER_ERRORCOUNT" in script
        assert "params.get('error_count')" in script


# --- RealScrapyRunner.run テスト（モック） ---


class TestRealScrapyRunnerRun:
    """RealScrapyRunner.run のモックテスト."""

    @pytest.mark.asyncio()
    async def test_successful_crawl(self, tmp_path: Path) -> None:
        """正常終了のクロール."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(start_url="https://example.com")

        assert result.success is True
        assert result.exit_code == 0
        assert result.output_dir.name == "html"
        assert result.jsonl_path.name == "metadata.jsonl"
        # output_dir と jsonl_path は同じクロールディレクトリ配下
        assert result.output_dir.parent == result.jsonl_path.parent

    @pytest.mark.asyncio()
    async def test_failed_crawl(self, tmp_path: Path) -> None:
        """異常終了のクロール."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 1

        async def fake_exec(*args, **kwargs):
            stderr_file = kwargs.get("stderr")
            if stderr_file and hasattr(stderr_file, "write"):
                stderr_file.write("ERROR: Something failed\n")
            return mock_process

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await runner.run(start_url="https://example.com")

        assert result.success is False
        assert result.exit_code == 1
        assert "Something failed" in result.stderr_tail

    @pytest.mark.asyncio()
    async def test_directory_structure_created(self, tmp_path: Path) -> None:
        """一時保存ディレクトリ構造が作成されること."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(start_url="https://docs.example.com/guide")

        # クロールキーベースのディレクトリが作成される
        assert result.output_dir.exists()
        assert result.output_dir.name == "html"
        # ドメインディレクトリの下にクロールキーディレクトリがある
        assert result.output_dir.parent.parent.name == "docs.example.com"

    @pytest.mark.asyncio()
    async def test_force_deletes_crawl_dir(self, tmp_path: Path) -> None:
        """--force でクロールディレクトリが削除されること."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        crawl_dir = _expected_crawl_dir(tmp_path, "https://example.com")

        # 事前に JOBDIR, html/, metadata.jsonl を作成
        jobdir = crawl_dir / "jobdir"
        jobdir.mkdir(parents=True)
        (jobdir / "requests.seen").write_text("data", encoding="utf-8")

        html_dir = crawl_dir / "html"
        html_dir.mkdir(parents=True)
        (html_dir / "page.html").write_text("<html>old</html>", encoding="utf-8")

        jsonl_path = crawl_dir / "metadata.jsonl"
        jsonl_path.write_text('{"url":"old"}\n', encoding="utf-8")

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await runner.run(start_url="https://example.com", force=True)

        # 旧ファイルが全て削除されている（run 内で mkdir されるのでディレクトリ自体は再作成される）
        assert not (jobdir / "requests.seen").exists()
        assert not (html_dir / "page.html").exists()
        assert not jsonl_path.exists()
        # html_dir は run 内で再作成されるので存在する
        assert html_dir.exists()

    @pytest.mark.asyncio()
    async def test_max_pages_override(self, tmp_path: Path) -> None:
        """max_pages パラメータがインスタンス設定を上書きすること."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path, max_pages=10000))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(start_url="https://example.com", max_pages=50)

        # パラメータ JSON ファイルに max_pages=50 が書き込まれていること
        params_path = result.output_dir.parent / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        assert params["max_pages"] == 50

    @pytest.mark.asyncio()
    async def test_env_pythonioencoding(self, tmp_path: Path) -> None:
        """環境変数 PYTHONIOENCODING=utf-8 が設定されること."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await runner.run(start_url="https://example.com")

        call_kwargs = mock_exec.call_args[1]
        assert call_kwargs["env"]["PYTHONIOENCODING"] == "utf-8"

    @pytest.mark.asyncio()
    async def test_params_json_written(self, tmp_path: Path) -> None:
        """パラメータ JSON ファイルが書き出されること."""
        runner = RealScrapyRunner(
            **make_scrapy_runner_args(
                temp_dir=tmp_path,
                delay_sec=1.5,
                max_pages=200,
                download_timeout=45,
            ),
        )

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(
                start_url="https://example.com/docs",
                allowed_domains="example.com",
                url_pattern=r"/docs/.*",
            )

        params_path = result.output_dir.parent / "spider_params.json"
        assert params_path.exists()
        params = json.loads(params_path.read_text(encoding="utf-8"))
        assert params["start_url"] == "https://example.com/docs"
        assert params["allowed_domains"] == "example.com"
        assert params["url_pattern"] == r"/docs/.*"
        assert params["max_pages"] == 200
        assert params["delay_sec"] == 1.5
        assert params["download_timeout"] == 45

    @pytest.mark.asyncio()
    async def test_params_json_includes_timeout_and_error_count(self, tmp_path: Path) -> None:
        """パラメータ JSON に timeout_sec と error_count が含まれること."""
        runner = RealScrapyRunner(
            **make_scrapy_runner_args(
                temp_dir=tmp_path,
                timeout_sec=600,
                error_count=50,
            ),
        )

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(start_url="https://example.com")

        params_path = result.output_dir.parent / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        assert params["timeout_sec"] == 600
        assert params["error_count"] == 50

    @pytest.mark.asyncio()
    async def test_url_pattern_auto_generated_from_path(self, tmp_path: Path) -> None:
        """url_pattern 未指定時にパスプレフィックスから自動生成されること."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(start_url="https://example.com/docs/guide/")

        params_path = result.output_dir.parent / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        # re.escape でドメインのドットがエスケープされたパターン
        assert params["url_pattern"] == r"^https://example\.com/docs/guide(?:/|$)"

    @pytest.mark.asyncio()
    async def test_url_pattern_not_generated_for_root(self, tmp_path: Path) -> None:
        """パスが / のみの場合は url_pattern が自動生成されないこと."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(start_url="https://example.com/")

        params_path = result.output_dir.parent / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        assert params["url_pattern"] == ""

    @pytest.mark.asyncio()
    async def test_url_pattern_auto_generated_from_file_url(self, tmp_path: Path) -> None:
        """開始 URL の末尾セグメントが Web 系拡張子の場合、親ディレクトリまで丸めた url_pattern が生成されること."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(start_url="https://example.com/docs/vol1/index.html")

        params_path = result.output_dir.parent / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        # 末尾の index.html を取り除き、親ディレクトリ /docs/vol1 までで丸める
        assert params["url_pattern"] == r"^https://example\.com/docs/vol1(?:/|$)"

    @pytest.mark.asyncio()
    async def test_url_pattern_auto_generated_keeps_extensionless_path(
        self, tmp_path: Path
    ) -> None:
        """末尾スラッシュなし + 拡張子なしのケースで丸めが発火しないこと（既存 from_path テストとの境界条件）."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(start_url="https://example.com/docs/guide")

        params_path = result.output_dir.parent / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        # 拡張子なしのパスは既存挙動を維持（/docs/guide まで）
        assert params["url_pattern"] == r"^https://example\.com/docs/guide(?:/|$)"

    @pytest.mark.asyncio()
    async def test_url_pattern_root_level_file_url_keeps_self_only(self, tmp_path: Path) -> None:
        """ルート直下の Web 系拡張子ファイル URL では従来挙動（自身のみマッチ）を保持すること."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(start_url="https://example.com/index.html")

        params_path = result.output_dir.parent / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        # 親ディレクトリがルートになるため丸めず、自身のみマッチするパターン
        # （意図しない全ドメインクロールを防ぐ）
        assert params["url_pattern"] == r"^https://example\.com/index\.html(?:/|$)"

    @pytest.mark.asyncio()
    async def test_url_pattern_non_web_extension_not_rolled_up(self, tmp_path: Path) -> None:
        """末尾セグメントが Web 系拡張子に含まれない場合は丸めないこと（PDF 等）."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(start_url="https://example.com/docs/manual.pdf")

        params_path = result.output_dir.parent / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        # .pdf は _WEB_EXTENSIONS に含まれないため丸めず
        assert params["url_pattern"] == r"^https://example\.com/docs/manual\.pdf(?:/|$)"

    @pytest.mark.asyncio()
    async def test_url_pattern_version_like_segment_not_rolled_up(self, tmp_path: Path) -> None:
        """バージョン番号風セグメント（/api/v1.0 等）が Web 系拡張子と誤検出されないこと."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(start_url="https://example.com/api/v1.0")

        params_path = result.output_dir.parent / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        # .0 は _WEB_EXTENSIONS に含まれないため丸めず（誤検出回避）
        assert params["url_pattern"] == r"^https://example\.com/api/v1\.0(?:/|$)"

    @pytest.mark.asyncio()
    async def test_url_pattern_dotfile_not_rolled_up(self, tmp_path: Path) -> None:
        """先頭ドットの dotfile 形式（/.gitignore 等）が誤検出されないこと."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(start_url="https://example.com/repo/.gitignore")

        params_path = result.output_dir.parent / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        # dotfile（.gitignore）は Path.suffix が空文字列のため丸めず
        assert params["url_pattern"] == r"^https://example\.com/repo/\.gitignore(?:/|$)"

    @pytest.mark.asyncio()
    async def test_url_pattern_explicit_takes_priority(self, tmp_path: Path) -> None:
        """url_pattern を明示指定した場合は自動生成より優先されること."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(
                start_url="https://example.com/docs/guide",
                url_pattern=r"/custom/.*",
            )

        params_path = result.output_dir.parent / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        assert params["url_pattern"] == r"/custom/.*"


# --- JOBDIR 分離テスト ---


class TestJobdirIsolation:
    """異なるクロール設定間の JOBDIR 分離テスト."""

    @pytest.mark.asyncio()
    async def test_different_start_url_different_jobdir(self, tmp_path: Path) -> None:
        """同一ドメインでも異なる start_url は異なる JOBDIR を使用する."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result_a = await runner.run(
                start_url="https://example.com/a.html",
                url_pattern="a",
            )
            result_b = await runner.run(
                start_url="https://example.com/b.html",
                url_pattern="b",
            )

        # 出力ディレクトリが異なること
        assert result_a.output_dir != result_b.output_dir
        # どちらも同一ドメインの下にあること
        assert result_a.output_dir.parent.parent.name == "example.com"
        assert result_b.output_dir.parent.parent.name == "example.com"

    @pytest.mark.asyncio()
    async def test_different_url_pattern_different_jobdir(self, tmp_path: Path) -> None:
        """同じ start_url でも異なる url_pattern は異なる JOBDIR を使用する."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result_a = await runner.run(
                start_url="https://example.com",
                url_pattern="/docs/.*",
            )
            result_b = await runner.run(
                start_url="https://example.com",
                url_pattern="/api/.*",
            )

        assert result_a.output_dir != result_b.output_dir

    @pytest.mark.asyncio()
    async def test_same_params_same_directory(self, tmp_path: Path) -> None:
        """同じパラメータは同じディレクトリを使用する（レジューム可能）."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result_1 = await runner.run(
                start_url="https://example.com/docs",
                url_pattern="/docs/.*",
            )
            result_2 = await runner.run(
                start_url="https://example.com/docs",
                url_pattern="/docs/.*",
            )

        assert result_1.output_dir == result_2.output_dir
        assert result_1.jsonl_path == result_2.jsonl_path

    @pytest.mark.asyncio()
    async def test_force_does_not_affect_other_crawl(self, tmp_path: Path) -> None:
        """--force は対象クロールのディレクトリのみ削除し、他のクロールに影響しない."""
        runner = RealScrapyRunner(**make_scrapy_runner_args(temp_dir=tmp_path))

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0

        # 1回目のクロール（a.html）を実行してファイルを作成
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result_a = await runner.run(
                start_url="https://example.com/a.html",
                url_pattern="a",
            )

        # a のクロールディレクトリにダミーファイルを作成
        marker_file = result_a.output_dir / "marker.html"
        marker_file.write_text("data", encoding="utf-8")

        # 2回目のクロール（b.html）を --force で実行
        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await runner.run(
                start_url="https://example.com/b.html",
                url_pattern="b",
                force=True,
            )

        # a のマーカーファイルは影響を受けていない
        assert marker_file.exists()

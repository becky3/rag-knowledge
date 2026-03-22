"""Scrapy Runner のユニットテスト.

仕様: docs/specs/site-ingest.md

テスト方針:
- subprocess ラッパーのモックテスト（Scrapy プロセスの起動・終了・エラーハンドリング）
- _build_spider_script の設定値埋め込み（JSON ファイル経由）
- ディレクトリ構造の準備（domain_dir, html_dir, jobdir）
- --force オプションによる JOBDIR 削除
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from rag.scrapy.runner import CrawlResult, ScrapyRunner


async def _async_lines_iter(lines: list[bytes]):
    """AsyncMock の stderr 用の非同期イテレータ."""
    for line in lines:
        yield line


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
        runner = ScrapyRunner(
            temp_dir=tmp_path,
            delay_sec=0.5,
            max_pages=100,
            download_timeout=60,
        )
        params_path = _write_params(tmp_path)
        script = runner._build_spider_script(params_path=params_path)

        # JSON から設定を読み込む構造
        assert "params['delay_sec']" in script
        assert "params['download_timeout']" in script
        assert "params['max_pages']" in script
        assert "'LOG_LEVEL': 'INFO'" in script

    def test_script_contains_spider_args(self, tmp_path: Path) -> None:
        """スクリプトに Spider 引数が JSON 経由で渡される構造を含むこと."""
        runner = ScrapyRunner(temp_dir=tmp_path)
        params_path = _write_params(
            tmp_path,
            start_url="https://docs.example.com/guide",
            allowed_domains="docs.example.com",
            url_pattern=r"/guide/.*",
        )
        script = runner._build_spider_script(params_path=params_path)

        # Spider 引数が params から渡される
        assert "params['start_url']" in script
        assert "params['allowed_domains']" in script
        assert "params['url_pattern']" in script

    def test_script_imports(self, tmp_path: Path) -> None:
        """スクリプトに必要な import が含まれること."""
        runner = ScrapyRunner(temp_dir=tmp_path)
        params_path = _write_params(tmp_path)
        script = runner._build_spider_script(params_path=params_path)

        assert "from scrapy.crawler import CrawlerProcess" in script
        assert "from rag.scrapy.spider import SiteSpider" in script
        assert "process.crawl(" in script
        assert "process.start()" in script

    def test_script_adds_src_to_path(self, tmp_path: Path) -> None:
        """スクリプトが src/ を sys.path に追加すること."""
        runner = ScrapyRunner(temp_dir=tmp_path)
        params_path = _write_params(tmp_path)
        script = runner._build_spider_script(params_path=params_path)

        assert "sys.path.insert(0, src_dir)" in script

    def test_script_reads_json_params(self, tmp_path: Path) -> None:
        """スクリプトが JSON ファイルからパラメータを読み込むこと."""
        runner = ScrapyRunner(temp_dir=tmp_path)
        params_path = _write_params(tmp_path)
        script = runner._build_spider_script(params_path=params_path)

        assert "json.load(f)" in script
        assert str(params_path).replace("\\", "/") in script

    def test_script_contains_closespider_timeout_conditional(self, tmp_path: Path) -> None:
        """スクリプトに CLOSESPIDER_TIMEOUT の条件分岐が含まれること."""
        runner = ScrapyRunner(temp_dir=tmp_path)
        params_path = _write_params(tmp_path, timeout_sec=300)
        script = runner._build_spider_script(params_path=params_path)

        assert "CLOSESPIDER_TIMEOUT" in script
        assert "params.get('timeout_sec')" in script

    def test_script_contains_closespider_errorcount_conditional(self, tmp_path: Path) -> None:
        """スクリプトに CLOSESPIDER_ERRORCOUNT の条件分岐が含まれること."""
        runner = ScrapyRunner(temp_dir=tmp_path)
        params_path = _write_params(tmp_path, error_count=10)
        script = runner._build_spider_script(params_path=params_path)

        assert "CLOSESPIDER_ERRORCOUNT" in script
        assert "params.get('error_count')" in script


# --- ScrapyRunner.run テスト（モック） ---


class TestScrapyRunnerRun:
    """ScrapyRunner.run のモックテスト."""

    @pytest.mark.asyncio()
    async def test_successful_crawl(self, tmp_path: Path) -> None:
        """正常終了のクロール."""
        runner = ScrapyRunner(temp_dir=tmp_path)

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await runner.run(start_url="https://example.com")

        assert result.success is True
        assert result.exit_code == 0
        assert result.output_dir == tmp_path / "example.com" / "html"
        assert result.jsonl_path == tmp_path / "example.com" / "metadata.jsonl"

    @pytest.mark.asyncio()
    async def test_failed_crawl(self, tmp_path: Path) -> None:
        """異常終了のクロール."""
        runner = ScrapyRunner(temp_dir=tmp_path)

        mock_process = AsyncMock()
        mock_process.wait.return_value = 1

        # stderr はファイルリダイレクト方式のため、
        # subprocess 起動時に stderr.log にエラーを書き込む
        domain_dir = tmp_path / "example.com"

        async def fake_exec(*args, **kwargs):
            domain_dir.mkdir(parents=True, exist_ok=True)
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
        runner = ScrapyRunner(temp_dir=tmp_path)

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await runner.run(start_url="https://docs.example.com/guide")

        # ドメインベースのディレクトリが作成される
        assert (tmp_path / "docs.example.com" / "html").exists()

    @pytest.mark.asyncio()
    async def test_force_deletes_domain_dir(self, tmp_path: Path) -> None:
        """--force でドメインディレクトリ全体が削除されること."""
        runner = ScrapyRunner(temp_dir=tmp_path)

        domain_dir = tmp_path / "example.com"

        # 事前に JOBDIR, html/, metadata.jsonl を作成
        jobdir = domain_dir / "jobdir"
        jobdir.mkdir(parents=True)
        (jobdir / "requests.seen").write_text("data", encoding="utf-8")

        html_dir = domain_dir / "html"
        html_dir.mkdir(parents=True)
        (html_dir / "page.html").write_text("<html>old</html>", encoding="utf-8")

        jsonl_path = domain_dir / "metadata.jsonl"
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
        runner = ScrapyRunner(temp_dir=tmp_path, max_pages=10000)

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await runner.run(start_url="https://example.com", max_pages=50)

        # パラメータ JSON ファイルに max_pages=50 が書き込まれていること
        params_path = tmp_path / "example.com" / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        assert params["max_pages"] == 50

    @pytest.mark.asyncio()
    async def test_env_pythonioencoding(self, tmp_path: Path) -> None:
        """環境変数 PYTHONIOENCODING=utf-8 が設定されること."""
        runner = ScrapyRunner(temp_dir=tmp_path)

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
        runner = ScrapyRunner(
            temp_dir=tmp_path,
            delay_sec=1.5,
            max_pages=200,
            download_timeout=45,
        )

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await runner.run(
                start_url="https://example.com/docs",
                allowed_domains="example.com",
                url_pattern=r"/docs/.*",
            )

        params_path = tmp_path / "example.com" / "spider_params.json"
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
        runner = ScrapyRunner(
            temp_dir=tmp_path,
            timeout_sec=600,
            error_count=50,
        )

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await runner.run(start_url="https://example.com")

        params_path = tmp_path / "example.com" / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        assert params["timeout_sec"] == 600
        assert params["error_count"] == 50

    @pytest.mark.asyncio()
    async def test_url_pattern_auto_generated_from_path(self, tmp_path: Path) -> None:
        """url_pattern 未指定時にパスプレフィックスから自動生成されること."""
        runner = ScrapyRunner(temp_dir=tmp_path)

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await runner.run(start_url="https://example.com/docs/guide/")

        params_path = tmp_path / "example.com" / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        # re.escape でドメインのドットがエスケープされたパターン
        assert params["url_pattern"] == r"^https://example\.com/docs/guide(?:/|$)"

    @pytest.mark.asyncio()
    async def test_url_pattern_not_generated_for_root(self, tmp_path: Path) -> None:
        """パスが / のみの場合は url_pattern が自動生成されないこと."""
        runner = ScrapyRunner(temp_dir=tmp_path)

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await runner.run(start_url="https://example.com/")

        params_path = tmp_path / "example.com" / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        assert params["url_pattern"] == ""

    @pytest.mark.asyncio()
    async def test_url_pattern_explicit_takes_priority(self, tmp_path: Path) -> None:
        """url_pattern を明示指定した場合は自動生成より優先されること."""
        runner = ScrapyRunner(temp_dir=tmp_path)

        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_process.stderr = _async_lines_iter([])

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await runner.run(
                start_url="https://example.com/docs/guide",
                url_pattern=r"/custom/.*",
            )

        params_path = tmp_path / "example.com" / "spider_params.json"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        assert params["url_pattern"] == r"/custom/.*"

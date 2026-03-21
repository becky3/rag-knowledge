"""Scrapy subprocess ラッパー.

仕様: docs/specs/site-ingest.md

asyncio.create_subprocess_exec で Scrapy を起動し、
プロセスの監視・終了判定を行う。
JOBDIR 指定で中断再開に対応する。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass
class CrawlResult:
    """Scrapy クロールの実行結果."""

    exit_code: int
    output_dir: Path
    jsonl_path: Path
    success: bool
    stderr_tail: str = ""


class ScrapyRunner:
    """Scrapy プロセスの subprocess ラッパー.

    asyncio.create_subprocess_exec で Scrapy Spider を起動し、
    プロセスの終了を待機して結果を返す。
    """

    def __init__(
        self,
        *,
        temp_dir: str | Path,
        delay_sec: float = 0.1,
        max_pages: int = 10000,
        download_timeout: int = 30,
        timeout_sec: float = 0.0,
        error_count: int = 0,
    ) -> None:
        self._temp_dir = Path(temp_dir)
        self._delay_sec = delay_sec
        self._max_pages = max_pages
        self._download_timeout = download_timeout
        self._timeout_sec = timeout_sec
        self._error_count = error_count

    async def run(
        self,
        *,
        start_url: str,
        allowed_domains: str = "",
        url_pattern: str = "",
        max_pages: int | None = None,
        force: bool = False,
    ) -> CrawlResult:
        """Scrapy Spider を subprocess で起動してクロールを実行する.

        Args:
            start_url: クロール開始 URL
            allowed_domains: ドメイン制約（カンマ区切り）
            url_pattern: URL フィルタ正規表現
            max_pages: ページ数上限（None の場合はインスタンス設定値を使用）
            force: True の場合、JOBDIR を削除して最初からクロール

        Returns:
            クロール実行結果
        """
        effective_max_pages = max_pages if max_pages is not None else self._max_pages

        # url_pattern 未指定時: 開始 URL のパスプレフィックスから自動生成
        parsed = urlparse(start_url)
        if not url_pattern:
            path = parsed.path.rstrip("/")
            if path and path != "/":
                # スキーム + ホスト + パスプレフィックスを正規表現エスケープ
                # 末尾スラッシュ有無の両方にマッチするようにする
                base_prefix = f"{parsed.scheme}://{parsed.hostname}{path}"
                url_pattern = f"^{re.escape(base_prefix)}(?:/|$)"
                logger.info("url_pattern を自動生成: %s", url_pattern)

        # ドメインから一時保存ディレクトリを決定
        domain = parsed.hostname or "unknown"
        domain_dir = self._temp_dir / domain

        html_dir = domain_dir / "html"
        jsonl_path = domain_dir / "metadata.jsonl"
        jobdir = domain_dir / "jobdir"

        # --force: ドメインディレクトリ全体をクリア（html/, metadata.jsonl, jobdir/）
        if force and domain_dir.exists():
            logger.info("--force: ドメインディレクトリを削除します: %s", domain_dir)
            try:
                shutil.rmtree(domain_dir)
            except OSError as exc:
                msg = f"--force 指定時にドメインディレクトリの削除に失敗しました: {domain_dir}"
                logger.error(msg, exc_info=True)
                raise RuntimeError(msg) from exc

        # ディレクトリ準備
        html_dir.mkdir(parents=True, exist_ok=True)
        domain_dir.mkdir(parents=True, exist_ok=True)

        # パラメータを JSON ファイルに書き出し（コードインジェクション防止）
        params_path = domain_dir / "spider_params.json"
        params = {
            "start_url": start_url,
            "allowed_domains": allowed_domains,
            "url_pattern": url_pattern,
            "output_dir": str(html_dir),
            "jsonl_path": str(jsonl_path),
            "jobdir": str(jobdir),
            "max_pages": effective_max_pages,
            "delay_sec": self._delay_sec,
            "download_timeout": self._download_timeout,
            "timeout_sec": self._timeout_sec,
            "error_count": self._error_count,
        }
        params_path.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")

        # Scrapy 起動スクリプトを構築
        spider_script = self._build_spider_script(params_path=params_path)

        # 環境変数の準備（Windows エンコーディング対策）
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        logger.info(
            "Scrapy Spider を起動: url=%s, max_pages=%d, delay=%.2f",
            start_url,
            effective_max_pages,
            self._delay_sec,
        )

        # subprocess 起動（stdout は /dev/null、stderr のみ末尾を保持）
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            spider_script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # stderr をストリーミングで読み、末尾20行のみ保持
        stderr_lines: list[str] = []
        max_tail = 20
        assert process.stderr is not None  # noqa: S101
        async for raw_line in process.stderr:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            stderr_lines.append(line)
            if len(stderr_lines) > max_tail:
                stderr_lines.pop(0)

        exit_code = await process.wait()
        stderr_tail = "\n".join(stderr_lines)

        if exit_code != 0:
            logger.error(
                "Scrapy プロセスが異常終了: exit_code=%d", exit_code,
            )
            if stderr_tail:
                logger.error("Scrapy stderr (末尾):\n%s", stderr_tail)

        success = exit_code == 0

        logger.info(
            "Scrapy プロセス終了: exit_code=%d, success=%s",
            exit_code,
            success,
        )

        return CrawlResult(
            exit_code=exit_code,
            output_dir=html_dir,
            jsonl_path=jsonl_path,
            success=success,
            stderr_tail=stderr_tail,
        )

    def _build_spider_script(self, *, params_path: Path) -> str:
        """Scrapy Spider を実行するインラインスクリプトを構築する.

        コードインジェクション防止のため、ユーザー入力は JSON ファイル経由で渡す。
        インラインスクリプトにはファイルパス（内部生成値）と src_dir のみ埋め込む。
        """
        # src/ ディレクトリのパスを計算
        # runner.py は src/rag/scrapy/runner.py にあるので、3階層上が src/
        src_dir = str(
            Path(__file__).resolve().parent.parent.parent
        ).replace("\\", "/")

        # params_path は内部生成値のため安全
        safe_params_path = str(params_path).replace("\\", "/")

        return f"""\
import json
import sys

# rag パッケージが import できるように src/ を sys.path に追加
src_dir = {json.dumps(src_dir)}
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

# パラメータを JSON ファイルから読み込み（コードインジェクション防止）
with open({json.dumps(safe_params_path)}, encoding='utf-8') as f:
    params = json.load(f)

from scrapy.crawler import CrawlerProcess
from rag.scrapy.spider import SiteSpider
from rag.scrapy.middleware import SsrfMiddleware

settings = {{
    # ハード制約（Spider 実装変更で無効化されないよう Runner 側で明示）
    'ROBOTSTXT_OBEY': True,
    'TELNETCONSOLE_ENABLED': False,
    'DOWNLOADER_MIDDLEWARES': {{
        SsrfMiddleware: 50,
    }},
    # クロール設定
    'JOBDIR': params['jobdir'],
    'FEEDS': {{
        params['jsonl_path']: {{
            'format': 'jsonlines',
            'encoding': 'utf-8',
            'overwrite': False,
        }},
    }},
    'DOWNLOAD_DELAY': params['delay_sec'],
    'DOWNLOAD_TIMEOUT': params['download_timeout'],
    'CLOSESPIDER_PAGECOUNT': params['max_pages'],
    'LOG_LEVEL': 'INFO',
}}
if params.get('timeout_sec'):
    settings['CLOSESPIDER_TIMEOUT'] = params['timeout_sec']
if params.get('error_count'):
    settings['CLOSESPIDER_ERRORCOUNT'] = params['error_count']

process = CrawlerProcess(settings=settings)

process.crawl(
    SiteSpider,
    start_url=params['start_url'],
    allowed_domains=params['allowed_domains'],
    url_pattern=params['url_pattern'],
    output_dir=params['output_dir'],
)
process.start()
"""

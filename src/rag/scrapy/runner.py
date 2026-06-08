"""Scrapy subprocess ラッパー.

仕様: docs/specs/site-ingest.md

asyncio.create_subprocess_exec で Scrapy を起動し、
プロセスの監視・終了判定を行う。
JOBDIR 指定で中断再開に対応する。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

from rag.scrapy._constants import WEB_EXTENSIONS

if TYPE_CHECKING:
    from rag.config import RAGSettings

logger = logging.getLogger(__name__)


def _crawl_key(start_url: str, url_pattern: str) -> str:
    """クロールパラメータから一意のキーを生成する.

    同じ start_url + url_pattern の組み合わせは同じキーを返す。
    異なる組み合わせは異なるキーを返し、JOBDIR の分離を保証する。
    """
    raw = f"{start_url}\n{url_pattern}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _multi_url_key(urls: list[str]) -> str:
    """複数 URL リストから一意のキーを生成する."""
    raw = "\n".join(sorted(urls))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class CrawlResult:
    """Scrapy クロールの実行結果."""

    exit_code: int
    output_dir: Path
    jsonl_path: Path
    success: bool
    crawl_dir: Path | None = None
    stderr_tail: str = ""

    def cleanup(self) -> None:
        """クロールディレクトリを削除する（正常完了後のクリーンアップ）.

        仕様: docs/specs/site-ingest.md「正常完了後のクリーンアップ」
        削除失敗時は警告ログを出力し、例外を送出しない。
        """
        crawl_dir = self.crawl_dir
        if not crawl_dir or not crawl_dir.exists():
            return
        try:
            shutil.rmtree(crawl_dir)
            # 親ディレクトリ（ドメイン）が空なら削除
            parent = crawl_dir.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except FileNotFoundError:
            # 競合状態などで既に削除されていた場合は正常扱い
            return
        except OSError:
            logger.warning(
                "クロールディレクトリの削除に失敗しました: %s",
                crawl_dir,
                exc_info=True,
            )


class ScrapyRunner(Protocol):
    """Scrapy 実行の抽象 Port.

    Real / Fake で同じシグネチャを実装する。Real は subprocess で Scrapy Spider を
    起動して JSONL + HTML を生成し、Fake は fixture から相当ファイル群を tmp に
    展開する。戻り値はいずれの実装でも ``CrawlResult`` 型に揃える。
    """

    async def run(
        self,
        *,
        start_url: str = "",
        start_urls: list[str] | None = None,
        allowed_domains: str = "",
        url_pattern: str = "",
        max_pages: int | None = None,
        restart: bool = False,
    ) -> CrawlResult:
        """Scrapy クロールを実行する.

        モードは引数で明示的に切り替える。``start_url`` 指定はクロールモード
        （リンク辿りあり、単一 URL 起点）、``start_urls`` 指定は取得モード
        （リンク辿りなし、指定 URL のみ取得）。両方指定・両方未指定は ValueError。

        Args:
            start_url: クロール開始 URL（クロールモード、``start_urls`` と排他）
            start_urls: 取得対象 URL のリスト（取得モード、``start_url`` と排他）
            allowed_domains: ドメイン制約（カンマ区切り）
            url_pattern: URL フィルタ正規表現（クロールモードのみ）
            max_pages: ページ数上限（None の場合は実装依存のデフォルト）
            restart: True の場合、JOBDIR + 一時 HTML/JSONL を削除して最初から
                再実行（クロールモードのみ有効。取得モードでは無視）

        Returns:
            クロール実行結果
        """
        ...


class RealScrapyRunner:
    """Scrapy プロセスの subprocess ラッパー（Real 実装）.

    asyncio.create_subprocess_exec で Scrapy Spider を起動し、
    プロセスの終了を待機して結果を返す。
    """

    def __init__(
        self,
        *,
        temp_dir: str | Path,
        delay_sec: float,
        max_pages: int,
        download_timeout: int,
        timeout_sec: float,
        error_count: int,
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
        start_url: str = "",
        start_urls: list[str] | None = None,
        allowed_domains: str = "",
        url_pattern: str = "",
        max_pages: int | None = None,
        restart: bool = False,
    ) -> CrawlResult:
        """Scrapy Spider を subprocess で起動してクロールを実行する.

        モードは引数で明示的に切り替える。件数ヒューリスティック（Issue #797）は
        廃止済み。``start_url`` 指定 ⇒ クロールモード、``start_urls`` 指定 ⇒
        取得モード（リンク辿りなし）。

        Args:
            start_url: クロール開始 URL（クロールモード、``start_urls`` と排他）
            start_urls: 取得対象 URL のリスト（取得モード、``start_url`` と排他）
            allowed_domains: ドメイン制約（カンマ区切り）
            url_pattern: URL フィルタ正規表現（クロールモードのみ）
            max_pages: ページ数上限（None の場合はインスタンス設定値を使用）
            restart: True の場合、JOBDIR + 一時 HTML/JSONL を削除して最初から
                再実行（クロールモードのみ有効。取得モードでは無視）

        Returns:
            クロール実行結果
        """
        _urls = start_urls or []
        if start_url and _urls:
            raise ValueError("start_url と start_urls は排他です")
        if not start_url and not _urls:
            raise ValueError("start_url または start_urls は必須です")

        fetch_mode = bool(_urls)

        if fetch_mode:
            # 取得モード: リンク辿りなし、URL リストをそのまま取得
            effective_start_urls = _urls
            effective_start_url = ""
            no_follow = True
            effective_max_pages = 0  # 無制限（URL 数 = ページ数）
            url_pattern = ""  # パターンフィルタ無効
        else:
            # クロールモード: 単一 URL 起点、リンク辿りあり
            effective_start_url = start_url
            effective_start_urls = []
            no_follow = False
            effective_max_pages = max_pages if max_pages is not None else self._max_pages

        # クロールモード: url_pattern 未指定時は自動生成
        if not fetch_mode:
            parsed = urlparse(effective_start_url)
            if not url_pattern:
                path = parsed.path.rstrip("/")
                if path and path != "/":
                    # パス末尾セグメントが Web 系拡張子で終わる場合は親ディレクトリまで丸める
                    # （同ディレクトリ内の他ファイル展開のため）。判定は _constants.py の
                    # WEB_EXTENSIONS を共有し、HTML 系コンテンツのみを対象とすることで
                    # バージョン番号風セグメント（/api/v1.0 等）の誤検出を防ぐ
                    head, _, last_segment = path.rpartition("/")
                    suffix = Path(last_segment).suffix.lower()
                    if suffix in WEB_EXTENSIONS and head:
                        # head が空（ルート直下ファイル URL）の場合は丸めず従来挙動を保持
                        path = head
                if path and path != "/":
                    base_prefix = f"{parsed.scheme}://{parsed.hostname}{path}"
                    url_pattern = f"^{re.escape(base_prefix)}(?:/|$)"
                    logger.info("url_pattern を自動生成: %s", url_pattern)

        # 一時保存ディレクトリの決定
        if fetch_mode:
            domain = "_multi_"
            key = _multi_url_key(effective_start_urls)
        else:
            parsed = urlparse(effective_start_url)
            domain = parsed.hostname or "unknown"
            key = _crawl_key(effective_start_url, url_pattern)

        crawl_dir = self._temp_dir / domain / key
        html_dir = crawl_dir / "html"
        jsonl_path = crawl_dir / "metadata.jsonl"
        jobdir = crawl_dir / "jobdir"

        # --restart: クロールディレクトリ全体をクリア（クロールモードのみ有効）
        if restart and not fetch_mode and crawl_dir.exists():
            logger.info("--restart: クロールディレクトリを削除します: %s", crawl_dir)
            try:
                shutil.rmtree(crawl_dir)
            except OSError as exc:
                msg = (
                    f"--restart 指定時にクロールディレクトリの削除に失敗しました: {crawl_dir}"
                )
                logger.error(msg, exc_info=True)
                raise RuntimeError(msg) from exc

        # 取得モード: 前回の一時ディレクトリをクリア（レジューム不要）
        if fetch_mode and crawl_dir.exists():
            try:
                shutil.rmtree(crawl_dir)
            except OSError:
                logger.warning(
                    "取得モードの一時ディレクトリ削除に失敗: %s", crawl_dir, exc_info=True,
                )

        # ディレクトリ準備（html_dir.mkdir で親の crawl_dir も自動生成される）
        html_dir.mkdir(parents=True, exist_ok=True)

        # パラメータを JSON ファイルに書き出し（コードインジェクション防止）
        params_path = crawl_dir / "spider_params.json"
        params: dict[str, Any] = {
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
            "no_follow": no_follow,
        }
        if fetch_mode:
            params["start_urls_json"] = json.dumps(effective_start_urls)
            params["start_url"] = ""
        else:
            params["start_url"] = effective_start_url
            params["start_urls_json"] = ""

        params_path.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")

        # Scrapy 起動スクリプトを構築
        spider_script = self._build_spider_script(params_path=params_path)

        # 環境変数の準備（Windows エンコーディング対策）
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        if fetch_mode:
            preview = ", ".join(effective_start_urls[:3])
            log_url = f"{preview}..." if len(effective_start_urls) > 3 else preview
            # 取得モードは max_pages=0（無制限）固定。値だけだと「上限ゼロ」と誤読されうるため補足する
            max_pages_display = "0 (mode=fetch, unlimited)"
        else:
            log_url = effective_start_url
            max_pages_display = str(effective_max_pages)
        logger.info(
            "Scrapy Spider を起動: url=%s, max_pages=%s, delay=%.2f, no_follow=%s",
            log_url,
            max_pages_display,
            self._delay_sec,
            no_follow,
        )

        # stderr をファイルにリダイレクト（Windows で Twisted の子プロセス/スレッドが
        # stderr パイプを継承し、メインプロセス終了後もパイプが閉じない問題を回避）
        stderr_path = crawl_dir / "stderr.log"
        stderr_file = open(stderr_path, "w", encoding="utf-8")  # noqa: SIM115
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                spider_script,
                stdout=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
                stderr=stderr_file,
                env=env,
            )
            exit_code = await process.wait()
        finally:
            stderr_file.close()

        # stderr ファイルから末尾20行を読み取り
        # Twisted / 外部プロセスが FD に直接書く非 UTF-8 バイトで decode 失敗すると
        # 本来の stderr_tail が取れず原因調査が困難になるため、bytes で読んで replace。
        # server.py の _run_cli_subprocess と同じ「stderr 側は decode 失敗で落とさない」方針。
        stderr_text = stderr_path.read_bytes().decode("utf-8", errors="replace")
        stderr_lines = stderr_text.rstrip().splitlines()
        stderr_tail = "\n".join(stderr_lines[-20:])

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
            crawl_dir=crawl_dir,
            stderr_tail=stderr_tail,
        )

    def _build_spider_script(self, *, params_path: Path) -> str:
        """Scrapy Spider を実行するインラインスクリプトを構築する.

        コードインジェクション防止のため、ユーザー入力は JSON ファイル経由で渡す。
        インラインスクリプトにはファイルパス（内部生成値）と src_dir のみ埋め込む。
        """
        # src/ ディレクトリのパスを計算（PROJECT_ROOT 経由で算出、SSoT に統合）
        from rag.config import PROJECT_ROOT
        src_dir = str(PROJECT_ROOT / "src").replace("\\", "/")

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
settings = {{
    # ハード制約（Spider 実装変更で無効化されないよう Runner 側で明示）
    'ROBOTSTXT_OBEY': True,
    'TELNETCONSOLE_ENABLED': False,
    'DOWNLOADER_MIDDLEWARES': {{
        'rag.scrapy.middleware.SsrfMiddleware': 50,
    }},
    # クロール設定（JOBDIR はクロールモードのみ）
    **({{ 'JOBDIR': params['jobdir'] }} if not params.get('no_follow') else {{}}),
    'FEEDS': {{
        params['jsonl_path']: {{
            'format': 'jsonlines',
            'encoding': 'utf-8',
            'overwrite': False,
        }},
    }},
    'DOWNLOAD_DELAY': params['delay_sec'],
    'DOWNLOAD_TIMEOUT': params['download_timeout'],
    # BFS: 同一 depth のページを優先的に取得する
    'DEPTH_PRIORITY': 1,
    'SCHEDULER_DISK_QUEUE': 'scrapy.squeues.PickleFifoDiskQueue',
    'SCHEDULER_MEMORY_QUEUE': 'scrapy.squeues.FifoMemoryQueue',
    'LOG_LEVEL': 'INFO',
}}
if params.get('timeout_sec'):
    settings['CLOSESPIDER_TIMEOUT'] = params['timeout_sec']
if params.get('error_count'):
    settings['CLOSESPIDER_ERRORCOUNT'] = params['error_count']

process = CrawlerProcess(settings=settings)

process.crawl(
    SiteSpider,
    start_url=params.get('start_url', ''),
    start_urls_json=params.get('start_urls_json', ''),
    allowed_domains=params['allowed_domains'],
    url_pattern=params.get('url_pattern', ''),
    output_dir=params['output_dir'],
    max_pages=params.get('max_pages', 0),
    no_follow=params.get('no_follow', False),
)
process.start()
"""


def create_scrapy_runner(settings: RAGSettings) -> ScrapyRunner:
    """Settings から Real / Fake のいずれかを選択して返すファクトリ.

    .env の RAG_WEB_FAKE_MODE が true（または RAG_SCRAPY_FAKE_MODE が true）の場合
    は FakeScrapyRunner を返し、subprocess を起動しない。
    """
    if settings.rag_scrapy_fake_mode:
        from rag.config import PROJECT_ROOT
        from rag.scrapy._fake import FakeScrapyRunner

        fixture_dir = Path(settings.rag_scrapy_fake_fixture_dir)
        if not fixture_dir.is_absolute():
            fixture_dir = PROJECT_ROOT / fixture_dir
        if not fixture_dir.exists():
            raise FileNotFoundError(
                f"Scrapy fake fixture ディレクトリが見つかりません: {fixture_dir}。"
                f"RAG_SCRAPY_FAKE_FIXTURE_DIR を確認してください"
            )
        return FakeScrapyRunner(
            temp_dir=settings.site_ingest_temp_dir,
            fixture_dir=fixture_dir,
        )
    return RealScrapyRunner(
        temp_dir=settings.site_ingest_temp_dir,
        delay_sec=settings.site_ingest_delay_sec,
        max_pages=settings.site_ingest_max_pages,
        download_timeout=settings.site_ingest_download_timeout,
        timeout_sec=settings.site_ingest_timeout_sec,
        error_count=settings.site_ingest_error_count,
    )

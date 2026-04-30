"""site-ingest 実行のコア処理（Python API）.

仕様: docs/specs/site-ingest.md

CLI コマンド ``site-ingest`` および BlueSky インジェスターの URL 自動取り込みから
共通利用される。Scrapy subprocess 経由でクロール → source_store への配置までを
実行し、git commit / インデックス化は呼び出し元の責務とする。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse

from ..scrapy.bridge import BridgeResult, import_to_source_store
from ..scrapy.runner import CrawlResult, ScrapyRunner

if TYPE_CHECKING:
    from ..config import RAGSettings
    from ..store.source_store import SourceStore

logger = logging.getLogger(__name__)


class SiteIngestRunner(Protocol):
    """site-ingest 実行を抽象化する Port.

    仕様: docs/specs/architecture.md
    仕様: docs/specs/site-ingest.md

    bluesky 等の他インジェスターから site-ingest を越境直 import せず、
    Protocol 経由で利用するための抽象。Real 実装（``RealSiteIngestRunner``、
    U2 で追加）は本ファイルの ``execute_site_ingest`` 関数をラップする。
    """

    async def run_for_urls(
        self,
        urls: list[str],
        *,
        source_store: SourceStore,
        settings: RAGSettings,
    ) -> SiteIngestExecution:
        """指定 URL 群に対して site-ingest を実行する.

        URL 値のバリデーション（スキーム、SSRF 等）は呼び出し元で実施済み
        である前提。Real 実装は ``execute_site_ingest`` 関数をそのまま呼び
        出す形になる。

        Args:
            urls: 取得対象 URL のリスト（1 件以上必須）
            source_store: 配置先の SourceStore
            settings: 設定

        Returns:
            実行結果（``SiteIngestExecution``）

        Raises:
            ValueError: ``urls`` が空リストの場合
        """
        ...


@dataclass
class SiteIngestExecution:
    """site-ingest 実行結果.

    ``crawl_result`` を保持するのは、呼び出し元がパイプライン処理の完了後に
    ``crawl_result.cleanup()`` を呼び出せるようにするため。仕様書「処理フロー」の
    順序（クロール → Bridge → パイプライン処理 → クリーンアップ）と整合させる。
    所要時間は呼び出し元（CLI なら全体時間、bluesky なら集計対象外）が独自に
    測るため、本クラスは経過時間を保持しない。
    """

    bridge: BridgeResult = field(default_factory=BridgeResult)
    scrapy_exit_code: int = 0
    scrapy_success: bool = True
    no_output: bool = False
    crawl_result: CrawlResult | None = None


async def execute_site_ingest(
    *,
    urls: list[str],
    source_store: SourceStore,
    settings: RAGSettings,
    url_pattern: str | None = None,
    max_pages: int | None = None,
    force: bool = False,
) -> SiteIngestExecution:
    """site-ingest のコア処理を実行する.

    URL の値そのもののバリデーション（スキーム、SSRF 等）は呼び出し元で実施済み
    である前提。本関数では空リストのみプログラミング契約として弾く。

    Args:
        urls: 取得対象 URL のリスト。**1 件以上必須**（空リストは不可）
        source_store: 配置先の SourceStore
        settings: 設定
        url_pattern: URL フィルタ正規表現（クロールモードのみ有効）
        max_pages: ページ数上限（クロールモードのみ有効）
        force: クロールディレクトリを削除して再実行する

    Returns:
        実行結果。``bridge`` は配置結果、``scrapy_*`` は Scrapy の終了状態、
        ``no_output`` は JSONL が出力されなかった場合に True。

    Raises:
        ValueError: ``urls`` が空リストの場合（呼び出し元の契約違反）
    """
    if not urls:
        raise ValueError("urls must contain at least one URL")

    multi_url_mode = len(urls) >= 2

    if multi_url_mode:
        domains: list[str] = []
        for u in urls:
            hostname = urlparse(u).hostname
            if hostname and hostname not in domains:
                domains.append(hostname)
        allowed_domains = ",".join(domains)
    else:
        parsed = urlparse(urls[0])
        allowed_domains = parsed.hostname or ""

    runner = ScrapyRunner(
        temp_dir=settings.site_ingest_temp_dir,
        delay_sec=settings.site_ingest_delay_sec,
        max_pages=max_pages or settings.site_ingest_max_pages,
        download_timeout=settings.site_ingest_download_timeout,
        timeout_sec=settings.site_ingest_timeout_sec,
        error_count=settings.site_ingest_error_count,
    )

    if multi_url_mode:
        crawl_result = await runner.run(
            start_urls=urls,
            allowed_domains=allowed_domains,
        )
    else:
        crawl_result = await runner.run(
            start_url=urls[0],
            allowed_domains=allowed_domains,
            url_pattern=url_pattern or "",
            max_pages=max_pages,
            force=force,
        )

    execution = SiteIngestExecution(
        scrapy_exit_code=crawl_result.exit_code,
        scrapy_success=crawl_result.success,
        crawl_result=crawl_result,
    )

    if not crawl_result.jsonl_path.exists():
        execution.no_output = True
        return execution

    execution.bridge = import_to_source_store(
        jsonl_path=crawl_result.jsonl_path,
        html_dir=crawl_result.output_dir,
        source_store=source_store,
    )

    return execution


class RealSiteIngestRunner:
    """既存 ``execute_site_ingest`` 関数をラップする実 Runner 実装.

    bluesky 等の他インジェスターから site_ingest_runner モジュールを越境
    直 import せずに site-ingest を起動するための薄いブリッジ。
    """

    async def run_for_urls(
        self,
        urls: list[str],
        *,
        source_store: SourceStore,
        settings: RAGSettings,
    ) -> SiteIngestExecution:
        return await execute_site_ingest(
            urls=urls,
            source_store=source_store,
            settings=settings,
        )


def create_site_ingest_runner() -> SiteIngestRunner:
    """``SiteIngestRunner`` のファクトリ.

    現時点では Real 実装のみ（site-ingest の Fake 化は #705 のスコープ）。
    """
    return RealSiteIngestRunner()

"""WebIngester 本体.

仕様: docs/specs/architecture.md §3.3
仕様: docs/specs/site-ingest.md

Scrapy subprocess 経由で Web ページをクロール → source_store への配置までを
実行する Ingester。git commit / インデックス化は呼び出し元の責務とする。

ScrapyRunner Protocol を Fetcher 相当依存として注入し、bridge 層の中間処理は
本クラス内部の実装詳細として隠蔽する（呼び出し元には IngestResult のみが見える）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from rag.pipeline.ingesters._common import IngestResult
from rag.scrapy.bridge import import_to_source_store
from rag.scrapy.runner import CrawlResult, ScrapyRunner

if TYPE_CHECKING:
    from rag.config import RAGSettings
    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)


@dataclass
class SiteIngestExecution:
    """WebIngester 実行結果.

    ``crawl_result`` を保持するのは、呼び出し元がパイプライン処理の完了後に
    ``crawl_result.cleanup()`` を呼び出せるようにするため。仕様書「処理フロー」の
    順序（クロール → Bridge → パイプライン処理 → クリーンアップ）と整合させる。
    所要時間は呼び出し元（CLI なら全体時間、bluesky なら集計対象外）が独自に
    測るため、本クラスは経過時間を保持しない。

    ``ingest`` / ``total_lines`` / ``parse_errors`` は Bridge 処理結果の展開。
    Bridge の中間型（``BridgeResult``）は ``bridge.py`` 内部に隠蔽され、
    呼び出し元は ``IngestResult`` を直接参照する。
    """

    ingest: IngestResult = field(default_factory=IngestResult)
    total_lines: int = 0
    parse_errors: int = 0
    scrapy_exit_code: int = 0
    scrapy_success: bool = True
    no_output: bool = False
    crawl_result: CrawlResult | None = None


class WebIngester:
    """Web ページ取り込み Ingester.

    仕様: docs/specs/architecture.md §3.3
    仕様: docs/specs/site-ingest.md

    ScrapyRunner Protocol を Fetcher 相当依存として注入する。bridge 層は
    本クラス内部実装として隠蔽され、呼び出し元には IngestResult のみが見える。
    他 Ingester からの委譲は ``WebDelegator`` Protocol 経由で行う。
    """

    def __init__(self, *, scrapy_runner: ScrapyRunner) -> None:
        self._scrapy_runner = scrapy_runner

    async def crawl_urls(
        self,
        urls: list[str],
        *,
        source_store: SourceStore,
        settings: RAGSettings,  # noqa: ARG002
        url_pattern: str | None = None,
        max_pages: int | None = None,
        force: bool = False,
    ) -> SiteIngestExecution:
        """指定 URL 群を Scrapy 経由でクロールし source_store に配置する.

        URL 値のバリデーション（スキーム、SSRF 等）は呼び出し元で実施済み
        である前提。本メソッドでは空リストのみプログラミング契約として弾く。

        Args:
            urls: 取得対象 URL のリスト。**1 件以上必須**（空リストは不可）
            source_store: 配置先の SourceStore
            settings: 設定（互換性のため受け取るが、ScrapyRunner は注入済み）
            url_pattern: URL フィルタ正規表現（クロールモードのみ有効）
            max_pages: ページ数上限（クロールモードのみ有効）
            force: クロールディレクトリを削除して再実行する

        Returns:
            実行結果。``ingest`` は配置結果、``scrapy_*`` は Scrapy の終了状態、
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

        if multi_url_mode:
            crawl_result = await self._scrapy_runner.run(
                start_urls=urls,
                allowed_domains=allowed_domains,
            )
        else:
            crawl_result = await self._scrapy_runner.run(
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

        bridge_result = import_to_source_store(
            jsonl_path=crawl_result.jsonl_path,
            html_dir=crawl_result.output_dir,
            source_store=source_store,
        )
        execution.ingest = bridge_result.ingest
        execution.total_lines = bridge_result.total_lines
        execution.parse_errors = bridge_result.parse_errors

        return execution

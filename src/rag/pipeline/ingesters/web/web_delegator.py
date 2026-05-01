"""WebDelegator Protocol — 他 Ingester から WebIngester への委譲 Port.

仕様: docs/specs/architecture.md §3.3

bluesky 等の他 Ingester から WebIngester を越境直 import せず、
Protocol 経由で利用するための抽象。Real 実装（``RealWebDelegator``）は
``WebIngester`` をラップする。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from rag.pipeline.ingesters.web._facade import SiteIngestExecution, WebIngester
from rag.scrapy.runner import create_scrapy_runner

if TYPE_CHECKING:
    from rag.config import RAGSettings
    from rag.store.source_store import SourceStore


class WebDelegator(Protocol):
    """WebIngester への委譲を抽象化する Port.

    仕様: docs/specs/architecture.md §3.3

    bluesky 等の他 Ingester からの委譲経路として用いる。Real 実装は
    ``WebIngester.crawl_urls`` をそのまま呼び出す薄いブリッジ。

    ``source_store`` は実装時にバインド済み（factory 経由で WebIngester
    に注入される）のため、呼び出しごとに渡す必要はない。
    """

    async def run_for_urls(
        self,
        urls: list[str],
    ) -> SiteIngestExecution:
        """指定 URL 群に対して WebIngester による取り込みを実行する.

        URL 値のバリデーション（スキーム、SSRF 等）は呼び出し元で実施済み
        である前提。

        Args:
            urls: 取得対象 URL のリスト（1 件以上必須）

        Returns:
            実行結果（``SiteIngestExecution``）

        Raises:
            ValueError: ``urls`` が空リストの場合
        """
        ...


class RealWebDelegator:
    """``WebIngester`` をラップする実 Delegator 実装.

    bluesky 等の他 Ingester から WebIngester を越境直 import せずに
    web 取り込みを起動するための薄いブリッジ。
    """

    def __init__(self, *, web_ingester: WebIngester) -> None:
        self._web_ingester = web_ingester

    async def run_for_urls(
        self,
        urls: list[str],
    ) -> SiteIngestExecution:
        return await self._web_ingester.crawl_urls(urls=urls)


def create_web_delegator(
    settings: RAGSettings,
    source_store: SourceStore,
) -> WebDelegator:
    """``WebDelegator`` のファクトリ.

    内部で ``ScrapyRunner`` を生成し ``WebIngester`` を組み立てる。
    ``source_store`` は WebIngester のコンストラクタにバインドされるため、
    呼び出しごとに渡す必要はない。
    """
    scrapy_runner = create_scrapy_runner(settings)
    web_ingester = WebIngester(source_store, scrapy_runner=scrapy_runner)
    return RealWebDelegator(web_ingester=web_ingester)

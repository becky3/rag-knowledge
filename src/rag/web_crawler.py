"""Webページクローラー

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urldefrag, urljoin, urlparse
from typing import Any
from urllib.robotparser import RobotFileParser

import aiohttp
from bs4 import BeautifulSoup
from charset_normalizer import from_bytes
from markdownify import MarkdownConverter

from rag.safety.constrained_client import (
    ConstrainedClient,
    _clamp_request_interval,
    _clamp_request_timeout,
)

logger = logging.getLogger(__name__)

USER_AGENT = "RAGKnowledgeBot"

# max_pages の許容上限（ハードリミット）
_HARD_LIMIT_MAX_PAGES = 500


@dataclass
class _RobotsCacheEntry:
    """robots.txt キャッシュエントリ."""

    parser: RobotFileParser
    fetched_at: float
    crawl_delay: float | None = field(default=None)


class _RagMarkdownConverter(MarkdownConverter):  # type: ignore[misc]
    """RAG用カスタムMarkdownコンバーター.

    リンクURLと画像URLを除去し、テキスト情報のみを保持する。
    RAGではリンク先URLはチャンクサイズの無駄遣いとなり、
    出典情報は source_url メタデータで管理するため不要。
    """

    def convert_a(self, el: Any, text: str, convert_as_inline: bool) -> str:
        """リンクはテキストのみ保持（URLは出典管理で別途管理）."""
        return text or ""

    def convert_img(self, el: Any, text: str, convert_as_inline: bool) -> str:
        """画像タグはalt属性のみ保持（RAGでは画像不要）."""
        alt: str = el.attrs.get("alt", None) or ""
        return alt


@dataclass
class CrawlPreviewPage:
    """クロールプレビュー結果（タイトルとURLのみ）."""

    url: str
    title: str


@dataclass
class CrawledPage:
    """クロール結果."""

    url: str
    title: str
    text: str  # Markdown形式テキスト
    crawled_at: str  # ISO 8601 タイムスタンプ


class RobotsChecker:
    """robots.txt の取得・解析・キャッシュを管理する.

    仕様: docs/specs/rag-knowledge.md
    """

    def __init__(self, cache_ttl: int = 3600) -> None:
        """RobotsCheckerを初期化する.

        Args:
            cache_ttl: robots.txt キャッシュの有効期間（秒）
        """
        self._cache: dict[str, _RobotsCacheEntry] = {}
        self._cache_ttl = cache_ttl
        self._lock = asyncio.Lock()

    @staticmethod
    def _robots_url(url: str) -> str:
        """URLからrobots.txtのURLを生成する."""
        parsed = urlparse(url)
        if not parsed.scheme or parsed.hostname is None:
            raise ValueError(f"Invalid URL for robots.txt: {url!r}")
        return f"{parsed.scheme}://{parsed.hostname}{':' + str(parsed.port) if parsed.port else ''}/robots.txt"

    @staticmethod
    def _cache_key(url: str) -> str:
        """URLからキャッシュキーを生成する（スキーム+ホスト+ポート）."""
        parsed = urlparse(url)
        if not parsed.scheme or parsed.hostname is None:
            raise ValueError(f"Invalid URL for robots.txt cache key: {url!r}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return f"{parsed.scheme}://{parsed.hostname}:{port}"

    async def _fetch_and_parse(
        self, url: str, client: ConstrainedClient
    ) -> _RobotsCacheEntry:
        """robots.txt を取得して解析する.

        取得に失敗した場合は全てを許可するパーサーを返す（フェイルオープン）。

        Args:
            url: 対象ページのURL
            client: 制約付き HTTP クライアント

        Returns:
            キャッシュエントリ
        """
        robots_url = self._robots_url(url)
        parser = RobotFileParser()
        crawl_delay: float | None = None

        try:
            resp = await client.get(robots_url)
            try:
                if resp.status == 200:
                    text = await resp.text()
                    lines = text.splitlines()
                    parser.parse(lines)
                    crawl_delay = parser.crawl_delay(USER_AGENT)  # type: ignore[assignment]
                    logger.debug("Fetched robots.txt from %s", robots_url)
                else:
                    # 404等: robots.txt が存在しない → 全て許可
                    parser.parse([])
                    logger.debug(
                        "robots.txt not found at %s (status=%d), allowing all",
                        robots_url,
                        resp.status,
                    )
            finally:
                resp.release()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # 取得失敗 → フェイルオープン
            parser.parse([])
            logger.warning(
                "Failed to fetch robots.txt from %s, allowing all (fail-open)",
                robots_url,
            )
        except Exception:
            # 安全例外（BudgetExhaustedError, CircuitBreakerOpenError 等）含む
            parser.parse([])
            logger.warning(
                "Unexpected error fetching robots.txt from %s, allowing all",
                robots_url,
                exc_info=True,
            )

        return _RobotsCacheEntry(
            parser=parser,
            fetched_at=time.monotonic(),
            crawl_delay=crawl_delay,
        )

    async def _get_entry(
        self, url: str, client: ConstrainedClient
    ) -> _RobotsCacheEntry:
        """キャッシュからエントリを取得するか、新規に取得する.

        Args:
            url: 対象ページのURL
            client: 制約付き HTTP クライアント

        Returns:
            キャッシュエントリ
        """
        key = self._cache_key(url)

        async with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                elapsed = time.monotonic() - entry.fetched_at
                if elapsed < self._cache_ttl:
                    return entry

        # キャッシュミスまたは期限切れ: 再取得
        new_entry = await self._fetch_and_parse(url, client)

        async with self._lock:
            self._cache[key] = new_entry

        return new_entry

    async def can_fetch(self, url: str, client: ConstrainedClient) -> bool:
        """指定URLのクロールが robots.txt で許可されているか判定する.

        Args:
            url: 判定対象のURL
            client: 制約付き HTTP クライアント

        Returns:
            クロールが許可されている場合は True
        """
        entry = await self._get_entry(url, client)
        allowed: bool = entry.parser.can_fetch(USER_AGENT, url)
        if not allowed:
            logger.info("robots.txt disallows crawling: %s", url)
        return allowed

    async def get_crawl_delay(
        self, url: str, client: ConstrainedClient
    ) -> float | None:
        """robots.txt で指定された Crawl-delay を取得する.

        Args:
            url: 対象のURL
            client: 制約付き HTTP クライアント

        Returns:
            Crawl-delay 値（秒）。未指定の場合は None
        """
        entry = await self._get_entry(url, client)
        return entry.crawl_delay


class WebCrawler:
    """Webページクローラー.

    仕様: docs/specs/rag-knowledge.md
    全ての外部 HTTP リクエストは ConstrainedClient 経由で実行する。
    """

    def __init__(
        self,
        timeout: float = 30.0,
        max_pages: int = 50,
        crawl_delay: float = 1.0,
        max_concurrent: int = 5,
        respect_robots_txt: bool = True,
        robots_txt_cache_ttl: int = 3600,
    ) -> None:
        """WebCrawlerを初期化する.

        Args:
            timeout: HTTPリクエストのタイムアウト秒数（許容範囲: 1〜120）
            max_pages: 1回のクロールで取得する最大ページ数（許容範囲: 1〜500）
            crawl_delay: 同一ドメインへの連続リクエスト間の待機秒数（許容範囲: 0.5〜60）
            max_concurrent: 同時接続数の上限
            respect_robots_txt: robots.txt を遵守するかどうか
            robots_txt_cache_ttl: robots.txt キャッシュの有効期間（秒）
        """
        self._request_timeout = _clamp_request_timeout(timeout)
        # max_pages をハードリミットにクランプ
        if max_pages > _HARD_LIMIT_MAX_PAGES:
            logger.warning(
                "max_pages=%d がハードリミット %d を超過。クランプします",
                max_pages,
                _HARD_LIMIT_MAX_PAGES,
            )
            max_pages = _HARD_LIMIT_MAX_PAGES
        self._max_pages = max(1, max_pages)
        # crawl_delay をハードリミットにクランプ（上下限とも）
        self._crawl_delay = _clamp_request_interval(crawl_delay)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._respect_robots_txt = respect_robots_txt
        self._robots_checker: RobotsChecker | None = (
            RobotsChecker(cache_ttl=robots_txt_cache_ttl)
            if respect_robots_txt
            else None
        )
        self._md_converter = _RagMarkdownConverter(
            heading_style="ATX",
            table_infer_header=True,
            escape_underscores=False,
            escape_asterisks=False,
        )

    def create_client(self) -> ConstrainedClient:
        """操作用の ConstrainedClient を作成する.

        呼び出し元は async context manager として使用すること::

            async with crawler.create_client() as client:
                page = await crawler.crawl_page(url, client=client)

        Returns:
            設定済みの ConstrainedClient
        """
        return ConstrainedClient(
            request_timeout=self._request_timeout,
            request_interval=self._crawl_delay,
            headers={"User-Agent": USER_AGENT},
        )

    def validate_url(self, url: str) -> str:
        """URL検証・正規化. 問題なければ正規化済みURLを返す.

        検証・正規化内容:
        - URLフラグメント（#以降）を除去
        - スキームが http または https であること
        - ホスト名がプライベートIP/localhost/リンクローカルでないこと（SSRF対策）
        - 検証失敗時は ValueError を送出

        Args:
            url: 検証するURL

        Returns:
            正規化済みURL（フラグメント除去済み）

        Raises:
            ValueError: URL検証に失敗した場合
        """
        # フラグメント除去（#以降を除去して正規化）
        defragmented_url, _ = urldefrag(url)
        parsed = urlparse(defragmented_url)

        # スキーム検証
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"許可されていないスキームです: {parsed.scheme}. http または https のみ許可されます。"
            )

        # ホスト名検証
        hostname = parsed.hostname or ""
        if not hostname:
            raise ValueError("URLにホスト名が含まれていません。")

        # SSRF対策: プライベートIP/localhost/リンクローカルをブロック
        self._validate_hostname_not_private(hostname)

        return defragmented_url

    async def _decode_response(self, resp: aiohttp.ClientResponse) -> str:
        """レスポンスボディをエンコーディング自動検出でデコードする.

        charset_normalizerを使用してエンコーディングを自動検出し、
        日本語サイトのShift_JIS/EUC-JP等にも対応する。

        Args:
            resp: aiohttpのレスポンスオブジェクト

        Returns:
            デコードされたHTML文字列
        """
        raw_bytes = await resp.read()
        detected = from_bytes(raw_bytes).best()
        if detected:
            return str(detected)
        return raw_bytes.decode("utf-8", errors="replace")

    def _validate_hostname_not_private(self, hostname: str) -> None:
        """ホスト名がプライベートIP/localhost/リンクローカルでないことを検証する.

        SSRF対策として、以下のアドレスへのアクセスをブロック:
        - localhost / 127.0.0.0/8 (ループバック)
        - 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 (RFC1918 プライベート)
        - 169.254.0.0/16 (リンクローカル)
        - ::1 (IPv6 ループバック)
        - fc00::/7 (IPv6 ユニークローカル)
        - fe80::/10 (IPv6 リンクローカル)

        Args:
            hostname: 検証するホスト名

        Raises:
            ValueError: プライベートアドレスへのアクセスが検出された場合
        """
        # localhost の文字列チェック
        if hostname.lower() in ("localhost", "localhost.localdomain"):
            raise ValueError("localhost へのアクセスは許可されていません。")

        # DNS解決してIPアドレスを取得
        try:
            # getaddrinfo で IPv4/IPv6 両方を取得
            addr_infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            # DNS解決に失敗した場合は通す（接続時にエラーになる）
            return

        # 全ての解決済みIPアドレスをチェック
        for addr_info in addr_infos:
            ip_str = addr_info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue

            # プライベート/予約済みIPをブロック
            # 注: 順序が重要。is_private は is_loopback/is_link_local を含むため、
            # より具体的なチェックを先に行う
            if ip.is_loopback:
                raise ValueError(
                    f"ループバックアドレス ({ip_str}) へのアクセスは許可されていません。"
                )
            if ip.is_link_local:
                raise ValueError(
                    f"リンクローカルアドレス ({ip_str}) へのアクセスは許可されていません。"
                )
            # IPv4の場合、169.254.0.0/16 (リンクローカル) も追加チェック
            if isinstance(ip, ipaddress.IPv4Address):
                if ip in ipaddress.ip_network("169.254.0.0/16"):
                    raise ValueError(
                        f"リンクローカルアドレス ({ip_str}) へのアクセスは許可されていません。"
                    )
            if ip.is_private:
                raise ValueError(
                    f"プライベートIPアドレス ({ip_str}) へのアクセスは許可されていません。"
                )
            if ip.is_reserved:
                raise ValueError(
                    f"予約済みアドレス ({ip_str}) へのアクセスは許可されていません。"
                )

    def _extract_text(self, html: str) -> tuple[str, str]:
        """HTMLから本文をMarkdown形式で抽出する.

        抽出ロジック:
        1. <script>, <style>, <nav>, <header>, <footer>, <aside>, <noscript> タグを除去
        2. <article> → <main> → <body> の優先順で本文領域を特定
        3. markdownify でHTML→Markdown変換
        4. クリーンアップ（連続空行・行末空白の正規化）

        Args:
            html: HTML文字列

        Returns:
            (title, text) のタプル。textはMarkdown形式。
        """
        soup = BeautifulSoup(html, "html.parser")

        # タイトル抽出
        title = ""
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            title = title_tag.string.strip()

        # 不要なタグを除去
        for tag_name in ("script", "style", "nav", "header", "footer", "aside", "noscript"):
            for tag in soup.find_all(tag_name):
                tag.decompose()

        # 本文領域を特定（優先順: article → main → body）
        content_element = soup.find("article")
        if content_element is None:
            content_element = soup.find("main")
        if content_element is None:
            content_element = soup.find("body")
        if content_element is None:
            content_element = soup

        # HTML→Markdown変換
        markdown_text = self._md_converter.convert_soup(content_element)

        # クリーンアップ
        # 行末空白を除去（空白のみの行も空行に正規化）
        text = re.sub(r"[ \t]+\n", "\n", markdown_text)
        # 連続する空白行を1つにまとめる
        text = re.sub(r"\n{3,}", "\n\n", text)

        return title, text.strip()

    @staticmethod
    def _extract_title(html: str) -> str:
        """HTMLからタイトルのみを抽出する.

        Args:
            html: HTML文字列

        Returns:
            ページタイトル（取得できない場合は空文字列）
        """
        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            return title_tag.string.strip()
        return ""

    async def crawl_index_page(
        self,
        index_url: str,
        url_pattern: str = "",
        *,
        client: ConstrainedClient | None = None,
    ) -> list[str]:
        """リンク集ページ内の <a> タグからURLリストを抽出する（深度1のみ、再帰クロールは行わない）.

        - index_url および抽出したリンクURLを validate_url() で検証
        - 抽出URL数が max_pages を超える場合は先頭 max_pages 件に制限

        Args:
            index_url: リンク集ページのURL
            url_pattern: 正規表現パターンでリンクをフィルタリング（任意）
            client: 共有 ConstrainedClient（None の場合は一時的に作成）

        Returns:
            抽出されたURLのリスト

        Raises:
            ValueError: URL検証に失敗した場合
        """
        if client is not None:
            return await self._crawl_index_page_impl(index_url, url_pattern, client)

        async with self.create_client() as c:
            return await self._crawl_index_page_impl(index_url, url_pattern, c)

    async def _crawl_index_page_impl(
        self,
        index_url: str,
        url_pattern: str,
        client: ConstrainedClient,
    ) -> list[str]:
        """crawl_index_page の実装."""
        # インデックスページのURL検証
        validated_url = self.validate_url(index_url)

        # パターンのコンパイル
        pattern = re.compile(url_pattern) if url_pattern else None

        # ページ取得（SSRF対策: リダイレクト追従を無効化）
        resp = await client.get(validated_url)
        try:
            # リダイレクト応答の場合はログを出して空リストを返す
            if resp.status in (301, 302, 303, 307, 308):
                logger.warning(
                    "Redirect detected (SSRF protection): %s -> %s",
                    index_url,
                    resp.headers.get("Location", "unknown"),
                )
                return []
            if resp.status != 200:
                logger.warning("Failed to fetch index page: %s (status=%d)", index_url, resp.status)
                return []
            html = await self._decode_response(resp)
        finally:
            resp.release()

        # リンク抽出
        soup = BeautifulSoup(html, "html.parser")
        urls: list[str] = []
        seen: set[str] = set()

        # インデックスページのホスト名を取得（同一ドメインチェック用）
        index_hostname = urlparse(validated_url).hostname or ""

        for a_tag in soup.find_all("a", href=True):
            href = a_tag.get("href")
            if not isinstance(href, str):
                continue
            # 相対URLを絶対URLに変換
            absolute_url = urljoin(index_url, href)

            # フラグメント除去（DNS解決を含むvalidate_url()より先に実行）
            normalized_url, _ = urldefrag(absolute_url)

            # 同一ドメインチェック: インデックスページと異なるドメインのリンクはスキップ
            link_hostname = urlparse(normalized_url).hostname or ""
            if link_hostname != index_hostname:
                logger.debug(
                    "Skipping external domain link: %s (index: %s)",
                    link_hostname,
                    index_hostname,
                )
                continue

            # 正規化済みURLで重複スキップ
            if normalized_url in seen:
                continue

            # パターンフィルタリング（正規化済みURLに対して適用）
            if pattern and not pattern.search(normalized_url):
                continue

            # スキーム検証・SSRF対策（DNS解決を含むので最後に実行）
            # 重複・パターンで弾かれたURLには問い合わせしない
            try:
                self.validate_url(normalized_url)
            except ValueError:
                continue

            seen.add(normalized_url)
            urls.append(normalized_url)

            # max_pages に達したら終了
            if len(urls) >= self._max_pages:
                break

        # robots.txt チェック: Disallow されたURLをフィルタリング
        if self._robots_checker and urls:
            allowed_urls: list[str] = []
            for url in urls:
                if await self._robots_checker.can_fetch(url, client):
                    allowed_urls.append(url)
            if len(allowed_urls) < len(urls):
                logger.info(
                    "robots.txt filtered %d URLs out of %d",
                    len(urls) - len(allowed_urls),
                    len(urls),
                )
            urls = allowed_urls

        # バジェット残量に基づいて URL 数を制限
        # 共有 client が渡された場合、インデックスページ取得分を含む先行消費により
        # budget.remaining < max_pages となりうる（例: max_requests=500 で
        # インデックス取得後は remaining=499）
        budget_remaining = client.budget.remaining
        if len(urls) > budget_remaining:
            logger.info(
                "バジェット残量に合わせて URL 数を制限: %d → %d",
                len(urls),
                budget_remaining,
            )
            urls = urls[:budget_remaining]

        return urls

    async def crawl_preview(
        self,
        index_url: str,
        url_pattern: str = "",
    ) -> list[CrawlPreviewPage]:
        """リンク集ページからクロール対象ページのタイトルとURLを一覧取得する.

        実際の取り込み（チャンキング・ベクトル化）は行わず、
        対象ページのタイトルとURLのみを返す。

        Args:
            index_url: リンク集ページのURL
            url_pattern: 正規表現パターンでリンクをフィルタリング（任意）

        Returns:
            CrawlPreviewPage のリスト（タイトルとURL）

        Raises:
            ValueError: URL検証に失敗した場合
        """
        async with self.create_client() as client:
            # crawl_index_page でリンク抽出（URL検証・robots.txt チェック込み）
            urls = await self._crawl_index_page_impl(index_url, url_pattern, client)

            if not urls:
                return []

            # 各URLのタイトルを取得（セマフォで同時接続数を制限）
            title_tasks = [
                asyncio.create_task(self._fetch_title(url, client))
                for url in urls
            ]
            titles = await asyncio.gather(*title_tasks)

        results: list[CrawlPreviewPage] = [
            CrawlPreviewPage(url=url, title=title)
            for url, title in zip(urls, titles)
        ]

        return results

    async def _fetch_title(
        self, url: str, client: ConstrainedClient
    ) -> str:
        """URLからページタイトルのみを取得する.

        タイトル取得に失敗した場合は空文字列を返す（処理を中断しない）。

        Args:
            url: タイトルを取得するURL
            client: 制約付き HTTP クライアント

        Returns:
            ページタイトル（取得失敗時は空文字列）
        """
        try:
            async with self._semaphore:
                resp = await client.get(url)
                try:
                    if resp.status in (301, 302, 303, 307, 308):
                        logger.debug(
                            "Redirect detected during title fetch: %s", url
                        )
                        return ""
                    if resp.status != 200:
                        logger.debug(
                            "Failed to fetch title: %s (status=%d)", url, resp.status
                        )
                        return ""
                    html = await self._decode_response(resp)
                finally:
                    resp.release()

            return self._extract_title(html)
        except (asyncio.TimeoutError, aiohttp.ClientError):
            logger.debug("Error fetching title for %s", url)
            return ""
        except Exception:
            logger.debug("Unexpected error fetching title for %s", url, exc_info=True)
            return ""

    async def crawl_page(
        self,
        url: str,
        *,
        client: ConstrainedClient | None = None,
    ) -> CrawledPage | None:
        """単一ページの本文テキストを取得する. 失敗時は None.

        - validate_url() でURL検証後にHTTPアクセスを行う
        - robots.txt が有効な場合、Disallow パスはスキップする

        Args:
            url: クロールするURL
            client: 共有 ConstrainedClient（None の場合は一時的に作成）

        Returns:
            CrawledPage オブジェクト、または失敗時は None
        """
        if client is not None:
            return await self._crawl_page_impl(url, client)

        async with self.create_client() as c:
            return await self._crawl_page_impl(url, c)

    async def _crawl_page_impl(
        self, url: str, client: ConstrainedClient
    ) -> CrawledPage | None:
        """crawl_page の実装."""
        try:
            validated_url = self.validate_url(url)
        except ValueError as e:
            logger.warning("URL validation failed: %s - %s", url, e)
            return None

        # robots.txt チェック
        if self._robots_checker:
            if not await self._robots_checker.can_fetch(validated_url, client):
                return None

        try:
            async with self._semaphore:
                # SSRF対策: リダイレクト追従を無効化
                resp = await client.get(validated_url)
                try:
                    # リダイレクト応答の場合はログを出して None を返す
                    if resp.status in (301, 302, 303, 307, 308):
                        logger.warning(
                            "Redirect detected (SSRF protection): %s -> %s",
                            url,
                            resp.headers.get("Location", "unknown"),
                        )
                        return None
                    if resp.status != 200:
                        logger.warning("Failed to fetch page: %s (status=%d)", url, resp.status)
                        return None
                    html = await self._decode_response(resp)
                finally:
                    resp.release()

            title, text = self._extract_text(html)
            crawled_at = datetime.now(tz=timezone.utc).isoformat()

            return CrawledPage(
                url=validated_url,
                title=title,
                text=text,
                crawled_at=crawled_at,
            )
        except asyncio.TimeoutError:
            logger.warning("Timeout while fetching page: %s", url)
            return None
        except aiohttp.ClientError as e:
            logger.warning("HTTP error while fetching page: %s - %s", url, e)
            return None
        except Exception:
            logger.exception("Unexpected error while fetching page: %s", url)
            return None

    async def _get_effective_crawl_delay(
        self, url: str, client: ConstrainedClient
    ) -> float:
        """設定値と robots.txt の Crawl-delay のうち大きい方を返す.

        Args:
            url: 対象のURL
            client: 制約付き HTTP クライアント

        Returns:
            適用すべきクロール遅延（秒）
        """
        delay = self._crawl_delay
        if self._robots_checker:
            robots_delay = await self._robots_checker.get_crawl_delay(url, client)
            if robots_delay is not None and robots_delay > delay:
                # robots.txt 由来の値も許容範囲にクランプ
                clamped_robots_delay = _clamp_request_interval(robots_delay)
                logger.debug(
                    "Using robots.txt Crawl-delay=%.1f (> configured %.1f) for %s",
                    clamped_robots_delay,
                    delay,
                    urlparse(url).hostname,
                )
                delay = clamped_robots_delay
        return delay

    async def crawl_pages(
        self,
        urls: list[str],
        *,
        client: ConstrainedClient | None = None,
    ) -> list[CrawledPage]:
        """複数ページを並行クロールする.

        - Semaphore により同時接続数を max_concurrent に制限
        - 同一ドメインへの連続リクエスト間に crawl_delay 秒の待機を挿入（負荷軽減）
        - robots.txt の Crawl-delay が設定値より大きい場合はそちらを採用
        - ページ単位でエラーを隔離し、他のページの処理は継続

        Args:
            urls: クロールするURLのリスト
            client: 共有 ConstrainedClient（None の場合は一時的に作成）

        Returns:
            クロールに成功したページのリスト
        """
        if not urls:
            return []

        if client is not None:
            return await self._crawl_pages_impl(urls, client)

        async with self.create_client() as c:
            return await self._crawl_pages_impl(urls, c)

    async def _crawl_pages_impl(
        self,
        urls: list[str],
        client: ConstrainedClient,
    ) -> list[CrawledPage]:
        """crawl_pages の実装."""
        # ホストごとの最終リクエスト時刻を管理するロック付き辞書
        last_request_time: dict[str, float] = {}
        time_lock = asyncio.Lock()

        # ホストごとの実効遅延をキャッシュ
        effective_delays: dict[str, float] = {}

        async def crawl_with_delay(url: str) -> CrawledPage | None:
            """同一ドメインへの遅延を挿入してクロールする."""
            hostname = urlparse(url).hostname

            if hostname:
                # 実効遅延を取得（ホスト単位でキャッシュ）
                delay: float
                async with time_lock:
                    if hostname in effective_delays:
                        delay = effective_delays[hostname]
                    else:
                        pass

                # ロック外で遅延を取得（初回のみ）
                if hostname not in effective_delays:
                    delay_value = await self._get_effective_crawl_delay(url, client)
                    async with time_lock:
                        if hostname not in effective_delays:
                            effective_delays[hostname] = delay_value
                        delay = effective_delays[hostname]
                else:
                    delay = effective_delays[hostname]

                if delay > 0:
                    async with time_lock:
                        previous = last_request_time.get(hostname)
                        if previous is not None:
                            now = asyncio.get_running_loop().time()
                            elapsed = now - previous
                            if elapsed < delay:
                                await asyncio.sleep(delay - elapsed)
                        last_request_time[hostname] = asyncio.get_running_loop().time()

            page = await self._crawl_page_impl(url, client)

            # リクエスト後に実際の完了時刻を更新
            if hostname:
                async with time_lock:
                    last_request_time[hostname] = asyncio.get_running_loop().time()

            return page

        # 並行実行（Semaphore は _crawl_page_impl 内で適用される）
        tasks = [crawl_with_delay(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 成功したページのみを収集（例外はログ済みなのでスキップ）
        pages: list[CrawledPage] = []
        for result in results:
            if isinstance(result, CrawledPage):
                pages.append(result)

        return pages

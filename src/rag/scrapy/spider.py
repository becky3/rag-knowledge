"""汎用 Scrapy Spider.

仕様: docs/specs/site-ingest.md

URL・ドメイン制約・URL パターンをパラメータで受け取り、
HTML ファイルを一時保存ディレクトリに保存する。
FEEDS 機能で JSONL にメタデータを出力する。

このモジュールは Scrapy subprocess 内で実行される。
MCP サーバーのプロセスとは分離されている。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urldefrag, urlparse

import scrapy
from scrapy.http import Response


class SiteSpider(scrapy.Spider):  # type: ignore[misc]
    """汎用サイトクロール Spider.

    パラメータ（Spider 引数）で動作をカスタマイズする:
    - start_url: クロール開始 URL
    - allowed_domains: ドメイン制約（カンマ区切り）
    - url_pattern: URL フィルタ正規表現（任意）
    - output_dir: HTML ファイルの保存先ディレクトリ
    """

    name = "site_spider"

    # 固定設定（仕様: 安全制約）
    custom_settings: dict[str, Any] = {
        "ROBOTSTXT_OBEY": True,
        "TELNETCONSOLE_ENABLED": False,
        "REQUEST_FINGERPRINTER_IMPLEMENTATION": "2.7",
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "USER_AGENT": "RAGKnowledgeBot/1.0 (+rag-knowledge)",
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "RETRY_TIMES": 2,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 522, 524, 408, 429],
    }

    def __init__(
        self,
        start_url: str = "",
        allowed_domains: str = "",
        url_pattern: str = "",
        output_dir: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

        if not start_url:
            raise ValueError("start_url は必須です")
        if not output_dir:
            raise ValueError("output_dir は必須です")

        self.start_urls = [start_url]

        # allowed_domains: カンマ区切り文字列をリストに変換
        if allowed_domains:
            self.allowed_domains = [
                d.strip() for d in allowed_domains.split(",") if d.strip()
            ]
        else:
            # start_url からドメインを自動導出
            parsed = urlparse(start_url)
            hostname = parsed.hostname
            if hostname:
                self.allowed_domains = [hostname]

        # URL パターンフィルタ（正規表現）
        self._url_pattern: re.Pattern[str] | None = None
        if url_pattern:
            self._url_pattern = re.compile(url_pattern)

        # HTML 保存先
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # 統計
        self._page_count = 0
        self._error_count = 0

    def start_requests(self) -> Iterator[scrapy.Request]:
        """start_url の重複フィルタを有効にする.

        デフォルトの start_requests は dont_filter=True でリクエストを発行するため、
        parse 内のリンク辿りで同じ URL が再発見されると重複処理される。
        dont_filter=False にすることでフィンガープリントが記録され、重複を防止する。
        """
        for url in self.start_urls:
            yield scrapy.Request(url, dont_filter=False)

    def parse(self, response: Response) -> Iterator[Any]:
        """レスポンスを処理し、HTML を保存してメタデータを yield する."""
        # 非テキストレスポンスのスキップ（仕様: エッジケース）
        content_type = response.headers.get("Content-Type", b"").decode(
            "utf-8", errors="replace"
        )
        if not self._is_text_response(content_type):
            self.logger.info(
                "非テキストレスポンスをスキップ: %s (%s)",
                response.url,
                content_type,
            )
            return

        self._page_count += 1
        self.logger.info(
            "[%d] Crawled: %s (status=%d, size=%d)",
            self._page_count,
            response.url,
            response.status,
            len(response.body),
        )

        # HTML ファイルを保存
        filepath = self._save_html(response)
        if filepath is None:
            # 保存失敗時はメタデータを yield しない（JSONL に不整合な行を書かない）
            yield from self._follow_links(response)
            return

        # JSONL 用メタデータを yield（FEEDS 機能で自動出力）
        title = self._extract_title(response)
        yield {
            "url": response.url,
            "title": title,
            "status": response.status,
            "depth": response.meta.get("depth", 0),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "filepath": str(filepath.relative_to(self._output_dir.resolve())),
        }

        # リンクを辿る
        yield from self._follow_links(response)

    def _is_text_response(self, content_type: str) -> bool:
        """Content-Type がテキスト系かどうか判定する."""
        mime = content_type.split(";")[0].strip().lower()
        return mime.startswith("text/") or mime in (
            "application/xhtml+xml",
            "application/xml",
        )

    def _save_html(self, response: Response) -> Path | None:
        """レスポンスの HTML を一時保存ディレクトリに保存する.

        Returns:
            保存したファイルパス。保存失敗時は None。
        """
        filename = self._url_to_filename(response.url)
        filepath = self._output_dir / filename
        try:
            # パストラバーサル防止
            output_root = self._output_dir.resolve()
            resolved_filepath = filepath.resolve()
            if not resolved_filepath.is_relative_to(output_root):
                self._error_count += 1
                self.logger.warning(
                    "出力ディレクトリ外への書き込みをブロック: %s", resolved_filepath,
                )
                return None

            resolved_filepath.parent.mkdir(parents=True, exist_ok=True)
            resolved_filepath.write_bytes(response.body)
        except OSError:
            self._error_count += 1
            self.logger.exception("ファイル保存に失敗: %s", filepath)
            return None
        return resolved_filepath

    # .html 付加をスキップする Web 系拡張子
    _WEB_EXTENSIONS = {".html", ".htm", ".xhtml", ".shtml", ".php", ".asp", ".aspx", ".jsp"}

    @staticmethod
    def _url_to_filename(url: str) -> str:
        """URL からファイル相対パスを生成する.

        URL パスのディレクトリ構造を維持する。
        Web 系拡張子で終わるパスはそのまま、それ以外は `.html` を付与する。
        例: /docs/api/auth → docs/api/auth.html
            /privacy.html → privacy.html
        """
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        if not path:
            return "index.html"
        suffix = Path(path).suffix.lower()
        if suffix in SiteSpider._WEB_EXTENSIONS:
            return path
        return f"{path}.html"

    def _extract_title(self, response: Response) -> str:
        """HTML レスポンスからタイトルを抽出する."""
        try:
            title = response.css("title::text").get("")
            return str(title).strip() if title else ""
        except Exception:
            self.logger.debug(
                "タイトル抽出に失敗: %s", response.url, exc_info=True,
            )
            return ""

    def _follow_links(self, response: Response) -> Iterator[Any]:
        """ページ内のリンクを辿る."""
        for href in response.css("a::attr(href)").getall():
            full_url, _ = urldefrag(response.urljoin(href))

            parsed = urlparse(full_url)

            # スキームフィルタ: http/https 以外 (mailto:, tel:, javascript: 等) は辿らない
            if parsed.scheme not in ("http", "https"):
                continue

            # クエリを削除して canonical URL に揃える
            canonical_url = parsed._replace(query="").geturl()

            # URL パターンフィルタ（canonical URL に対して適用）
            if self._url_pattern and not self._url_pattern.search(canonical_url):
                continue

            yield scrapy.Request(canonical_url, callback=self.parse)

    def closed(self, reason: str) -> None:
        """Spider 終了時のログ出力."""
        self.logger.info(
            "Spider 終了: reason=%s, pages=%d, errors=%d",
            reason,
            self._page_count,
            self._error_count,
        )

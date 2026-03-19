"""Web インジェスター.

仕様: docs/specs/ingesters/web.md

Web ページを HTTP 経由で取得し、source_store にファイルを配置する。
URL 安全性チェック・robots.txt 遵守・SSRF 対策を含む。
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup

from pathlib import PurePosixPath

from rag.pipeline.ingesters._common import IngestResult, now_iso

# converter が認識する拡張子（変換対象 + パススルー対象）
# この拡張子を持つ URL は .html を付与しない
_KNOWN_WEB_EXTENSIONS: frozenset[str] = frozenset(
    {".html", ".htm", ".pdf", ".json", ".md", ".txt", ".adoc"},
)

if TYPE_CHECKING:

    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)

# ハードリミット: クロール対象ページ数上限
MAX_CRAWL_PAGES_HARD_LIMIT = 500

# Safe Browsing キャッシュ最大エントリ数
SAFE_BROWSING_CACHE_MAX_ENTRIES = 1000

# SSRF 対策: ブロック対象ホスト名
_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})

# SSRF 対策: ブロック対象ネットワーク
_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
]


def _validate_url(url: str) -> str:
    """URL のバリデーションを行う.

    Returns:
        フラグメントを除去した URL

    Raises:
        ValueError: 不正な URL の場合
    """
    if not url or not url.strip():
        raise ValueError("URL が空です")

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"無効な URL スキームです: {parsed.scheme}")

    if not parsed.hostname:
        raise ValueError(f"URL にホスト名がありません: {url}")

    # フラグメント除去
    if parsed.fragment:
        url = url.split("#")[0]

    return url


def _check_ssrf(url: str) -> None:
    """SSRF 対策: プライベート IP・ローカルホストへのリクエストを拒否する.

    Raises:
        ValueError: SSRF の疑いがある場合
    """
    parsed = urlparse(url)
    hostname = parsed.hostname

    if not hostname:
        raise ValueError(f"URL にホスト名がありません: {url}")

    # ホスト名文字列マッチ
    if hostname.lower() in _BLOCKED_HOSTNAMES:
        raise ValueError(
            f"プライベートホストへのアクセスは拒否されています: {hostname}"
        )

    # DNS 解決 + IP 検証
    try:
        addrinfos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"DNS 解決に失敗しました: {hostname}") from e

    for addrinfo in addrinfos:
        addr = addrinfo[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                raise ValueError(
                    f"プライベート IP へのアクセスは拒否されています: "
                    f"{hostname} ({addr})"
                )


def _extract_title(data: bytes) -> str:
    """HTML バイト列からタイトルを抽出する.

    charset_normalizer でエンコーディングを自動検出し、
    BeautifulSoup でタイトルタグを取得する。
    """
    # エンコーディング検出
    try:
        import charset_normalizer
        detected = charset_normalizer.from_bytes(data).best()
        encoding = str(detected.encoding) if detected else "utf-8"
    except ImportError:
        encoding = "utf-8"
    try:
        html_text = data.decode(encoding, errors="replace")
    except Exception:
        html_text = data.decode("utf-8", errors="replace")

    soup = BeautifulSoup(html_text, "html.parser")
    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        return title_tag.string.strip()
    return ""


def _extract_links(html_text: str, base_url: str) -> list[str]:
    """HTML テキストから同一ドメインのリンクを抽出する.

    Args:
        html_text: HTML テキスト
        base_url: ベース URL（リンク解決・ドメイン判定用）

    Returns:
        重複除去済みの URL リスト
    """
    base_parsed = urlparse(base_url)
    base_domain = base_parsed.hostname

    soup = BeautifulSoup(html_text, "html.parser")
    seen: set[str] = set()
    links: list[str] = []

    for a_tag in soup.find_all("a", href=True):
        href = str(a_tag["href"])
        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)

        # 同一ドメインフィルタ
        if parsed.hostname != base_domain:
            continue

        # フラグメント除去
        clean_url = abs_url.split("#")[0]

        if clean_url not in seen:
            seen.add(clean_url)
            links.append(clean_url)

    return links


def _needs_html_extension(url: str) -> bool:
    """URL パスが既知の拡張子を持たない場合に True を返す.

    converter が認識する拡張子（.html, .pdf 等）を既に持つ URL には
    .html を付与しない。拡張子がないか未知の場合のみ .html を付与する。
    """
    path = urlparse(url).path
    ext = PurePosixPath(path).suffix.lower()
    return ext not in _KNOWN_WEB_EXTENSIONS


class WebIngester:
    """Web インジェスター.

    Web ページを HTTP 経由で取得し、source_store にファイルを配置する。
    URL 安全性チェック・robots.txt 遵守・SSRF 対策を含む。
    """

    def __init__(
        self,
        source_store: SourceStore,
        *,
        max_crawl_pages: int = 50,
        crawl_request_timeout: int = 30,
        respect_robots_txt: bool = True,
        robots_txt_cache_ttl: int = 3600,
        url_safety_check: bool = True,
        url_safety_cache_ttl: int = 300,
        url_safety_fail_open: bool = True,
        url_safety_timeout: float = 5.0,
    ) -> None:
        self._store = source_store
        self._max_crawl_pages = min(max_crawl_pages, MAX_CRAWL_PAGES_HARD_LIMIT)
        self._crawl_request_timeout = crawl_request_timeout
        self._respect_robots_txt = respect_robots_txt
        self._robots_txt_cache_ttl = robots_txt_cache_ttl
        self._url_safety_check = url_safety_check
        self._url_safety_cache_ttl = url_safety_cache_ttl
        self._url_safety_fail_open = url_safety_fail_open
        self._url_safety_timeout = url_safety_timeout

        # robots.txt キャッシュ: key = scheme://hostname:port
        self._robots_cache: dict[str, tuple[RobotFileParser | None, float]] = {}

        # Safe Browsing キャッシュ: key = url, value = (is_safe, timestamp)
        self._safety_cache: dict[str, tuple[bool, float]] = {}

    async def add(
        self,
        url: str,
        *,
        client: Any | None = None,
        safe_browsing_api_key: str = "",
    ) -> IngestResult:
        """単一ページを取得して source_store に配置する.

        Args:
            url: 取り込み対象ページの URL
            client: ConstrainedClient インスタンス
            safe_browsing_api_key: Safe Browsing API キー

        Returns:
            配置結果
        """
        result = IngestResult()

        url = _validate_url(url)
        _check_ssrf(url)

        if client is None:
            raise ValueError("client (ConstrainedClient) が必要です")

        # Safe Browsing チェック
        if self._url_safety_check and not safe_browsing_api_key:
            logger.warning("Safe Browsing が有効ですが API キーが未指定です。チェックをスキップします")
        if self._url_safety_check and safe_browsing_api_key:
            is_safe = await self._check_safe_browsing(
                [url], safe_browsing_api_key, client
            )
            if not is_safe.get(url, True):
                logger.warning("Safe Browsing で危険と判定された URL: %s", url)
                result.errors += 1
                result.error_details.append(f"Unsafe URL: {url}")
                return result

        # robots.txt チェック
        if self._respect_robots_txt:
            if not await self._can_fetch(url, client):
                logger.info("robots.txt により拒否されました: %s", url)
                result.skipped += 1
                return result

        # ページ取得（リダイレクトブロック）
        resp = await client.get(
            url,
            follow_redirects=False,
        )
        if 300 <= resp.status_code < 400:
            raise ValueError(
                f"リダイレクトは SSRF 防止のため拒否されています: "
                f"{resp.status_code} ({url})"
            )

        data = resp.content

        # タイトル抽出
        title = _extract_title(data)

        # source_store に配置
        metadata = {
            "source_id": url,
            "source_type": "web",
            "title": title,
            "collected_at": now_iso(),
            "url": url,
        }

        ext = ".html" if _needs_html_extension(url) else ""
        self._store.place_file_from_url(
            url=url,
            data=data,
            metadata=metadata,
            extension=ext,
        )
        result.placed += 1

        return result

    async def crawl(
        self,
        url: str,
        *,
        pattern: str = "",
        client: Any | None = None,
        safe_browsing_api_key: str = "",
    ) -> IngestResult:
        """リンク集ページから一括クロールして source_store に配置する.

        Args:
            url: リンク集ページの URL
            pattern: リンクをフィルタする正規表現パターン
            client: ConstrainedClient インスタンス
            safe_browsing_api_key: Safe Browsing API キー

        Returns:
            配置結果
        """
        result = IngestResult()

        url = _validate_url(url)
        _check_ssrf(url)

        if client is None:
            raise ValueError("client (ConstrainedClient) が必要です")

        # インデックスページ取得
        resp = await client.get(
            url,
            follow_redirects=False,
        )
        if 300 <= resp.status_code < 400:
            raise ValueError(
                f"リダイレクトは SSRF 防止のため拒否されています: "
                f"{resp.status_code} ({url})"
            )

        html_text = resp.content.decode("utf-8", errors="replace")

        # リンク抽出
        links = _extract_links(html_text, url)

        # パターンフィルタ
        if pattern:
            try:
                regex = re.compile(pattern)
            except re.error as e:
                result.errors = 1
                result.error_details.append(f"無効な正規表現パターン: {e}")
                return result
            links = [link for link in links if regex.search(link)]

        # robots.txt フィルタ
        if self._respect_robots_txt:
            filtered: list[str] = []
            for link in links:
                if await self._can_fetch(link, client):
                    filtered.append(link)
                else:
                    logger.info("robots.txt により除外: %s", link)
            links = filtered

        # Safe Browsing 一括チェック
        if self._url_safety_check and not safe_browsing_api_key:
            logger.warning("Safe Browsing が有効ですが API キーが未指定です。チェックをスキップします")
        if self._url_safety_check and safe_browsing_api_key and links:
            safety_results = await self._check_safe_browsing(
                links, safe_browsing_api_key, client
            )
            safe_links: list[str] = []
            for link in links:
                if safety_results.get(link, True):
                    safe_links.append(link)
                else:
                    logger.warning("Safe Browsing で除外: %s", link)
                    result.skipped += 1
            links = safe_links

        # ページ数上限チェック
        if len(links) > self._max_crawl_pages:
            logger.warning(
                "クロール対象ページ数が上限 %d を超えています（%d）。上限で打ち切ります",
                self._max_crawl_pages,
                len(links),
            )
            links = links[: self._max_crawl_pages]

        # 各ページを順次処理
        for link in links:
            try:
                _check_ssrf(link)

                page_resp = await client.get(
                    link,
                    follow_redirects=False,
                )
                if 300 <= page_resp.status_code < 400:
                    logger.warning("リダイレクト（SSRF 防止）: %s", link)
                    result.errors += 1
                    result.error_details.append(f"Redirect blocked: {link}")
                    continue

                page_data = page_resp.content
                title = _extract_title(page_data)

                metadata = {
                    "source_id": link,
                    "source_type": "web",
                    "title": title,
                    "collected_at": now_iso(),
                    "url": link,
                }

                ext = ".html" if _needs_html_extension(link) else ""
                self._store.place_file_from_url(
                    url=link,
                    data=page_data,
                    metadata=metadata,
                    extension=ext,
                )
                result.placed += 1

            except Exception:
                logger.exception("ページの取得に失敗しました: %s", link)
                result.errors += 1
                result.error_details.append(link)

        return result

    async def crawl_preview(
        self,
        url: str,
        *,
        pattern: str = "",
        client: Any | None = None,
    ) -> list[dict[str, str]]:
        """クロール対象ページのタイトル・URL 一覧を返す.

        source_store への配置は行わない。

        Args:
            url: リンク集ページの URL
            pattern: リンクをフィルタする正規表現パターン
            client: ConstrainedClient インスタンス

        Returns:
            タイトルと URL の辞書リスト
        """
        try:
            url = _validate_url(url)
        except ValueError:
            return []

        _check_ssrf(url)

        if client is None:
            raise ValueError("client (ConstrainedClient) が必要です")

        # インデックスページ取得
        resp = await client.get(
            url,
            follow_redirects=False,
        )
        if 300 <= resp.status_code < 400:
            raise ValueError(
                f"リダイレクトは SSRF 防止のため拒否されています: "
                f"{resp.status_code} ({url})"
            )

        html_text = resp.content.decode("utf-8", errors="replace")

        # リンク抽出
        links = _extract_links(html_text, url)

        # パターンフィルタ
        if pattern:
            try:
                regex = re.compile(pattern)
                links = [link for link in links if regex.search(link)]
            except re.error:
                logger.warning("無効な正規表現パターン: %s", pattern)
                return []

        # robots.txt フィルタ
        if self._respect_robots_txt:
            filtered: list[str] = []
            for link in links:
                if await self._can_fetch(link, client):
                    filtered.append(link)
            links = filtered

        # 各ページのタイトル取得
        previews: list[dict[str, str]] = []
        for link in links:
            title = ""
            try:
                page_resp = await client.get(
                    link,
                    follow_redirects=False,
                )
                if page_resp.status_code < 300:
                    title = _extract_title(page_resp.content)
            except Exception:
                logger.debug("タイトル取得に失敗: %s", link)

            previews.append({"title": title, "url": link})

        return previews

    # --- robots.txt ---

    async def _can_fetch(self, url: str, client: Any) -> bool:
        """robots.txt でクロール許可されているかチェックする."""
        parsed = urlparse(url)
        key = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            key += f":{parsed.port}"

        now = time.time()

        # キャッシュヒットチェック
        if key in self._robots_cache:
            rp, cached_at = self._robots_cache[key]
            if now - cached_at < self._robots_txt_cache_ttl:
                if rp is None:
                    return True  # フェイルオープン
                return rp.can_fetch("*", url)

        # robots.txt 取得
        robots_url = f"{key}/robots.txt"
        try:
            resp = await client.get(
                robots_url,
                follow_redirects=False,
            )
            if 300 <= resp.status_code < 400:
                # リダイレクトはフェイルオープン
                logger.debug("robots.txt リダイレクト（フェイルオープン）: %s", robots_url)
                self._robots_cache[key] = (None, now)
                return True
            rp = RobotFileParser()
            rp.parse(resp.text.splitlines())
            self._robots_cache[key] = (rp, now)
            return rp.can_fetch("*", url)
        except Exception:
            logger.debug("robots.txt の取得に失敗（フェイルオープン）: %s", robots_url)
            self._robots_cache[key] = (None, now)
            return True

    # --- Safe Browsing ---

    async def _check_safe_browsing(
        self,
        urls: list[str],
        api_key: str,
        client: Any,
    ) -> dict[str, bool]:
        """Safe Browsing API で URL の安全性をチェックする.

        Returns:
            URL -> is_safe の辞書
        """
        results: dict[str, bool] = {}
        unchecked: list[str] = []
        now = time.time()

        # キャッシュから取得
        for url in urls:
            if url in self._safety_cache:
                is_safe, cached_at = self._safety_cache[url]
                if now - cached_at < self._url_safety_cache_ttl:
                    results[url] = is_safe
                    continue
            unchecked.append(url)

        if not unchecked:
            return results

        # API 呼び出し
        try:
            sb_url = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
            body = {
                "client": {"clientId": "rag-knowledge", "clientVersion": "1.0"},
                "threatInfo": {
                    "threatTypes": [
                        "MALWARE",
                        "SOCIAL_ENGINEERING",
                        "UNWANTED_SOFTWARE",
                        "POTENTIALLY_HARMFUL_APPLICATION",
                    ],
                    "platformTypes": ["ANY_PLATFORM"],
                    "threatEntryTypes": ["URL"],
                    "threatEntries": [{"url": u} for u in unchecked],
                },
            }

            resp = await client.post(
                sb_url,
                params={"key": api_key},
                json=body,
                timeout=self._url_safety_timeout,
            )
            data = resp.json()

            # 脅威検出された URL を特定
            threat_urls: set[str] = set()
            for match in data.get("matches", []):
                threat_url = match.get("threat", {}).get("url", "")
                if threat_url:
                    threat_urls.add(threat_url)

            for u in unchecked:
                is_safe = u not in threat_urls
                results[u] = is_safe
                # キャッシュ更新（TTL 切れエントリを先に削除してから追加）
                if len(self._safety_cache) >= SAFE_BROWSING_CACHE_MAX_ENTRIES:
                    expired = [
                        k for k, (_, ts) in self._safety_cache.items()
                        if now - ts >= self._url_safety_cache_ttl
                    ]
                    for k in expired:
                        del self._safety_cache[k]
                if len(self._safety_cache) < SAFE_BROWSING_CACHE_MAX_ENTRIES:
                    self._safety_cache[u] = (is_safe, now)

        except Exception:
            logger.warning("Safe Browsing API の呼び出しに失敗しました")
            # フェイルオープン/フェイルクローズ
            for u in unchecked:
                results[u] = self._url_safety_fail_open

        return results

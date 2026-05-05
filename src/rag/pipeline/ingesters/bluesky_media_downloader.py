"""BlueSky メディア DL の Protocol（画像 / HLS 動画の抽象化）.

仕様: docs/specs/infrastructure/fake-mode.md
仕様: docs/specs/infrastructure/fake-adapters/bluesky.md
仕様: docs/specs/ingesters/bluesky.md

BlueSky 投稿に添付される画像・HLS 動画の DL 処理を抽象化した Protocol を定義する。
Real 実装は ConstrainedClient をコンストラクタ注入で受け取り、HLS バリアント
選択・SSRF 防止のための同一オリジンリダイレクト追従を内包する。

AT Protocol API アクセス（getAuthorFeed 等）と独立した Protocol として分離する
理由は、メディア DL が AT Protocol AppView ではなく BlueSky CDN への HTTP アクセス
であり、責務・対象・レート制御が異なるため（``docs/specs/architecture.md`` の
「正解パターン: youtube インジェスター」と整合する Protocol 単機能化方針）。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urljoin, urlparse

import httpx

if TYPE_CHECKING:
    from py_common_lib.httpx import ConstrainedClient

    from rag.config import RAGSettings

logger = logging.getLogger(__name__)

_BW_RE = re.compile(r"BANDWIDTH=(\d+)")
_REDIRECT_STATUSES = frozenset({301, 302, 307, 308})


def _base_domain(hostname: str | None) -> str:
    """ホスト名から末尾2セグメント（eTLD+1 相当）を返す.

    ccTLD（.co.uk 等）では正確な eTLD+1 を返さない簡易実装。
    BlueSky CDN のドメイン構成（.app TLD）では問題ない。
    """
    if not hostname:
        return ""
    parts = hostname.rsplit(".", 2)
    if len(parts) >= 2:
        return f"{parts[-2]}.{parts[-1]}"
    return hostname


def _select_hls_variant(
    master_playlist: str,
    master_url: str,
) -> str | None:
    """マスタープレイリストから最低 BANDWIDTH のバリアント URL を返す.

    Vision 解析用途のため低画質で十分。BANDWIDTH 属性をパースし、
    最小値のバリアントを選択する。BANDWIDTH が取得できない場合は
    最初のバリアントにフォールバックする。
    """
    lines = master_playlist.splitlines()
    candidates: list[tuple[int, str]] = []
    fallback_url: str | None = None

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            variant_url: str | None = None
            for j in range(i + 1, len(lines)):
                variant_line = lines[j].strip()
                if variant_line and not variant_line.startswith("#"):
                    variant_url = variant_line
                    i = j
                    break
            if variant_url is not None:
                if fallback_url is None:
                    fallback_url = variant_url
                m = _BW_RE.search(line)
                if m:
                    candidates.append((int(m.group(1)), variant_url))
        i += 1

    if candidates:
        _, best_url = min(candidates, key=lambda c: c[0])
        return urljoin(master_url, best_url)
    if fallback_url is not None:
        return urljoin(master_url, fallback_url)
    return None


async def _get_following_same_origin_redirect(
    client: ConstrainedClient,
    url: str,
    *,
    max_redirects: int = 5,
) -> httpx.Response:
    """同一ベースドメインのリダイレクトのみ追従する GET リクエスト.

    ConstrainedClient の follow_redirects=False を維持しつつ、
    CDN の 3xx リダイレクトに対応する。

    契約: 2xx レスポンスのみを返す。以下のケースはすべて
    httpx.HTTPStatusError を送出する（3xx レスポンスを呼び出し側に漏らさない）:
    - 異ドメインへのリダイレクト（SSRF 防止）
    - リダイレクト回数上限到達
    - 3xx なのに location ヘッダが欠落
    - リダイレクト対象外の 3xx（300 / 303 / 304 等）・4xx・5xx
    """
    original_parsed = urlparse(url)
    original_base = _base_domain(original_parsed.hostname)
    resp = await client.get(url)

    for _ in range(max_redirects):
        if resp.status_code not in _REDIRECT_STATUSES:
            if not resp.is_success:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code} error for url '{url}'",
                    request=resp.request,
                    response=resp,
                )
            return resp
        location = resp.headers.get("location", "")
        if not location:
            raise httpx.HTTPStatusError(
                f"redirect response without location header: {url}",
                request=resp.request,
                response=resp,
            )
        resolved = urljoin(url, location)
        parsed = urlparse(resolved)
        redirect_base = _base_domain(parsed.hostname)
        if parsed.scheme != original_parsed.scheme or redirect_base != original_base:
            logger.warning(
                "リダイレクト先が異なるドメイン（拒否）: %s → %s",
                url,
                resolved,
            )
            raise httpx.HTTPStatusError(
                f"cross-domain redirect rejected: {url} -> {resolved}",
                request=resp.request,
                response=resp,
            )
        url = resolved
        resp = await client.get(url)

    if resp.status_code in _REDIRECT_STATUSES:
        logger.warning("リダイレクト回数上限に到達: %s", url)
        raise httpx.HTTPStatusError(
            f"redirect limit exceeded: {url}",
            request=resp.request,
            response=resp,
        )
    if not resp.is_success:
        raise httpx.HTTPStatusError(
            f"HTTP {resp.status_code} error for url '{url}'",
            request=resp.request,
            response=resp,
        )
    return resp


class BlueskyMediaDownloader(Protocol):
    """BlueSky メディア（画像 / HLS 動画）DL を抽象化する Port.

    HLS 動画 DL はマスタープレイリスト解決・バリアント選択・ts セグメント
    結合を内包する。SSRF 防止のための同一オリジンリダイレクト追従は Real
    実装が担い、Fake 実装はプレイリスト URL を受け取って固定バイトを返す。

    詳細な振る舞いは ``docs/specs/ingesters/bluesky.md`` 「メディア DL と配置」
    セクションを参照。
    """

    async def download_image(
        self,
        url: str,
    ) -> tuple[bytes, str]:
        """画像を DL する.

        Args:
            url: BlueSky CDN の fullsize 画像 URL

        Returns:
            ``(画像バイナリ, Content-Type ヘッダー値)``。Content-Type は
            拡張子決定（``image/webp`` → ``.webp`` 等）に使用される。
        """
        ...

    async def download_hls_video(
        self,
        playlist_url: str,
    ) -> bytes:
        """HLS 動画を DL する（プレイリスト解決 + バリアント選択 + ts 結合）.

        マスタープレイリスト（``#EXT-X-STREAM-INF`` を含む）の場合は
        BANDWIDTH 最小のバリアントを選択する。バリアントプレイリストの
        ts セグメントを順次取得して結合する。

        Args:
            playlist_url: HLS プレイリスト URL（マスター or バリアント）

        Returns:
            結合済み ts バイナリ。
        """
        ...


class HlsVariantNotSelectableError(RuntimeError):
    """マスタープレイリストからバリアントを選択できなかった."""


class HlsPlaylistEmptyError(RuntimeError):
    """HLS プレイリストに ts セグメントが含まれていなかった."""


class RealBlueskyMediaDownloader:
    """ConstrainedClient + 内蔵 HLS ロジックを使う実 MediaDownloader 実装.

    BlueSky CDN への HTTP 通信、SSRF 防止のための同一オリジンリダイレクト
    追従、HLS マスター/バリアント解決、ts セグメント結合をすべて内包する。

    ``ConstrainedClient`` 所有権: Pattern A (CLI 所有)。
    BlueSky 投稿取り込み経路では本 MediaDownloader と ``RealBlueskyFetcher`` が
    委譲先 Adapter（YouTube / Web）と同一 ``ConstrainedClient`` を共有して
    バジェットを合算する必要があるため、上位の起動経路（CLI / MCP）が
    ``ConstrainedClient`` を生成し、本 Adapter にコンストラクタ注入する。
    所有権原則の判断軸は ``docs/specs/architecture.md §6`` を参照。
    """

    def __init__(
        self,
        client: ConstrainedClient,
        *,
        max_redirects: int = 5,
    ) -> None:
        self._client = client
        self._max_redirects = max_redirects

    async def download_image(
        self,
        url: str,
    ) -> tuple[bytes, str]:
        resp = await _get_following_same_origin_redirect(
            self._client, url, max_redirects=self._max_redirects,
        )
        content_type = resp.headers.get("content-type", "")
        return resp.content, content_type

    async def download_hls_video(
        self,
        playlist_url: str,
    ) -> bytes:
        resp = await _get_following_same_origin_redirect(
            self._client, playlist_url, max_redirects=self._max_redirects,
        )
        playlist_text = resp.text

        if "#EXT-X-STREAM-INF" in playlist_text:
            variant_url = _select_hls_variant(playlist_text, playlist_url)
            if variant_url is None:
                raise HlsVariantNotSelectableError(
                    f"HLS variant not selectable: {playlist_url}"
                )
            logger.debug("HLS バリアント選択: %s", variant_url)
            resp = await _get_following_same_origin_redirect(
                self._client, variant_url, max_redirects=self._max_redirects,
            )
            playlist_text = resp.text
            playlist_url = variant_url

        segment_urls: list[str] = []
        for line in playlist_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            segment_urls.append(urljoin(playlist_url, line))

        if not segment_urls:
            raise HlsPlaylistEmptyError(
                f"HLS playlist has no ts segments: {playlist_url}"
            )

        chunks: list[bytes] = []
        for seg_url in segment_urls:
            seg_resp = await _get_following_same_origin_redirect(
                self._client, seg_url, max_redirects=self._max_redirects,
            )
            chunks.append(seg_resp.content)
        return b"".join(chunks)


def create_bluesky_media_downloader(
    settings: RAGSettings,
    client: ConstrainedClient | None,
) -> BlueskyMediaDownloader:
    """Settings から Real / Fake のいずれかを選択して返すファクトリ.

    .env の ``RAG_BLUESKY_FAKE_MODE`` が true の場合は ``FakeBlueskyMediaDownloader``
    を返し、実 BlueSky CDN アクセスを発生させない。

    ``client`` は REAL モード時のみ必須。fake モード時は ``None`` を許容し
    （fake downloader は外部 HTTP を行わないため）、REAL モード時に ``None``
    を渡すと ``ValueError`` を送出する（``create_bluesky_fetcher`` と同方針）。
    """
    if settings.rag_bluesky_fake_mode:
        from rag.pipeline.ingesters._fake.bluesky import (
            FakeBlueskyMediaDownloader,
        )

        # MediaDownloader は fixture ファイルを利用しないため fixture_dir 検証は不要
        # （bluesky_fake_fixture_dir は Fetcher 用、fail-fast は create_bluesky_fetcher 側で実施）
        return FakeBlueskyMediaDownloader()
    if client is None:
        raise ValueError(
            "REAL モードで create_bluesky_media_downloader を呼ぶ場合は "
            "ConstrainedClient を渡してください（fake モード時のみ None 許容）"
        )
    return RealBlueskyMediaDownloader(client)

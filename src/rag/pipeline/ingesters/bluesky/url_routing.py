"""BlueSky 投稿の URL 抽出 / 種別判定 / パース（純関数群）.

仕様: docs/specs/ingesters/bluesky.md
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from rag.pipeline.ingesters.youtube_protocols import YoutubeClassifier


_BSKY_URL_RE = re.compile(
    r"^https?://bsky\.app/profile/",
)

_BSKY_POST_URL_RE = re.compile(
    r"^https?://bsky\.app/profile/([^/]+)/post/([a-zA-Z0-9]+)(?:[?#].*)?$",
)


def extract_urls_from_item(item: dict[str, Any]) -> list[str]:
    """フィードアイテムから URL を抽出する.

    仕様: docs/specs/ingesters/bluesky.md「投稿内 URL の自動取り込み」

    抽出元:
    1. facets（リッチテキスト内リンク）
    2. embed.external（外部リンクカード）
    3. embed.media.external（recordWithMedia の外部リンクカード）

    引用元投稿の URL は対象外。

    Args:
        item: getAuthorFeed レスポンスのフィードアイテム

    Returns:
        重複排除済みの URL リスト（出現順を保持）
    """
    urls: list[str] = []
    seen: set[str] = set()

    post = item.get("post")
    if not isinstance(post, dict):
        return urls

    record = post.get("record")
    if not isinstance(record, dict):
        return urls

    def _add(url: str) -> None:
        if url and url not in seen and url.startswith(("http://", "https://")):
            seen.add(url)
            urls.append(url)

    facets = record.get("facets")
    if isinstance(facets, list):
        for facet in facets:
            if not isinstance(facet, dict):
                continue
            features = facet.get("features")
            if not isinstance(features, list):
                continue
            for feature in features:
                if (
                    isinstance(feature, dict)
                    and feature.get("$type") == "app.bsky.richtext.facet#link"
                ):
                    _add(feature.get("uri", ""))

    embed = record.get("embed")
    if isinstance(embed, dict):
        embed_type = embed.get("$type", "")
        if embed_type == "app.bsky.embed.external":
            external = embed.get("external")
            if isinstance(external, dict):
                _add(external.get("uri", ""))
        elif embed_type == "app.bsky.embed.recordWithMedia":
            media = embed.get("media")
            if isinstance(media, dict):
                media_type = media.get("$type", "")
                if media_type == "app.bsky.embed.external":
                    external = media.get("external")
                    if isinstance(external, dict):
                        _add(external.get("uri", ""))

    return urls


def classify_url(
    url: str,
    classifier: YoutubeClassifier,
) -> Literal["youtube", "web", "skip", "invalid_youtube"]:
    """URL を種別判定する.

    YouTube 動画 URL の判定は ``YoutubeClassifier`` Protocol 経由で実行する
    （SSoT は ``rag.pipeline.ingesters.youtube`` モジュール、Protocol 経由で
    越境直 import を回避）。

    Returns:
        - "youtube": 認識可能な YouTube 動画 URL
        - "invalid_youtube": YouTube 動画 URL のパターンに見えるが video_id 形式が不正
        - "skip": BlueSky 投稿 URL 等の取り込み対象外
        - "web": 上記以外（チャンネル URL・プレイリスト URL・一般 Web ページ等）
    """
    if _BSKY_URL_RE.match(url):
        return "skip"
    yt = classifier.classify(url)
    if yt == "video":
        return "youtube"
    if yt == "malformed":
        return "invalid_youtube"
    return "web"


def parse_bluesky_url(url: str) -> tuple[str, str] | None:
    """BlueSky 投稿 URL から (handle, rkey) を抽出する.

    Returns:
        (handle, rkey) タプル、またはパース失敗時は None
    """
    m = _BSKY_POST_URL_RE.match(url.strip())
    if m is None:
        return None
    return m.group(1), m.group(2)

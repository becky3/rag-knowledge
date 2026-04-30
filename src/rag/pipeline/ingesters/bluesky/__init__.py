"""BlueSky インジェスター（package 化）.

仕様: docs/specs/ingesters/bluesky.md
仕様: docs/specs/architecture.md（正解パターン: youtube パターンの bluesky 適用）

bluesky.py（1320 LOC）を責務単位で分解した package。
``BlueskyIngester`` を再 export し、外部からは package import のみで利用できる。

サブモジュール:

- ``_facade``: ``BlueskyIngester`` クラス（オーケストレーション）
- ``feed_fetcher``: AT Protocol API 利用層（pagination + DID 解決）
- ``post_placer``: 投稿配置 + .meta 生成 + メディア DL のキック
- ``url_routing``: URL 抽出 / 種別判定 / パース（純関数）
- ``delegations``: 投稿内 URL の自動取り込み委譲（YouTube / site-ingest）
"""

from __future__ import annotations

from rag.pipeline.ingesters.bluesky._facade import (
    FEED_PAGE_SIZE,
    MAX_POSTS_HARD_LIMIT,
    BlueskyIngester,
)

__all__ = ["BlueskyIngester", "FEED_PAGE_SIZE", "MAX_POSTS_HARD_LIMIT"]

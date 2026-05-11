"""YouTube インジェスターへの委譲を抽象化する Protocol 群.

仕様: docs/specs/architecture.md
仕様: docs/specs/ingesters/bluesky.md（YouTube への委譲経路）

bluesky など他インジェスターから YouTube インジェスターを越境直 import せず、
Protocol 経由で利用するための型を集約する。

- ``YoutubeClassifier``: URL 種別判定の抽象化（既存
  ``rag.pipeline.ingesters.youtube.classify_youtube_url`` 関数を SSoT として
  ラップする）
- ``YoutubeDelegator``: 動画取り込みの抽象化（既存
  ``YoutubeIngester.ingest_videos`` メソッドを SSoT としてラップする。
  bulk 取り込み境界で Whisper モデルの VRAM 解放を保証する公開 API）
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from rag.pipeline.ingesters._common import IngestResult
    from rag.pipeline.ingesters.youtube import YoutubeIngester


class YoutubeClassifier(Protocol):
    """YouTube URL 分類の抽象化.

    分類対応 URL パターンの SSoT は ``rag.pipeline.ingesters.youtube`` モジュール
    （詳細は ``docs/specs/ingesters/youtube.md`` 「対応 URL 形式」セクション）。
    """

    def classify(
        self,
        url: str,
    ) -> Literal["video", "malformed", "not_youtube"]:
        """URL を判定する.

        Returns:
            - ``"video"``: 認識可能な YouTube 動画 URL
            - ``"malformed"``: YouTube 動画 URL のパターンに見えるが video_id 形式不正
            - ``"not_youtube"``: YouTube 動画 URL ではない（チャンネル / プレイリスト
              / 一般 URL）
        """
        ...


class YoutubeDelegator(Protocol):
    """YouTube インジェスターへの動画取り込み委譲を抽象化する Port.

    呼び出し側（bluesky 等）は本 Protocol 経由でのみ YouTube インジェスターを
    利用する。実装は ``rag.pipeline.ingesters.youtube.YoutubeIngester`` を
    ラップする ``RealYoutubeDelegator`` または各テスト・QA 用の Fake。
    """

    async def ingest_videos(
        self,
        video_urls: list[str],
    ) -> list[IngestResult]:
        """YouTube 動画を bulk 取り込みする（単発取り込みは長さ 1 のリストで呼ぶ）.

        本メソッドは取り込み境界として Whisper モデルの VRAM 解放を保証する公開 API。
        内部で `try/finally` を配置し、bulk 末尾で `unload_whisper` を呼び出す。

        Args:
            video_urls: YouTube 動画 URL のリスト（長さ 1 以上）

        Returns:
            URL ごとの配置結果リスト（順序保証）。委譲先での失敗は各要素の
            ``errors`` / ``error_details`` に計上される。
        """
        ...

    def unload_whisper(self) -> None:
        """委譲先 YouTube インジェスターの Whisper モデルをアンロードする.

        ※ 同期メソッド（GPU メモリ解放を確実に同期実行するため）。
        `ingest_videos` が `async` であるのに対し、本メソッドは sync で呼び出すこと。

        通常は `ingest_videos` 内部の `try/finally` で自動的にアンロードされるため、
        外部から明示呼び出しする必要はない。プロセス終了前の保険的な明示呼び出しのために残す。
        Whisper モデル未ロード時は no-op（実装は委譲先側で判定）。
        """
        ...


class RealYoutubeClassifier:
    """既存 ``rag.pipeline.ingesters.youtube.classify_youtube_url`` をラップする実装.

    YouTube URL 判定の SSoT は youtube モジュール側にあり、本実装は単なる
    委譲層。bluesky 等の他インジェスターから youtube モジュールを越境直
    import せずに分類関数を利用するための薄いブリッジ。
    """

    def classify(
        self,
        url: str,
    ) -> Literal["video", "malformed", "not_youtube"]:
        from rag.pipeline.ingesters.youtube import classify_youtube_url

        return classify_youtube_url(url)


class RealYoutubeDelegator:
    """``YoutubeIngester.ingest_videos`` をラップする実装.

    bluesky の URL 自動取り込みなど、他インジェスターから YouTube への
    委譲を Protocol 経由に統一するための薄いブリッジ。
    """

    def __init__(self, ingester: YoutubeIngester) -> None:
        self._ingester = ingester

    async def ingest_videos(
        self,
        video_urls: list[str],
    ) -> list[IngestResult]:
        return await self._ingester.ingest_videos(video_urls)

    def unload_whisper(self) -> None:
        self._ingester.unload_whisper()


def create_youtube_classifier() -> YoutubeClassifier:
    """``YoutubeClassifier`` のファクトリ.

    現時点では Real 実装のみ（Fake 切替は不要 — 純関数のため
    テスト側で直接 stub を渡す）。
    """
    return RealYoutubeClassifier()


def create_youtube_delegator(ingester: YoutubeIngester) -> YoutubeDelegator:
    """``YoutubeDelegator`` のファクトリ.

    委譲先 ``YoutubeIngester`` インスタンスを受け取り、Real 実装で
    ラップする。委譲先 ingester 自体の Fake 化は YoutubeFetcher
    （``youtube_fetcher.py`` の ``create_youtube_fetcher``）で行う。
    """
    return RealYoutubeDelegator(ingester)

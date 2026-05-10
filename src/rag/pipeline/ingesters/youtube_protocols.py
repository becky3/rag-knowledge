"""YouTube インジェスターへの委譲を抽象化する Protocol 群.

仕様: docs/specs/architecture.md
仕様: docs/specs/ingesters/bluesky.md（YouTube への委譲経路）

bluesky など他インジェスターから YouTube インジェスターを越境直 import せず、
Protocol 経由で利用するための型を集約する。

- ``YoutubeClassifier``: URL 種別判定の抽象化（既存
  ``rag.pipeline.ingesters.youtube.classify_youtube_url`` 関数を SSoT として
  ラップする）
- ``YoutubeDelegator``: 動画取り込みの抽象化（既存
  ``YoutubeIngester.ingest_video`` メソッドを SSoT としてラップする）

本ファイルは U1 で Protocol のみを公開する。Real 実装（``RealYoutubeClassifier``
/ ``RealYoutubeDelegator``）と factory 関数は U2 で追加される。
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
    ラップする ``RealYoutubeDelegator``（U2）または各テスト・QA 用の Fake。
    """

    async def ingest_video(
        self,
        video_url: str,
        *,
        playlist_id: str | None = None,
    ) -> IngestResult:
        """単一 YouTube 動画を取り込む.

        Args:
            video_url: YouTube 動画 URL
            playlist_id: プレイリスト経由の場合のプレイリスト ID

        Returns:
            配置結果（``IngestResult``）。委譲先での失敗は ``errors`` /
            ``error_details`` に計上される。
        """
        ...

    def unload_whisper(self) -> None:
        """委譲先 YouTube インジェスターの Whisper モデルをアンロードする.

        ※ 同期メソッド（GPU メモリ解放を確実に同期実行するため）。
        `ingest_video` が `async` であるのに対し、本メソッドは sync で呼び出すこと。

        BlueSky 等の他インジェスターが bulk 取り込み完了時に呼び出し、
        delegation 経由で保持された Whisper モデルの VRAM を解放する。
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
    """``YoutubeIngester.ingest_video`` をラップする実装.

    bluesky の URL 自動取り込みなど、他インジェスターから YouTube への
    委譲を Protocol 経由に統一するための薄いブリッジ。
    """

    def __init__(self, ingester: YoutubeIngester) -> None:
        self._ingester = ingester

    async def ingest_video(
        self,
        video_url: str,
        *,
        playlist_id: str | None = None,
    ) -> IngestResult:
        return await self._ingester.ingest_video(
            video_url, playlist_id=playlist_id,
        )

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

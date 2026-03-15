"""インジェスター基盤: 抽象基底クラスと共通データモデル

仕様: docs/specs/rag-knowledge.md
Issue: #62
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class IngestedContent:
    """インジェスターの共通中間データモデル.

    あらゆるデータソース（Web, ファイル, API 等）から取り込んだコンテンツを
    統一的に表現する。RAGKnowledgeService はこのモデルを受け取り、
    チャンキング・ベクトル保存を行う。

    Attributes:
        source_id: ソース識別子（URL, ファイルパス等）
        title: コンテンツのタイトル
        text: 抽出済みテキスト
        ingested_at: 取り込みタイムスタンプ（ISO 8601）
        source_type: データソース種別（"web", "file" 等）
        metadata: ソース固有のメタデータ
        skip_chunking: True の場合、チャンキングをスキップし 1 チャンクで格納する
    """

    source_id: str
    title: str
    text: str
    ingested_at: str
    source_type: str
    metadata: dict[str, object] = field(default_factory=dict)
    skip_chunking: bool = False

    @staticmethod
    def now_iso() -> str:
        """現在時刻を ISO 8601 形式で返す."""
        return datetime.now(tz=timezone.utc).isoformat()


class BaseIngester(abc.ABC):
    """インジェスターの抽象基底クラス.

    各データソース用のインジェスターはこのクラスを継承し、
    fetch_single() と validate_identifier() を実装する。
    """

    @abc.abstractmethod
    async def fetch_single(self, identifier: str) -> IngestedContent | None:
        """単一コンテンツを取得する.

        Args:
            identifier: ソース識別子（URL, ファイルパス等）

        Returns:
            IngestedContent、または取得失敗時は None
        """

    async def fetch_batch(self, identifiers: list[str]) -> list[IngestedContent]:
        """複数コンテンツを一括取得する.

        デフォルト実装は fetch_single() を順次呼び出す。
        サブクラスで並行処理等の最適化が可能。

        Args:
            identifiers: ソース識別子のリスト

        Returns:
            取得に成功した IngestedContent のリスト
        """
        results: list[IngestedContent] = []
        for identifier in identifiers:
            content = await self.fetch_single(identifier)
            if content is not None:
                results.append(content)
        return results

    async def discover(self, source: str, **kwargs: object) -> list[str]:
        """取り込み対象の識別子を発見する（オプション）.

        リンク集ページからの URL 抽出など、取り込み対象を動的に発見する用途。
        デフォルト実装は空リストを返す。

        Args:
            source: 発見元の識別子（リンク集 URL 等）
            **kwargs: サブクラス固有のパラメータ

        Returns:
            発見された識別子のリスト
        """
        return []

    @abc.abstractmethod
    def validate_identifier(self, identifier: str) -> str:
        """識別子を検証し、正規化済みの識別子を返す.

        Args:
            identifier: 検証する識別子

        Returns:
            正規化済みの識別子

        Raises:
            ValueError: 識別子が不正な場合
        """

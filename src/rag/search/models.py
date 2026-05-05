"""検索系 dto 群.

仕様: docs/specs/rag-knowledge.md / docs/specs/search-response.md
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RAGRetrievalResult:
    """RAG検索結果.

    Attributes:
        context: フォーマット済みテキスト（システムプロンプト注入用）
        sources: ユニークなソースURLリスト（表示用）
    """

    context: str
    sources: list[str]


@dataclass
class VectorSearchItem:
    """ベクトル検索の生結果アイテム.

    Attributes:
        text: チャンクテキスト
        source_url: ソースURL（source_id）
        distance: cosine距離（0に近いほど類似）
        chunk_index: チャンクインデックス（0始まり）
        title: コンテンツのタイトル
        source_type: ソース種別
        total_chunks: 当該ソースのチャンク総数（0はレガシーデータ）
        section_path: 見出し階層（> 区切り）
    """

    text: str
    source_url: str
    distance: float
    chunk_index: int
    title: str = ""
    source_type: str = ""
    total_chunks: int = 0
    collected_at: str = ""
    section_path: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, object]:
        """JSON 出力用 dict に変換する."""
        d: dict[str, object] = {
            "text": self.text,
            "source_url": self.source_url,
            "distance": self.distance,
            "chunk_index": self.chunk_index,
            "title": self.title,
            "source_type": self.source_type,
            "total_chunks": self.total_chunks,
            "collected_at": self.collected_at,
            "section_path": self.section_path,
        }
        if self.url:
            d["url"] = self.url
        return d


@dataclass
class BM25SearchItem:
    """BM25検索の生結果アイテム.

    Attributes:
        text: チャンクテキスト
        source_url: ソースURL（source_id）
        score: BM25スコア（高いほどキーワード一致）
        doc_id: ドキュメントID
        chunk_index: チャンクインデックス（0始まり）
        title: コンテンツのタイトル
        source_type: ソース種別
        total_chunks: 当該ソースのチャンク総数（0はレガシーデータ）
        section_path: 見出し階層（> 区切り）
        url: 元 URL（取得元の外部 URL。存在する場合のみ）
    """

    text: str
    source_url: str
    score: float
    doc_id: str
    chunk_index: int = 0
    title: str = ""
    source_type: str = ""
    total_chunks: int = 0
    collected_at: str = ""
    section_path: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, object]:
        """JSON 出力用 dict に変換する."""
        d: dict[str, object] = {
            "text": self.text,
            "source_url": self.source_url,
            "score": self.score,
            "doc_id": self.doc_id,
            "chunk_index": self.chunk_index,
            "title": self.title,
            "source_type": self.source_type,
            "total_chunks": self.total_chunks,
            "collected_at": self.collected_at,
            "section_path": self.section_path,
        }
        if self.url:
            d["url"] = self.url
        return d


@dataclass
class RawSearchResults:
    """ベクトル検索・BM25検索の生結果（準Agentic Search用）.

    統合パイプライン（min-max正規化 + CC結合）を迂回し、
    各エンジンの生結果を個別にLLMに渡して判断を委譲する。

    Attributes:
        vector_results: ベクトル検索の生結果リスト
        bm25_results: BM25検索の生結果リスト
    """

    vector_results: list[VectorSearchItem]
    bm25_results: list[BM25SearchItem]

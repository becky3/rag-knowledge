"""Fake Embedding 実装.

仕様: docs/specs/infrastructure/fake-mode.md

Embedding API（LM Studio / OpenAI）への実アクセスを排除する Fake 実装。
SHA-256(text) を seed に決定論的な L2 正規化済みベクトルを生成する。

決定論性が必要な理由: e2e テスト・CI で「同じ入力には同じベクトル」が保証されると、
search アサーションが Python バージョン・プラットフォーム間で再現する。
SHA-256 を採用した理由は Python 標準ライブラリで実装非依存に同じバイト列を
返せるため。乱数 seed 固定方式（random.seed 等）は実装依存性が残る。

L2 正規化を行う理由: ChromaDB の cosine 類似度検索はユニットベクトル前提の
距離計算を行うため、Real Embedding（LM Studio / OpenAI）出力と同じ前提に揃える。
"""

from __future__ import annotations

import hashlib
import math
import struct

from rag.embedding.base import EmbeddingProvider


class FakeEmbedding(EmbeddingProvider):
    """決定論的な固定ベクトルを返す Fake Embedding 実装.

    仕様: docs/specs/infrastructure/fake-mode.md
    """

    def __init__(self, dimensions: int) -> None:
        if dimensions < 1:
            raise ValueError(
                f"dimensions は 1 以上である必要があります: {dimensions}"
            )
        self._dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """テキストリストを決定論的な固定ベクトルリストに変換する."""
        return [self._deterministic_vector(t) for t in texts]

    async def is_available(self) -> bool:
        """Fake は常に利用可能."""
        return True

    def _deterministic_vector(self, text: str) -> list[float]:
        """SHA-256(text) を seed に L2 正規化済みベクトルを生成する.

        SHA-256 のダイジェストを繰り返しハッシュして必要バイト数を確保し、
        4 バイトずつ符号付き int32 に変換して [-1.0, 1.0] にスケールしたあと
        L2 正規化する。
        """
        bytes_needed = self._dimensions * 4
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        buf = bytearray()
        counter = 0
        while len(buf) < bytes_needed:
            chunk = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
            buf.extend(chunk)
            counter += 1
        raw = bytes(buf[:bytes_needed])
        ints = struct.unpack(f">{self._dimensions}i", raw)
        scale = float(2**31)
        vector = [v / scale for v in ints]
        norm = math.sqrt(sum(x * x for x in vector))
        if norm == 0.0:
            # 理論上の安全装置: SHA-256 の非ゼロ性により全要素 0 になる確率は天文学的に
            # 低く、実質発生しない。ここに到達した場合は L2 norm = 1.0 を保つ単位ベクトル
            # にフォールバックして cosine 類似度計算の 0 除算を回避する。
            vector[0] = 1.0
            return vector
        return [x / norm for x in vector]

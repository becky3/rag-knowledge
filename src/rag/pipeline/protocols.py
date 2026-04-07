"""パイプライン制御 — 後続ステージのプロトコル定義.

仕様: docs/specs/pipeline-controller.md

コンバーター（#249）・インデクサー（#250）は未実装のため、
Protocol で抽象化し、スタブで動作可能にする。
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Protocol

from rag.store.models import SourceMetadata, SourceType


class ConverterProtocol(Protocol):
    """コンバーターのプロトコル.

    source_store のファイルを converted_store のテキストに変換する。
    変換不要なファイル（md/txt/adoc）はそのままコピーする。
    """

    def convert(
        self,
        file_path: str,
        source_store_dir: Path,
        converted_store_dir: Path,
    ) -> Path:
        """ソースファイルをテキストに変換する.

        Args:
            file_path: source_store 内の相対パス
            source_store_dir: source_store のルートディレクトリ
            converted_store_dir: converted_store のルートディレクトリ

        Returns:
            converted_store 内の変換済みファイルパス
        """
        ...

    def delete(
        self,
        file_path: str,
        converted_store_dir: Path,
    ) -> None:
        """converted_store から変換済みファイルを削除する.

        Args:
            file_path: source_store 内の相対パス
            converted_store_dir: converted_store のルートディレクトリ
        """
        ...

    def clear(
        self,
        converted_store_dir: Path,
        source_type: SourceType | None = None,
    ) -> None:
        """converted_store をクリアする.

        Args:
            converted_store_dir: converted_store のルートディレクトリ
            source_type: 指定時はその媒体のみクリア
        """
        ...


class IndexerProtocol(Protocol):
    """インデクサーのプロトコル.

    converted_store からチャンキング・Embedding・インデックス構築を行う。
    """

    async def add(
        self,
        source_id: str,
        converted_path: Path,
        metadata: SourceMetadata,
    ) -> None:
        """変換済みファイルをインデックスに追加する.

        Args:
            source_id: ソース識別子
            converted_path: converted_store 内の変換済みファイルパス
            metadata: ソースメタデータ
        """
        ...

    async def update(
        self,
        source_id: str,
        converted_path: Path,
        metadata: SourceMetadata,
    ) -> None:
        """インデックスを更新する.

        Args:
            source_id: ソース識別子
            converted_path: converted_store 内の変換済みファイルパス
            metadata: ソースメタデータ
        """
        ...

    async def delete(self, source_id: str) -> None:
        """インデックスから削除する.

        Args:
            source_id: ソース識別子
        """
        ...

    async def upsert_metadata(
        self,
        source_id: str,
        metadata: SourceMetadata,
    ) -> None:
        """メタデータのみ更新する（チャンク再生成なし）.

        Args:
            source_id: ソース識別子
            metadata: ソースメタデータ
        """
        ...

    def set_bm25_deferred_save(self, enabled: bool) -> None:
        """BM25 の遅延 save モードを切り替える.

        Args:
            enabled: True で遅延モード有効
        """
        ...

    def flush_bm25(self) -> None:
        """BM25 の未保存変更を一括 rebuild + 永続化する."""
        ...

    def bm25_deferred(self) -> contextlib.AbstractContextManager[None]:
        """BM25 遅延 save のコンテキストマネージャ."""
        ...

    async def clear(self, source_type: SourceType | None = None) -> None:
        """インデックスをクリアする.

        Args:
            source_type: 指定時はその媒体のみクリア
        """
        ...

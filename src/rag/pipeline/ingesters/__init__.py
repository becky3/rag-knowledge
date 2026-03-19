"""パイプライン用インジェスター.

仕様: docs/specs/ingesters/common.md

新アーキテクチャのインジェスター群。
責務は source_store へのファイル配置 + .meta サイドカーファイルの生成に限定。
"""

from rag.pipeline.ingesters._common import IngestResult

__all__ = ["IngestResult"]

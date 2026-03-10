"""RAG MCP サーバー設定管理.

仕様: docs/specs/rag-knowledge.md
独立リポジトリとして動作する。
"""

from __future__ import annotations

import functools
import io
import sys
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# LM Studio のデフォルトベースURL
DEFAULT_LMSTUDIO_BASE_URL = "http://localhost:1234"

# プロジェクトルートの .env を参照
_ENV_FILE = Path(__file__).parent.parent.parent / ".env"


class RAGSettings(BaseSettings):
    """RAG MCP サーバーの設定.

    仕様: docs/specs/rag-knowledge.md
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Embedding設定
    embedding_provider: Literal["local", "online"] = "local"
    embedding_model_local: str = "nomic-embed-text"
    embedding_model_online: str = "text-embedding-3-small"
    embedding_prefix_enabled: bool = True
    lmstudio_base_url: str = DEFAULT_LMSTUDIO_BASE_URL
    openai_api_key: str = ""

    # ストレージ（MCP サーバー起動 cwd からの相対パス）
    chromadb_persist_dir: str = "./chroma_db"
    bm25_persist_dir: str = "./bm25_index"

    # チャンキング
    rag_chunk_size: int = Field(default=200, ge=1)
    rag_chunk_overlap: int = Field(default=30, ge=0)

    # 検索
    rag_retrieval_count: int = Field(default=3, ge=1)
    rag_similarity_threshold: float | None = Field(
        default=None, ge=0.0, le=2.0
    )

    # ハイブリッド検索
    rag_hybrid_search_enabled: bool = False
    rag_vector_weight: float = Field(default=0.90, ge=0.0, le=1.0)
    rag_bm25_k1: float = Field(default=2.5, gt=0.0)
    rag_bm25_b: float = Field(default=0.50, ge=0.0, le=1.0)
    rag_min_combined_score: float | None = Field(
        default=0.75, ge=0.0, le=1.0
    )

    # クロール
    rag_max_crawl_pages: int = Field(default=50, ge=1)
    rag_crawl_delay_sec: float = Field(default=1.0, ge=0)

    # robots.txt
    rag_respect_robots_txt: bool = True
    rag_robots_txt_cache_ttl: int = Field(default=3600, ge=0)

    # URL安全性チェック (Google Safe Browsing API)
    rag_url_safety_check: bool = False
    google_safe_browsing_api_key: str = ""
    rag_url_safety_cache_ttl: int = Field(default=300, ge=0)
    rag_url_safety_fail_open: bool = True
    rag_url_safety_timeout: float = Field(default=5.0, gt=0)

    # トランスポート
    rag_transport: Literal["stdio", "http"] = "stdio"
    rag_http_host: str = "127.0.0.1"
    rag_http_port: int = Field(default=8081, ge=1, le=65535)
    rag_dns_rebinding_protection: bool = True

    # レスポンスサイズ制限
    rag_max_response_chars: int | None = Field(default=None, ge=1)

    # デバッグ
    rag_debug_log_enabled: bool = False

    @model_validator(mode="after")
    def validate_chunk_settings(self) -> RAGSettings:
        """チャンク設定の相関バリデーション."""
        if self.rag_chunk_overlap >= self.rag_chunk_size:
            raise ValueError(
                f"rag_chunk_overlap ({self.rag_chunk_overlap}) must be less than "
                f"rag_chunk_size ({self.rag_chunk_size})"
            )
        return self


@functools.lru_cache(maxsize=1)
def get_settings() -> RAGSettings:
    """キャッシュ付きでRAGSettingsインスタンスを返す."""
    return RAGSettings()


def ensure_utf8_streams(*, include_stdout: bool = False) -> None:
    """stderr（およびオプションで stdout）を UTF-8 に再構成する.

    Windows 環境では stderr/stdout のデフォルトエンコーディングが cp932 等に
    なり、日本語ログ出力時に UnicodeEncodeError が発生する。
    MCP stdio プロトコルは stdout を使うため、stdout の再構成はCLI用途に限定する。

    Args:
        include_stdout: True の場合は stdout も UTF-8 に再構成する
    """
    if (
        hasattr(sys.stderr, "buffer")
        and hasattr(sys.stderr, "encoding")
        and sys.stderr.encoding
        and sys.stderr.encoding.lower().replace("-", "") != "utf8"
    ):
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )

    if include_stdout and (
        hasattr(sys.stdout, "buffer")
        and hasattr(sys.stdout, "encoding")
        and sys.stdout.encoding
        and sys.stdout.encoding.lower().replace("-", "") != "utf8"
    ):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

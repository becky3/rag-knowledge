"""RAG MCP サーバー設定管理.

仕様: docs/specs/rag-knowledge.md
独立リポジトリとして動作する。

設定値はセキュリティレベルに応じて3層に分離し、
各設定値の取得元は1つに固定する（フォールバックなし）:
- シークレット: OS セキュアストレージ (keyring)
- 環境依存値: .env（_EnvLoader）
- 共通設定値: config.toml

全設定値は明示的に .env / config.toml に記載する必要がある。
config.toml が存在しない場合は FileNotFoundError、
設定値が不足している場合は pydantic の ValidationError となる。
例外: rag_similarity_threshold, rag_max_response_chars, rag_min_combined_score
（未設定 = 機能無効を意図する項目は None がデフォルト）
"""

from __future__ import annotations

import functools
import io
import sys
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# LM Studio のデフォルトベースURL（LMStudioEmbedding コンストラクタ用）
DEFAULT_LMSTUDIO_BASE_URL = "http://localhost:1234"

# デフォルトEmbeddingモデル名（LMStudioEmbedding コンストラクタ用）
DEFAULT_EMBEDDING_MODEL_LOCAL = "nomic-embed-text"

# プロジェクトルートのパス
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
_TOML_FILE = _PROJECT_ROOT / "config.toml"


class _EnvLoader(BaseSettings):
    """環境依存設定のローダー（内部用）.

    .env および環境変数から、デプロイ先・マシンごとに異なる値を取得する。
    デフォルト値を持つフィールドは任意、それ以外は必須。未設定の必須フィールドはバリデーションエラーとなる。
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Embedding 接続
    embedding_provider: Literal["local", "online"]
    lmstudio_base_url: str

    # ストレージ（MCP サーバー起動 cwd からの相対パス）
    chromadb_persist_dir: str
    bm25_persist_dir: str
    source_store_dir: str
    converted_store_dir: str

    # ChromaDB サーバー接続
    chromadb_server_host: str = "localhost"
    chromadb_server_port: int = Field(default=8000, ge=1, le=65535)
    # MCP サーバー起動時に ChromaDB サーバーを自動起動するか。
    # ChromaDBServerManager (infrastructure/chromadb_manager.py) が参照する。
    chromadb_auto_start: bool = True

    # トランスポート
    rag_transport: Literal["stdio", "http"]
    rag_http_host: str
    rag_http_port: int = Field(ge=1, le=65535)
    rag_dns_rebinding_protection: bool

    # デバッグ
    rag_debug_log_enabled: bool

    # YouTube インジェスター（Whisper）
    rag_youtube_whisper_model: str = "base"
    rag_youtube_whisper_device: Literal["cuda", "cpu"] = "cuda"

    # サイト一括取り込み
    site_ingest_temp_dir: str = ".tmp/site_ingest"


# .env 管理フィールド名の集合（重複検出に使用）
_ENV_FIELD_NAMES = frozenset(_EnvLoader.model_fields.keys())


class RAGSettings(BaseModel):
    """RAG MCP サーバーの統合設定.

    仕様: docs/specs/rag-knowledge.md

    各設定値の取得元は1つに固定されている（フォールバックなし）:
    - 環境依存値(.env): embedding_provider, lmstudio_base_url 等
    - 共通設定値(config.toml): rag_chunk_size, rag_retrieval_count 等

    全フィールドは必須。例外: float | None / int | None 型のフィールドは
    未設定時に None（機能無効）がデフォルト。
    """

    # --- .env から取得（環境依存値） ---
    embedding_provider: Literal["local", "online"]
    lmstudio_base_url: str
    chromadb_persist_dir: str
    bm25_persist_dir: str
    source_store_dir: str
    converted_store_dir: str
    chromadb_server_host: str
    chromadb_server_port: int = Field(ge=1, le=65535)
    # MCP サーバー起動時に ChromaDB サーバーを自動起動するか
    chromadb_auto_start: bool
    rag_transport: Literal["stdio", "http"]
    rag_http_host: str
    rag_http_port: int = Field(ge=1, le=65535)
    rag_dns_rebinding_protection: bool
    rag_debug_log_enabled: bool
    rag_youtube_whisper_model: str
    rag_youtube_whisper_device: Literal["cuda", "cpu"]
    site_ingest_temp_dir: str

    # --- config.toml から取得（共通設定値） ---

    # Embedding モデル
    embedding_model_local: str
    embedding_model_online: str
    embedding_prefix_enabled: bool

    # チャンキング
    rag_chunk_size: int = Field(ge=1)
    rag_chunk_overlap: int = Field(ge=0)
    rag_embedding_context_length: int = Field(default=512, ge=1)
    rag_worst_token_char_ratio: float = Field(default=0.7, gt=0.0, le=1.0)
    # 検索
    rag_retrieval_count: int = Field(ge=1)
    rag_similarity_threshold: float | None = Field(
        default=None, ge=0.0, le=2.0
    )

    # ハイブリッド検索
    rag_hybrid_search_enabled: bool
    rag_vector_weight: float = Field(ge=0.0, le=1.0)
    rag_bm25_k1: float = Field(gt=0.0)
    rag_bm25_b: float = Field(ge=0.0, le=1.0)
    rag_min_combined_score: float | None = Field(
        default=None, ge=0.0, le=1.0
    )

    # クロール（範囲外の値は WebCrawler / ConstrainedClient が警告付きでクランプする）
    rag_max_crawl_pages: int = Field(ge=1)
    rag_crawl_delay_sec: float = Field(ge=0)
    rag_crawl_default_depth: int = Field(ge=1, le=10)
    rag_crawl_max_errors: int = Field(ge=5, le=10)

    # robots.txt
    rag_respect_robots_txt: bool
    rag_robots_txt_cache_ttl: int = Field(ge=0)

    # URL安全性チェック (Google Safe Browsing API)
    rag_url_safety_check: bool
    rag_url_safety_cache_ttl: int = Field(ge=0)
    rag_url_safety_timeout: float = Field(gt=0)

    # レスポンスサイズ制限
    rag_max_response_chars: int | None = Field(default=None, ge=1)

    # rag_stats ソース一覧の最大表示件数
    rag_stats_max_sources: int = Field(ge=1)

    # rag_list_recent デフォルト取得件数
    rag_list_recent_limit: int = Field(ge=1, le=100)

    # Zenn インジェスター
    rag_zenn_max_articles: int = Field(ge=1, le=100)
    rag_zenn_request_timeout: int = Field(ge=1, le=120)
    rag_zenn_request_interval: float = Field(ge=0.1, le=60.0)

    # クロール per-request タイムアウト
    rag_crawl_request_timeout: int = Field(ge=1, le=120)

    # ドキュメントインジェスター
    rag_document_supported_extensions: str
    rag_document_http_mode_enabled: bool
    rag_document_allowed_dirs: str

    # Upload HTTP API
    rag_upload_max_file_size_mb: int = Field(ge=1, le=500)

    # PDF バックエンド
    rag_pdf_backend: Literal["auto", "mineru", "pymupdf4llm"]
    rag_pdf_mineru_mfd_conf_thres: float = Field(ge=0.0, le=1.0)
    rag_pdf_quality_ufffd_threshold: float = Field(ge=0.0, le=1.0)
    rag_pdf_quality_greek_threshold: float = Field(ge=0.0, le=1.0)
    rag_pdf_quality_cjk_min_threshold: float = Field(ge=0.0, le=1.0)
    rag_pdf_quality_min_chars_per_page: int = Field(ge=1, le=10000)
    rag_pdf_quality_sample_pages: int = Field(ge=1, le=100)

    # YouTube インジェスター
    rag_youtube_max_videos: int = Field(ge=1, le=500)
    rag_youtube_request_interval: float = Field(ge=0.1, le=60.0)
    rag_youtube_request_timeout: int = Field(ge=1, le=120)
    rag_youtube_transcript_languages: list[str] = Field(min_length=1)
    rag_youtube_merge_gap_sec: float = Field(ge=0.1, le=60.0)
    rag_youtube_merge_max_chars: int = Field(ge=50, le=2000)
    rag_youtube_max_duration: int = Field(ge=60, le=86400)

    # 青空文庫インジェスター
    rag_aozora_max_works: int = Field(ge=1, le=500)
    rag_aozora_request_interval: float = Field(ge=0.1, le=60.0)
    rag_aozora_request_timeout: int = Field(ge=1, le=120)

    # BlueSky インジェスター
    rag_bluesky_appview_url: str
    rag_bluesky_max_posts: int = Field(ge=1, le=1000)
    rag_bluesky_request_timeout: int = Field(ge=1, le=120)
    rag_bluesky_request_interval: float = Field(ge=0.1, le=60.0)
    rag_bluesky_include_reposts: bool

    # サイト一括取り込み（Scrapy subprocess）
    site_ingest_delay_sec: float = Field(ge=0.05, le=60.0)
    site_ingest_max_pages: int = Field(ge=1, le=50000)
    site_ingest_download_timeout: int = Field(ge=1, le=300)
    site_ingest_timeout_sec: float = Field(ge=60.0, le=86400.0)
    site_ingest_error_count: int = Field(ge=1, le=1000)

    @model_validator(mode="after")
    def validate_chunk_settings(self) -> RAGSettings:
        """チャンク設定の相関バリデーション."""
        if self.rag_chunk_overlap >= self.rag_chunk_size:
            raise ValueError(
                f"rag_chunk_overlap ({self.rag_chunk_overlap}) must be less than "
                f"rag_chunk_size ({self.rag_chunk_size})"
            )
        return self


def _load_toml_config() -> dict[str, Any]:
    """config.toml を読み込み、層の重複を検証する."""
    if not _TOML_FILE.exists():
        msg = f"config.toml が見つかりません: {_TOML_FILE}"
        raise FileNotFoundError(msg)
    with open(_TOML_FILE, "rb") as f:
        data: dict[str, Any] = tomllib.load(f)
    # config.toml に環境依存値が混入していないか検証
    env_overlap = set(data.keys()) & _ENV_FIELD_NAMES
    if env_overlap:
        msg = (
            f"config.toml に環境依存設定が含まれています（.env に移動してください）: "
            f"{sorted(env_overlap)}"
        )
        raise ValueError(msg)
    # 未知のキーを検証
    toml_field_names = frozenset(RAGSettings.model_fields.keys()) - _ENV_FIELD_NAMES
    unknown = set(data.keys()) - toml_field_names
    if unknown:
        msg = f"config.toml に未知の設定が含まれています: {sorted(unknown)}"
        raise ValueError(msg)
    return data


@functools.lru_cache(maxsize=1)
def get_settings() -> RAGSettings:
    """キャッシュ付きでRAGSettingsインスタンスを返す.

    .env から環境依存値、config.toml から共通設定値を取得し、
    統合した RAGSettings を返す。
    """
    env_loader = _EnvLoader()  # type: ignore[call-arg]  # pydantic-settings が .env/環境変数から読み込み
    toml_data = _load_toml_config()
    return RAGSettings(**env_loader.model_dump(), **toml_data)


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

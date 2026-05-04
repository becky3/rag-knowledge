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
import os
import sys
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, cast

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Upload HTTP API 認証の keyring パラメータ
UPLOAD_API_KEY_SERVICE = "rag-knowledge"
UPLOAD_API_KEY_NAME = "UPLOAD_API_KEY"

# プロジェクトルートのパス（リポジトリ・work tree のルート、editable install 前提）
# 公開シンボル PROJECT_ROOT として他モジュールから利用される（fixture_dir / fixture file の絶対パス解決等）。
# src/rag/config.py から見て 3 階層上が project root。本算出ロジックは本モジュール 1 箇所のみ
# に集約し、利用側はマジックナンバー (parents[N]) を持たない。
PROJECT_ROOT = Path(__file__).parent.parent.parent
_PROJECT_ROOT = PROJECT_ROOT  # 後方互換のための旧 private 名
_ENV_FILE = PROJECT_ROOT / ".env"
_TOML_FILE = PROJECT_ROOT / "config.toml"
_LMSTUDIO_TOML_FILE = PROJECT_ROOT / "lmstudio.toml"


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

    # ストレージパス（デプロイ先により異なるため .env 管理）
    chromadb_persist_dir: str
    bm25_persist_dir: str
    # .meta サイドカーファイルがメタデータの原本。metadata.db は索引であり .meta から再構築可能
    source_store_dir: str
    converted_store_dir: str

    # ChromaDB サーバー接続
    chromadb_server_host: str = "localhost"
    chromadb_server_port: int = Field(default=8000, ge=1, le=65535)
    # MCP サーバー起動時に ChromaDB を自動起動するか（手動管理環境では無効化）
    chromadb_auto_start: bool = True

    # トランスポート
    rag_transport: Literal["stdio", "http"]
    rag_http_host: str
    rag_http_port: int = Field(ge=1, le=65535)
    # ローカル専用想定のため DNS リバインディング攻撃を防止
    rag_dns_rebinding_protection: bool

    # デバッグ
    rag_debug_log_enabled: bool

    # 長時間運用で stderr が失われる環境向けにログをファイル永続化する。未設定時は stderr のみ
    rag_log_dir: str | None = None

    # YouTube インジェスター（Whisper）— GPU 有無で選択が変わるため .env 管理
    rag_youtube_whisper_model: str = "base"
    rag_youtube_whisper_device: Literal["cuda", "cpu"] = "cuda"

    # YouTube Fake モード — テスト・QA で実 YouTube アクセスを排除する。デフォルトは安全側（fake 有効）
    rag_youtube_fake_mode: bool = True
    # Fake Fetcher が読み込む fixture ディレクトリ。プロジェクトルートからの相対パス
    rag_youtube_fake_fixture_dir: str = "src/rag/pipeline/ingesters/_fake/youtube/data"

    # BlueSky Fake モード — テスト・QA で実 BlueSky AT Protocol アクセスを排除する。デフォルトは安全側（fake 有効）
    rag_bluesky_fake_mode: bool = True
    # Fake Fetcher が読み込む fixture ディレクトリ。プロジェクトルートからの相対パス
    rag_bluesky_fake_fixture_dir: str = "src/rag/pipeline/ingesters/_fake/bluesky/data"

    # Zenn Fake モード — テスト・QA で実 Zenn API アクセスを排除する。デフォルトは安全側（fake 有効）
    rag_zenn_fake_mode: bool = True
    # Fake Fetcher が読み込む fixture ディレクトリ。プロジェクトルートからの相対パス
    rag_zenn_fake_fixture_dir: str = "src/rag/pipeline/ingesters/_fake/zenn/data"

    # Aozora Fake モード — テスト・QA で実 青空文庫 / GitHub Raw アクセスを排除する。デフォルトは安全側（fake 有効）
    rag_aozora_fake_mode: bool = True
    # Fake Fetcher が読み込む fixture ディレクトリ。プロジェクトルートからの相対パス
    rag_aozora_fake_fixture_dir: str = "src/rag/pipeline/ingesters/_fake/aozora/data"

    # Embedding Fake モード — テスト専用フック。本番運用では設定しないこと。
    # pytest の autouse fixture が `RAG_EMBEDDING_FAKE_MODE=true` を強制し、subprocess 越境テストでも
    # 環境変数経由で Fake Embedding を注入する。本番ランタイムでは `False`（デフォルト）固定で
    # 実 LM Studio / OpenAI Embedding が利用される
    rag_embedding_fake_mode: bool = False
    # Fake Embedding が生成するベクトルの次元数。Real モデルの次元に合わせる
    rag_embedding_fake_dimensions: int = Field(default=768, ge=1)

    # Web (scrapy) Fake モード — site-ingest で実 Web アクセスを排除する上位スイッチ
    # `.env` で公開する唯一の web 系 fake フラグ。内部の rag_scrapy_fake_mode の既定値として派生する
    rag_web_fake_mode: bool = True
    # 内部 Settings: ScrapyRunner Fake モード切替。
    # `.env` で個別指定がなければ rag_web_fake_mode から派生する（model_validator で None→派生）
    rag_scrapy_fake_mode: bool | None = None
    # Fake ScrapyRunner が読み込む fixture ディレクトリ
    rag_scrapy_fake_fixture_dir: str = "src/rag/scrapy/_fake/data"

    # サイト一括取り込み一時ディレクトリ（Scrapy クロール結果の一時保管）
    site_ingest_temp_dir: str = ".tmp/site_ingest"

    # Embedding プロバイダーの処理能力に応じて並列数を調整する
    rag_embedding_concurrency: int = Field(default=32, ge=1)

    @model_validator(mode="after")
    def _derive_web_fake_modes(self) -> _EnvLoader:
        """RAG_WEB_FAKE_MODE を内部 web 系 fake フラグの既定値として派生する.

        個別 env (RAG_SCRAPY_FAKE_MODE 等) が `.env` で明示されていない場合、
        rag_web_fake_mode の値を採用する。将来 web_fetch 等を追加する場合は
        ここに分岐を増やす。
        """
        if self.rag_scrapy_fake_mode is None:
            self.rag_scrapy_fake_mode = self.rag_web_fake_mode
        return self


# .env 管理フィールド名の集合（重複検出に使用）
_ENV_FIELD_NAMES = frozenset(_EnvLoader.model_fields.keys())


class RAGSettings(BaseModel):
    """RAG MCP サーバーの統合設定.

    仕様: docs/specs/rag-knowledge.md

    各設定値の取得元は1つに固定されている（フォールバックなし）:
    - 環境依存値(.env): embedding_provider, lmstudio_base_url 等
    - 共通設定値(config.toml): rag_chunk_size, rag_retrieval_count 等

    全フィールドは必須。例外: float | None / int | None / str | None 型のフィールドは
    未設定時に None（機能無効）がデフォルト。
    """

    # --- .env から取得（環境依存値） ---
    embedding_provider: Literal["local", "online"]
    lmstudio_base_url: str
    chromadb_persist_dir: str
    bm25_persist_dir: str
    # .meta サイドカーファイルがメタデータの原本。metadata.db は索引であり .meta から再構築可能
    source_store_dir: str
    converted_store_dir: str
    chromadb_server_host: str
    chromadb_server_port: int = Field(ge=1, le=65535)
    chromadb_auto_start: bool
    rag_transport: Literal["stdio", "http"]
    rag_http_host: str
    rag_http_port: int = Field(ge=1, le=65535)
    rag_dns_rebinding_protection: bool
    rag_debug_log_enabled: bool
    # 長時間運用で stderr が失われる環境向けにログをファイル永続化する。未設定時は stderr のみ
    rag_log_dir: str | None = None
    rag_youtube_whisper_model: str
    rag_youtube_whisper_device: Literal["cuda", "cpu"]
    # YouTube Fake モード切替。デフォルト fake（安全側）、本番運用時のみ false を .env で明示
    rag_youtube_fake_mode: bool
    # Fake Fetcher が読み込む fixture ディレクトリ
    rag_youtube_fake_fixture_dir: str
    # BlueSky Fake モード切替。デフォルト fake（安全側）、本番運用時のみ false を .env で明示
    rag_bluesky_fake_mode: bool
    # Fake Fetcher が読み込む fixture ディレクトリ
    rag_bluesky_fake_fixture_dir: str
    # Zenn Fake モード切替。デフォルト fake（安全側）、本番運用時のみ false を .env で明示
    rag_zenn_fake_mode: bool
    # Fake Fetcher が読み込む fixture ディレクトリ
    rag_zenn_fake_fixture_dir: str
    # Aozora Fake モード切替。デフォルト fake（安全側）、本番運用時のみ false を .env で明示
    rag_aozora_fake_mode: bool
    # Fake Fetcher が読み込む fixture ディレクトリ
    rag_aozora_fake_fixture_dir: str
    # Embedding Fake モード切替（テスト専用フック）。本番運用では false 固定。
    # pytest autouse fixture が env 強制し、subprocess 越境テストでも引き継ぐ
    rag_embedding_fake_mode: bool
    # Fake Embedding が生成するベクトルの次元数
    rag_embedding_fake_dimensions: int = Field(ge=1)
    # Web (scrapy) Fake モード切替の上位スイッチ。`.env` の RAG_WEB_FAKE_MODE で制御
    rag_web_fake_mode: bool
    # 内部: ScrapyRunner Fake モード切替（_EnvLoader で rag_web_fake_mode から派生済み）
    rag_scrapy_fake_mode: bool
    # Fake ScrapyRunner が読み込む fixture ディレクトリ
    rag_scrapy_fake_fixture_dir: str
    site_ingest_temp_dir: str
    rag_embedding_concurrency: int = Field(ge=1)

    # --- lmstudio.toml から取得（LM Studio モデル key） ---
    # ローカル Embedding モデルの key（lms load で使用）
    embedding_model_local: str = Field(min_length=1)
    # Vision モデルの key（lms load で使用）
    rag_vision_model: str = Field(min_length=1)

    # --- config.toml から取得（共通設定値） ---

    # Embedding モデル
    embedding_model_online: str
    # 検索クエリに prefix を付与して検索精度を向上させる（モデル依存）
    embedding_prefix_enabled: bool
    # 高並列時の一時的な接続タイムアウトに対応するリトライ
    rag_embedding_retry_count: int = Field(ge=0, le=10)
    rag_embedding_retry_base_delay: float = Field(ge=0.1, le=30.0)

    # チャンキング
    rag_chunk_size: int = Field(ge=1)
    rag_chunk_overlap: int = Field(ge=0)
    # ローカル Embedding モデルのコンテキスト長制限に基づく文字数制御
    rag_embedding_context_length: int = Field(default=512, ge=1)
    # トークン数推定の安全マージン（文字→トークン変換の最悪ケース比率）
    rag_worst_token_char_ratio: float = Field(default=0.7, gt=0.0, le=1.0)

    # 検索
    # fetch_count = n * 3 のメモリ影響を抑制
    rag_retrieval_count: int = Field(ge=1, le=100)
    # None = 閾値フィルタ無効（全結果を返す）
    rag_similarity_threshold: float | None = Field(
        default=None, ge=0.0, le=2.0
    )

    # ハイブリッド検索
    rag_hybrid_search_enabled: bool
    # ベクトル検索とBM25のスコア配分（残りがBM25の重み）
    rag_vector_weight: float = Field(ge=0.0, le=1.0)
    # BM25 の単語頻度飽和パラメータ
    rag_bm25_k1: float = Field(gt=0.0)
    # BM25 の文書長正規化パラメータ
    rag_bm25_b: float = Field(ge=0.0, le=1.0)
    # None = スコア下限フィルタ無効（全結果を返す）
    rag_min_combined_score: float | None = Field(
        default=None, ge=0.0, le=1.0
    )

    # ChromaDB コレクション名
    chromadb_collection_name: str

    # URL安全性チェック (Google Safe Browsing API)
    rag_url_safety_check: bool
    # キャッシュで API 呼び出し頻度を抑制（秒単位）
    rag_url_safety_cache_ttl: int = Field(ge=0)
    rag_url_safety_timeout: float = Field(gt=0)

    # None = トランケーション無効（全文返却）
    rag_max_response_chars: int | None = Field(default=None, ge=1)

    # MCP レスポンスの情報量を制御
    rag_list_recent_limit: int = Field(ge=1, le=100)

    # 単一ログファイルの肥大化を防ぎ、ツール側での読み込みコストを抑える
    rag_log_file_max_bytes: int = Field(ge=1)

    # Zenn インジェスター — API BAN 回避のためのレート制限
    rag_zenn_max_articles: int = Field(ge=1, le=100)
    rag_zenn_request_timeout: int = Field(ge=1, le=120)
    rag_zenn_request_interval: float = Field(ge=0.1, le=60.0)

    # HTML → Markdown 変換時に除去する class トークン（完全一致）
    rag_html_remove_class_tokens: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=1,
    )

    # ドキュメントインジェスター
    rag_document_supported_extensions: str
    # HTTP Upload API 経由の取り込みを許可するか
    rag_document_http_mode_enabled: bool
    # ローカルファイル取り込みの許可ディレクトリ（パストラバーサル防止）
    rag_document_allowed_dirs: str

    # Upload HTTP API
    rag_upload_max_file_size_mb: int = Field(ge=1, le=500)
    # ロック競合時の Retry-After ヘッダ値（秒）。クライアントの即リトライを抑制するためのヒント
    rag_upload_retry_after_write_sec: int = Field(ge=1, le=3600)
    # 再構築は長時間動作するため、書き込み側より長い間隔を設定する
    rag_upload_retry_after_rebuild_sec: int = Field(ge=1, le=86400)

    # PDF バックエンド
    # auto: MinerU 利用可能なら優先、未インストール時は pymupdf4llm にフォールバック
    rag_pdf_backend: Literal["auto", "mineru", "pymupdf4llm"]
    # MinerU の数式検出信頼度閾値
    rag_pdf_mineru_mfd_conf_thres: float = Field(ge=0.0, le=1.0)
    # PDF 抽出品質の自動判定閾値（低品質時にバックエンド切替を行う）
    rag_pdf_quality_ufffd_threshold: float = Field(ge=0.0, le=1.0)
    rag_pdf_quality_greek_threshold: float = Field(ge=0.0, le=1.0)
    rag_pdf_quality_cjk_min_threshold: float = Field(ge=0.0, le=1.0)
    rag_pdf_quality_min_chars_per_page: int = Field(ge=1, le=10000)
    # 全ページ走査のコスト回避（先頭 N ページで品質を推定）
    rag_pdf_quality_sample_pages: int = Field(ge=1, le=100)

    # YouTube インジェスター — API BAN 回避のためのレート制限
    rag_youtube_max_videos: int = Field(ge=1, le=500)
    rag_youtube_request_interval: float = Field(ge=0.1, le=60.0)
    rag_youtube_request_timeout: int = Field(ge=1, le=120)
    # 字幕取得の優先言語順（先頭が最優先）
    rag_youtube_transcript_languages: list[str] = Field(min_length=1)
    # 短い字幕セグメントを統合して検索単位の粒度を改善
    rag_youtube_merge_gap_sec: float = Field(ge=0.1, le=60.0)
    rag_youtube_merge_max_chars: int = Field(ge=50, le=2000)
    # 長時間動画の処理コスト・ストレージ消費を制限
    rag_youtube_max_duration: int = Field(ge=60, le=86400)

    # 青空文庫インジェスター — サーバー負荷軽減のためのレート制限
    rag_aozora_max_works: int = Field(ge=1, le=500)
    rag_aozora_request_interval: float = Field(ge=0.1, le=60.0)
    rag_aozora_request_timeout: int = Field(ge=1, le=120)

    # BlueSky インジェスター — AT Protocol API のレート制限準拠
    rag_bluesky_appview_url: str
    rag_bluesky_max_posts: int = Field(ge=1, le=1000)
    rag_bluesky_request_timeout: int = Field(ge=1, le=120)
    rag_bluesky_request_interval: float = Field(ge=0.1, le=60.0)
    # リポストを含めるとノイズが増えるため選択可能
    rag_bluesky_include_reposts: bool
    # --force 時の YouTube 再取り込みはコストが高いため個別に抑制可能
    rag_bluesky_force_youtube_reingest: bool

    # メディア解析（Vision モデル）
    # rag_vision_model は lmstudio.toml が SSoT（_load_lmstudio_config 経由で読み込み、
    # get_settings で RAGSettings に統合）
    # 推論の深度を制御し、処理速度と品質のバランスを調整
    rag_vision_reasoning_effort: Literal["none", "low", "medium", "high"]
    # 動画フレーム抽出の間隔（秒）。抽出頻度を制御し、処理時間とカバレッジのバランスを調整
    rag_vision_frame_interval: int = Field(ge=1, le=300)
    # Vision API レスポンスの出力長を制限
    rag_vision_max_tokens: int = Field(ge=1, le=16384)
    # Vision API のリクエストタイムアウト（秒）。大きな画像・動画フレームの解析に十分な時間を確保
    rag_vision_api_timeout: float = Field(ge=1.0, le=600.0)

    # HNSW パラメータ（ChromaDB ベクトルインデックス）
    # m, construction_ef はコレクション作成時のみ適用（変更には rebuild --mode full が必要）
    hnsw_m: int = Field(ge=2, le=100)
    hnsw_construction_ef: int = Field(ge=10, le=2000)
    # search_ef は起動時に collection.modify() で既存コレクションにも自動適用
    hnsw_search_ef: int = Field(ge=10, le=2000)

    # サイト一括取り込み（Scrapy subprocess）
    # download_only 指定時も source_store への git commit は実行される（後から rebuild で差分処理可能）
    site_ingest_delay_sec: float = Field(ge=0.05, le=60.0)
    site_ingest_max_pages: int = Field(ge=1, le=1000)
    site_ingest_download_timeout: int = Field(ge=1, le=300)
    site_ingest_timeout_sec: float = Field(ge=60.0, le=86400.0)
    # 連続エラーで早期停止し、対象サーバーへの過剰な負荷を防止
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

    @model_validator(mode="after")
    def validate_log_dir(self) -> RAGSettings:
        """rag_log_dir が空文字列の場合は設定ミスとして検出する."""
        if self.rag_log_dir is not None and self.rag_log_dir.strip() == "":
            raise ValueError(
                "rag_log_dir must be a non-empty directory path (use None/unset "
                "to disable file logging)"
            )
        return self


# lmstudio.toml の TOML パスと RAGSettings フィールド名の対応表（SSoT）
# 新フィールド追加時はここだけ更新する。_LMSTUDIO_FIELD_NAMES と _load_lmstudio_config
# は本定数から派生する。
_LMSTUDIO_TOML_PATHS: dict[str, tuple[str, ...]] = {
    "embedding_model_local": ("models", "embedding", "key"),
    "rag_vision_model": ("models", "vision", "key"),
}

# lmstudio.toml が SSoT のフィールド名（_LMSTUDIO_TOML_PATHS から派生、重複検出に使用）
_LMSTUDIO_FIELD_NAMES = frozenset(_LMSTUDIO_TOML_PATHS.keys())


class _LMStudioFlatData(TypedDict):
    """lmstudio.toml から読み込んだフラット辞書（RAGSettings 統合用）."""

    embedding_model_local: str
    rag_vision_model: str


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
    # config.toml に lmstudio.toml SSoT のフィールドが混入していないか検証
    lmstudio_overlap = set(data.keys()) & _LMSTUDIO_FIELD_NAMES
    if lmstudio_overlap:
        msg = (
            f"config.toml に lmstudio.toml で管理する設定が含まれています "
            f"（lmstudio.toml に移動してください）: {sorted(lmstudio_overlap)}"
        )
        raise ValueError(msg)
    # 未知のキーを検証
    toml_field_names = (
        frozenset(RAGSettings.model_fields.keys())
        - _ENV_FIELD_NAMES
        - _LMSTUDIO_FIELD_NAMES
    )
    unknown = set(data.keys()) - toml_field_names
    if unknown:
        msg = f"config.toml に未知の設定が含まれています: {sorted(unknown)}"
        raise ValueError(msg)
    return data


def _collect_lmstudio_paths(
    data: Any, prefix: tuple[str, ...] = (),
) -> set[tuple[str, ...]]:
    """lmstudio.toml の dict をリーフまで再帰し、全リーフパスを返す."""
    paths: set[tuple[str, ...]] = set()
    if isinstance(data, dict):
        for key, value in data.items():
            paths.update(_collect_lmstudio_paths(value, (*prefix, key)))
    else:
        paths.add(prefix)
    return paths


def _load_lmstudio_config() -> _LMStudioFlatData:
    """lmstudio.toml を読み込み、モデル key を辞書で返す.

    返り値のキーは RAGSettings の対応フィールド名（_LMSTUDIO_TOML_PATHS で定義）。
    TOML パスのいずれかが欠損している場合は ValueError を送出（fail-fast）。
    _LMSTUDIO_TOML_PATHS に列挙されていない未知のキーが含まれている場合も
    ValueError を送出する（config.toml の未知キー検証と同等の挙動）。
    キーが空文字列の場合は RAGSettings の Field(min_length=1) で fail-fast する。
    """
    if not _LMSTUDIO_TOML_FILE.exists():
        msg = f"lmstudio.toml が見つかりません: {_LMSTUDIO_TOML_FILE}"
        raise FileNotFoundError(msg)
    with open(_LMSTUDIO_TOML_FILE, "rb") as f:
        data = tomllib.load(f)
    result: dict[str, str] = {}
    for field_name, toml_path in _LMSTUDIO_TOML_PATHS.items():
        node: Any = data
        for segment in toml_path:
            if not isinstance(node, dict) or segment not in node:
                joined = ".".join(toml_path)
                msg = (
                    f"lmstudio.toml に必須フィールドが見つかりません: "
                    f"{joined} (path={_LMSTUDIO_TOML_FILE})"
                )
                raise ValueError(msg)
            node = node[segment]
        if not isinstance(node, str):
            joined = ".".join(toml_path)
            msg = (
                f"lmstudio.toml の {joined} は文字列である必要があります "
                f"(actual type={type(node).__name__}, path={_LMSTUDIO_TOML_FILE})"
            )
            raise ValueError(msg)
        result[field_name] = node
    # 未知キー/セクションの検出（必須フィールド検証後、config.toml の未知キー検証と整合）
    known_paths = set(_LMSTUDIO_TOML_PATHS.values())
    actual_paths = _collect_lmstudio_paths(data)
    unknown_paths = actual_paths - known_paths
    if unknown_paths:
        unknown_keys = sorted(".".join(p) for p in unknown_paths)
        msg = (
            f"lmstudio.toml に未知の設定が含まれています "
            f"(path={_LMSTUDIO_TOML_FILE}): {unknown_keys}"
        )
        raise ValueError(msg)
    # _LMSTUDIO_TOML_PATHS の全キーを result に格納したため、TypedDict として扱える
    return cast(_LMStudioFlatData, result)


@functools.lru_cache(maxsize=1)
def get_settings() -> RAGSettings:
    """キャッシュ付きでRAGSettingsインスタンスを返す.

    .env から環境依存値、config.toml から共通設定値、
    lmstudio.toml から LM Studio モデル key を取得し、統合した RAGSettings を返す。
    """
    env_loader = _EnvLoader()  # type: ignore[call-arg]  # pydantic-settings が .env/環境変数から読み込み
    toml_data = _load_toml_config()
    lmstudio_data = _load_lmstudio_config()
    return RAGSettings(
        **env_loader.model_dump(),
        **toml_data,
        **lmstudio_data,
    )


def log_fake_mode_status(settings: RAGSettings) -> None:
    """Fake モード設定の起動時ログを出力する.

    仕様: docs/specs/infrastructure/fake-mode.md
    Settings はシングルトン化されているため、各エントリポイントの起動時に
    1 回だけ呼び出される。
    """
    import logging

    logger = logging.getLogger("rag.config")
    if settings.rag_youtube_fake_mode:
        logger.warning(
            "[FAKE MODE: youtube] YouTube は FAKE モードで起動中（fixture: %s）。"
            "実 YouTube アクセスは発生しません。"
            "本番運用時は RAG_YOUTUBE_FAKE_MODE=false を .env に設定してください",
            settings.rag_youtube_fake_fixture_dir,
        )
    else:
        logger.info(
            "YouTube は REAL モードで起動中。実 YouTube アクセスが発生します"
        )

    if settings.rag_bluesky_fake_mode:
        logger.warning(
            "[FAKE MODE: bluesky] BlueSky は FAKE モードで起動中（fixture: %s）。"
            "実 BlueSky AT Protocol アクセスは発生しません。"
            "本番運用時は RAG_BLUESKY_FAKE_MODE=false を .env に設定してください",
            settings.rag_bluesky_fake_fixture_dir,
        )
    else:
        logger.info(
            "BlueSky は REAL モードで起動中。実 BlueSky AT Protocol アクセスが発生します"
        )

    if settings.rag_embedding_fake_mode:
        logger.warning(
            "[FAKE MODE: embedding] Embedding は FAKE モードで起動中（dimensions: %d）。"
            "本フラグはテスト専用フックです。本番運用では RAG_EMBEDDING_FAKE_MODE を設定しないでください",
            settings.rag_embedding_fake_dimensions,
        )
    # real モード（デフォルト）は通常運用のため INFO 出力なし

    if settings.rag_scrapy_fake_mode:
        logger.warning(
            "[FAKE MODE: web] Web (scrapy) は FAKE モードで起動中（fixture: %s）。"
            "subprocess による実 Web クロールは発生しません。"
            "本番運用時は RAG_WEB_FAKE_MODE=false を .env に設定してください",
            settings.rag_scrapy_fake_fixture_dir,
        )
    else:
        logger.info(
            "Web (scrapy) は REAL モードで起動中。実 Web クロールが発生します"
        )

    if settings.rag_zenn_fake_mode:
        logger.warning(
            "[FAKE MODE: zenn] Zenn は FAKE モードで起動中（fixture: %s）。"
            "実 Zenn API アクセスは発生しません。"
            "本番運用時は RAG_ZENN_FAKE_MODE=false を .env に設定してください",
            settings.rag_zenn_fake_fixture_dir,
        )
    else:
        logger.info(
            "Zenn は REAL モードで起動中。実 Zenn API アクセスが発生します"
        )

    if settings.rag_aozora_fake_mode:
        logger.warning(
            "[FAKE MODE: aozora] Aozora は FAKE モードで起動中（fixture: %s）。"
            "実 青空文庫 / GitHub Raw アクセスは発生しません。"
            "本番運用時は RAG_AOZORA_FAKE_MODE=false を .env に設定してください",
            settings.rag_aozora_fake_fixture_dir,
        )
    else:
        logger.info(
            "Aozora は REAL モードで起動中。実 青空文庫 / GitHub Raw アクセスが発生します"
        )



def _normalize_encoding(value: str) -> str:
    """エンコーディング名を比較可能な正規形式に変換する.

    "utf-8" / "UTF-8" / "utf8" / "UTF8" 等の表記揺れを "utf8" に正規化する。
    """
    return value.lower().replace("-", "").replace("_", "")


def check_utf8_environment(
    env: dict[str, str], stdout_encoding: str
) -> list[str]:
    """UTF-8 強制環境変数の違反項目を返す純粋関数（テスト容易性のため分離）.

    Args:
        env: 検証対象の環境変数マッピング
        stdout_encoding: 検証対象の sys.stdout.encoding 値

    Returns:
        違反項目を表すメッセージのリスト。違反なしなら空リスト。
    """
    errors: list[str] = []

    pyutf8 = env.get("PYTHONUTF8", "")
    if pyutf8 != "1":
        errors.append(f"  PYTHONUTF8={pyutf8!r}     (expected: '1')")

    pyio = env.get("PYTHONIOENCODING", "")
    if _normalize_encoding(pyio) != "utf8":
        errors.append(f"  PYTHONIOENCODING={pyio!r} (expected: 'utf-8')")

    if _normalize_encoding(stdout_encoding) != "utf8":
        errors.append(
            f"  sys.stdout.encoding={stdout_encoding!r} (expected: 'utf-8')"
        )

    return errors


def validate_utf8_environment() -> None:
    """起動時に UTF-8 強制環境変数を検証し、違反時 fail-fast で終了する.

    検証項目:
    - PYTHONUTF8 == "1"
    - PYTHONIOENCODING の正規化値が "utf8"
    - sys.stdout.encoding の正規化値が "utf8"

    違反時はエラーメッセージを stderr に出力し ``sys.exit(1)`` で終了する。
    silent な mojibake / UnicodeEncodeError 握り潰しの構造的予防が目的。

    エントリポイント（``rag.server`` / ``rag.cli`` / ``tests.conftest``）の
    起動直後に呼び出す。
    """
    stdout_encoding = getattr(sys.stdout, "encoding", "") or ""
    errors = check_utf8_environment(dict(os.environ), stdout_encoding)
    if not errors:
        return

    # ASCII-only message: stderr が cp932 等の場合に Japanese 出力が
    # UnicodeEncodeError を引き起こして exit code が紛れることを避ける
    msg_lines = [
        "ERROR: UTF-8 environment is not enforced.",
        *errors,
        "Set OS env to enforce UTF-8:",
        "  Windows: setx PYTHONUTF8 1 / setx PYTHONIOENCODING utf-8",
        "  Unix:    export PYTHONUTF8=1 / export PYTHONIOENCODING=utf-8",
    ]
    for line in msg_lines:
        print(line, file=sys.stderr)
    sys.exit(1)



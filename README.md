# RAG Knowledge

外部 Web ページの知識をベクトル DB に蓄積し、MCP サーバーとして検索機能を提供する RAG ナレッジサービス。

## 主な機能

| 機能 | 概要 |
|------|------|
| **Web クロール** | 外部ページを取得・HTML 解析・テキスト抽出 |
| **チャンキング** | テキストを適切なサイズに分割（見出し・テーブル対応） |
| **ベクトル検索** | ChromaDB による類似度検索 |
| **ハイブリッド検索** | ベクトル検索 + BM25 のスコア統合 |
| **全文取得** | ソースドキュメントの全文取得（変換済みテキスト/オリジナル） |
| **MCP サーバー** | FastMCP による stdio/HTTP インターフェース（CLI 薄層アダプター） |
| **評価 CLI** | 検索精度の評価パイプライン |
| **URL 安全性チェック** | Google Safe Browsing API による URL 検証 |
| **Zenn インジェスター** | Zenn 記事を API 経由で取得・ナレッジベースに取り込み |
| **BlueSky インジェスター** | BlueSky 投稿を AT Protocol API 経由で取得し、投稿および投稿内 URL をナレッジベースに取り込み |
| **YouTube インジェスター** | YouTube 動画の字幕・音声文字起こしを取得・ナレッジベースに取り込み |
| **ドキュメントインジェスター** | テキストドキュメント（Markdown、テキスト、PDF、AsciiDoc）をナレッジベースに取り込み |
| **Journal インジェスター** | 開発ジャーナル（セッション作業記録）をナレッジベースに登録・検索 |
| **サイト一括取り込み（Scrapy）** | Scrapy subprocess による大規模サイトの一括取り込み |
| **青空文庫インジェスター** | 青空文庫の著作権切れ作品をカタログ検索・取り込み |
| **コンテンツ一覧取得** | source_type 別に最新ソースを一覧取得（MCP + CLI） |
| **Upload HTTP API** | HTTP モードでのファイル直接アップロード（multipart/form-data） |
| **メディア解析** | 画像・動画を Vision モデル（LM Studio）でテキスト化し、RAG 検索対象に含める |
| **制約付き HTTP クライアント** | バジェット・サーキットブレーカー・レート制限を統合した安全な HTTP アクセス（py-common-lib 提供） |

## 動作環境

- **OS**: Windows 11（主要開発・運用環境）
- **ランタイム**: Python 3.11+
- **パッケージ管理**: uv

## 技術スタック

| カテゴリ | 技術 |
|---------|------|
| 言語 | Python 3.11+ |
| パッケージ管理 | uv |
| MCP SDK | FastMCP |
| HTTP クライアント | httpx |
| 制約付き HTTP クライアント | py-common-lib (ConstrainedClient) |
| ベクトル DB | ChromaDB（client/server 構成、HttpClient 接続） |
| キーワード検索 | BM25s |
| Embedding | OpenAI SDK / LM Studio (OpenAI 互換 API) |
| HTML 解析 | BeautifulSoup4 |
| HTML→Markdown 変換 | markdownify |
| PDF テキスト抽出 | pymupdf4llm / MinerU（CUDA 環境、未インストール時は pymupdf4llm にフォールバック） |
| YouTube 字幕取得 | youtube-transcript-api |
| YouTube メタデータ・音声DL | yt-dlp |
| 音声文字起こし | faster-whisper |
| Web クローラー（大規模サイト） | Scrapy |
| multipart フォーム解析 | python-multipart |
| YAML パーサー | PyYAML |
| gitignore パターン判定 | pathspec |
| Vision モデル（メディア解析） | LM Studio (OpenAI 互換 API) + Gemma 4 等 |
| 動画フレーム抽出 | ffmpeg |
| 画像処理 | Pillow |
| プロセス間排他制御 | ファイルベースロック（fcntl/msvcrt） |

## セットアップ

以下の順序でセットアップする:

1. **uv sync** — 依存パッケージのインストール
2. **.env コピー・編集** — 環境依存値の設定
3. **LM Studio 起動** — Embedding モデルの準備
4. **keyring 登録** — API キーの登録
5. **サーバー起動** — ChromaDB + MCP サーバー

HTTP モードで運用する場合は、ステップ 5 の後に「HTTP モードセットアップ」も参照。

### 前提: ffmpeg（メディア解析の動画処理に必要）

動画のフレーム抽出にはシステムに ffmpeg がインストールされている必要がある。画像のみの解析であれば ffmpeg は不要。

- [ffmpeg 公式サイト](https://ffmpeg.org/download.html) からダウンロード・インストール
- `ffmpeg` コマンドが PATH で利用可能であること

### 1. 依存パッケージのインストール

CUDA 環境と CPU 環境のどちらかを選択する。CUDA 環境は MinerU（高精度 PDF テキスト抽出）を含む。PDF 取り込みで高精度抽出が不要な場合は CPU 環境で十分。

#### CUDA 環境（NVIDIA GPU + CUDA 12.4）

```bash
uv sync
```

#### CPU 環境（GPU なし / AMD GPU）

MinerU + PyTorch を除外する。PDF 抽出は pymupdf4llm にフォールバック。

```bash
uv sync --no-group with-mineru
```

### 2. 環境設定

```bash
cp .env.example .env
```

`.env` を編集し、ストレージパス等を環境に合わせて設定する。主要な設定項目:

| 項目 | 説明 | デフォルト |
|------|------|-----------|
| `EMBEDDING_PROVIDER` | Embedding プロバイダー（`local` or `online`） | `local` |
| `LMSTUDIO_BASE_URL` | LM Studio の API エンドポイント | `http://localhost:1234/v1` |
| `CHROMADB_PERSIST_DIR` | ChromaDB データディレクトリ | `./chroma_db` |
| `RAG_TRANSPORT` | MCP トランスポート（`stdio` or `http`） | `stdio` |

### 3. LM Studio

`EMBEDDING_PROVIDER=local` の場合、LM Studio が必要:

1. [LM Studio](https://lmstudio.ai/) をインストール
2. Embedding モデル（config.toml の `embedding_model_local` に対応するモデル）を読み込み
3. LM Studio のサーバーを起動（デフォルト: `http://localhost:1234`）

### 4. API キー（keyring）

API キーは py-common-lib の `get_secret` で OS セキュアストレージから取得する（サービス名: `rag-knowledge`）。
登録方法は [py-common-lib の仕様書](https://github.com/becky3/py-common-lib/blob/main/docs/specs/infrastructure/secret-store.md) を参照。

| キー名 | 用途 | 必須条件 |
|--------|------|----------|
| `OPENAI_API_KEY` | OpenAI Embedding API | `EMBEDDING_PROVIDER=online` の場合 |
| `GOOGLE_SAFE_BROWSING_API_KEY` | URL 安全性チェック | config.toml の `rag_url_safety_check=true` の場合 |
| `UPLOAD_API_KEY` | Upload HTTP API 認証 | `RAG_TRANSPORT=http` の場合 |

`UPLOAD_API_KEY` は専用コマンドで生成・登録する:

```bash
uv run python -m rag.cli generate-api-key --save
```

クライアント側では `X-API-Key` ヘッダーに生成したキーを設定する。

### HTTP モードセットアップ（任意）

HTTP モード（`RAG_TRANSPORT=http`）で MCP サーバーを起動する場合の追加設定:

| 項目 | 説明 | デフォルト |
|------|------|-----------|
| `RAG_TRANSPORT` | `http` に設定 | `stdio` |
| `RAG_HTTP_HOST` | バインドアドレス | `127.0.0.1` |
| `RAG_HTTP_PORT` | リッスンポート | `8081` |
| `RAG_DNS_REBINDING_PROTECTION` | DNS リバインディング保護 | `true` |

API キー（`UPLOAD_API_KEY`）の事前登録が必要（上記「API キー」セクション参照）。

## 設定管理

設定値はセキュリティレベルに応じて3層に分離し、各値の取得元は1つに固定する（フォールバックなし）。

| 層 | 保管先 | git管理 | 分類基準 |
|---|--------|---------|---------|
| シークレット | OS セキュアストレージ (keyring) | 管理外 | 漏洩時に直接被害が発生する値（API キー、トークン、パスワード） |
| 環境依存値 | `.env` | 管理外 | デプロイ先・マシンごとに異なる値（接続先 URL、ストレージパス、ネットワーク設定、デバッグフラグ） |
| 共通設定値 | `config.toml` | **管理する** | プロジェクトとして統一管理する値（チューニングパラメータ、ポリシー設定、モデル名、機能フラグ） |

新しい設定値を追加する際は、上記の判断基準に従って適切な層に配置すること。詳細は [設定管理仕様](docs/specs/rag-knowledge.md#設定管理) を参照。

## 起動

### ChromaDB サーバー

MCP サーバー起動時に ChromaDB サーバーが自動起動される（`CHROMADB_AUTO_START=true`、デフォルト）。CLI 単体で使用する場合は手動起動が必要:

```bash
uv run chroma run --path <CHROMADB_PERSIST_DIR>
```

起動確認（heartbeat チェック）:

```bash
curl http://localhost:8000/api/v2/heartbeat
# 正常時: {"nanosecond heartbeat":<timestamp>}
```

### MCP サーバー / CLI

```bash
# MCP サーバー (stdio モード、デフォルト)
uv run python -m rag.server

# MCP サーバー (HTTP モード)
# .env で RAG_TRANSPORT=http を設定
uv run python -m rag.server

# CLI
uv run python -m rag.cli --help
```

### MCP クライアント設定（.mcp.json）

stdio モード:

```json
{
  "mcpServers": {
    "rag-knowledge": {
      "command": "uv",
      "args": ["run", "python", "-m", "rag.server"],
      "cwd": "<rag-knowledge リポジトリのパス>"
    }
  }
}
```

HTTP モード（`/mcp` パスが必要）:

```json
{
  "mcpServers": {
    "rag-knowledge": {
      "url": "http://localhost:8081/mcp"
    }
  }
}
```

## CLI コマンド一覧

| コマンド | 概要 |
|---------|------|
| `search` | ナレッジベースを検索 |
| `get-document` | ソースの全文を取得 |
| `crawl-zenn` | Zenn コンテンツを一括取り込み |
| `crawl-bluesky` | BlueSky 投稿を一括取り込み |
| `ingest-youtube` | YouTube 動画を取り込み |
| `ingest-youtube-playlist` | YouTube プレイリストを一括取り込み |
| `add-document` | ドキュメントファイルを取り込み |
| `crawl-documents` | ディレクトリ内ドキュメントを一括取り込み |
| `site-ingest` | Scrapy でサイトを一括取り込み（大規模サイト向け） |
| `update-aozora-catalog` | 青空文庫カタログを更新 |
| `search-aozora` | 青空文庫カタログを検索 |
| `ingest-aozora` | 青空文庫の作品を取り込み |
| `ingest-aozora-author` | 青空文庫の著者作品を一括取り込み |
| `add-journal` | ジャーナルエントリを登録 |
| `migrate-journal` | 既存ジャーナルファイルを一括配置 |
| `stats` | ナレッジベースの統計情報を表示 |
| `list-recent` | 指定 source_type のソースを新しい順で一覧取得 |
| `delete` | ソースをナレッジベースから論理削除 |
| `rebuild` | ナレッジベースを再構築 |
| `migrate` | metadata.db のスキーマをマイグレーション |
| `evaluate` | RAG 検索精度を評価 |
| `init-test-db` | テスト用 ChromaDB・BM25 初期化 |
| `generate-api-key` | Upload HTTP API 用の API キーを生成 |

各コマンドの詳細は `uv run python -m rag.cli <command> --help` を参照。MCP ツール一覧は [rag-knowledge.md](docs/specs/rag-knowledge.md) を参照。

## Journal CLI

### 単一エントリ登録

```bash
uv run python -m rag.cli add-journal --title "セッション記録" --file path/to/journal.md --repository rag-knowledge
```

| パラメータ | 短縮 | 必須 | 説明 |
|-----------|------|------|------|
| `--title` | `-t` | Yes | エントリタイトル |
| `--file` | `-f` | Yes | 本文 Markdown ファイルのパス。CLI がファイルを読み込んでコンテンツをインジェスターに渡す |
| `--repository` | `-r` | Yes | リポジトリ名 |
| `--entry-id` | `-e` | No | エントリ識別子（省略時は自動生成） |

### 既存ジャーナル一括取り込み（マイグレーション）

```bash
uv run python -m rag.cli migrate-journal --dir <path> --repository <name>
# 事後: uv run python -m rag.cli rebuild --mode incremental
```

| パラメータ | 短縮 | 必須 | 説明 |
|-----------|------|------|------|
| `--dir` | `-d` | Yes | ジャーナルファイルが格納されたディレクトリパス |
| `--repository` | `-r` | Yes | リポジトリ名（メタデータに記録） |

## RAG 評価 CLI

評価用フィクスチャ（テスト文書・評価データセット）はリポジトリに含まれない。ローカルに用意したフィクスチャを `--fixture` / `--dataset` で指定して使用する。

```bash
# テスト用 DB 初期化
uv run python -m rag.cli init-test-db \
  --chunk-size 200 --chunk-overlap 30 \
  --persist-dir .tmp/test_chroma_db \
  --bm25-persist-dir .tmp/test_bm25_index \
  --bm25-k1 1.5 --bm25-b 0.75 \
  --fixture <path/to/rag_test_documents.json>

# 検索精度評価
uv run python -m rag.cli evaluate \
  --persist-dir .tmp/test_chroma_db \
  --output-dir .tmp/rag-evaluation \
  --chunk-size 200 --chunk-overlap 30 \
  --vector-weight 0.6 \
  --bm25-k1 1.5 --bm25-b 0.75 \
  --fixture <path/to/rag_test_documents.json> \
  --dataset <path/to/rag_evaluation_dataset.json>
```

## テスト

```bash
uv run pytest          # pytest-xdist で自動並列実行（-n auto）
uv run pytest -n0      # シングルプロセスで実行（デバッグ時）
uv run ruff check .
uv run mypy src
```

## CI/CD

PR 作成・更新時に GitHub Actions で品質チェックが自動実行される。全チェックの通過が develop / main へのマージ条件。

| ワークフロー | トリガー | 概要 |
|-------------|---------|------|
| Quality Check | PR (develop, main) | テスト・lint・型チェック・markdownlint |
| Validate Enums | PR・push (develop, main) | enum スキーマの整合性検証 |
| Raw HTTP Check | PR・push (develop, main) | Python ソース内の raw HTTP 使用検出 |
| Copilot Auto Fix | PR | Copilot レビューコメントの自動修正 |
| Claude Code | Issue 作成・ラベル付け・Issue comment・PR comment・PR review | メンション応答・自動実装 |
| Late Review Scanner | スケジュール（毎時） | 24 時間以上レビュー待ちの PR を検出 |
| Post Merge | PR close | マージ後の review-batch Issue 更新 |

品質チェック（Quality Check）は [shared-workflows](https://github.com/becky3/shared-workflows) の reusable workflow を使用。

## プロジェクト構成

プロジェクトのディレクトリ構成・モジュール責務・仕様書との対応は [ARCHITECTURE.md](ARCHITECTURE.md) を参照。

## Git 運用

git-flow ベースのブランチ戦略を採用。詳細は `~/.claude/docs/specs/workflows/git-flow.md` を参照。

- **常設ブランチ**: `main`（安定版）/ `develop`（開発統合）
- **作業ブランチ**: `feature/{機能名}-#{Issue番号}` / `bugfix/{修正内容}-#{Issue番号}`
- コミット: `type(scope): 説明 (#Issue番号)` ※scope は仕様書ファイル名（拡張子なし）
- PR は `develop` をベースに作成

## 開発ガイドライン

**開発を始める前に必ず [CLAUDE.md](CLAUDE.md) を読んでください。**

## ドキュメント

### 全体仕様

- [全体仕様概要](docs/specs/overview.md)

### 基盤仕様

- [RAG ナレッジ](docs/specs/rag-knowledge.md)
- [検索レスポンス + 全文取得](docs/specs/search-response.md)
- [source_store](docs/specs/source-store.md)
- [パイプライン制御](docs/specs/pipeline-controller.md)
- [コンバーター](docs/specs/converter.md)
- [インデクサー](docs/specs/indexer.md)
- [サイト一括取り込み（Scrapy）](docs/specs/site-ingest.md)
- [再構築・統計・バックアップ](docs/specs/rebuild-stats.md)
- [コンテンツ一覧取得](docs/specs/infrastructure/content-listing.md)
- [コンテンツアップロード](docs/specs/infrastructure/content-upload.md)
- [Upload HTTP API 認証](docs/specs/infrastructure/upload-auth.md)
- [定期 index rebuild](docs/specs/infrastructure/scheduled-rebuild.md)
- [メディア解析](docs/specs/infrastructure/media-analysis.md)

### インジェスター仕様

- [インジェスター共通仕様](docs/specs/ingesters/common.md)
- [BlueSky インジェスター](docs/specs/ingesters/bluesky.md)
- [Zenn インジェスター](docs/specs/ingesters/zenn.md)
- [YouTube インジェスター](docs/specs/ingesters/youtube.md)
- [Local インジェスター](docs/specs/ingesters/local.md)
- [Journal インジェスター](docs/specs/ingesters/journal.md)
- [青空文庫インジェスター](docs/specs/ingesters/aozora.md)

### Claude Code 拡張（agentic）

**プロジェクト固有スキル:**

- `/test-run` — テスト実行・コード品質チェック（`.claude/skills/test-run/SKILL.md`）
- `/qa` — MCP・CLI・HTTP API の動作確認（`.claude/skills/qa/SKILL.md`）

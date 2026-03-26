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
| **MCP サーバー** | FastMCP による stdio/HTTP インターフェース |
| **評価 CLI** | 検索精度の評価パイプライン |
| **URL 安全性チェック** | Google Safe Browsing API による URL 検証 |
| **クロールプレビュー** | クロール対象ページのタイトル・URL 一覧を事前確認 |
| **Zenn インジェスター** | Zenn 記事を API 経由で取得・ナレッジベースに取り込み |
| **BlueSky インジェスター** | BlueSky 投稿を AT Protocol API 経由で取得し、投稿および投稿内 URL をナレッジベースに取り込み |
| **YouTube インジェスター** | YouTube 動画の字幕・音声文字起こしを取得・ナレッジベースに取り込み |
| **ドキュメントインジェスター** | テキストドキュメント（Markdown、テキスト、PDF、AsciiDoc）をナレッジベースに取り込み |
| **Journal インジェスター** | 開発ジャーナル（セッション作業記録）をナレッジベースに登録・検索 |
| **サイト一括取り込み（Scrapy）** | Scrapy subprocess による大規模サイトの一括取り込み |
| **青空文庫インジェスター** | 青空文庫の著作権切れ作品をカタログ検索・取り込み |
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
| ベクトル DB | ChromaDB |
| キーワード検索 | BM25s |
| Embedding | OpenAI SDK / LM Studio (OpenAI 互換 API) |
| HTML 解析 | BeautifulSoup4 |
| HTML→Markdown 変換 | markdownify |
| PDF テキスト抽出 | pymupdf4llm / MinerU（CUDA 環境、未インストール時は pymupdf4llm にフォールバック） |
| YouTube 字幕取得 | youtube-transcript-api |
| YouTube メタデータ・音声DL | yt-dlp |
| 音声文字起こし | faster-whisper |
| Web クローラー（大規模サイト） | Scrapy |
| YAML パーサー | PyYAML |

## セットアップ

### CUDA 環境（開発用、デフォルト）

NVIDIA GPU + CUDA 12.4 環境向け。MinerU + PyTorch (CUDA 12.4) を含む全依存が自動インストールされる。

```bash
uv sync
cp .env.example .env  # 環境依存値を編集
```

### CPU 環境（GPU なし / AMD GPU）

MinerU + PyTorch を除外してセットアップする。PDF 抽出は pymupdf4llm にフォールバックする。

```bash
uv sync --no-group with-mineru
cp .env.example .env  # 環境依存値を編集
```

### API キー

API キーは py-common-lib の `get_secret` で OS セキュアストレージから取得する（サービス名: `rag-knowledge`）。
登録方法は [py-common-lib の仕様書](https://github.com/becky3/py-common-lib/blob/main/docs/specs/infrastructure/secret-store.md) を参照。

## 設定管理

設定値はセキュリティレベルに応じて3層に分離し、各値の取得元は1つに固定する（フォールバックなし）。

| 層 | 保管先 | git管理 | 分類基準 |
|---|--------|---------|---------|
| シークレット | OS セキュアストレージ (keyring) | 管理外 | 漏洩時に直接被害が発生する値（API キー、トークン、パスワード） |
| 環境依存値 | `.env` | 管理外 | デプロイ先・マシンごとに異なる値（接続先 URL、ストレージパス、ネットワーク設定、デバッグフラグ） |
| 共通設定値 | `config.toml` | **管理する** | プロジェクトとして統一管理する値（チューニングパラメータ、ポリシー設定、モデル名、機能フラグ） |

新しい設定値を追加する際は、上記の判断基準に従って適切な層に配置すること。詳細は [設定管理仕様](docs/specs/rag-knowledge.md#設定管理) を参照。

## 起動

```bash
# MCP サーバー (stdio モード、デフォルト)
uv run python -m rag.server

# MCP サーバー (HTTP モード)
# .env で RAG_TRANSPORT=http を設定
uv run python -m rag.server

# CLI
uv run python -m rag.cli --help
```

## YouTube インジェスター利用時の注意

YouTube インジェスターは非公式 API（youtube-transcript-api）を使用して字幕を取得する。短時間に多数のリクエストを送ると YouTube に IP をブロックされる場合がある。

**実測データ（ローカル PC 環境）:**

- 約 20 動画を 30 分間で取り込んだ時点で字幕取得 API（youtube-transcript-api）の IP ブロックが発生
- ブロックは字幕取得 API（youtube-transcript-api）のみに影響し、メタデータ取得・音声ダウンロード（yt-dlp）は継続動作
- IP ブロック時は Whisper フォールバックせずエラーとしてスキップされる（品質低下防止のため）
- ブロックは一時的（通常は数十分〜数時間で解除）

**推奨運用:**

- プレイリスト一括取り込み時は `--max-videos` で段階的に取り込む（1 回あたり 10〜20 動画推奨）
- `rag_youtube_request_interval`（デフォルト: 5.0 秒）を短くしすぎない
- IP ブロックが発生した場合は時間を置いて再実行する

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

### インジェスター仕様

- [インジェスター共通仕様](docs/specs/ingesters/common.md)
- [Web インジェスター](docs/specs/ingesters/web.md)
- [BlueSky インジェスター](docs/specs/ingesters/bluesky.md)
- [Zenn インジェスター](docs/specs/ingesters/zenn.md)
- [YouTube インジェスター](docs/specs/ingesters/youtube.md)
- [Local インジェスター](docs/specs/ingesters/local.md)
- [Journal インジェスター](docs/specs/ingesters/journal.md)
- [青空文庫インジェスター](docs/specs/ingesters/aozora.md)

### Claude Code 拡張（agentic）

**プロジェクト固有スキル:**

- `/test-run` — テスト実行・コード品質チェック（`.claude/skills/test-run/SKILL.md`）

# RAG Knowledge

外部 Web ページの知識をベクトル DB に蓄積し、MCP サーバーとして検索機能を提供する RAG ナレッジサービス。

## 主な機能

| 機能 | 概要 |
|------|------|
| **Web クロール** | 外部ページを取得・HTML 解析・テキスト抽出 |
| **チャンキング** | テキストを適切なサイズに分割（見出し・テーブル対応） |
| **ベクトル検索** | ChromaDB による類似度検索 |
| **ハイブリッド検索** | ベクトル検索 + BM25 のスコア統合 |
| **MCP サーバー** | FastMCP による stdio/HTTP インターフェース |
| **評価 CLI** | 検索精度の評価パイプライン |
| **URL 安全性チェック** | Google Safe Browsing API による URL 検証 |
| **クロールプレビュー** | クロール対象ページのタイトル・URL 一覧を事前確認 |
| **制約付き HTTP クライアント** | バジェット・サーキットブレーカー・レート制限を統合した安全な HTTP アクセス |

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
| HTTP クライアント | aiohttp |
| ベクトル DB | ChromaDB |
| キーワード検索 | BM25s |
| Embedding | OpenAI SDK / LM Studio (OpenAI 互換 API) |
| HTML 解析 | BeautifulSoup4 |
| HTML→Markdown 変換 | markdownify |

## セットアップ

```bash
uv sync
cp .env.example .env  # Embedding 設定等を編集
```

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

## RAG 評価 CLI

```bash
# テスト用 DB 初期化
uv run python -m rag.cli init-test-db \
  --chunk-size 200 --chunk-overlap 30 \
  --persist-dir .tmp/test_chroma_db \
  --bm25-persist-dir .tmp/test_bm25_index \
  --fixture tests/fixtures/rag_test_documents.json

# 検索精度評価
uv run python -m rag.cli evaluate \
  --persist-dir .tmp/test_chroma_db \
  --output-dir .tmp/rag-evaluation \
  --chunk-size 200 --chunk-overlap 30 \
  --vector-weight 0.6 \
  --bm25-k1 1.5 --bm25-b 0.75
```

## テスト

```bash
uv run pytest
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

### Claude Code 拡張（agentic）

**プロジェクト固有エージェント:**

- [Test Runner エージェント](docs/specs/agentic/agents/test-runner-agent.md)

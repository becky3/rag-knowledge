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

## 動作環境

- **OS**: Windows 11（主要開発・運用環境）
- **ランタイム**: Python 3.11+
- **パッケージ管理**: uv

## セットアップ

```bash
uv sync
cp .env.example .env  # Embedding 設定等を編集
```

## 起動

```bash
# MCP サーバー (stdio モード)
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

## 開発ガイドライン

**開発を始める前に必ず [CLAUDE.md](CLAUDE.md) を読んでください。**

## プロジェクト構成

```
src/rag/           # RAG サービスのソースコード
  embedding/       # Embedding プロバイダー
tests/             # テスト
  fixtures/        # テスト用フィクスチャ
scripts/           # 評価・分析スクリプト
docs/
  specs/           # 仕様書
```


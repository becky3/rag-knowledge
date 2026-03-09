# RAG Knowledge - 開発ガイドライン

## プロジェクト基盤情報

@docs/specs/rag-knowledge.md

## プロジェクト概要

RAG ナレッジサービス。外部 Web ページをクロール・チャンキングしてベクトル DB に蓄積し、MCP サーバーとして検索機能を提供する。

## 技術スタック

- Python 3.11+ / uv
- MCP SDK (FastMCP)
- aiohttp (HTTP クライアント)
- ChromaDB (ベクトル DB)
- BM25s (BM25 検索)
- OpenAI SDK (Embedding)
- BeautifulSoup4 (HTML 解析)

## ディレクトリ構成

| パス | 説明 |
|------|------|
| `src/rag/` | RAG サービスのソースコード |
| `src/rag/embedding/` | Embedding プロバイダー |
| `tests/` | テストファイル |
| `tests/fixtures/` | テスト用フィクスチャ |
| `scripts/` | 評価・分析スクリプト |
| `docs/specs/` | 仕様書 |

## 起動方法

```bash
# MCP サーバー (stdio モード)
uv run python -m rag.server

# CLI
uv run python -m rag.cli --help
```

## テスト・品質チェック

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

## Claude Code 拡張機能

### 自律呼び出しルール

| ユーザー表現 | 呼び出し先 | 種別 |
|-------------|-----------|------|
| 「テスト実行して」「テスト通して」 | test-runner | エージェント |

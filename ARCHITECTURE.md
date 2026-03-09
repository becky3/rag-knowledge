# アーキテクチャガイド

本ドキュメントはプロジェクトのディレクトリ構成とモジュール責務をフォルダ単位で記述する。ファイル単位の詳細は意図的に省略しており、各モジュールの役割と関係性の把握を目的とする。

## トップレベル構造

| ディレクトリ | 説明 |
|---|---|
| `src/rag/` | RAG サービス本体 |
| `docs/` | 仕様書 |
| `tests/` | テストコード・フィクスチャ |
| `scripts/` | 評価・分析スクリプト |
| `.claude/` | Claude Code プロジェクト設定 |
| `.github/` | GitHub Actions ワークフロー・PR テンプレート |

## src/rag/ モジュール構成

### サブディレクトリ

| ディレクトリ | 責務 |
|---|---|
| `src/rag/embedding/` | Embedding プロバイダー抽象化（ローカル / OpenAI）とファクトリ |

### ルートレベルファイル

| ファイル | 責務 |
|---|---|
| `src/rag/server.py` | MCP サーバー（FastMCP）エントリーポイント・ツール定義 |
| `src/rag/cli.py` | CLI エントリーポイント（評価・DB 初期化） |
| `src/rag/config.py` | pydantic-settings による環境変数・設定管理 |
| `src/rag/rag_knowledge.py` | ナレッジサービス（取り込み・検索・削除のオーケストレーション） |
| `src/rag/web_crawler.py` | Web クローラー（ページ取得・本文抽出・SSRF 対策・robots.txt 遵守） |
| `src/rag/vector_store.py` | ベクトルストア（ChromaDB による Embedding 格納・検索） |
| `src/rag/bm25_index.py` | BM25 インデックス（日本語形態素解析・ディスク永続化） |
| `src/rag/hybrid_search.py` | ハイブリッド検索エンジン（ベクトル + BM25 スコア統合） |
| `src/rag/chunker.py` | テキストチャンカー（段落・文・文字数ベース分割） |
| `src/rag/heading_chunker.py` | 見出しチャンカー（見出し単位分割・階層情報保持） |
| `src/rag/table_chunker.py` | テーブルチャンカー（行単位分割・ヘッダー付加） |
| `src/rag/content_detector.py` | コンテンツタイプ検出（通常テキスト・テーブル・見出し構造） |
| `src/rag/evaluation.py` | 検索精度評価ツール（Precision・Recall・F1・NDCG・MRR） |
| `src/rag/safe_browsing.py` | URL 安全性チェック（Google Safe Browsing API） |

## 補助ディレクトリ

| ディレクトリ | 説明 |
|---|---|
| `docs/specs/` | 機能仕様書・エージェント定義（実装の根拠） |
| `tests/` | pytest テストコード |
| `tests/fixtures/` | テスト用フィクスチャ（評価データセット・テスト文書） |
| `scripts/` | 評価データ収集・パラメータスイープ・分析スクリプト |
| `.claude/` | Claude Code プロジェクト設定（エージェント・スキル） |
| `.github/` | GitHub Actions ワークフロー・PR テンプレート |

## 仕様書 — 実装モジュール対応表

| 仕様書 | 実装モジュール |
|---|---|
| `rag-knowledge.md` | `src/rag/` 全体 |

### agentic/

| 仕様書 | 対象 |
|---|---|
| `agentic/agents/test-runner-agent.md` | テスト実行・品質チェックエージェント |

## 関連ドキュメント

- [全体仕様概要](docs/specs/overview.md) — 機能一覧・技術スタック
- 仕様書スタイルガイド（`~/.claude/docs/specs/style-guide.md`）— 仕様書の分類・命名規則・記述ルール
- [CLAUDE.md](CLAUDE.md) — 開発ガイドライン

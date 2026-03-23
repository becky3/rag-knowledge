# アーキテクチャガイド

本ドキュメントはプロジェクトのディレクトリ構成とモジュール責務をフォルダ単位で記述する。ファイル単位の詳細は意図的に省略しており、各モジュールの役割と関係性の把握を目的とする。

## トップレベル構造

| ディレクトリ | 説明 |
|---|---|
| `src/rag/` | RAG サービス本体 |
| `docs/specs/` | 機能仕様書・エージェント定義（実装の根拠） |
| `tests/` | pytest テストコード |
| `tests/fixtures/` | テスト用フィクスチャ（評価データセット・テスト文書） |
| `scripts/` | 評価データ収集・パラメータスイープ・分析・CI チェックスクリプト |
| `.claude/` | Claude Code プロジェクト設定（エージェント・スキル） |
| `.github/` | GitHub Actions ワークフロー |

## src/rag/ モジュール構成

### サブディレクトリ

| ディレクトリ | 責務 |
|---|---|
| `src/rag/embedding/` | Embedding プロバイダー抽象化（ローカル / OpenAI）とファクトリ |
| `src/rag/ingesters/` | インジェスタープラグイン（BaseIngester 抽象基底・IngestedContent 共通モデル・WebIngester・ZennIngester・DocumentIngester・BlueskyIngester） |
| `src/rag/pipeline/` | パイプライン制御（git 操作・差分検知・ステージ間連携・4モード実行） |
| `src/rag/pipeline/worker.py` | MCP 用サブプロセスエントリポイント（`python -m rag.pipeline.worker` で起動、クラッシュ耐性確保） |
| `src/rag/pipeline/factory.py` | PipelineController のファクトリ関数（server/cli/worker 共通） |
| `src/rag/pipeline/ingesters/` | 新アーキテクチャ用インジェスター（source_store へのファイル配置 + .meta 生成） |
| `src/rag/store/` | source_store 管理（ファイル配置・.meta 読み書き・metadata.db 操作・URL パス変換） |

### ルートレベルファイル

| ファイル | 責務 |
|---|---|
| `src/rag/server.py` | MCP サーバー（FastMCP）エントリーポイント・ツール定義 |
| `src/rag/cli.py` | CLI エントリーポイント（評価・DB 初期化） |
| `src/rag/config.py` | pydantic-settings による環境変数・設定管理 |
| `src/rag/rag_knowledge.py` | ナレッジサービス（取り込み・検索・削除のオーケストレーション） |
| `src/rag/markdown.py` | RAG 用 Markdown コンバーター（リンク・画像 URL 除去） |
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

## 仕様書 — 実装モジュール対応表

| 仕様書 | 実装モジュール |
|---|---|
| `rag-knowledge.md` | `src/rag/` 全体 |
| `source-store.md` | `src/rag/store/` |
| `pipeline-controller.md` | `src/rag/pipeline/` |
| `ingesters/common.md` | `src/rag/pipeline/ingesters/_common.py` |
| `ingesters/web.md` | `src/rag/pipeline/ingesters/web.py` |
| `ingesters/bluesky.md` | `src/rag/pipeline/ingesters/bluesky.py` |
| `ingesters/youtube.md` | `src/rag/pipeline/ingesters/youtube.py` |
| `ingesters/zenn.md` | `src/rag/pipeline/ingesters/zenn.py` |
| `ingesters/local.md` | `src/rag/pipeline/ingesters/local.py` |
| `ingesters/aozora.md` | `src/rag/pipeline/ingesters/aozora.py` |

### Claude Code 拡張

テストランナーエージェント（test-runner）は dotfiles（`~/.claude/agents/test-runner.md`）で汎用定義されており、プロジェクト固有の `/test-run` スキル（`.claude/skills/test-run/SKILL.md`）に委譲して実行する。

## 関連ドキュメント

- [全体仕様概要](docs/specs/overview.md) — 機能一覧・技術スタック
- 仕様書スタイルガイド（`~/.claude/docs/specs/style-guide.md`）— 仕様書の分類・命名規則・記述ルール
- [CLAUDE.md](CLAUDE.md) — 開発ガイドライン

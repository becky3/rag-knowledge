# RAG Knowledge — 全体仕様概要

## 1. プロダクト概要

RAG Knowledge は、外部 Web ページから収集した知識をベクトル DB に蓄積し、MCP サーバーとして検索機能を提供する RAG（Retrieval-Augmented Generation）基盤である。MCP クライアントからのクエリに対して関連情報を検索・提供する。

## 2. 機能一覧

| # | 機能名 | 概要 | 仕様書 |
|---|--------|------|--------|
| 1 | Web クロール | 外部ページを取得・HTML 解析・テキスト抽出 | [rag-knowledge.md](rag-knowledge.md) |
| 2 | チャンキング | テキストを適切なサイズに分割（見出し・テーブル対応） | [rag-knowledge.md](rag-knowledge.md) |
| 3 | ベクトル検索 | ChromaDB による類似度検索 | [rag-knowledge.md](rag-knowledge.md) |
| 4 | ハイブリッド検索 | ベクトル検索 + BM25 のスコア統合 | [rag-knowledge.md](rag-knowledge.md) |
| 5 | MCP サーバー | FastMCP による stdio/HTTP インターフェース | [rag-knowledge.md](rag-knowledge.md) |
| 6 | 評価 CLI | 検索精度の評価パイプライン | [rag-knowledge.md](rag-knowledge.md) |
| 7 | URL 安全性チェック | Google Safe Browsing API による URL 検証 | [rag-knowledge.md](rag-knowledge.md) |
| 8 | クロールプレビュー | クロール対象ページのタイトル・URL 一覧を事前確認 | [rag-knowledge.md](rag-knowledge.md) |
| 9 | Zenn インジェスター | Zenn 記事を API 経由で取得・ナレッジベースに取り込み | [zenn-ingester.md](zenn-ingester.md) |
| 10 | BlueSky インジェスター | BlueSky 投稿を AT Protocol API 経由で取得・ナレッジベースに取り込み | [bluesky-ingester.md](bluesky-ingester.md) |
| 11 | ドキュメントインジェスター | テキストドキュメントをナレッジベースに取り込み | [document-ingester.md](document-ingester.md) |

## 3. 技術スタック

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
| PDF テキスト抽出 | pymupdf4llm |
| 設定管理 | pydantic-settings (.env + config.toml) |

## 4. 開発方針

### 仕様駆動開発

1. GitHub Issue で機能・タスクを管理
2. 各機能の仕様書を先に作成・承認
3. 仕様書に基づいて実装・テスト

### 仕様書スタイルガイド

仕様書の分類・命名規則・記述ルールは仕様書スタイルガイド（`~/.claude/docs/specs/style-guide.md`）を参照。

### 仕様書テンプレート

共通テンプレートは agent-commons（`~/.claude/docs/templates/`）で管理。一覧は `~/.claude/docs/overview.md` を参照。

### Git 運用（git-flow）

git-flow ベースのブランチ戦略を採用。詳細は `~/.claude/docs/specs/workflows/git-flow.md` を参照。

- **常設ブランチ**: `main`（安定版）/ `develop`（開発統合）
- **作業ブランチ**:
  - `feature/{機能名}-#{Issue番号}` — 新機能（`develop` → `develop`）
  - `bugfix/{修正内容}-#{Issue番号}` — バグ修正（`develop` → `develop` / `release/*` → `release/*`）
  - `release/v{X.Y.Z}` — リリース準備（`develop` → `main` squash マージ）
  - `hotfix/{修正内容}-#{Issue番号}` — 緊急修正（`main` → `main` + `develop`）
- コミット: `type(scope): 説明 (#Issue番号)` ※scope は仕様書ファイル名（拡張子なし）
- PR 作成時に `Closes #{Issue番号}` で Issue を紐付け

## 5. Claude Code 拡張（agentic）

**プロジェクト固有スキル:**

- `/test-run` — テスト実行・コード品質チェック（`.claude/skills/test-run/SKILL.md`）

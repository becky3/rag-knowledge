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
| 5 | MCP サーバー | FastMCP による stdio/HTTP インターフェース（CLI 薄層アダプター） | [rag-knowledge.md](rag-knowledge.md) |
| 6 | 評価 CLI | 検索精度の評価パイプライン | [rag-knowledge.md](rag-knowledge.md) |
| 7 | URL 安全性チェック | Google Safe Browsing API による URL 検証 | [rag-knowledge.md](rag-knowledge.md) |
| 8 | source_store | 全データの根源ストレージ（git 管理、.meta サイドカー） | [source-store.md](source-store.md) |
| 9 | パイプライン制御 | 3段パイプラインのステージ間連携・差分更新制御 | [pipeline-controller.md](pipeline-controller.md) |
| 10 | コンバーター | source_store のファイルを converted_store のテキストに変換 | [converter.md](converter.md) |
| 11 | インデクサー | converted_store からチャンキング・Embedding・インデックス構築 | [indexer.md](indexer.md) |
| 12 | インジェスター共通仕様 | インジェスターの共通制約・重複検出・パイプライン通知 | [ingesters/common.md](ingesters/common.md) |
| 13 | BlueSky インジェスター | AT Protocol API 経由の投稿取得 | [ingesters/bluesky.md](ingesters/bluesky.md) |
| 14 | Zenn インジェスター | Zenn API 経由の記事・スクラップ取得 | [ingesters/zenn.md](ingesters/zenn.md) |
| 15 | ドキュメントインジェスター | テキストドキュメント（Markdown、テキスト、PDF、AsciiDoc）の取り込み | [ingesters/local.md](ingesters/local.md) |
| 16 | 検索レスポンス + 全文取得 | チャンク単位検索レスポンスと全文取得ツール | [search-response.md](search-response.md) |
| 17 | 再構築・統計・バックアップ | パイプライン再構築の MCP/CLI 公開・統計拡張・バックアップ手順 | [rebuild-stats.md](rebuild-stats.md) |
| 18 | サイト一括取り込み（Scrapy subprocess） | Scrapy subprocess による大規模サイトの一括取り込み | [site-ingest.md](site-ingest.md) |
| 19 | YouTube インジェスター | YouTube 動画の字幕・音声文字起こし取得 | [ingesters/youtube.md](ingesters/youtube.md) |
| 20 | 青空文庫インジェスター | 青空文庫の著作権切れ作品を取り込み | [ingesters/aozora.md](ingesters/aozora.md) |
| 21 | Journal インジェスター | 開発ジャーナルの登録・検索 | [ingesters/journal.md](ingesters/journal.md) |
| 22 | コンテンツ一覧取得 | source_type 別の最新ソース一覧取得 | [infrastructure/content-listing.md](infrastructure/content-listing.md) |
| 23 | コンテンツアップロード | HTTP モードでのファイル直接アップロード（multipart/form-data） | [infrastructure/content-upload.md](infrastructure/content-upload.md) |
| 24 | Upload HTTP API 認証 | API キー認証・バインドアドレス制約 | [infrastructure/upload-auth.md](infrastructure/upload-auth.md) |
| 25 | 定期 index rebuild | 条件付き index rebuild の自動実行 | [infrastructure/scheduled-rebuild.md](infrastructure/scheduled-rebuild.md) |
| 26 | メディア解析 | 画像・動画の Vision モデルによるテキスト変換 | [infrastructure/media-analysis.md](infrastructure/media-analysis.md) |
| 27 | BM25 スケーラビリティ | BM25 インデックスの SQLite 外部化・トークンキャッシュによる大規模対応 | [infrastructure/bm25-scalability.md](infrastructure/bm25-scalability.md) |
| 28 | Fake モード基盤 | 外部 API・ライブラリの Fake Adapter 注入による実アクセス排除（テスト・QA・運用環境共通） | [infrastructure/fake-mode.md](infrastructure/fake-mode.md) |
| 29 | YouTube Fake Adapter | YouTube インジェスター用 FakeYoutubeFetcher（シナリオ切替・JSON 返却） | [infrastructure/fake-adapters/youtube.md](infrastructure/fake-adapters/youtube.md) |
| 30 | QA 戦略（3 レイヤー） | L1 Unit Test / L2 Mock E2E / L3 本番相当 QA の責務分離と実施頻度ポリシー | [workflows/qa-strategy.md](workflows/qa-strategy.md) |
| 31 | アーキテクチャ採用方針 | rag-knowledge のアーキテクチャ説明・正解パターン（youtube）・構造判断 SSoT | [architecture.md](architecture.md) |
| 32 | BlueSky Fake Adapter | BlueSky AT Protocol 用 Fake Fetcher + MediaDownloader | [infrastructure/fake-adapters/bluesky.md](infrastructure/fake-adapters/bluesky.md) |
| 33 | Scrapy Fake Adapter | site-ingest 用 FakeScrapyRunner（subprocess 起動なし、JSONL+HTML fixture 展開） | [infrastructure/fake-adapters/scrapy.md](infrastructure/fake-adapters/scrapy.md) |
| 34 | Zenn Fake Adapter | Zenn API 用 FakeZennFetcher | [infrastructure/fake-adapters/zenn.md](infrastructure/fake-adapters/zenn.md) |
| 35 | Aozora Fake Adapter | 青空文庫用 FakeAozoraFetcher（カタログ ZIP / XHTML を fixture から bytes 返却） | [infrastructure/fake-adapters/aozora.md](infrastructure/fake-adapters/aozora.md) |
| 36 | Local Fake Adapter | Local 用 FakeLocalFetcher（filesystem 抽象化のみ。PDF/AsciiDoc 抽出は converter 層） | [infrastructure/fake-adapters/local.md](infrastructure/fake-adapters/local.md) |

## 3. 技術スタック

技術スタックの一覧は [README.md の「技術スタック」セクション](../../README.md#技術スタック) を参照（SSoT）。

## 4. 開発方針

### 仕様駆動開発

1. GitHub Issue で機能・タスクを管理
2. 各機能の仕様書を先に作成・承認
3. 仕様書に基づいて実装・テスト

### SSoT（Single Source of Truth）階層

設定制約や列挙値の正規定義を一元管理し、仕様書での値の重複を防ぐ。

| 情報 | SSoT | 仕様書の役割 |
|------|------|-------------|
| 設定制約（min/max/default） | pydantic Field（`src/rag/config.py`） | 設計意図（Why）のみ |
| 横断列挙値（source_type 等） | [`_schema/enums.yml`](../../_schema/enums.yml) | 参照リンク |
| ConstrainedClient 定数 | [py-common-lib `constrained_client`](https://github.com/becky3/py-common-lib/blob/main/src/py_common_lib/httpx/constrained_client.py) | 参照リンク + Why |
| 振る舞い/設計制約 | 仕様書「制約」セクション | 正規の定義 |

SSoT 階層の詳細は `~/.claude/rules/spec-driven.md` を参照。

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
- `/qa` — MCP・CLI・HTTP API の動作確認（`.claude/skills/qa/SKILL.md`）

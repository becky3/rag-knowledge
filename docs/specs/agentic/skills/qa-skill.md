# QA スキル

## 概要

MCP ツール・CLI コマンド・HTTP API の機能を実際に実行し、正常動作を検証する動作確認スキル。

スコープ:

- 全機能の正常系動作確認（取り込み・検索・削除・再構築・統計）
- 取り込み後の実データ検証（source_store ファイル・Git 状態・検索結果）
- グループ単位の選択実行

スコープ外:

- 自動テスト（pytest）の実行（`/test-run` の範疇）
- 異常系・エッジケースの網羅的テスト（自動テストの範疇）
- パフォーマンス計測

## 背景

- 実装セッションで動作確認を行うと、「テスト通過 = 動く」という確信バイアスにより検証が形骸化する
- 品質ゲートの手順として明記しても、実装者が手順を省略・簡略化する問題が繰り返し発生した
- 実装と検証を別セッションに分離し、検証手順をスキルとして固定することでバイアスと省略を排除する

## 制約

- **worktree 環境での実行**: 本番ストレージに影響を与えないよう、worktree 環境で実行する。ストレージパスは worktree 内の絶対パスを使用する
- **MCP サーバーの排他**: MCP ツール検証時は MCP サーバーを enabled にする。CLI 検証時は disabled にする。同時アクセスは禁止
- **YouTube の実行制限**: YouTube グループ（E）を選択した場合、実行直前にユーザー確認を必ず挟む。IP ブロックリスクがあるため
- **外部 API への最小アクセス**: 外部 API を使用するグループでは、取り込み件数を最小（1件）に制限する
- **エラー時の継続動作**: 各ステップでエラーが発生した場合、NG として記録し次のステップに進む。グループ全体を中断しない
- **取り込み後の共通検証フロー**: 取り込み・再構築を行うステップの後には、必ず共通検証フローを実行する（後述）
- **実行パラメータの明示**: 全ステップで実行するコマンド・パラメータを表示してから実行する

## コマンド体系

### 呼び出し

| コマンド | 用途 |
|---------|------|
| `/qa` | 動作確認を開始（引数なし、対話的にグループ・インターフェースを選択） |

### 検証グループ

| ID | グループ | 内容 | 外部 API | インターフェース |
|----|---------|------|---------|----------------|
| A | Core | stats, search, get-document, delete, rebuild | 不要（Embedding のみ） | CLI / MCP |
| B | Local | add-document, crawl-documents, add-journal, migrate-journal | 不要 | CLI / MCP |
| C | Web | add, crawl, crawl-preview, site-ingest | Web アクセス | CLI / MCP |
| D | SNS | crawl-zenn, crawl-bluesky | Zenn/BlueSky API | CLI / MCP |
| E | YouTube | ingest-youtube | YouTube API | CLI / MCP |
| F | Aozora | update-aozora-catalog, search-aozora, ingest-aozora | 青空文庫 | CLI / MCP |
| G | Upload | POST /upload/document, POST /upload/journal | 不要 | HTTP 固定 |
| H | Eval | init-test-db, evaluate | 不要 | CLI 固定 |

### インターフェース選択

| 選択肢 | 説明 |
|--------|------|
| CLI | CLI コマンドのみで検証 |
| MCP | MCP ツールのみで検証 |
| both | CLI と MCP の両方で検証 |

Upload（G）は HTTP 固定、Eval（H）は CLI 固定のため、インターフェース選択に関わらず固定方式で実行する。

## 処理フロー

### ステップ 1: グループ選択

一覧を表示し、ユーザーに検証対象を問い合わせる。

表示形式:

```
QA 検証グループ:

  A) Core     — stats, search, get-document, delete, rebuild
  B) Local    — add-document, crawl-documents, add-journal, migrate-journal
  C) Web      — add, crawl, crawl-preview, site-ingest
  D) SNS      — crawl-zenn, crawl-bluesky
  E) YouTube  — ingest-youtube（実行前にユーザー確認）
  F) Aozora   — update-aozora-catalog, search-aozora, ingest-aozora
  G) Upload   — HTTP Upload API（HTTP モード固定）
  H) Eval     — init-test-db, evaluate（フィクスチャ必要）

どのグループを検証しますか？（例: A B D, all）
```

ユーザーの回答を受け取り、対象グループを確定する。

### ステップ 2: インターフェース選択

```
インターフェースを選択してください:
  1) CLI のみ
  2) MCP のみ
  3) both（CLI + MCP）
```

### ステップ 3: 環境確認

- 現在のブランチ・ディレクトリを確認する
- MCP サーバーの状態を確認する（CLI 検証時は disabled であることを確認）
- LM Studio の接続確認（Embedding API が必要なグループの場合）
- worktree 環境の場合、ストレージパスが worktree 内を指していることを確認する

### ステップ 4: 選択グループの順次実行

選択されたグループを ID 順に実行する。各グループの詳細手順はグループ別検証手順に定義する。

CLI → MCP の順で実行する（both 選択時）。

YouTube（E）選択時は、グループ実行直前に以下を表示してユーザー確認を取る:

```
YouTube 検証を実行します。
YouTube API へのアクセスにより IP ブロックのリスクがあります。
実行しますか？ (y/n)
```

### ステップ 5: 結果サマリー

全グループの実行完了後、結果サマリーを表示する。

```
QA 検証結果:

| グループ | インターフェース | 結果 |
|---------|----------------|------|
| A Core  | CLI            | OK   |
| A Core  | MCP            | OK   |
| B Local | CLI            | NG — add-journal で検索結果にヒットしない |
| ...     | ...            | ...  |

NG 項目: N 件
```

NG が検出された場合、Issue 起票を提案する。

## グループ別検証手順

### 共通検証フロー（取り込み後）

取り込み・再構築を行うステップの後に、以下を必ず実行する。

| # | 検証 | 方法 | 確認観点 |
|---|------|------|---------|
| 1 | 実データ確認 | source_store / converted_store のファイルを `ls` で確認 | ファイルが存在すること。`.meta` サイドカーファイルの内容（source_type, title, collected_at 等）が正しいこと |
| 2 | Git 状態確認 | `git status` / `git diff --stat` | 新規ファイルが追加されていること。意図しない変更がないこと |
| 3 | 検索確認 | `search --query <取り込み内容に関連するクエリ>` | 取り込んだソースがヒットすること。メタデータ行（Source, Title, Type, Section, Collected）が正しいこと |

削除ステップの後は逆方向を確認する:

| # | 検証 | 確認観点 |
|---|------|---------|
| 1 | 実データ確認 | source_store からファイルが削除されていること |
| 2 | Git 状態確認 | ファイル削除が `git status` に反映されていること |
| 3 | 検索確認 | 削除したソースが検索結果に含まれないこと |

### A) Core

目的: パイプライン基盤（統計・検索・全文取得・削除・再構築）の動作確認。

前提: グループ B 以降で取り込みが行われていない場合、Core 単独では検索・全文取得・削除のテストデータがない。Core 単独実行時は、最初にテスト用 Markdown ファイルを add-document で取り込んでからテストする。

手順:

1. **stats** — 現在の統計情報を取得・表示する
2. **テストデータ取り込み**（Core 単独実行時のみ）— テスト用 Markdown を add-document で取り込み、共通検証フローを実行
3. **search** — 取り込み済みコンテンツに関連するクエリで検索。ベクトル検索・BM25 の両方で結果が返ること
4. **get-document** — 検索結果の source_id を指定して全文取得。`text` 形式と `original` 形式の両方を確認
5. **delete** — 取り込んだソースを削除。共通検証フロー（削除版）を実行
6. **rebuild --mode incremental** — 差分再構築を実行。stats で統計が更新されていること
7. **stats** — 再構築後の統計を確認

CLI 対応コマンド:

| MCP ツール | CLI コマンド |
|-----------|------------|
| `rag_stats` | `stats` |
| `rag_search` | `search --query <クエリ>` |
| `rag_get_document` | `get-document <source_id>` |
| `rag_delete` | `delete <source_id>` |
| `rag_rebuild` | `rebuild --mode <mode>` |

### B) Local

目的: ローカルファイル取り込み（ドキュメント・ジャーナル）の動作確認。

手順:

1. **add-document** — テスト用 Markdown ファイルを取り込み。共通検証フローを実行
2. **add-document（上書き）** — 同名ファイルで `upload_mode=replace` を指定して上書き。共通検証フローを実行
3. **crawl-documents** — テスト用ディレクトリ（複数ファイル）を一括取り込み。共通検証フローを実行
4. **add-journal** — テスト用ジャーナルエントリを登録。共通検証フローを実行
5. **migrate-journal** — テスト用ジャーナルディレクトリを一括配置。`rebuild --mode incremental` 後に共通検証フローを実行

CLI 対応コマンド:

| MCP ツール | CLI コマンド |
|-----------|------------|
| `rag_add_document` | `add-document <file_path>` |
| `rag_crawl_documents` | `crawl-documents <dir_path>` |
| `rag_add_journal` | `add-journal --title <title> --file <file> --repository <repo>` |
| — | `migrate-journal --dir <dir> --repository <repo>` |

### C) Web

目的: Web ページ取り込み・クロールの動作確認。

手順:

1. **crawl-preview** — 対象 URL のプレビューを取得。ページ一覧が表示されること
2. **add** — Web ページ 1 件を取り込み。共通検証フローを実行
3. **crawl** — リンク集ページからクロール（`depth=1`、少数ページ）。共通検証フローを実行
4. **site-ingest** — Scrapy 一括取り込み（`--max-pages 3 --download-only`）。source_store にファイルが配置されること

CLI 対応コマンド:

| MCP ツール | CLI コマンド |
|-----------|------------|
| `rag_crawl_preview` | `crawl-preview --url <url>` |
| `rag_add` | `add <url>` |
| `rag_crawl` | `crawl <url> --depth 1` |
| `rag_site_ingest` | `site-ingest <url> --max-pages 3 --download-only` |

### D) SNS

目的: Zenn・BlueSky インジェスターの動作確認。

手順:

1. **crawl-zenn** — Zenn ユーザーの記事を 1 件取り込み（`--max-articles 1`）。共通検証フローを実行
2. **crawl-bluesky** — BlueSky ユーザーの投稿を 1 件取り込み（`--max-posts 1`）。共通検証フローを実行

CLI 対応コマンド:

| MCP ツール | CLI コマンド |
|-----------|------------|
| `rag_crawl_zenn` | `crawl-zenn <username> --max-articles 1` |
| `rag_crawl_bluesky` | `crawl-bluesky <handle> --max-posts 1` |

### E) YouTube

目的: YouTube インジェスターの動作確認。実行前にユーザー確認を必須とする。

手順:

1. **ユーザー確認** — IP ブロックリスクを説明し、実行可否を確認する
2. **ingest-youtube** — YouTube 動画 1 件を取り込み。共通検証フローを実行

CLI 対応コマンド:

| MCP ツール | CLI コマンド |
|-----------|------------|
| `rag_add_youtube` | `ingest-youtube <video_url>` |

プレイリスト一括（`rag_crawl_youtube` / `ingest-youtube-playlist`）は IP ブロックリスクが高いため、QA では単一動画のみ検証する。

### F) Aozora

目的: 青空文庫インジェスターの動作確認。

手順:

1. **update-aozora-catalog** — カタログ CSV を更新。source_store にカタログファイルが配置されること
2. **search-aozora** — カタログから著者名で検索。結果が返ること
3. **ingest-aozora** — 作品 1 件を取り込み（search-aozora で得た book_id を使用）。共通検証フローを実行

CLI 対応コマンド:

| MCP ツール | CLI コマンド |
|-----------|------------|
| `rag_update_aozora_catalog` | `update-aozora-catalog` |
| `rag_search_aozora` | `search-aozora --author <著者名>` |
| `rag_add_aozora` | `ingest-aozora <book_id>` |

著者一括（`rag_crawl_aozora` / `ingest-aozora-author`）は単一取り込みで検証十分なため、QA ではスキップする。

### G) Upload

目的: HTTP Upload API の動作確認。HTTP モードでの MCP サーバー起動が必要。

前提: MCP サーバーを HTTP モードで起動する（`.env` で `RAG_TRANSPORT=http` を設定）。

手順:

1. **POST /upload/document（正常系）** — テスト用 Markdown ファイルをアップロード。レスポンスの `status: ok` と `source_id` を確認。共通検証フローを実行
2. **POST /upload/document（同名エラー）** — 同じファイルを再度アップロード（`upload_mode=fail`）。HTTP 409 が返ること
3. **POST /upload/document（上書き）** — `upload_mode=replace` で上書き。HTTP 200 が返ること
4. **POST /upload/journal（正常系）** — テスト用ジャーナルをアップロード。レスポンスの `status: ok` と `source_id` を確認。共通検証フローを実行
5. **POST /upload/journal（必須フィールド欠落）** — title なしでアップロード。HTTP 400 が返ること

### H) Eval

目的: 評価パイプラインの動作確認。評価フィクスチャが必要。

前提: テストドキュメントフィクスチャ（JSON）と評価データセット（JSON）がローカルに存在すること。

手順:

1. **init-test-db** — テスト用 DB を初期化。ChromaDB・BM25 ディレクトリが作成されること
2. **evaluate** — 検索精度評価を実行。レポートファイル（report.json, report.md）が生成されること。レポート内容を表示

## 入出力

### 入力

- ユーザーによるグループ選択（A〜H、all）
- ユーザーによるインターフェース選択（CLI / MCP / both）
- YouTube 実行時のユーザー確認（y/n）

### 出力

- 各ステップの実行コマンド・パラメータ表示
- 各ステップの実行結果と検証結果
- 全グループ完了後の結果サマリー（OK/NG テーブル）
- NG 検出時の Issue 起票提案

## 関連ドキュメント

- 品質ゲート（`~/.claude/rules/quality-gate.md`）— QA フェーズの位置づけ
- 仕様駆動開発（`~/.claude/rules/spec-driven.md`）— QA フェーズの原則
- [RAG Knowledge 全体仕様](../../overview.md) — 機能一覧
- [content-upload](../../infrastructure/content-upload.md) — Upload HTTP API 仕様

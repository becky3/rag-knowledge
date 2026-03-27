# QA スキル

## 概要

MCP ツール・CLI コマンド・HTTP API の機能を実際に実行し、正常動作を検証する動作確認スキル。

スコープ:

- 全機能の正常系動作確認（取り込み・検索・一覧・削除・再構築・統計）
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
- **MCP サーバーの分離**: MCP ツール検証時は MCP サーバーを enabled にする。CLI 検証時は disabled を推奨する（HttpClient + ファイルベースロックにより同時アクセスは技術的に可能だが、検証環境の単純化のため分離する）
- **YouTube の実行制限**: YouTube グループ（D）を選択した場合、実行直前にユーザー確認を必ず挟む。IP ブロックリスクがあるため
- **外部 API への最小アクセス**: 外部 API を使用するグループでは、取り込み件数を最小限に制限する（具体値は検証リソース定義に従う）
- **エラー時の継続動作**: 各ステップでエラーが発生した場合、NG として記録し次のステップに進む。グループ全体を中断しない
- **取り込み後の共通検証フロー**: 取り込み・再構築を行うステップの後には、必ず共通検証フローを実行する（詳細は [QA 実行スキル](qa-execute-skill.md) に定義）
- **実行パラメータの明示**: 全ステップで実行するコマンド・パラメータを表示してから実行する
- **qa-execute による実行制御**: 各検証ステップの実行は [QA 実行スキル](qa-execute-skill.md) に委譲する。コマンドの直接実行・共通検証フローの省略は禁止

## コマンド体系

### 呼び出し

| コマンド | 用途 |
|---------|------|
| `/qa` | 動作確認を開始（引数なし、対話的にグループ・インターフェースを選択） |

### 検証グループ

データ取り込み系グループを先に実行し、Core を最後に配置する。Core は他グループの取り込みデータを使ってパイプライン基盤を検証する。

| ID | グループ | 内容 | 外部 API | インターフェース |
|----|---------|------|---------|----------------|
| A | Local | add-document, crawl-documents, add-journal, migrate-journal | 不要 | CLI / MCP |
| B | Web | add, site-ingest | Web アクセス | CLI / MCP |
| C | SNS | crawl-zenn, crawl-bluesky | Zenn/BlueSky API | CLI / MCP |
| D | YouTube | ingest-youtube, ingest-youtube-playlist | YouTube API | CLI / MCP |
| E | Aozora | update-aozora-catalog, search-aozora, ingest-aozora | 青空文庫 | CLI / MCP |
| F | Upload | POST /upload/document, POST /upload/journal | 不要 | HTTP 固定 |
| G | Eval | init-test-db, evaluate | 不要 | CLI 固定 |
| H | Core | stats, list-recent, search, get-document, delete, rebuild | 不要（Embedding のみ） | CLI / MCP |

### インターフェース選択

| 選択肢 | 説明 |
|--------|------|
| CLI | CLI コマンドのみで検証 |
| MCP | MCP ツールのみで検証 |
| both | CLI と MCP の両方で検証 |

Upload（F）は HTTP 固定、Eval（G）は CLI 固定のため、インターフェース選択に関わらず固定方式で実行する。

## 処理フロー

### ステップ 1: グループ選択

一覧を表示し、ユーザーに検証対象を問い合わせる。

表示形式:

```
QA 検証グループ:

  A) Local    — add-document, crawl-documents, add-journal, migrate-journal
  B) Web      — add, site-ingest
  C) SNS      — crawl-zenn, crawl-bluesky
  D) YouTube  — ingest-youtube, ingest-youtube-playlist（実行前にユーザー確認）
  E) Aozora   — update-aozora-catalog, search-aozora, ingest-aozora
  F) Upload   — HTTP Upload API（HTTP モード固定）
  G) Eval     — init-test-db, evaluate（フィクスチャ必要）
  H) Core     — stats, list-recent, search, get-document, delete, rebuild

どのグループを検証しますか？（例: A B D, all）
```

ユーザーの回答を受け取り、対象グループを確定する。

### ステップ 2: インターフェース選択

```
インターフェースを選択してください:
  1) CLI のみ
  2) MCP のみ
  3) both（CLI + MCP）

※ Upload(F) は HTTP 固定、Eval(G) は CLI 固定です
```

### ステップ 3: worktree セットアップ

本番ストレージに影響を与えないよう、worktree 環境で QA を実行する。

1. ベースブランチ（develop）を最新に更新する
2. worktree を作成する（配置: リポジトリの親ディレクトリ、命名: `<リポジトリ名>-wt-<Issue番号>`）
3. メインリポジトリから `.env` をコピーし、ストレージパスを worktree 内の絶対パスに変更する。以下も設定する:
   - `CHROMADB_SERVER_PORT=8001`（メインリポジトリの MCP サーバーとのポート競合回避）
   - `CHROMADB_AUTO_START=false`（worktree では手動起動するため。`true` のままだと MCP サーバー起動時に競合する可能性がある）
4. LM Studio の接続先を確認し、必要に応じて `localhost` に変更する
5. `.tmp` ディレクトリを作成する
6. メインリポジトリから `.qa/` ディレクトリをコピーする（`.qa/` は `.gitignore` 対象のため worktree に含まれない）
7. CLI フェーズを含む場合、ChromaDB サーバーを手動起動する: `uv run chroma run --path <persist_dir> --port <CHROMADB_SERVER_PORT>`（初回は venv 構築のため起動に時間がかかる。目安: 10〜20 秒。heartbeat 確認前に十分待機すること）。「MCP のみ」選択時は MCP サーバーが自動起動するためスキップする

以降の全コマンドは worktree ディレクトリで実行する。

### ステップ 4: 環境確認

- 現在のブランチ・ディレクトリを確認する
- MCP サーバーの状態を確認する（CLI 検証時は disabled 推奨）
- ChromaDB サーバー疎通確認（CLI フェーズを含む場合のみ）: `curl http://localhost:<CHROMADB_SERVER_PORT>/api/v2/heartbeat` で応答を確認する。「MCP のみ」選択時はスキップする
- LM Studio の接続確認（Embedding API が必要なグループの場合）
- `.env` のストレージパスが worktree 内の絶対パスを指していることを確認する

### ステップ 5: 選択グループの順次実行

選択されたグループを ID 順（A→H）に実行する。各グループの詳細手順はグループ別検証手順に定義する。

各検証ステップの実行は [QA 実行スキル](qa-execute-skill.md) に委譲する。取り込み系ステップは検証種別 `ingest`、削除系ステップは `delete`、それ以外は `none` を指定する。

qa-execute がステップ結果として「STOP」を返した場合、残りのステップ・グループをすべてスキップし、ステップ 6（結果サマリー）に進む。STOP までに完了したステップの結果はサマリーに含める。

インターフェース選択に応じた実行方針:

- **CLI のみ**: MCP サーバーが disabled であることを確認し、全グループを CLI で実行する
- **MCP のみ**: MCP サーバーが enabled であることを確認し、全グループを MCP で実行する
- **both**: 2フェーズで実行する
  1. **CLI フェーズ**: MCP disabled を確認し、全グループを CLI で実行する
  2. **切り替え**: ユーザーに `/mcp` で MCP サーバーを enabled に切り替えてもらう
  3. **MCP フェーズ**: MCP enabled を確認し、全グループを MCP で実行する

MCP フェーズでの追加確認（書き込み系ツール実行時）:

- MCP ツールの実行結果が正常に返されること（CLI サブプロセス経由の JSON パースが成功していることの間接確認）
- エラー発生時にエラーメッセージが適切に返されること

YouTube（D）選択時は、グループ実行直前に以下を表示してユーザー確認を取る（qa-execute の都度確認とは別に、グループレベルで事前確認する）:

```
YouTube 検証を実行します。
YouTube API へのアクセスにより IP ブロックのリスクがあります。
実行しますか？ (y/n)
```

### ステップ 6: 結果サマリー

全グループの実行完了後（STOP による中止を含む）、結果サマリーを表示する。

```
QA 検証結果:

| グループ | インターフェース | 結果 |
|---------|----------------|------|
| A Local | CLI            | OK   |
| B Web   | CLI            | OK   |
| C SNS   | CLI            | STOP — ユーザーにより中止 |
| D YouTube | ---          | 未実行 |
| ...     | ...            | ...  |

NG 項目: N 件
```

STOP で中止されたグループは「STOP — ユーザーにより中止」、STOP 以降の未実行グループは「未実行」と表示する。

NG が検出された場合、Issue 起票を提案する。

## グループ別検証手順

### 共通検証フロー

共通検証フロー（取り込み後・削除後）の詳細は [QA 実行スキル仕様](qa-execute-skill.md) のステップ 4 に定義する。各グループの検証種別テーブルで `ingest` / `delete` を指定したステップで自動的に実行される。

### A) Local

目的: ローカルファイル取り込み（ドキュメント・ジャーナル）の動作確認。

各ステップを qa-execute に委譲する:

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `add-document --file README.md` | Markdown 取り込み成功 | `ingest` |
| 2 | `add-document --file .qa/pdf_add_test.pdf` | PDF 取り込み成功 | `ingest` |
| 3 | `add-document --file README.md --upload-mode replace` | 上書き成功、エラーなし | `none` |
| 4 | `crawl-documents docs/specs/`（`dir_path` は positional 引数） | ディレクトリ一括取り込み成功 | `ingest` |
| 5 | `add-journal --title "コンテンツ一覧取得機能の実装" --file .qa/journal_add_test.md --repository rag-knowledge` | ジャーナル登録成功 | `ingest` |
| 6a | `migrate-journal --dir .qa/journals --repository rag-knowledge` | ジャーナル一括配置成功 | `none` |
| 6b | `rebuild --mode incremental` | 再構築成功、migrate 分がインデックスに反映 | `ingest` |

CLI / MCP 対応:

| CLI コマンド | MCP ツール |
|------------|-----------|
| `add-document --file <path>` | `rag_add_document` |
| `crawl-documents <dir>` | `rag_crawl_documents` |
| `add-journal --title <t> --file <f> --repository <r>` | `rag_add_journal` |
| `migrate-journal --dir <d> --repository <r>` | （CLI のみ） |

### B) Web

目的: Web ページ取り込み・サイト一括取り込みの動作確認。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `add https://github.com/becky3/rag-knowledge` | Web ページ 1 件取り込み成功 | `ingest` |
| 2 | `site-ingest https://books.toscrape.com --max-pages 20` | Scrapy 一括取り込み成功 | `ingest` |

CLI / MCP 対応: `rag_add` / `rag_site_ingest`

### C) SNS

目的: Zenn・BlueSky インジェスターの動作確認。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `crawl-zenn rhythmcan --max-articles 1` | Zenn 記事 1 件取り込み成功 | `ingest` |
| 2 | `crawl-bluesky rhythmcan.bsky.social --max-posts 1` | BlueSky 投稿 1 件取り込み成功 | `ingest` |

CLI / MCP 対応: `rag_crawl_zenn` / `rag_crawl_bluesky`

### D) YouTube

目的: YouTube インジェスターの動作確認。グループ実行前にユーザー確認を必須とする（qa-execute の都度確認とは別）。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `ingest-youtube https://www.youtube.com/watch?v=GuFBDpzH3ck` | 動画 1 件取り込み成功 | `ingest` |
| 2 | `ingest-youtube-playlist https://www.youtube.com/playlist?list=PLaFZvPBpvhKKgHIDI16ja0jwEG_vIH55K --max-videos 1` | プレイリストから 1 件取り込み成功 | `ingest` |

CLI / MCP 対応: `rag_add_youtube` / `rag_crawl_youtube`

### E) Aozora

目的: 青空文庫インジェスターの動作確認。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `update-aozora-catalog` | カタログ CSV が source_store に配置される | `none` |
| 2 | `search-aozora --author 太宰` | 結果に `001567`（走れメロス）が含まれる | `none` |
| 3 | `ingest-aozora 001567` | 作品 1 件取り込み成功 | `ingest` |

CLI / MCP 対応: `rag_update_aozora_catalog` / `rag_search_aozora` / `rag_add_aozora`

著者一括（`rag_crawl_aozora` / `ingest-aozora-author`）は単一取り込みで検証十分なため、QA ではスキップする。

### F) Upload

目的: HTTP Upload API の動作確認。HTTP モードでの MCP サーバー起動が必要。

前提: MCP サーバーを HTTP モードで起動する（`.env` で `RAG_TRANSPORT=http` を設定）。

ベース URL: `http://localhost:<RAG_HTTP_PORT>`（デフォルト: `8081`）

| # | コマンド（curl） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `curl -X POST http://localhost:8081/upload/document -F "file=@README.md"` | HTTP 200、`status: ok` と `source_id` が返る | `ingest` |
| 2 | `curl -X POST http://localhost:8081/upload/document -F "file=@README.md"` | HTTP 409（重複検出エラー、`upload_mode` デフォルト `fail`） | `none` |
| 3 | `curl -X POST http://localhost:8081/upload/document -F "file=@README.md" -F "upload_mode=replace"` | HTTP 200 | `none` |
| 4 | `curl -X POST http://localhost:8081/upload/journal -F "file=@.qa/journal_upload_test.md" -F "title=QA スキルの仕様書・スキル定義作成" -F "repository=rag-knowledge"` | HTTP 200、`status: ok` と `source_id` が返る | `ingest` |
| 5 | `curl -X POST http://localhost:8081/upload/journal -F "file=@.qa/journal_upload_test.md" -F "repository=rag-knowledge"` | HTTP 400（必須フィールド `title` 欠落） | `none` |
| 6 | ステップ 1 のコマンドを `&` でバックグラウンド送信 + 同一コマンドをフォアグラウンドで実行 | いずれか一方が HTTP 409 Conflict（ロック競合） | `none` |

### G) Eval

目的: 評価パイプラインの動作確認。評価フィクスチャが必要。

前提: テストドキュメントフィクスチャ（JSON）と評価データセット（JSON）がローカルに存在すること。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `init-test-db --chunk-size 200 --chunk-overlap 30 --bm25-k1 1.5 --bm25-b 0.75 --persist-dir .tmp/eval_chroma_db --bm25-persist-dir .tmp/eval_bm25_index --fixture <フィクスチャパス>` | ChromaDB・BM25 ディレクトリが作成される | `none` |
| 2 | `evaluate --chunk-size 200 --chunk-overlap 30 --bm25-k1 1.5 --bm25-b 0.75 --vector-weight 0.6 --persist-dir .tmp/eval_chroma_db --output-dir .tmp/rag-evaluation --fixture <フィクスチャパス> --dataset <データセットパス>` | レポートファイル（report.json, report.md）が生成される | `none` |

### H) Core

目的: パイプライン基盤（統計・一覧・検索・全文取得・削除・再構築）の動作確認。A〜G の取り込みデータを使用する。

前提: H 単独実行時は、先にテスト用 Markdown を add-document で取り込んでからテストする。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `stats` | 統計情報が表示される | `none` |
| 2 | `list-recent --source-type local` | 取り込み済み local ソースが一覧に表示される | `none` |
| 3 | `list-recent --source-type journal` | 取り込み済み journal ソースが一覧に表示される | `none` |
| 4 | `search --query <取り込み内容に関連するワード>` | ベクトル検索・BM25 の両方で結果が返る | `none` |
| 5a | `get-document <source_id> --format text`（`source_id` は positional 引数） | A) の README.md の全文がテキスト形式で取得できる | `none` |
| 5b | `get-document <source_id> --format original` | A) の README.md の全文がオリジナル形式で取得できる | `none` |
| 6 | `delete <source_id>`（`source_id` は positional 引数） | A) の README.md が削除される | `delete` |
| 7 | `rebuild --mode incremental` | 差分再構築が成功する | `none` |
| 8 | `stats` | 再構築後の統計が更新されている | `none` |

CLI / MCP 対応: `rag_stats` / `rag_list_recent` / `rag_search` / `rag_get_document` / `rag_delete` / `rag_rebuild`

### ステップ 7: クリーンアップ

QA 完了後、worktree 環境を片付ける。ChromaDB や MCP サーバーのプロセスがファイルをロックしているため、先にプロセスを停止する必要がある。

1. バックグラウンドの ChromaDB サーバーを停止する（プロセス特定: `wmic process where "CommandLine like '%<worktree-path>%'"` または `netstat -ano | grep <port>`）
2. worktree ディレクトリを削除する: `git worktree remove <worktree-path>`
3. worktree の参照をクリーンアップする: `git worktree prune`

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

- [QA 実行スキル](qa-execute-skill.md) — 各検証ステップの実行制御
- 品質ゲート（`~/.claude/rules/quality-gate.md`）— QA フェーズの位置づけ
- 仕様駆動開発（`~/.claude/rules/spec-driven.md`）— QA フェーズの原則
- [RAG Knowledge 全体仕様](../../overview.md) — 機能一覧
- [RAG ナレッジ](../../rag-knowledge.md) — ChromaDB client/server 構成、MCP 薄層アダプターパターン
- [content-upload](../../infrastructure/content-upload.md) — Upload HTTP API 仕様
- [content-listing](../../infrastructure/content-listing.md) — コンテンツ一覧取得仕様

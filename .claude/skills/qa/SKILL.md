---
name: qa
description: MCP・CLI・HTTP API の動作確認（グループ選択式）
user-invocable: true
allowed-tools: Bash, Read, Grep, Glob, AskUserQuestion
argument-hint: ""
---

## タスク

マージ後の動作確認スキル。MCP ツール・CLI コマンド・HTTP API の機能を実際に実行し、正常動作を検証する。仕様書: `docs/specs/agentic/skills/qa-skill.md`

各検証ステップの実行は `/qa-execute` スキル（`.claude/skills/qa-execute/SKILL.md`）に委譲する。コマンドの直接実行・共通検証フローの省略は禁止。

## 処理手順

### 1. グループ選択

以下を表示し、ユーザーに検証対象を問い合わせる:

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

### 2. インターフェース選択

```
インターフェースを選択してください:
  1) CLI のみ
  2) MCP のみ
  3) both（CLI + MCP）

※ Upload(F) は HTTP 固定、Eval(G) は CLI 固定です
```

### 3. worktree セットアップ

本番ストレージに影響を与えないよう、worktree 環境で QA を実行する。

1. ベースブランチ（develop）を最新に更新する
2. worktree を作成する: `git worktree add -b qa/qa-skill-<Issue番号> <worktree-path> develop`
   - 配置: リポジトリの親ディレクトリ、命名: `<リポジトリ名>-wt-<Issue番号>`
3. メインリポジトリから `.env` をコピーする
4. `.env` のストレージパスを worktree 内の**絶対パス**に変更する（相対パスだとメインリポジトリのストレージを参照してしまう）。以下も設定する:
   - `CHROMADB_SERVER_PORT=8001`（メインリポジトリの MCP サーバーとのポート競合回避）
   - `CHROMADB_AUTO_START=false`（worktree では手動起動するため。`true` のままだと MCP サーバー起動時に競合する可能性がある）
5. LM Studio の接続先を確認し、必要に応じて `localhost` に変更する
6. `.tmp` ディレクトリを作成する
7. メインリポジトリから `.qa/` ディレクトリをコピーする（`.qa/` は `.gitignore` 対象のため worktree に含まれない）
8. CLI フェーズを含む場合、ChromaDB サーバーを手動起動する: `uv run chroma run --path <persist_dir> --port <CHROMADB_SERVER_PORT>`（初回は venv 構築のため起動に時間がかかる。目安: 10〜20 秒。heartbeat 確認前に十分待機すること）。「MCP のみ」選択時は MCP サーバーが自動起動するためスキップする

以降の全コマンドは worktree ディレクトリで実行する。

### 4. 環境確認

- 現在のブランチ・作業ディレクトリを表示
- MCP サーバーの状態確認（CLI 検証時は disabled 推奨、MCP 検証時は enabled であることを確認）
- ChromaDB サーバー疎通確認（CLI フェーズを含む場合のみ）: `curl http://localhost:<CHROMADB_SERVER_PORT>/api/v2/heartbeat` で応答を確認。「MCP のみ」選択時はスキップ
- LM Studio の接続確認（Embedding API が必要なグループの場合）
- `.env` のストレージパスが worktree 内の絶対パスを指していることを確認
- 問題があればユーザーに報告し、解決してから続行

### 5. グループ実行

選択されたグループを ID 順（A→H）に実行する。

インターフェース選択に応じた実行方針:

- **CLI のみ**: MCP disabled を確認し、全グループを CLI で実行
- **MCP のみ**: MCP enabled を確認し、全グループを MCP で実行
- **both**: 2フェーズで実行
  1. CLI フェーズ: MCP disabled を確認 → 全グループを CLI で実行
  2. 切り替え: ユーザーに `/mcp` で enabled に切り替えてもらう
  3. MCP フェーズ: MCP enabled を確認 → 全グループを MCP で実行

**全ステップ共通ルール:**

- 各検証ステップは `/qa-execute` スキルに委譲する。コマンド・期待結果・検証種別を渡し、qa-execute の「事前表示 → 許可 → 実行 → 検証 → 結果表示 → 次ステップ進行確認」フローに従う
- qa-execute がステップ結果として「STOP」を返した場合、残りのステップ・グループをすべてスキップし、結果サマリーに進む。STOP までに完了したステップの結果はサマリーに含める
- 外部 API を使用するグループでは取り込み件数を最小限に制限する（検証リソースの定義値に従う）
- 取り込み系ステップは検証種別 `ingest`、削除系ステップは検証種別 `delete`、それ以外は `none` を指定する

**YouTube（D）選択時の必須確認:**

グループ D の実行直前に以下を表示し、ユーザー確認を取る。確認なしに実行してはならない（qa-execute の都度確認とは別に、グループレベルで事前確認する）:

```
YouTube 検証を実行します。
YouTube API へのアクセスにより IP ブロックのリスクがあります。
実行しますか？ (y/n)
```

**MCP フェーズでの追加確認:**

MCP フェーズで書き込み系ツール（取り込み・削除・再構築）を実行する際、以下を追加で確認する:

- MCP ツールの実行結果が正常に返されること（CLI サブプロセス経由の JSON パースが成功していることの間接確認）
- エラー発生時にエラーメッセージが適切に返されること

### 6. 結果サマリー

全グループ完了後（STOP による中止を含む）に結果テーブルを表示:

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

NG を検出した場合、Issue 起票を提案する。

## 検証リソース

各グループで使用するテストデータ・URL・アカウントを以下に定義する。実行者が毎回判断する余地をなくし、再現性を担保する。

`.qa/` ディレクトリはテストリソースの置き場（git 管理外）。以下のファイルを事前に配置しておくこと:

- `.qa/pdf_add_test.pdf` — PDF 取り込みテスト用
- `.qa/journal_add_test.md` — add-journal テスト用
- `.qa/journal_upload_test.md` — Upload journal テスト用
- `.qa/journals/` — migrate-journal テスト用（ジャーナル 10 件）

| グループ | リソース | 値 |
|---------|---------|-----|
| A) Local | add-document（Markdown） | リポジトリの `README.md` |
| A) Local | add-document（PDF） | `.qa/pdf_add_test.pdf` |
| A) Local | add-document（上書き） | `README.md` を再取り込み（CLI: `--upload-mode replace` / MCP: `upload_mode="replace"`） |
| A) Local | crawl-documents | リポジトリの `docs/specs/` ディレクトリ全体 |
| A) Local | add-journal | `.qa/journal_add_test.md`（`--title "コンテンツ一覧取得機能の実装"` `--repository rag-knowledge`） |
| A) Local | migrate-journal | `.qa/journals/`（古いジャーナル 10 件、`--repository rag-knowledge`） |
| B) Web | add（単一ページ） | `https://github.com/becky3/rag-knowledge` |
| B) Web | site-ingest（Scrapy） | `https://books.toscrape.com`（`--max-pages 20`） |
| C) SNS | Zenn ユーザー | `rhythmcan` |
| C) SNS | BlueSky ハンドル | `rhythmcan.bsky.social` |
| D) YouTube | 動画 URL | `https://www.youtube.com/watch?v=GuFBDpzH3ck` |
| D) YouTube | プレイリスト URL | `https://www.youtube.com/playlist?list=PLaFZvPBpvhKKgHIDI16ja0jwEG_vIH55K`（`--max-videos 1`） |
| E) Aozora | 著者検索キーワード | `太宰`（「太宰 治」にマッチ） |
| E) Aozora | person_id | `000035`（太宰治） |
| E) Aozora | book_id | `001567`（走れメロス） |
| F) Upload | document | リポジトリの `README.md` |
| F) Upload | journal | `.qa/journal_upload_test.md`（`title: "QA スキルの仕様書・スキル定義作成"` `repository: rag-knowledge`） |
| G) Eval | フィクスチャ | メモリ `reference_eval_fixtures.md` を参照 |
| G) Eval | init-test-db パラメータ | `--chunk-size 200 --chunk-overlap 30 --bm25-k1 1.5 --bm25-b 0.75` |
| G) Eval | evaluate パラメータ | 上記 + `--vector-weight 0.6` |
| H) Core | search クエリ | 直前に取り込んだ内容に関連するワード（固定値なし） |
| H) Core | get-document / delete 対象 | A) で取り込んだ README.md の source_id |

## グループ別の検証内容

仕様書 `docs/specs/agentic/skills/qa-skill.md` の「グループ別検証手順」セクションに従う。

各ステップは qa-execute に委譲する。以下のテーブルの各行が 1 回の qa-execute 呼び出しに対応する。

### A) Local

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `add-document --file README.md` | Markdown 取り込み成功 | `ingest` |
| 2 | `add-document --file .qa/pdf_add_test.pdf` | PDF 取り込み成功 | `ingest` |
| 3 | `add-document --file README.md --upload-mode replace` | 上書き成功、エラーなし | `none` |
| 4 | `crawl-documents docs/specs/`（`dir_path` は positional 引数） | ディレクトリ一括取り込み成功 | `ingest` |
| 5 | `add-journal --title "コンテンツ一覧取得機能の実装" --file .qa/journal_add_test.md --repository rag-knowledge` | ジャーナル登録成功 | `ingest` |
| 6a | `migrate-journal --dir .qa/journals --repository rag-knowledge` | ジャーナル一括配置成功 | `none` |
| 6b | `rebuild --mode incremental` | 再構築成功、migrate 分がインデックスに反映 | `ingest` |

MCP 対応コマンド:

| CLI コマンド | MCP ツール |
|------------|-----------|
| `add-document --file <path>` | `rag_add_document` |
| `crawl-documents <dir>` | `rag_crawl_documents` |
| `add-journal --title <t> --file <f> --repository <r>` | `rag_add_journal` |
| `migrate-journal --dir <d> --repository <r>` | （CLI のみ） |

### B) Web

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `add https://github.com/becky3/rag-knowledge` | Web ページ 1 件取り込み成功 | `ingest` |
| 2 | `site-ingest https://books.toscrape.com --max-pages 20` | Scrapy 一括取り込み成功 | `ingest` |

MCP 対応: `rag_add` / `rag_site_ingest`

### C) SNS

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `crawl-zenn rhythmcan --max-articles 1` | Zenn 記事 1 件取り込み成功 | `ingest` |
| 2 | `crawl-bluesky rhythmcan.bsky.social --max-posts 1` | BlueSky 投稿 1 件取り込み成功 | `ingest` |

MCP 対応: `rag_crawl_zenn` / `rag_crawl_bluesky`

### D) YouTube

グループ実行前にユーザー確認（必須）を行った上で、各ステップを qa-execute に委譲する。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `ingest-youtube https://www.youtube.com/watch?v=GuFBDpzH3ck` | 動画 1 件取り込み成功 | `ingest` |
| 2 | `ingest-youtube-playlist https://www.youtube.com/playlist?list=PLaFZvPBpvhKKgHIDI16ja0jwEG_vIH55K --max-videos 1` | プレイリストから 1 件取り込み成功 | `ingest` |

MCP 対応: `rag_add_youtube` / `rag_crawl_youtube`

### E) Aozora

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `update-aozora-catalog` | カタログ CSV が source_store に配置される | `none` |
| 2 | `search-aozora --author 太宰` | 結果に `001567`（走れメロス）が含まれる | `none` |
| 3 | `ingest-aozora 001567` | 作品 1 件取り込み成功 | `ingest` |

MCP 対応: `rag_update_aozora_catalog` / `rag_search_aozora` / `rag_add_aozora`

### F) Upload（HTTP モード固定）

HTTP モードで MCP サーバーを起動して実行:

ベース URL: `http://localhost:<RAG_HTTP_PORT>`（デフォルト: `8081`）

| # | コマンド（curl） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `curl -X POST http://localhost:8081/upload/document -F "file=@README.md"` | HTTP 200、`status: ok` と `source_id` が返る | `ingest` |
| 2 | `curl -X POST http://localhost:8081/upload/document -F "file=@README.md"` | HTTP 409（重複検出エラー、`upload_mode` デフォルト `fail`） | `none` |
| 3 | `curl -X POST http://localhost:8081/upload/document -F "file=@README.md" -F "upload_mode=replace"` | HTTP 200 | `none` |
| 4 | `curl -X POST http://localhost:8081/upload/journal -F "file=@.qa/journal_upload_test.md" -F "title=QA スキルの仕様書・スキル定義作成" -F "repository=rag-knowledge"` | HTTP 200、`status: ok` と `source_id` が返る | `ingest` |
| 5 | `curl -X POST http://localhost:8081/upload/journal -F "file=@.qa/journal_upload_test.md" -F "repository=rag-knowledge"` | HTTP 400（必須フィールド `title` 欠落） | `none` |
| 6 | ステップ 1 のコマンドを `&` でバックグラウンド送信 + 同一コマンドをフォアグラウンドで実行 | いずれか一方が HTTP 409 Conflict（ロック競合） | `none` |

### G) Eval（CLI 固定）

評価フィクスチャのパスはメモリ `reference_eval_fixtures.md` を参照（メモリが利用できない場合はユーザーにパスを確認する）:

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `init-test-db --chunk-size 200 --chunk-overlap 30 --bm25-k1 1.5 --bm25-b 0.75 --persist-dir .tmp/eval_chroma_db --bm25-persist-dir .tmp/eval_bm25_index --fixture <フィクスチャパス>` | ChromaDB・BM25 ディレクトリが作成される | `none` |
| 2 | `evaluate --chunk-size 200 --chunk-overlap 30 --bm25-k1 1.5 --bm25-b 0.75 --vector-weight 0.6 --persist-dir .tmp/eval_chroma_db --output-dir .tmp/rag-evaluation --fixture <フィクスチャパス> --dataset <データセットパス>` | レポートファイル（report.json, report.md）が生成される | `none` |

### H) Core

A〜G の取り込みデータを使ってパイプライン基盤を検証する。H 単独実行時は、先に `README.md` を add-document で取り込んでからステップ 2 以降を実行する。

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

MCP 対応: `rag_stats` / `rag_list_recent` / `rag_search` / `rag_get_document` / `rag_delete` / `rag_rebuild`

### 7. クリーンアップ

QA 完了後、worktree 環境を片付ける。ChromaDB や MCP サーバーのプロセスがファイルをロックしているため、先にプロセスを停止する必要がある。

1. バックグラウンドの ChromaDB サーバーを停止する:

   ```bash
   # worktree のプロセスを特定（Windows）
   wmic process where "CommandLine like '%<worktree-path>%'" get ProcessId,CommandLine
   # または、ポートで特定
   netstat -ano | grep <CHROMADB_SERVER_PORT>
   # 停止
   taskkill //PID <pid> //F
   ```

2. worktree ディレクトリを削除する:

   ```bash
   git worktree remove <worktree-path>
   ```

3. worktree の参照をクリーンアップする:

   ```bash
   git worktree prune
   ```

## 制約

- テストデータの作成時、実在の著作物・キャラクター情報を使用しない（`~/.claude/rules/invariants.md`）
- 各グループの実行中にエラーが発生した場合、そのステップを NG として記録し、次のステップに進む。グループ全体を中断しない
- MCP ツールの検証では、MCP サーバーが enabled であることを確認してから実行する。disabled の場合はユーザーに `/mcp` での状態変更を依頼する
- **MCP と CLI の共存**: HttpClient + ファイルベースロックにより MCP と CLI の同時アクセスは技術的に可能だが、QA では検証環境の単純化のため CLI 検証時は MCP disabled を推奨する

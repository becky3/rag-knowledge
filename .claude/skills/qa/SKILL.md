---
name: qa
description: MCP・CLI・HTTP API の動作確認（グループ選択式）
user-invocable: true
allowed-tools: Bash, Read, Grep, Glob, AskUserQuestion
argument-hint: ""
---

## タスク

マージ後の動作確認スキル。MCP ツール・CLI コマンド・HTTP API の機能を実際に実行し、正常動作を検証する。仕様書: `docs/specs/agentic/skills/qa-skill.md`

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
4. `.env` のストレージパスを worktree 内の**絶対パス**に変更する（相対パスだとメインリポジトリのストレージを参照してしまう）
5. LM Studio の接続先を確認し、必要に応じて `localhost` に変更する
6. `.tmp` ディレクトリを作成する
7. メインリポジトリから `.qa/` ディレクトリをコピーする（`.qa/` は `.gitignore` 対象のため worktree に含まれない）

以降の全コマンドは worktree ディレクトリで実行する。

### 4. 環境確認

- 現在のブランチ・作業ディレクトリを表示
- MCP サーバーの状態確認（CLI 検証時は disabled、MCP 検証時は enabled であることを確認）
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

- 実行するコマンドとパラメータを表示してから実行する
- 外部 API を使用するグループでは取り込み件数を最小（1件）に制限する

**YouTube（D）選択時の必須確認:**

グループ D の実行直前に以下を表示し、ユーザー確認を取る。確認なしに実行してはならない:

```
YouTube 検証を実行します。
YouTube API へのアクセスにより IP ブロックのリスクがあります。
実行しますか？ (y/n)
```

**取り込み後の共通検証フロー:**

取り込み・再構築を行うステップの後に、以下を必ず実行する:

1. **実データ確認**: source_store / converted_store のファイルを `ls` で確認。メタデータの確認方法は媒体により異なる:
   - **local 以外**（web, bluesky, zenn, youtube, aozora, journal）: `.meta` サイドカーファイルの内容を確認（source_type, title, collected_at）
   - **local**: `.meta` は存在しない。metadata.db の sources テーブルで確認する（source_type, title, collected_at は DB から導出される）
2. **Git 状態確認**: source_store 内の git リポジトリで `git status` を実行し、新規ファイルの追加を確認（source_store は独立した git リポジトリ）
3. **検索確認**: `search --query <取り込み内容に関連するクエリ>` で検索し、取り込んだソースがヒットすること、メタデータ行が正しいことを確認

削除ステップの後は逆方向（ファイル削除、git status 反映、検索結果から消失）を確認する。

### 6. 結果サマリー

全グループ完了後に結果テーブルを表示:

```
QA 検証結果:

| グループ | インターフェース | 結果 |
|---------|----------------|------|
| A Local | CLI            | OK   |
| B Web   | CLI            | OK   |
| H Core  | CLI            | NG — 詳細 |
| ...     | ...            | ...  |

NG 項目: N 件
```

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
| A) Local | add-document（上書き） | `README.md` を `--upload-mode replace` で再取り込み |
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

仕様書 `docs/specs/agentic/skills/qa-skill.md` の「グループ別検証手順」セクションに従う。以下は各グループの要約:

### A) Local

1. `add-document` で `README.md`（Markdown）を取り込み + 共通検証
2. `add-document` で `.qa/pdf_add_test.pdf`（PDF）を取り込み + 共通検証
3. `add-document` で `README.md` を `--upload-mode replace` で再取り込み（上書きが成功しエラーにならないことを確認）
4. `crawl-documents` で `docs/specs/` ディレクトリ一括 + 共通検証
5. `add-journal` で `.qa/journal_add_test.md` を登録（`--title "コンテンツ一覧取得機能の実装" --repository rag-knowledge`）+ 共通検証
6. `migrate-journal` で `.qa/journals/` を一括配置（`--dir .qa/journals --repository rag-knowledge`）+ `rebuild --mode incremental` + 共通検証

### B) Web

1. `add` で `https://github.com/becky3/rag-knowledge` を 1 ページ取り込み + 共通検証
2. `site-ingest` で `https://books.toscrape.com` から Scrapy 取り込み（--max-pages 20）+ 共通検証

### C) SNS

1. `crawl-zenn` で `rhythmcan` の記事 1 件（--max-articles 1）+ 共通検証
2. `crawl-bluesky` で `rhythmcan.bsky.social` の投稿 1 件（--max-posts 1）+ 共通検証

### D) YouTube

1. ユーザー確認（必須）
2. `ingest-youtube` で `https://www.youtube.com/watch?v=GuFBDpzH3ck` を取り込み + 共通検証
3. `ingest-youtube-playlist` で `https://www.youtube.com/playlist?list=PLaFZvPBpvhKKgHIDI16ja0jwEG_vIH55K`（`--max-videos 1`）を取り込み + 共通検証

### E) Aozora

1. `update-aozora-catalog` でカタログ更新 + ファイル配置確認
2. `search-aozora --author 太宰` で検索し、結果に `001567`（走れメロス）が含まれることを確認
3. `ingest-aozora 001567` で走れメロスを取り込み + 共通検証

### F) Upload（HTTP モード固定）

HTTP モードで MCP サーバーを起動して実行:

1. `POST /upload/document` で `README.md` をアップロード（正常系）+ 共通検証
2. `POST /upload/document` で同ファイルを再アップロード（同名エラー、409）
3. `POST /upload/document` で `upload_mode=replace` で上書き（200）
4. `POST /upload/journal` で `.qa/journal_upload_test.md` をアップロード（`title: "QA スキルの仕様書・スキル定義作成"` `repository: rag-knowledge`）+ 共通検証
5. `POST /upload/journal` で title なしアップロード（必須フィールド欠落、400）

### G) Eval（CLI 固定）

評価フィクスチャのパスはメモリ `reference_eval_fixtures.md` を参照（メモリが利用できない場合はユーザーにパスを確認する）:

1. `init-test-db` でテスト DB 初期化（`--chunk-size 200 --chunk-overlap 30 --bm25-k1 1.5 --bm25-b 0.75 --persist-dir .tmp/eval_chroma_db --bm25-persist-dir .tmp/eval_bm25_index --fixture <フィクスチャパス>`）+ ディレクトリ作成確認
2. `evaluate` で評価実行（`--chunk-size 200 --chunk-overlap 30 --bm25-k1 1.5 --bm25-b 0.75 --vector-weight 0.6 --persist-dir .tmp/eval_chroma_db --output-dir .tmp/rag-evaluation --fixture <フィクスチャパス> --dataset <データセットパス>`）+ レポート生成確認

### H) Core

A〜G の取り込みデータを使ってパイプライン基盤を検証する。H 単独実行時は、先に `README.md` を add-document で取り込んでからステップ 2 以降を実行する。

1. `stats` で統計表示
2. `list-recent --source-type local` で local ソース一覧を確認
3. `list-recent --source-type journal` で journal ソース一覧を確認
4. `search` で直前に取り込んだ内容に関連するワードで検索確認
5. `get-document` で A) の README.md の source_id を指定して全文取得（text / original）
6. `delete` で同 source_id を削除 + 共通検証（削除版）
7. `rebuild --mode incremental` で再構築 + `stats` 確認

## 制約

- テストデータの作成時、実在の著作物・キャラクター情報を使用しない（`~/.claude/rules/invariants.md`）
- 各グループの実行中にエラーが発生した場合、そのステップを NG として記録し、次のステップに進む。グループ全体を中断しない
- MCP ツールの検証では、MCP サーバーが enabled であることを確認してから実行する。disabled の場合はユーザーに `/mcp` での状態変更を依頼する

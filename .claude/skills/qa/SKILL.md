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

### 2. インターフェース選択

```
インターフェースを選択してください:
  1) CLI のみ
  2) MCP のみ
  3) both（CLI + MCP）

※ Upload(G) は HTTP 固定、Eval(H) は CLI 固定です
```

### 3. 環境確認

- 現在のブランチ・作業ディレクトリを表示
- MCP サーバーの状態確認（CLI 検証時は disabled、MCP 検証時は enabled であることを確認）
- worktree 環境の場合、`.env` のストレージパスが worktree 内を指していることを確認
- 問題があればユーザーに報告し、解決してから続行

### 4. グループ実行

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

**YouTube（E）選択時の必須確認:**

グループ E の実行直前に以下を表示し、ユーザー確認を取る。確認なしに実行してはならない:

```
YouTube 検証を実行します。
YouTube API へのアクセスにより IP ブロックのリスクがあります。
実行しますか？ (y/n)
```

**取り込み後の共通検証フロー:**

取り込み・再構築を行うステップの後に、以下を必ず実行する:

1. **実データ確認**: source_store / converted_store のファイルを `ls` で確認。`.meta` ファイルの内容を確認（source_type, title, collected_at）
2. **Git 状態確認**: `git status` で新規ファイルの追加を確認
3. **検索確認**: `search --query <取り込み内容に関連するクエリ>` で検索し、取り込んだソースがヒットすること、メタデータ行が正しいことを確認

削除ステップの後は逆方向（ファイル削除、git status 反映、検索結果から消失）を確認する。

### 5. 結果サマリー

全グループ完了後に結果テーブルを表示:

```
QA 検証結果:

| グループ | インターフェース | 結果 |
|---------|----------------|------|
| A Core  | CLI            | OK   |
| A Core  | MCP            | OK   |
| B Local | CLI            | NG — 詳細 |
| ...     | ...            | ...  |

NG 項目: N 件
```

NG を検出した場合、Issue 起票を提案する。

## グループ別の検証内容

仕様書 `docs/specs/agentic/skills/qa-skill.md` の「グループ別検証手順」セクションに従う。以下は各グループの要約:

### A) Core

1. `stats` で統計表示
2. テストデータ取り込み（Core 単独時のみ、add-document で Markdown 取り込み + 共通検証）
3. `search` で検索確認
4. `get-document` で全文取得（text / original）
5. `delete` で削除 + 共通検証（削除版）
6. `rebuild --mode incremental` で再構築 + `stats` 確認

### B) Local

1. `add-document` で Markdown 取り込み + 共通検証
2. `add-document` で同名上書き（CLI: `--upload-mode replace` / MCP: `upload_mode="replace"`）+ 共通検証
3. `crawl-documents` でディレクトリ一括 + 共通検証
4. `add-journal` でジャーナル登録 + 共通検証
5. `migrate-journal` で一括配置 + `rebuild --mode incremental` + 共通検証

### C) Web

1. `crawl-preview` でプレビュー表示
2. `add` で 1 ページ取り込み + 共通検証
3. `crawl` でクロール（depth=1）+ 共通検証
4. `site-ingest` で Scrapy 取り込み（--max-pages 3 --download-only）+ ファイル配置確認

### D) SNS

1. `crawl-zenn` で記事 1 件（--max-articles 1）+ 共通検証
2. `crawl-bluesky` で投稿 1 件（--max-posts 1）+ 共通検証

### E) YouTube

1. ユーザー確認（必須）
2. `ingest-youtube` で動画 1 件 + 共通検証

### F) Aozora

1. `update-aozora-catalog` でカタログ更新 + ファイル配置確認
2. `search-aozora` でカタログ検索
3. `ingest-aozora` で作品 1 件 + 共通検証

### G) Upload（HTTP モード固定）

HTTP モードで MCP サーバーを起動して実行:

1. `POST /upload/document` 正常系 + 共通検証
2. `POST /upload/document` 同名エラー（409）
3. `POST /upload/document` 上書き（upload_mode=replace、200）
4. `POST /upload/journal` 正常系 + 共通検証
5. `POST /upload/journal` 必須フィールド欠落（400）

### H) Eval（CLI 固定）

評価フィクスチャのパスはメモリ `reference_eval_fixtures.md` を参照（メモリが利用できない場合はユーザーにパスを確認する）:

1. `init-test-db` でテスト DB 初期化 + ディレクトリ作成確認
2. `evaluate` で評価実行 + レポート生成確認

## 制約

- テストデータの作成時、実在の著作物・キャラクター情報を使用しない（`~/.claude/rules/invariants.md`）
- 各グループの実行中にエラーが発生した場合、そのステップを NG として記録し、次のステップに進む。グループ全体を中断しない
- MCP ツールの検証では、MCP サーバーが enabled であることを確認してから実行する。disabled の場合はユーザーに `/mcp` での状態変更を依頼する

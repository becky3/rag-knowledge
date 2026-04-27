# RAG Knowledge - 開発ガイドライン

## プロジェクト基盤情報

@README.md
@docs/specs/overview.md

## SSoT（Single Source of Truth）

設定制約（min/max/default）は `src/rag/config.py` の pydantic Field が SSoT であり、仕様書には設計意図（Why）のみ記載する。横断列挙値（`source_type` 等）は `_schema/enums.yml` が SSoT であり、仕様書では参照リンクで示す。詳細は overview.md の「SSoT 階層」セクションを参照。

## MCP サーバー運用ルール

本リポジトリは MCP サーバーの実装リポジトリであり、`.mcp.json` で Claude Code の MCP サーバーとして登録されている。MCP サーバーは HTTP モード（`RAG_TRANSPORT=http`）で運用し、`.mcp.json` は `url` ベースで接続する。サーバーは別プロセスで起動しておく必要がある。

### セッション開始時の状態確認

作業開始時に MCP サーバーの状態を確認すること。HTTP モードではサーバーは別プロセスで動作するため、enabled であってもサーバープロセスが停止していればツールは使えない。

- **サーバー起動中 + enabled の場合**: MCP ツールが使用可能。CLI 操作はファイルベースロックにより共存可能
- **サーバー停止中 or disabled の場合**: MCP ツールは使用できないが、CLI・テスト・開発作業は自由に行える

MCP の接続状態の変更はユーザーに `/mcp` での操作を依頼する。サーバープロセスの起動・停止はエージェントから実行可能。

#### Fake モード状態の確認

YouTube インジェスター等は Fake モード基盤（`docs/specs/infrastructure/fake-mode.md`）で実外部アクセス排除を制御する。MCP サーバーまたは CLI の起動ログで現在のモードを確認できる:

- `WARNING: [FAKE MODE] YouTube は FAKE モードで起動中（fixture: ...）` → fake モード（実 YouTube アクセス発生せず）
- `INFO: YouTube は REAL モードで起動中` → real モード（実 YouTube API アクセスあり）

`.env` の `RAG_YOUTUBE_FAKE_MODE` が未設定の場合は安全側のデフォルト（fake 有効）で起動する。本番運用時のみ `RAG_YOUTUBE_FAKE_MODE=false` を `.env` に明示する必要がある。MCP ツール `rag_add_youtube` / `rag_crawl_youtube` の応答冒頭に `[FAKE MODE]` ラベルが付与される場合、fake モードで動作している。

### MCP サーバーと他プロセスの共存

ChromaDB は HttpClient 経由でサーバーに接続するため、MCP と CLI の同時アクセスが可能。書き込み操作はファイルベースロック（fcntl/msvcrt）でプロセス間排他制御される。

| 作業 | MCP サーバー | 備考 |
|------|-------------|------|
| 通常開発・CLI 操作 | 起動中 / 停止中どちらでも可 | |
| テスト実行 | 停止推奨 | テスト用 DB との分離のため |
| MCP ツールの動作確認 | 起動中 + enabled | |

### DB 破損時の復旧

MCP サーバープロセスを停止 → ChromaDB サーバーが起動していることを確認（停止していれば `uv run chroma run --path <CHROMADB_PERSIST_DIR>` で手動起動）→ CLI `rebuild --mode full` で復旧する。

### worktree 環境セットアップ

worktree で CLI 操作・動作確認を行う場合、以下のセットアップを実施すること。

1. メインリポジトリから `.env` をコピーする（ストレージパスは相対パスのため、worktree 内の `.tmp/` が自動的に使用される。書き換え不要）:

   ```bash
   cp .env <worktree-path>/.env
   ```

2. LM Studio の接続先を確認し、必要に応じて `localhost` に変更する:

   ```
   LMSTUDIO_BASE_URL=http://localhost:1234/v1
   ```

3. `.tmp` ディレクトリを作成する:

   ```bash
   mkdir -p <worktree-path>/.tmp
   ```

4. ChromaDB サーバーを起動する。ポート競合時（前セッションの停止漏れ等）は `.env` で `CHROMADB_SERVER_PORT` を変更すること。初回は venv 構築のため起動に時間がかかる（目安: 10〜20 秒）。heartbeat 確認前に十分待機すること:

   ```bash
   uv run chroma run --path <worktree-path>/.tmp/test_chroma_db
   ```

### CLI 動作確認の確認観点

データ取り込み後、以下の確認を行うこと。

#### 登録データの直接確認

ChromaDB に格納されたデータを直接確認する。

- チャンク数が妥当か
- メタデータ（`section_path`, `source_type`, `title` 等）が正しく設定されているか
- ドキュメント（Embedding 入力）の内容が期待通りか

#### 検索結果の確認

```bash
uv run python -m rag.cli search --query "<クエリ>"
```

- ベクトル検索・BM25 の両方で結果が返ること
- メタデータ行（Source, Title, Chunk, Type, Section, Collected）が正しく表示されること
- 検索結果の内容がクエリに関連していること

### worktree コードでの MCP 動作確認

worktree で開発中のコードを MCP サーバーとして動作確認する手順:

1. worktree で MCP サーバーを HTTP モードで起動する（ストレージパスは相対パスのため書き換え不要）:

   ```bash
   cd <worktree-path> && uv run python -m rag.server &
   ```

2. `.mcp.json` の `url` は `http://localhost:<RAG_HTTP_PORT>/mcp` のまま変更不要（worktree のサーバーが同じポートで起動するため）。ポート競合時（前セッションの停止漏れ等）は先に該当プロセスを停止すること

3. `/mcp` で disabled の場合は enable、enabled の場合は reconnect（サーバー再接続）。`.mcp.json` を変更した場合はセッション再起動が必要

4. 動作確認完了後、worktree のサーバープロセスを停止する

## Claude Code 拡張機能

### 自律呼び出しルール（プロジェクト固有）

以下はプロジェクト固有のルール:

| ユーザー表現 | 呼び出し先 | 種別 |
|-------------|-----------|------|
| 「テスト実行して」「テスト通して」 | test-runner | エージェント |
| 「QAして」「動作確認して」 | `/qa` | スキル |

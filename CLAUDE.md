# RAG Knowledge - 開発ガイドライン

## プロジェクト基盤情報

@README.md
@docs/specs/overview.md

## MCP サーバー運用ルール

本リポジトリは MCP サーバーの実装リポジトリであり、`.mcp.json` で Claude Code の MCP サーバーとして登録されている。MCP サーバーが enabled の場合、Claude Code セッション中はサーバープロセスが常駐し、ChromaDB・BM25 等のリソースを占有する。

### セッション開始時の状態確認

作業開始時に MCP サーバーの状態を確認すること。前回セッションの状態が引き継がれるため、意図しない状態で作業を始めるリスクがある。

- **enabled の場合**: MCP サーバーが DB を占有している。CLI や テストで DB にアクセスする作業は競合する
- **disabled の場合**: MCP ツールは使用できないが、CLI・テスト・開発作業は自由に行える

状態に応じて必要なら、ユーザーに `/mcp` での状態変更を依頼する。`/mcp` での状態変更はエージェントからは実行できない。

### MCP サーバーと他プロセスの排他

MCP サーバー enabled 中は、**同じ DB に対する CLI 操作・テスト実行・別プロセスからのアクセスを行わないこと**。ChromaDB の PersistentClient は単一プロセスアクセスを前提としており、複数プロセスからの同時アクセスでインデックスが破損する。

| 作業 | 必要な MCP 状態 |
|------|---------------|
| 通常開発・CLI 操作・テスト実行 | disabled |
| MCP ツールの動作確認 | enabled |

### DB 破損時の復旧

MCP を disable → CLI `rebuild --mode full` で復旧する。

### worktree コードでの MCP 動作確認

worktree で開発中のコードを MCP サーバーとして動作確認する手順:

1. `.mcp.json` の `args` に `--directory` を追加し、worktree の絶対パスを指定する:

   ```json
   "args": ["--directory", "D:\\GitHub\\becky3\\rag-knowledge-wt-XXX", "run", "python", "-m", "rag.server"]
   ```

2. worktree の `.env` でストレージパスを絶対パスに変更する（相対パスだとメインリポジトリのストレージを参照してしまう）:

   ```
   CHROMADB_PERSIST_DIR=D:/GitHub/becky3/rag-knowledge-wt-XXX/.tmp/test_chroma_db
   ```

3. `/mcp` で disable → enable（プロセス再起動が必要。reconnect では `.env` が再読み込みされない）

4. 動作確認完了後、`.mcp.json` を元に戻す（`--directory` を削除）

注意: `--directory .` は MCP サーバー起動時の cwd に依存するため使用不可。絶対パスを指定すること。

## Claude Code 拡張機能

### 自律呼び出しルール（プロジェクト固有）

以下はプロジェクト固有のルール:

| ユーザー表現 | 呼び出し先 | 種別 |
|-------------|-----------|------|
| 「テスト実行して」「テスト通して」 | test-runner | エージェント |

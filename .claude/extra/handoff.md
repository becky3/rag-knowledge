# rag-knowledge: handoff 追加手順

worktree クリーンアップの前に、以下のプロセスを停止すること。停止しないとファイルロックにより worktree の削除に失敗する。

## 実行タイミング

handoff のステップ 1（本ファイル読み込み時）で即座に実行する。後続の `/merged` スキルによる worktree 削除時にファイルロックを回避するため、セッション終了前にプロセスを停止しておく。

## 停止対象プロセス

worktree パスを参照しているプロセスを特定し、停止する:

```bash
# worktree のプロセスを特定（Windows）
wmic process where "CommandLine like '%<worktree-path>%'" get ProcessId,CommandLine

# プロセスツリーごと停止（//T で子プロセスも含む）
taskkill //PID <pid> //T //F
```

主な停止対象:

- **ChromaDB サーバー**: worktree 内の `.tmp/` を参照して起動している場合
- **MCP サーバー**: worktree で `uv run python -m rag.server` を起動している場合

## 停止失敗時

プロセスの停止に失敗した場合、ユーザーに以下を報告する:

- 停止できなかったプロセスの PID とコマンドライン
- worktree の手動削除が必要な旨

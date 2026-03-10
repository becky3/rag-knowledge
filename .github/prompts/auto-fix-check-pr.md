# auto-fix: レビュー指摘対応プロンプト

<!-- markdownlint-disable MD014 MD031 -->

PR #{{PR_NUMBER}} のレビュー指摘に対応してください。
以下の手順を順番に全て実行すること。途中で停止しないこと（ただし「制約事項」に記載の中断条件を除く）。

## ステップ 1/8: PR情報の確認

```bash
gh pr view {{PR_NUMBER}} --json title,body,headRefName,baseRefName
gh pr view {{PR_NUMBER}} --json files --jq '.files[].path'
gh pr diff {{PR_NUMBER}}
```

## ステップ 2/8: 未解決レビューコメントの取得

まず owner/repo を取得する:

```bash
OWNER=$(gh repo view --json owner --jq '.owner.login')
REPO=$(gh repo view --json name --jq '.name')
echo "Owner: $OWNER, Repo: $REPO"
```

取得した OWNER, REPO を使って GraphQL で未解決スレッドを抽出:

```bash
gh api graphql -f query="
{
  repository(owner: \"$OWNER\", name: \"$REPO\") {
    pullRequest(number: {{PR_NUMBER}}) {
      reviewThreads(first: 100) {
        nodes {
          isResolved
          comments(first: 10) {
            nodes {
              author { login }
              body
              path
              line
            }
          }
        }
      }
    }
  }
" --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | .comments.nodes[0] | {author: .author.login, path, line, body}'
```

未解決の指摘がない場合は「未解決の指摘はありません」と表示して終了。

## 制約事項

本セッションのターン上限は約100です。
対応が重い指摘は無理に着手せず、下記の判断基準に従って別Issue化または中断してください。

## ステップ 3/8: 関連仕様書の確認と修正実施

- PRの目的に対応する `docs/specs/` の仕様書を特定して読む
- 各指摘について、以下の順で判断する:
  1. **指摘が妥当でない** → スキップ理由を記録して続行
  2. **妥当かつ軽微な修正で対応可能** → その場で修正
  3. **妥当だが重い対応が必要** → GitHub Issue を作成して ⏸️ 別Issue化し、続行:
     - 新規ファイルの作成を伴う対応
     - 設計判断が必要（複数の対応方法がありえる）
     - 既存の仕様・アーキテクチャの変更を伴う
  4. **妥当かつ、PRの実装方針自体の見直しが必要** → 対応を中断する。
     中断理由を `gh pr comment` で投稿し、`exit 1` で終了する。
     `auto:failed` ラベルの付与は既存のエラーハンドラが行う

## ステップ 4/8: テスト実行

注: GA環境ではサブエージェント（test-runner等）が利用できないため、直接実行する。

```bash
uv run pytest && uv run ruff check src/ tests/ && uv run mypy src/
npx markdownlint-cli2@0.20.0
```

Markdown のみの変更の場合、pytest / ruff / mypy はスキップ可（markdownlint のみ実行）。
失敗があれば修正して再実行。全て通過してから次へ進む。

## ステップ 5/8: ドキュメント整合性チェック

修正内容が以下のドキュメントの記述と矛盾しないか確認し、必要なら更新する:

- `docs/specs/` — 仕様・受け入れ条件に影響する変更の場合
- `CLAUDE.md` — 開発ルール・プロジェクト構造に影響する場合

注: GA環境ではサブエージェント（doc-reviewer等）が利用できないため、
`docs/` や `CLAUDE.md` に変更がある場合は自分で仕様書との整合性を確認すること。

## ステップ 6/8: PRに対応コメント投稿

`gh pr comment {{PR_NUMBER}}` で対応状況を表形式で投稿:

```text
## レビュー指摘への対応

| # | 状態 | 指摘 | 対応 |
|---|:----:|------|------|
| 1 | ✅ | 指摘の要約 | 対応内容の説明 |
| 2 | ⏸️ | 指摘の要約 | Issue #N に切り出し |
| 3 | ❌ | 指摘の要約 | 対応不要の理由 |
```

## ステップ 7/8: 判断済みスレッドの resolve

判断済みスレッド（✅ 対応済み、❌ 対応不要、⏸️ 別Issue化）を resolveReviewThread mutation で resolve する。
以下のエラーハンドリング方針に従うこと:

- owner/repo 取得失敗 → エラーログを出力して resolve をスキップ
- 認証エラー（401/403） → エラーログを出力して即停止
- 一時的API障害（個別resolve失敗） → warning でログし次のスレッドに継続
- 全件resolve失敗 → 認証/権限エラーの可能性があるためエラーログを出力

```bash
PR_NUMBER={{PR_NUMBER}}

# Note: このコードブロック内で "if !" パターンを使用しないこと
# Claude Code サンドボックスの shell-quote が ! を \! にエスケープし
# bash が予約語として認識できなくなる (claude-code#24136)

# owner/repo の取得と検証（環境情報付きエラーログ）
OWNER=$(gh repo view --json owner --jq '.owner.login' 2>&1) || {
  echo "::error::Failed to get repository owner: $OWNER (GH_REPO=${GH_REPO:-unset}, GH_TOKEN length=${#GH_TOKEN})"
  exit 1
}
REPO=$(gh repo view --json name --jq '.name' 2>&1) || {
  echo "::error::Failed to get repository name: $REPO (GH_REPO=${GH_REPO:-unset}, OWNER=$OWNER)"
  exit 1
}

# 未解決スレッドIDの取得
THREADS=""
QUERY_SUCCESS=false
if THREADS=$(gh api graphql -f query="
{
  repository(owner: \"$OWNER\", name: \"$REPO\") {
    pullRequest(number: $PR_NUMBER) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
        }
      }
    }
  }
}" --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | .id' 2>&1); then
  QUERY_SUCCESS=true
else
  # GraphQLエラー形式を確認してからフォールバック
  if echo "$THREADS" | jq -e '.errors' > /dev/null 2>&1; then
    ERROR_TYPE=$(echo "$THREADS" | jq -r '.errors[0].type // "UNKNOWN"')
    ERROR_MSG=$(echo "$THREADS" | jq -r '.errors[0].message // "No message"')
    if [ "$ERROR_TYPE" = "FORBIDDEN" ] || [ "$ERROR_TYPE" = "UNAUTHORIZED" ]; then
      echo "::error::GraphQL auth error (type=$ERROR_TYPE): $ERROR_MSG"
      exit 1
    fi
    echo "::warning::GraphQL error (type=$ERROR_TYPE): $ERROR_MSG"
  elif echo "$THREADS" | grep -qE '401|403|authentication|forbidden'; then
    echo "::error::Authentication/permission error: $THREADS"
    exit 1
  else
    echo "::warning::Failed to query review threads: $THREADS"
  fi
  THREADS=""
fi

# スレッドの resolve（失敗カウンター付き、エラー種別分類）
if [ -z "$THREADS" ]; then
  if [ "$QUERY_SUCCESS" = true ]; then
    echo "No unresolved threads to resolve. Skipping."
  else
    echo "::warning::Skipping resolve due to query failure."
  fi
else
  RESOLVED=0
  FAILED=0
  while IFS= read -r THREAD_ID; do
    [ -z "$THREAD_ID" ] && continue
    # THREAD_ID のフォーマット検証（GitHub thread IDはBase64エンコードされるため=も許容）
    [[ "$THREAD_ID" =~ ^[A-Za-z0-9_=-]+$ ]] || {
      echo "::warning::Invalid thread ID format: $THREAD_ID"
      FAILED=$((FAILED + 1))
      continue
    }
    # IDを直接埋め込む（GraphQL変数は環境依存の問題があるため）
    if ERROR=$(gh api graphql -f query="
    mutation {
      resolveReviewThread(input: { threadId: \"$THREAD_ID\" }) {
        thread { isResolved }
      }
    }" 2>&1); then
      RESOLVED=$((RESOLVED + 1))
    else
      FAILED=$((FAILED + 1))
      # エラー種別の推定と分類
      if echo "$ERROR" | jq -e '.errors' > /dev/null 2>&1; then
        ERR_TYPE=$(echo "$ERROR" | jq -r '.errors[0].type // "UNKNOWN"')
        ERR_MSG=$(echo "$ERROR" | jq -r '.errors[0].message // "No message"')
        echo "::warning::Failed to resolve thread $THREAD_ID: [GraphQL $ERR_TYPE] $ERR_MSG"
      elif echo "$ERROR" | grep -qE '401|403|authentication|forbidden'; then
        echo "::warning::Failed to resolve thread $THREAD_ID: [AUTH] $ERROR"
      elif echo "$ERROR" | grep -qE 'not found|NOT_FOUND|Could not resolve'; then
        echo "::warning::Failed to resolve thread $THREAD_ID: [NOT_FOUND] $ERROR"
      else
        echo "::warning::Failed to resolve thread $THREAD_ID: [API_ERROR] $ERROR"
      fi
    fi
  done <<< "$THREADS"

  echo "Resolved: $RESOLVED, Failed: $FAILED"
  if [ "$RESOLVED" -eq 0 ] && [ "$FAILED" -gt 0 ]; then
    echo "::error::All thread resolutions failed ($FAILED threads). Possible auth/permission issue."
    exit 1
  fi
fi
```

## ステップ 8/8: コミット & push

```bash
git add -A
if [ -n "$(git status --porcelain)" ]; then
  git diff --cached --stat
  git commit -m "fix: レビュー指摘対応 (PR #{{PR_NUMBER}})"
  git push origin HEAD
else
  echo "No changes to commit. Skipping."
fi
```

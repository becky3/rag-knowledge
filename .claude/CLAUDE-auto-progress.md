# 自動進行ルール（auto-progress）

> このファイルは GitHub Actions 環境（claude-code-action）専用のルール。
> ローカル対話セッションでは参照不要。
> 仕様: shared-workflows リポジトリの `docs/specs/auto-progress.md`

## 起動時の判断フロー

1. Issue/PRの内容を読む（タイトル、本文、全コメント）
2. 仕様書の存在確認（`docs/specs/` に対応する仕様書があるか）
   - あり + AC定義済み: 仕様書に従って実装を開始
   - あり + AC未定義: ACを追加してから実装を開始
   - なし + Issue/PR具体的: `/doc-gen spec` で仕様書を自動生成し、そのまま実装（事後拒否権モデル）
   - なし + Issue/PR曖昧: `auto:failed` 付与、不明点をIssue/PRにコメントして停止
3. 通常の開発ルール（仕様駆動開発）に従って実装
4. 実装完了後、通常の手順でPR作成まで行う（ベースブランチは `develop`）
5. ラベル更新:
   - 実装開始時: `auto-implement` ラベルを除去
   - 失敗時: `auto:failed` 付与

## Git運用

- PRのベースブランチは `develop`（`gh pr create --base develop`）
- `main` への直接PRは作成しない（リリースPR以外）
- ブランチ命名規則は既存ルールを踏襲

## Issue/PR具体性の判定

具体的と見なす: 入出力例あり、具体的な変更指示、バグ再現手順あり
曖昧と見なす: 「検討」「改善」のみ、複数要求混在、外部仕様未確定

## `auto:failed` ラベル

`auto:failed` ラベルが付いたIssue/PRは自動処理を一切行わない。
自動処理の失敗時に自動付与されるほか、管理者が手動で付与して緊急停止にも使う。
再開するには `auto:failed` を除去し、`auto-implement` を再付与する。

## 品質チェック

以下の4ステップを順番に全て実行すること。
途中で停止しないこと。全ステップ完了まで続けること。

### ステップ 1/4: テスト実行

```bash
uv run pytest && uv run ruff check src/ tests/ && uv run mypy src/ && npx markdownlint-cli2@0.20.0
```

- Markdown のみの変更の場合、pytest / ruff / mypy はスキップ可（markdownlint のみ実行）
- 失敗があれば修正して再実行。全て通過してから次へ進む

### ステップ 2/4: コードレビュー

`code-reviewer` エージェントを **フォアグラウンド**（`run_in_background: false`）で呼び出し、diff モードでコードレビューを実行する。

- コード変更がない場合（ドキュメントのみの変更）はスキップ可
- エージェントが返した指摘は、各指摘について対応すべきかそれぞれ判断し、修正する
- **検出した問題のスキップ禁止**: `~/.claude/rules/quality-gate.md`「問題スキップ禁止」に従うこと。判断に迷う問題は PR/Issue コメントに問題事項を記載し、作業を中断する

### ステップ 3/4: ドキュメントレビュー

`doc-reviewer` エージェントを **フォアグラウンド**（`run_in_background: false`）で呼び出し、diff モードでドキュメントレビューを実行する。

- ドキュメント変更がなく、対応する仕様書も存在しない場合はスキップ可
- 対応する仕様書が存在する場合は、実装のみの変更でも整合性チェックを実施すること

**ここで停止しないこと。次のステップ4を必ず実行すること。**

### ステップ 4/4: 完了処理（commit / push / PR作成 / Issue完了コメント）

1. `git status --porcelain` で変更確認（空なら停止）
2. `git add -A`（GA環境ではリポジトリに機密情報がない前提のため許容）
3. `git diff --cached --stat` で差分サマリ表示
4. 変更内容からコミットメッセージ自動生成（優先順位: fix > feat > docs > ci）
5. `git commit -m "生成したメッセージ"`
6. `BRANCH=$(git branch --show-current) && git push origin "$BRANCH"`
7. PR body は `.github/pull_request_template.md` の形式に従い、PR を作成する
8. `gh issue comment <Issue番号> --body "対応が完了しました。PR #<PR番号> をご確認ください。"`

エラー時: ステップ1-7は失敗で停止。ステップ8は警告して続行（PRは作成済み）。
commitが成功していない状態でpushしないこと。

## GitHub Actions 環境（claude-code-action）の制約

**対話不可の制約**:

- `AskUserQuestion` ツールは使用不可（`permission_denials` で拒否される）
- 不明点があっても質問せずに、以下の原則で自分で判断して進めること:
  - 情報不足で判断できない場合は、最も妥当な選択を行い、その判断理由をコメントに明記する
  - 曖昧な指示は、CLAUDE.md のルールに基づいて解釈する
  - 完璧を求めず、まず動くものを作ることを優先する

**Issueから実装する際の注意点**:

- **Issue本文だけでなく、コメントも必ず確認すること**
  - コメントに追加の要件・制約・補足情報が書かれていることがある
  - コメントの要件が仕様書のACに反映されていない場合は、ACに追加してから実装する
  - 過去の教訓: コメントで「外部サイトにアクセスしない」と明言されていたが、見落としてセキュリティ脆弱性が発生した

**タスク完了時の必須アクション**:

- 実装や設計書の作成を依頼された場合は、**必ず PR を作成すること**（`gh pr create --base develop`）
  - 調査のみ・設計案の提示のみの場合は PR 不要（Issue コメントで回答する）
- PRを作成したら、必ず対応したIssueに「新規コメント」として完了報告を投稿すること
- 既存コメントの編集ではなく、新しいコメントを追加する（編集では通知が飛ばないため）

**設定ファイル**: `.github/workflows/claude.yml`

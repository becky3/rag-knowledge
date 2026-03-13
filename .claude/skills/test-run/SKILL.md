---
name: test-run
description: pytest・ruff・mypy・shellcheck によるコード品質チェックの実行・分析・修正提案
user-invocable: true
allowed-tools: Bash, Read, Grep, Glob, Edit
argument-hint: "[diff|full]"
---

## タスク

pytest による自動テスト実行、ruff によるリント、mypy による型チェック、shellcheck によるシェルスクリプトチェックを実行し、結果を分析して修正提案を行う。ドキュメント品質チェック（markdownlint・mermaid-lint・GitHub 互換チェック）は `/doc-lint` スキルに委譲する。

## 引数

`$ARGUMENTS` の形式:

- `diff`: 変更に関連するテストのみ実行（デフォルト）
- `full`: 全テスト実行
- 未指定: `diff` モードで実行

## 実行対象

- `tests/*.py` の pytest テストファイル
- `src/` および `tests/` のリントチェック（ruff）
- `src/` の型チェック（mypy）
- プロジェクト内の `*.sh` ファイルのシェルスクリプトチェック（shellcheck）
- ドキュメント品質チェック（markdownlint・mermaid-lint・GitHub 互換チェック）は `/doc-lint` スキルに委譲
- プロジェクトは `uv` によるパッケージ管理を使用

## 実行コマンド

- テスト: `uv run pytest`
- リント: `uv run ruff check src/ tests/`
- 型チェック: `uv run mypy src/`
- ドキュメント品質チェック: `/doc-lint` スキルに委譲

## 制約

- pytest・ruff・mypy・`/doc-lint`・shellcheck のいずれかが失敗してもプロセスを中断せず、すべてのチェックを実行して統合レポートを生成する
- `uv` コマンドが利用できない環境では適切にエラーを報告する
- `shellcheck` コマンドが利用できない環境（`command -v shellcheck` が失敗する場合）では shellcheck をスキップし、他のチェックは続行する
- テスト失敗時・リント違反・型エラー・shellcheck 違反時は、必ず該当箇所のソースコードを Read で読んでから分析する
- 修正案は具体的で、ファイルパス・行番号を含める
- 修正を適用する場合は、必ずユーザーの承認を得る

## 処理手順

### 1. 実行モードの判定

- `$ARGUMENTS` に `full` が含まれる → `full` モード
- `$ARGUMENTS` に `diff` が含まれる、または未指定 → `diff` モード

### 2. テスト対象の特定

#### diff モードの場合

変更ファイルの取得（以下の優先順位で試行）:

1. 作業ツリーの変更（未ステージ・ステージング両方）: `git diff --name-only`
2. ベースブランチとの比較: `base=$(git merge-base HEAD origin/develop) && git diff --name-only "$base" HEAD`
3. ステージング済みの変更のみ: `git diff --cached --name-only`
4. 直近コミットの差分（フォールバック）: `git show --name-only --format="" HEAD`
5. すべて失敗した場合 → `full` モードにフォールバック

変更ファイルから対応テストを推定（下記マッピングルール参照）。

**特殊ケース**:

- 変更ファイル 0 件で指定もなし → `full` モードにフォールバック
- Markdown ファイル（`*.md`）のみの変更 → pytest・ruff・mypy・shellcheck はスキップ、`/doc-lint` のみ実行
- シェルスクリプト（`*.sh`）のみの変更 → pytest・ruff・mypy はスキップ、shellcheck のみ実行（`/doc-lint` もスキップ）
- テスト設定ファイル（`pyproject.toml`, `conftest.py` 等）の変更 → `full` モードにフォールバック

#### full モードの場合

- 引数でファイルパスやテスト名が指定されていればそれを実行
- `-k` オプションによるパターンマッチも対応
- カバレッジ測定が要求されていれば `--cov` オプションを付与
- 未指定なら全テストを実行

### 3. テスト実行

```bash
uv run pytest
```

必要に応じて `-v` や `-vv` で詳細出力を有効化。

### 4. リント (ruff) 実行

```bash
uv run ruff check src/ tests/
```

diff モードでは変更された `*.py` ファイルのみを対象にする。

### 5. 型チェック (mypy) 実行

```bash
uv run mypy src/
```

diff モードでは変更された `src/**/*.py` ファイルのみを対象にする。

### 6. ドキュメント品質チェック（/doc-lint 委譲）

`/doc-lint` スキルに委譲する。実行モード（diff/full）は test-run の現在のモードを引き継ぐ。

`/doc-lint` は markdownlint・md-mermaid-lint・GitHub 互換チェックを一括実行する。チェック対象やバージョン管理は `/doc-lint` 側の責務であり、test-run では管理しない。

### 7. シェルスクリプトチェック (shellcheck) 実行

**CRLF 自動修正（Windows 環境・前処理）**: shellcheck 実行前に、対象 `*.sh` ファイルの CRLF 改行を LF に変換する。
Windows の Write ツールが CRLF で書き出す問題への対策。
この変換は shellcheck を正常実行するための前処理であり、「ユーザー承認後の修正適用」（ステップ 10）とは別の工程として自動実行する。
変換があった場合はログ出力する。diff モードでは shellcheck 対象ファイル（変更された `*.sh`）のみを変換対象とする。

```bash
# CRLF → LF 自動変換（shellcheck SC1017 防止）
# - git ls-files で git 管理下の *.sh のみを対象（.venv 等の非管理ファイルを除外）
# - NUL 区切りでスペース/改行を含むパスを安全に列挙
# - sed -i で in-place 変換
git ls-files -z '*.sh' | while IFS= read -r -d '' f; do
  if grep -q $'\r' "$f" 2>/dev/null; then
    sed -i 's/\r$//' "$f"
    echo "[fix] CRLF→LF: $f"
  fi
done
```

プロジェクト内の `*.sh` ファイルを対象に shellcheck を実行する。

diff モードでは変更された `*.sh` ファイルのみを対象にする（CRLF 変換も同じ対象に限定する）。対象ファイルがなければスキップする。

### 8. 結果の解析・詳細調査

各チェックツールの結果を解析する:

- **pytest 成功時**: 実行件数、実行時間、カバレッジ率（要求された場合）
- **pytest 失敗時**: 成功/失敗件数、各失敗テストのエラーメッセージ、スタックトレース
- **ruff 違反あり時**: 違反ファイル、ルールコード、行番号、違反内容
- **mypy エラーあり時**: エラーファイル、エラー種別、行番号、エラー内容
- **/doc-lint 違反あり時**: `/doc-lint` の出力を統合レポートに含める
- **shellcheck 違反あり時**: 違反ファイル、エラーコード（SC????）、行番号、違反内容、重大度（error/warning/info）

失敗・違反がある場合:

- 失敗したテストファイル・テスト対象のソースコードを Read で読み込み
- エラーの種類を特定（AssertionError, TypeError, AttributeError 等）
- 根本原因を分析（テストコードの問題、ソースコードの問題、依存関係の問題）
- リント違反・型エラー・shellcheck 違反時も該当箇所のコードを Read で確認
- ドキュメント違反は `/doc-lint` の出力を参照

### 9. 修正案の生成・問題への対処

各失敗テスト・リント違反・型エラー・shellcheck 違反に対して:

- エラー内容の要約
- 原因の説明
- 具体的な修正案（ファイルパス、行番号、修正コード）
- 修正の優先度（Critical/Warning/Suggestion）

検出した問題を「対応範囲外」「既存問題」としてスキップしてはならない。以下のルールに従って必ず対処すること:

- **軽微な問題**（typo、lint エラー、簡単な型エラー等）: その場で修正する
- **大きな問題**（設計変更が必要、影響範囲が広い等）: Issue を作成して記録する
- **判断に迷う場合**: ユーザーに相談する（自己判断でスキップしない）

### 10. 修正適用と再実行

- 修正が必要な場合、Edit ツールで修正を適用
- ドキュメント修正は `/doc-lint` に再実行を委譲
- 修正後に再度テスト・リント・型チェック・shellcheck を実行して確認

## 出力フォーマット

### 全て成功時

```markdown
### コード品質チェック結果 ✅

#### pytest
- **実行件数**: {N} passed
- **実行時間**: {X.XX}s

#### ruff (lint)
- **違反**: なし

#### mypy (型チェック)
- **型エラー**: なし

#### /doc-lint
- **違反**: なし

#### shellcheck
- **違反**: なし

すべてのチェックが成功しました。
```

### 失敗時

```markdown
### コード品質チェック結果 ❌

#### pytest
- **成功**: {N} passed
- **失敗**: {M} failed
- **実行時間**: {X.XX}s

##### 失敗したテスト

**{番号}. {テストファイル}::{テストケース名}** **【{対処区分}】**

**エラー内容:**
{エラーメッセージ}

**原因:**
- {原因の説明}

**修正案:**
{ファイルパス}:{行番号} の修正コード

---

#### ruff (lint)
- **違反**: {N}件（または「なし」）
- [{ファイルパス}:{行番号}] {違反内容} **【{対処区分}】**

#### mypy (型チェック)
- **型エラー**: {N}件（または「なし」）
- [{ファイルパス}:{行番号}] {エラー内容} **【{対処区分}】**

#### /doc-lint
- **違反**: {N}件（または「なし」）

#### shellcheck
- **違反**: {N}件（または「なし」）
- [{ファイルパス}:{行番号}] {違反内容} (SC{XXXX}) **【{対処区分}】**

---

#### 対処区分

各問題に対して、問題の性質に応じて以下のいずれかを付与する:

| 対処区分 | 行動指針 |
|---------|---------|
| 要修正 | その場で修正が必要 |
| 要Issue化 | Issue を作成して記録が必要 |
| 要相談 | ユーザーに相談が必要（独断で判断しない） |

---

#### 次のステップ
上記の修正を適用しますか？修正後に再度テストを実行します。
```

### チェック不能時

権限エラー・ツール実行失敗等でチェックを正常に完了できなかった場合:

```markdown
### コード品質チェック結果 ⚠️ チェック未完了

各チェックの実行状態:

| チェック | 状態 | 理由 |
|---------|------|------|
| pytest | ❌ 未実行 | {権限エラー等で起動できなかった場合} |
| ruff | ⚠️ 実行失敗 | {実行したが途中で失敗した場合} |
| mypy | ✅ 完了 | — |
| /doc-lint | ❌ 未実行 | {具体的な理由} |
| shellcheck | ❌ 未実行 | {具体的な理由} |

**再実行が必要です。**
```

- 状態ラベル: `✅ 完了`（正常終了）/ `⚠️ 実行失敗`（起動したが途中で失敗）/ `❌ 未実行`（権限エラー等で起動できなかった）
- 全チェックが実行できなかった場合も、個別の失敗理由を記載する
- 「問題なし」「成功」と誤解される表現（`✅` のみのレポート等）を使わない
- diff 取得コマンドが権限エラーやツール実行失敗で実行できなかった場合、「変更0件」として扱わず、必ず本フォーマットで失敗理由を報告する

## 差分テストのマッピングルール

| ソースファイルパターン | 対応テストファイル |
|----------------------|-------------------|
| `src/rag/*.py` | `tests/test_rag_*.py` + RAG 精度テスト判定 |
| `src/rag/embedding/*.py` | `tests/test_embedding.py` + RAG 精度テスト判定 |
| `tests/*.py` | そのまま実行対象に追加 |

### フォールバック条件

以下の場合は全テスト実行にフォールバック:

- `pyproject.toml` が変更された場合
- テスト設定ファイル（`conftest.py` 等）が変更された場合

### マッピング未該当ファイルの扱い

マッピングで対応テストが見つからないファイルがある場合、そのファイルのテストはスキップし、レポートに以下を含めて返却する:

- テスト未対応ファイルの一覧
- テスト追加の検討を促す提言（優先度: Suggestion）

全テスト実行へのフォールバックは行わない。ただし、他の変更ファイルにマッピング該当があればそのテストは実行する。

## RAG 精度テスト

RAG 関連ファイル（`src/rag/**`, `src/rag/embedding/**`）が変更された場合、通常のテストに加えて RAG 精度テストの実行を検討する。

### 必要性の判定

| 変更内容 | 精度テストの必要性 |
|----------|-------------------|
| チャンキングロジック（`chunker.py`, `heading_chunker.py`, `table_chunker.py`） | **必須** |
| ベクトル検索ロジック（`vector_store.py`, `hybrid_search.py`, `bm25_index.py`） | **必須** |
| Embedding プロバイダー（`src/rag/embedding/**`） | **必須** |
| 類似度閾値・検索パラメータ | **必須** |
| RAG サービスの統合ロジック（`rag_knowledge.py`） | 推奨 |
| CLI・評価ロジック（`cli.py`, `evaluation.py`） | 不要（ユニットテストで十分） |

### 実行フロー

RAG 精度テストが必要と判断した場合、確認なしで自動実行する:

1. テスト用 DB を初期化（ChromaDB + BM25）:

   ```bash
   uv run python -m rag.cli init-test-db \
     --chunk-size 200 --chunk-overlap 30 \
     --persist-dir .tmp/test_chroma_db \
     --bm25-persist-dir .tmp/test_bm25_index \
     --fixture tests/fixtures/rag_test_documents.json
   ```

2. 精度評価を実行:

   ```bash
   uv run python -m rag.cli evaluate \
     --persist-dir .tmp/test_chroma_db \
     --output-dir reports/rag-evaluation \
     --chunk-size 200 --chunk-overlap 30 \
     --vector-weight 0.6 \
     --bm25-k1 1.5 --bm25-b 0.75
   ```

3. 結果を報告: レポート（`reports/rag-evaluation/report.md`）の内容を表示

## テスト名規約

テスト名は `test_` プレフィックス + snake_case で、テスト対象の振る舞いがわかる名前をつける:

- `test_crawled_page_is_chunked_and_stored`
- `test_duplicate_url_replaces_existing_chunks`

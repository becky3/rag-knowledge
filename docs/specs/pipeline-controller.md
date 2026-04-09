# パイプライン制御

## 概要

パイプライン制御は、3段パイプライン（インジェスター → コンバーター → インデクサー）のステージ間連携を担当するオーケストレーション層である。

各ステージは自分の責務のみに集中し、パイプライン制御が以下を一元管理する:

- source_store の git 操作（init、add、commit、diff）
- git diff による変更ファイルの特定
- ステージ間のデータ受け渡し
- 更新モードの制御（差分更新 / 全再構築）

スコープ:

- パイプラインの実行制御（差分更新、全再構築、コンバートのみ再実行、インデックスのみ再構築）
- source_store の git 操作（init、add、commit、diff）
- 変更ファイルの分類とステージへの振り分け
- `last_commit_id` の管理

スコープ外:

- 各ステージの内部処理ロジック（データ取得、テキスト変換、インデックス構築）
- 外部 API 通信（インジェスターの範疇）
- source_store のディレクトリ構成・.meta 形式の定義（source_store 仕様の範疇）

## 背景

- 3段パイプラインの各ステージ（インジェスター、コンバーター、インデクサー）が git 操作やステージ間連携を個別に実装すると、責務が混在し保守性が低下する
- git diff を使った変更検知を一箇所で管理することで、差分更新の信頼性を確保する

## 制約

- **git 操作の一元管理**: git 操作はパイプライン制御のみが実行する。各ステージ（インジェスター、コンバーター、インデクサー）は git コマンドを直接呼び出さない
- **ステージの独立性**: 各ステージは入力と出力のみを知り、他のステージの内部には依存しない
- **変更検知の正確性**: `last_commit_id` と HEAD の差分のみを処理対象とする。差分外のファイルは処理しない（全再構築モードを除く）
- **冪等性**: 同じ差分に対してパイプラインを再実行しても、結果が変わらないこと
- **バッチサイズ無制限**: 各ファイルを順次処理しメモリに全件を保持しないため、パイプライン制御層としてのバッチサイズ制限は設けない。ただし、後続ステージ（特にインデクサーの Embedding 生成）がオンラインプロバイダを使用する場合、外部 API へのリクエストが発生する。外部 API のレート制限・バックオフは各ステージ側の責務とする
- **git リネーム検知**: git のデフォルト挙動（類似度 50% 以上でリネームと判定）に従う。閾値のカスタマイズは行わない

本コンポーネントは外部 API 通信を行わないため、想定プロファイル・安全制約セクションは省略する。

## インターフェース

### パイプライン実行

| 操作 | 入力 | 出力 | 振る舞い |
|------|------|------|---------|
| 差分更新 | なし（自動検知） | 処理結果サマリ | source_store に未コミットの変更がある場合は自動コミットし、`last_commit_id` と HEAD の差分を検知して変更ファイルのみをパイプライン処理する。通常運用のデフォルト操作 |
| 全再構築 | source_type フィルタ（任意） | 処理結果サマリ | converted_store とインデックスをクリアし、source_store 全ファイルをパイプライン処理する。source_type 指定時はその媒体のみ対象。データ破損時や大規模な設計変更時に使用。source_store に未コミットの変更がある場合はエラー |
| コンバートのみ再実行 | source_type フィルタ（任意） | 処理結果サマリ | converted_store をクリアし、source_store 全ファイルをコンバーターで再処理する。source_type 指定時はその媒体のみ対象。コンバーターの変換ロジック改修時に使用。インデックスは後続のインデックス再構築で更新する。source_store に未コミットの変更がある場合はエラー |
| インデックスのみ再構築 | source_type フィルタ（任意） | 処理結果サマリ | ChromaDB + BM25 をクリアし、converted_store 全ファイルからインデックスを再構築する。source_type 指定時はその媒体のインデックスのみ削除して再構築する（他の source_type のインデックスは維持）。Embedding モデル変更時やチャンクパラメータ変更時に使用。source_store に未コミットの変更がある場合はエラー |
| 取り込み実行 | コミットメッセージ | 処理結果サマリ | インジェスター実行後の後処理を一括実行する。source_store の変更を `git add -A` + `git commit` し、差分更新を実行する。インジェスターと後続パイプライン処理を結合する便利操作 |

差分更新・コンバートのみ再実行・インデックスのみ再構築は処理結果サマリ（`PipelineSummary`）を返す。全再構築は convert フェーズと index フェーズそれぞれの `PipelineSummary` を保持する `FullRebuildResult` を返す。

サマリの構造:

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `mode` | str | 実行モード（`incremental`, `convert`, `index`） |
| `total_files` | int | 処理対象ファイル総数 |
| `processed` | int | 正常処理されたファイル数 |
| `errors` | list[str] | 予期しない例外が発生したファイルパスのリスト |
| `warnings` | list[str] | 非致命的スキップの詳細リスト（ファイルパス + 理由） |
| `from_commit_id` | str | 処理開始時点のコミット ID |
| `to_commit_id` | str | 処理終了時点のコミット ID |

全操作は `progress_callback`（任意）を受け取り、ファイル処理完了ごとにコールバックを呼び出す。callback シグネチャ: `(processed: int, total: int, current: str) -> None`。未指定時は進捗通知なし。詳細は [rebuild-stats.md](rebuild-stats.md) の CLI サブプロセス進捗通知セクションを参照。

`current` にはフェーズ名プレフィックスが付与される。フェーズ名はパイプライン処理の段階を示す:

| フェーズ名 | 意味 | 付与箇所 |
|-----------|------|---------|
| `[Fetch]` | 外部ソースからのデータ取得 | CLI がインジェスターに渡す callback をラップして付与 |
| `[Convert]` | ソースファイルのテキスト変換 | `run_convert_only`、`run_full_rebuild`（Phase 1） |
| `[Index]` | チャンキング・Embedding・インデックス登録 | `run_index_only`、`run_full_rebuild`（Phase 2） |
| `[Convert & Index]` | 変換とインデックスの一体処理 | `_process_changes`（差分更新） |

> **TODO:#495** MCP 経由の進捗通知では、Claude Code が `report_progress` の `message` パラメータを表示しないため、フェーズ名はユーザーに見えない
> （Claude Code の MCP クライアント側の制約。上流: anthropics/claude-code#3174）。
> CLI の `--output json` モードでは `current` フィールドにフェーズ名が含まれる。

### 未コミット変更の扱い

モードによって未コミット変更の扱いが異なる。

| モード | 未コミット変更がある場合の振る舞い |
|--------|-------------------------------|
| 差分更新（incremental） | 自動コミットしてから差分更新を実行する |
| 全再構築 / コンバートのみ / インデックスのみ | エラーとして再構築を拒否する |
| 取り込み実行 | 取り込み時のコミットに含まれる（従来動作） |

#### 差分更新の自動コミット

差分更新（incremental）は、未コミットの変更がある場合に自動コミットを実行してから差分を処理する。

1. source_store の未コミット変更を検知する
2. 変更がある場合、ステージング＋コミットで変更をコミットする（コミットメッセージ形式は「コミットメッセージ規則」を参照）
3. コミット成功後、差分更新処理に進む
4. 変更がない場合は自動コミットをスキップし、差分更新に進む
5. git commit コマンド自体が失敗した場合（権限エラー等）はエラーとする

#### 再構築操作の未コミットチェック

再構築操作（全再構築・コンバートのみ再実行・インデックスのみ再構築）は、未コミットの変更がある場合エラーとする。`source_type` 指定時はそのディレクトリのみをチェックする。

| source_type | 未コミットチェックの範囲 |
|-------------|---------------------|
| 指定あり | `git status --porcelain -- {source_type}/` でそのディレクトリのみチェック |
| 指定なし | `git status --porcelain` で全体チェック |

### git 操作

| 操作 | 入力 | 出力 | 振る舞い |
|------|------|------|---------|
| リポジトリ初期化 | source_store パス | なし | source_store ディレクトリで `git init` を実行する。既に初期化済みの場合は何もしない |
| ステージング＋コミット | コミットメッセージ、対象パス（任意） | コミット ID | source_store 内の変更をステージング＋コミットする。対象パス指定時は `git add {対象パス}/` でそのパス配下のみをステージングする。未指定時は `git add -A` で全体をステージングする。変更がない場合はスキップする。CLI `rebuild --commit-message` 指定時は再構築前にこの操作を実行する |
| 差分取得 | 基準コミット ID | 変更ファイルリスト | `git diff --name-status <基準ID>..HEAD` で変更ファイル（追加・変更・削除・リネーム）を取得する |

### 変更ファイルリストの構造

git diff から取得する変更ファイルリストの各エントリが持つ情報。

| フィールド | 内容 |
|-----------|------|
| `status` | 変更種別: `added`（追加）、`modified`（変更）、`deleted`（削除）、`renamed`（リネーム）、`meta_only`（メタデータのみ変更） |
| `file_path` | source_store 内の相対パス |
| `old_path` | リネーム時の旧パス（リネーム以外では空） |

## コンポーネント構成

### パイプラインフロー（差分更新）

1. 未コミットの変更がある場合、自動コミットを実行する
2. `pipeline_history` から `last_commit_id` を取得する
3. `last_commit_id` が null commit hash（初回）の場合は全ファイルを対象とする。通常のコミット ID の場合は `git diff` で変更ファイルを取得する
4. 変更ファイルがなければ終了する
5. 変更ファイルを種別（追加・変更 / 削除）ごとに分類する。BlueSky の `bluesky/.../media/{rkey}/` 配下のファイルが追加・変更された場合、対応する親ファイル（`{rkey}.json`）が diff に含まれていなくても `modified` として処理対象に追加する
6. 追加・変更ファイルはコンバーターで変換後、インデクサーでインデックスに追加・更新する
7. 削除ファイルはインデクサーでインデックスから削除し、metadata.db で論理削除する
8. `pipeline_history` に実行履歴を追加する

```mermaid
flowchart TD
    START["パイプライン開始"]
    CHECK_DIRTY{"未コミットの変更あり?"}
    AUTO_COMMIT["自動コミット実行"]
    GET_STATE["pipeline_history から last_commit_id 取得"]
    CHECK["last_commit_id が null commit hash か"]
    DIFF["git diff last_commit_id..HEAD"]
    FULL["全ファイルを対象とする"]
    HAS_CHANGES["変更ファイルあり"]
    NO_CHANGES["変更なし → 終了"]
    CLASSIFY["変更ファイルを分類"]

    subgraph PROCESS["変更種別ごとの処理"]
        ADDED["追加・変更ファイル"]
        DELETED["削除ファイル"]
        ADDED --> CONVERT["コンバーター: テキスト変換"]
        CONVERT --> INDEX_ADD["インデクサー: インデックス追加・更新"]
        DELETED --> INDEX_DEL["インデクサー: インデックス削除"]
        DELETED --> META_DEL["metadata.db: 論理削除"]
    end

    UPDATE_STATE["pipeline_history に実行履歴を追加"]
    END_NODE["パイプライン完了"]

    START --> CHECK_DIRTY
    CHECK_DIRTY -- Yes --> AUTO_COMMIT
    AUTO_COMMIT --> GET_STATE
    CHECK_DIRTY -- No --> GET_STATE
    GET_STATE --> CHECK
    CHECK -->|通常のコミット ID| DIFF
    CHECK -->|null commit hash（初回）| FULL
    DIFF --> HAS_CHANGES
    FULL --> HAS_CHANGES
    HAS_CHANGES -->|あり| CLASSIFY
    HAS_CHANGES -->|なし| NO_CHANGES
    CLASSIFY --> PROCESS
    PROCESS --> UPDATE_STATE
    UPDATE_STATE --> END_NODE
```

### パイプラインフロー（全再構築）

全再構築は「全 convert → 全 index」の2フェーズで処理する。convert フェーズで失敗したファイルは index フェーズから除外される。

1. 未コミットの変更がある場合、エラーとして rebuild を拒否する
2. source_store スキャンで metadata.db を再構築する
3. source_store の全ファイルをスキャンし、status が active のファイルを抽出する
4. **Phase 1 (Convert)**: converted_store をクリアし、全ファイルをテキスト変換する
5. **Phase 2 (Index)**: ChromaDB + BM25 をクリアし、convert 成功分のみインデックス構築する
6. 両フェーズともエラーなしの場合、`pipeline_history` に実行履歴を追加する

```mermaid
flowchart TD
    START["全再構築開始"]
    CHECK_DIRTY{"未コミットの変更あり?"}
    ERROR_DIRTY["エラー: rebuild 拒否"]
    REBUILD_DB["metadata.db 再構築（source_store スキャン）"]
    SCAN["全 active ファイルを抽出"]

    subgraph PHASE1["Phase 1: Convert"]
        CLEAR_CONV["converted_store をクリア"]
        CONVERT["全ファイルをテキスト変換"]
    end

    EXCLUDE["convert 失敗分を除外"]

    subgraph PHASE2["Phase 2: Index"]
        CLEAR_IDX["ChromaDB + BM25 をクリア"]
        INDEX["convert 成功分をインデックス構築"]
    end

    UPDATE["pipeline_history に実行履歴を追加"]
    END_NODE["全再構築完了"]

    START --> CHECK_DIRTY
    CHECK_DIRTY -- Yes --> ERROR_DIRTY
    CHECK_DIRTY -- No --> REBUILD_DB
    REBUILD_DB --> SCAN
    SCAN --> PHASE1
    PHASE1 --> EXCLUDE
    EXCLUDE --> PHASE2
    PHASE2 --> UPDATE
    UPDATE --> END_NODE
```

### インジェスター実行後のフロー

インジェスター（MCP ツール・CLI 経由）の実行後、パイプライン制御がコミットとパイプライン処理を行う。

```mermaid
sequenceDiagram
    participant USER as MCP/CLI
    participant ING as インジェスター
    participant PC as パイプライン制御
    participant SS as source_store
    participant CONV as コンバーター
    participant IDX as インデクサー

    USER->>ING: データ取り込み指示
    ING->>SS: ファイル配置 + .meta 生成
    ING->>PC: 取り込み完了通知
    PC->>SS: git add -A + git commit
    PC->>PC: git diff で変更ファイル特定
    PC->>CONV: 変更ファイルを変換
    CONV->>PC: 変換完了
    PC->>IDX: 変換済みファイルをインデックス追加
    IDX->>PC: インデックス完了
    PC->>SS: pipeline_history に実行履歴を追加
    PC->>USER: 処理結果を返却
```

### コンバーターのパススルー

変換不要なファイル（md/txt/adoc）はコンバーターが source_store から converted_store にそのままコピーする。これによりインデクサーは常に converted_store のみを参照すればよく、フォールバックロジックは不要。

### パイプライン処理対象外ファイル

source_store 内の以下のファイルは、git diff で検出されてもパイプライン処理をスキップする。

| ファイル | 理由 |
|---------|------|
| `.gitignore` | git メタデータ |
| `.ingest.lock` | インジェスト排他制御用ロックファイル |
| `.rebuild.lock` | 再構築排他制御用ロックファイル |
| `aozora/catalog.csv` | 青空文庫カタログ（検索用、インデックス対象外） |
| `aozora/catalog.csv.meta` | カタログのメタデータ |

### 変更種別と処理の対応

| git diff status | 変更種別 | コンバーター | インデクサー | metadata.db |
|-----------------|---------|-------------|-------------|-------------|
| `A`（追加） | 新規ファイル | 変換実行 | インデックス追加 | ソース登録 |
| `M`（変更） | 内容更新 | 再変換 | インデックス更新 | ソース更新（日時・ハッシュ） |
| `D`（削除） | ファイル削除 | converted_store から削除 | インデックス削除 | 論理削除 |
| `R`（リネーム） | パス変更 | 新パスで変換 | 旧パス削除 + 新パス追加 | `file_path` を更新。local 媒体は `source_id` も変わるため metadata.db の旧レコードを DELETE + 新規 INSERT する（ファイルの物理削除ではない） |
| `.meta` のみ変更 | メタデータ更新 | 再処理不要 | メタデータ upsert のみ | メタデータ更新 |

### コミットメッセージ規則

パイプライン制御が自動生成するコミットメッセージの形式。

| トリガー | メッセージ形式 |
|---------|-------------|
| インジェスター実行後 | `ingest({source_type}): {概要}` |
| 手動ファイル配置後（local） | `ingest(local): manual update` |
| 差分更新時の自動コミット | `auto-commit: incremental` |

`source_type` の取りうる値は [`_schema/enums.yml`](../../_schema/enums.yml) の `source_type` を参照。

## エッジケース

| ケース | 振る舞い |
|--------|---------|
| `last_commit_id` が null commit hash（初回実行） | source_store 全ファイルを対象として処理する |
| `last_commit_id` が指す commit が git 履歴に存在しない | 警告ログを出力し、全ファイルを対象として処理する |
| git diff の結果が空（変更なし） | 何もせず正常終了する |
| パイプライン処理中にエラーが発生した場合 | エラーが発生したファイルをスキップし、残りのファイルの処理を続行する。pipeline_history にレコードを追加しないことで `last_commit_id` が前回値のまま保持される（次回再実行で再処理される） |
| コンバーターが変換スキップを返したファイル（空テキスト、0バイト、未対応拡張子等） | 該当ファイルをスキップし、処理結果サマリの `warnings` に記録する（`errors` ではない）。非致命的なスキップであり、パイプライン全体の成否には影響しない |
| コンバーターが予期しない例外を発生させたファイル | 該当ファイルのインデックス追加をスキップし、処理結果サマリの `errors` に記録する |
| 大量のファイルが一度に変更された場合 | 全件を順次処理する。バッチサイズ制限は設けない |
| source_store の git リポジトリが未初期化 | パイプライン初回実行時に `git init` を自動実行する |
| .meta ファイルのみが変更された場合 | 拡張子 `.meta` で .meta ファイルを判定する。metadata.db のメタデータを更新する。コンバーターの再処理は行わない（データ本体に変更がないため）。インデックス側にメタデータ（title 等）を保持している場合は、インデクサーにメタデータ更新を指示する（チャンクの再生成は不要、メタデータのみ upsert） |
| metadata.db の `status` が `deleted` のファイルが git diff に含まれる場合 | ファイルの変更種別に応じた処理を行う。`deleted` ステータスのファイルはインデックスに追加しない |
| 再構築操作（全再構築・コンバートのみ・インデックスのみ）時に source_store に未コミットの変更がある場合 | エラーとして再構築を拒否する |
| 差分更新時に source_store に未コミットの変更がある場合 | 自動コミットを実行してから差分更新を続行する |
| 差分更新の自動コミット時に git commit が失敗した場合 | エラーとして差分更新を拒否する（コミット失敗の原因をエラーメッセージに含める） |

### 設定項目

| 設定項目 | 層 | 設計意図 |
|---------|-----|---------|
| `CONVERTED_STORE_DIR` | 環境依存値 | converted_store のディレクトリパス。環境ごとにストレージ配置が異なる |

source_store のパスや metadata.db の参照は [source-store.md](source-store.md) の設定項目を使用する。

## 関連ドキュメント

- [source-store.md](source-store.md) — source_store 仕様
- [rag-knowledge.md](rag-knowledge.md) — RAG ナレッジ仕様（既存）

# 再構築・統計・バックアップ

## 概要

3段パイプラインの運用支援機能を MCP ツールおよび CLI として提供する。

- **再構築**: パイプライン制御の4モードを MCP/CLI で公開し、データ破損時やパラメータ変更時の復旧・再生成手段を提供する
- **統計**: source_store・converted_store・インデックス・metadata.db の状態を統合的に可視化する（既存 `rag_stats` の拡張）
- **バックアップ**: source_store のバックアップ・リストア手順を定義する

スコープ:

- 再構築の4モード（全再構築 / コンバートのみ再実行 / インデックスのみ再構築 / 差分更新）の MCP/CLI インターフェース
- `source_type` フィルタによる対象媒体の絞り込み
- `rag_stats` の出力拡張（source_store / converted_store / metadata.db 統計の追加）
- バックアップ・リストア手順の定義

スコープ外:

- パイプラインの内部処理ロジック（パイプライン制御の範疇）
- 各ステージの変換・インデックス構築ロジック（コンバーター・インデクサーの範疇）
- source_store のディレクトリ構成・.meta 形式（source_store 仕様の範疇）

## 背景

- 新アーキテクチャでは converted_store とインデックスは source_store から再生成可能な派生データとなる。データ破損やパラメータ変更時に再構築する手段を MCP/CLI で提供する必要がある
- 運用監視のために、source_store・converted_store・パイプライン実行状態を含めた統計情報を統合的に把握したい
- source_store は全データの根源（SSoT）であり、バックアップ方針を明確に定義する必要がある

## 制約

- **パイプライン制御への委譲**: 再構築の実行ロジックはパイプライン制御に委譲する。本コンポーネントは MCP/CLI インターフェースの提供とパラメータの検証のみを担当する
- **再構築中の排他制御**: 再構築処理は同時に1つのみ実行可能とする。実行中に別の再構築要求を受けた場合はエラーを返す
- **全再構築のコスト認知**: 全再構築およびインデックスのみ再構築は Embedding API を呼び出すため、データ量に比例したコストが発生する。MCP ツール・CLI の説明文にこの旨を明記し、利用者が意図せず大量の API 呼び出しを行わないようにする
- **MCP サーバーのクラッシュ耐性**: MCP 経由のパイプライン処理（再構築・取り込み後のインデックス構築・削除）は CLI を別プロセスとして実行する（MCP 薄層アダプターパターン）。C 拡張（BM25s 等）の SEGFAULT が発生しても MCP サーバープロセスは生存し、エラーメッセージをクライアントに返却する。CLI 直接実行は独立プロセスのため対策不要

本コンポーネント自体は外部 API 通信を行わないが、再構築実行時にパイプラインを通じて Embedding API 等の外部通信が発生する。外部 API の安全制約は各ステージの仕様に従うため、想定プロファイル・安全制約セクションは省略する。

## インターフェース

### MCP ツール

#### rag_rebuild

ナレッジベースの再構築を実行する。

| 項目 | 内容 |
|------|------|
| ツール名 | `rag_rebuild` |
| 説明 | パイプラインの再構築を指定モードで実行する |

パラメータ:

| パラメータ | 型 | 必須 | 内容 |
|-----------|-----|------|------|
| `mode` | str | はい | 再構築モード（下表参照） |
| `source_type` | str | いいえ | 対象媒体フィルタ: `web`、`bluesky`、`zenn`、`local`。未指定時は全媒体 |

再構築モード:

| モード値 | 対応するパイプライン操作 | `source_type` フィルタ | 用途 |
|---------|----------------------|----------------------|------|
| `full` | 全再構築 | 適用可 | データ破損時や大規模な設計変更時。未コミット変更があればエラー |
| `convert` | コンバートのみ再実行 | 適用可 | コンバーターの変換ロジック改修時。未コミット変更があればエラー |
| `index` | インデックスのみ再構築 | 適用可 | Embedding モデル変更時やチャンクパラメータ変更時。未コミット変更があればエラー |
| `incremental` | 差分更新 | 適用不可（git diff に従う） | 通常運用。未コミット変更は自動コミットされる |

- `source_type` フィルタが適用不可のモード（`incremental`）で `source_type` が指定された場合、エラーを返す
- 戻り値: 処理結果サマリ（処理件数、スキップ件数、エラー件数、所要時間）をテキストで返す

#### rag_stats（拡張）

既存の `rag_stats` ツールを拡張し、source_store・converted_store・metadata.db の統計情報を追加する。

- パラメータの追加なし（既存インターフェースと後方互換）
- `SOURCE_STORE_DIR` / `CONVERTED_STORE_DIR` の検証はツール実行時に行う（サーバー起動時のバリデーション対象外）。未設定の場合は該当セクションを「未設定」と表示し、エラーにはしない

### CLI

#### rebuild コマンド

```
uv run python -m rag.cli rebuild --mode <MODE> [--source-type <TYPE>]
```

| オプション | 型 | 必須 | 内容 |
|-----------|-----|------|------|
| `--mode` | str | はい | 再構築モード: `full`、`convert`、`index`、`incremental` |
| `--source-type` | str | いいえ | 対象媒体フィルタ: `web`、`bluesky`、`zenn`、`local` |

MCP ツール `rag_rebuild` と同じバリデーション・振る舞いを適用する。

## コンポーネント構成

### 再構築フロー

```mermaid
flowchart TD
    ENTRY_MCP["MCP rag_rebuild"]
    ENTRY_CLI["CLI rebuild"]
    VALIDATE["パラメータ検証"]
    LOCK["排他ロック取得"]
    LOCK_FAIL["エラー: 別の再構築が実行中"]
    CLI_SUB["CLI サブプロセス"]
    DELEGATE["パイプライン制御に委譲"]
    CRASH["クラッシュ検出（exit code）"]
    RESULT["処理結果サマリを返却"]

    ENTRY_MCP --> VALIDATE
    ENTRY_CLI --> VALIDATE
    VALIDATE -->|不正| ERROR["エラー返却"]
    VALIDATE -->|正常| LOCK
    LOCK -->|取得失敗| LOCK_FAIL
    LOCK -->|取得成功 MCP| CLI_SUB
    LOCK -->|取得成功 CLI| DELEGATE
    CLI_SUB --> DELEGATE
    DELEGATE -->|正常終了| RESULT
    DELEGATE -->|処理エラー| ERROR_PROC["エラー返却（処理エラー）"]
    CLI_SUB -->|クラッシュ| CRASH
    CRASH --> ERROR_CRASH["エラー返却（サーバー生存）"]
```

MCP 経由の場合、パイプライン処理（再構築・取り込み後のインデックス構築・削除）は CLI を別プロセスとして実行する（MCP 薄層アダプターパターン、詳細は [rag-knowledge.md](rag-knowledge.md) を参照）。C 拡張の SEGFAULT が発生してもサーバープロセスは生存し、exit code からエラーメッセージを返却する。CLI 直接実行は独立プロセスのためサブプロセス化は不要。

### CLI サブプロセス進捗通知

MCP ツール経由のパイプライン処理中、CLI サブプロセスの進捗をリアルタイムで MCP クライアントに通知する。

#### 通知方式

2 系統の通知を並行して送信する:

| 方式 | MCP メッセージ | 用途 | クライアント要件 |
|------|--------------|------|----------------|
| Logging notification | `notifications/message` | テキストベースの進捗メッセージ | なし（標準 MCP） |
| Progress notification | `notifications/progress` | 数値進捗（processed / total） | `progressToken` の送信が必要。未送信時は no-op |

FastMCP の `Context` オブジェクト経由で送信する。ツール関数に `ctx: Context` パラメータを追加する。

#### CLI stdout プロトコル

CLI は `--output json` 指定時に JSON Lines 形式で stdout に出力する。各行は `type` フィールドで識別する。

| `type` 値 | 出力タイミング | フィールド |
|-----------|-------------|-----------|
| `progress` | ファイル処理完了ごと | `processed`（int）、`total`（int）、`current`（str: 処理済みファイルパス） |
| `result` | 処理完了時（最終行） | コマンド固有のフィールド（`mode`, `total_files`, `processed`, `skipped`, `errors`, `elapsed` 等） |
| `error` | エラー時（最終行） | `error`（bool, 常に `true`）、`message`（str） |

MCP サーバー（server.py）は stdout を行単位で読み取り、`progress` 行を MCP 通知に変換し、`result` / `error` 行で処理結果を確定する。

#### サーバー側の処理

`_run_cli_subprocess` で CLI コマンドを実行し、stdout を行単位でストリーミングする:

1. `asyncio.create_subprocess_exec` で CLI サブプロセスを起動
2. stdout を行単位で非同期に読み取る（`readline()` ループ）
3. 各行を JSON パースし:
   - `type: "progress"` → `ctx.info()` でログ通知 + `ctx.report_progress()` で数値通知
   - `type: "result"` → 結果として返却
   - `type: "error"` → エラーとして処理
4. stderr はプロセス終了後に読み取る

#### PipelineController の変更

処理ループを持つメソッドに `progress_callback` パラメータを追加する:

| メソッド | 対象ループ |
|---------|-----------|
| `run_full_rebuild` | 全レコードのコンバート + インデックス |
| `run_convert_only` | 全レコードのコンバート |
| `run_index_only` | 全レコードのインデックス |
| `_process_changes` | 差分変更エントリの処理 |

callback シグネチャ: `(processed: int, total: int, current: str) -> None`

`ingest_and_index` および `run_incremental` も `progress_callback` パラメータを受け取り、内部的に `_process_changes` に中継する。

CLI は `--output json` 指定時にこの callback 内で進捗 JSON を stdout に出力する。callback 未指定時は従来通り無出力。

| ファイル | 役割 |
|-------------|------|
| `src/rag/cli.py` | CLI コマンド。`--output json` 指定時に JSON Lines 形式で進捗・結果を出力。MCP 薄層アダプターからサブプロセスとして呼び出される際のエントリポイント |
| `src/rag/server.py` | MCP ツール（薄層アダプター）。パラメータ検証を行い CLI をサブプロセスで起動。stdout を行単位でストリーミングし、進捗を MCP 通知として転送 |
| `src/rag/pipeline/worker.py` | パイプライン処理の薄いエントリポイント。CLI から呼び出される内部モジュール |

### rag_stats 出力項目

`rag_stats` は以下の4セクションで統計情報を出力する。

#### source_store 統計

| 項目 | 内容 |
|------|------|
| 総ファイル数 | source_store 内のデータファイル数（`.meta` ファイルを除く） |
| 総サイズ | 全データファイルの合計サイズ |
| 媒体別ファイル数 | `source_type` ごとのファイル数 |
| 媒体別サイズ | `source_type` ごとの合計サイズ |

#### converted_store 統計

| 項目 | 内容 |
|------|------|
| 総ファイル数 | converted_store 内のファイル数 |
| 総サイズ | 全ファイルの合計サイズ |

#### インデックス統計（既存データ項目を維持）

| 項目 | 内容 |
|------|------|
| 総チャンク数 | ChromaDB コレクション内のチャンク数 |
| ソース数 | ユニークなソース数（source_id の種類数） |
| ドメイン別ソース一覧 | ドメインごとのページ数・チャンク数（表示上限: `rag_stats_max_sources` 設定値） |

既存の `rag_stats` が提供するデータ項目を維持する。出力テキストの形式は新セクション（source_store / converted_store / metadata.db）の追加に伴い統合フォーマットに変更する。

#### metadata.db 統計

| 項目 | 内容 |
|------|------|
| パイプライン最終処理日時 | `pipeline_history` の最新行の `processed_at` |
| パイプライン実行回数 | `pipeline_history` の総行数 |
| `last_commit_id` | `pipeline_history` の最新行の `to_commit_id` |
| 論理削除件数 | `status` が `deleted` のレコード数 |

#### 出力フォーマット

```
📊 RAG Knowledge 統計

■ source_store
  総ファイル数: 123
  総サイズ: 45.6 MB
  媒体別:
    web: 80 files (30.2 MB)
    bluesky: 25 files (5.1 MB)
    zenn: 10 files (8.0 MB)
    local: 8 files (2.3 MB)

■ converted_store
  総ファイル数: 120
  総サイズ: 12.3 MB

■ インデックス
  総チャンク数: 1,234
  ソース数: 120
  ドメイン別:
    example.com: 5 pages (150 chunks)
    ...
  (以下省略、50件まで表示)

■ パイプライン
  最終処理: 2026-03-19T10:30:00+09:00
  実行回数: 42
  last_commit_id: abc1234
  論理削除: 3 件
```

- source_store / converted_store が未設定の場合、該当セクションは「未設定」と表示する
- metadata.db が未初期化の場合、パイプラインセクションは「未初期化」と表示する
- サイズ表示は人間が読みやすい単位（B / KB / MB / GB）に自動変換する

### バックアップ

#### バックアップ対象

| 対象 | バックアップ | 理由 |
|------|------------|------|
| source_store ディレクトリ | 必須 | 全データの根源（SSoT） |
| metadata.db | 必須（source_store 内に含まれる） | メタデータ索引。再構築可能だが、`pipeline_history` の保持のためにバックアップに含める |
| converted_store | 不要 | source_store から再生成可能 |
| ChromaDB | 不要 | converted_store から再構築可能 |
| BM25 インデックス | 不要 | converted_store から再構築可能 |

#### バックアップ手順

前提条件: パイプライン処理が実行中でないこと。バックアップ中に metadata.db への書き込みが発生すると整合性が崩れる可能性がある。

1. metadata.db の WAL をフラッシュする

   ```
   PRAGMA wal_checkpoint(TRUNCATE)
   ```

2. source_store ディレクトリを丸ごとコピーまたは圧縮保存する

WAL フラッシュを行わないと、WAL ファイルと DB ファイルの不整合によりバックアップが破損する可能性がある。

#### リストア手順

1. source_store ディレクトリをリストア先に配置する
2. パイプライン制御の「全再構築」を実行して converted_store とインデックスを再生成する

   ```
   uv run python -m rag.cli rebuild --mode full
   ```

metadata.db が破損・消失している場合は、パイプライン制御の DB 再構築機能が source_store のファイルと `.meta` から metadata.db を再構築する（[source-store.md](source-store.md) のエッジケース参照）。

### 設定項目

本コンポーネント固有の設定項目はない。source_store のパスは [source-store.md](source-store.md) の設定項目、converted_store のパスは [pipeline-controller.md](pipeline-controller.md) の設定項目を使用する。

## エッジケース

| ケース | 振る舞い |
|--------|---------|
| `SOURCE_STORE_DIR` が未設定の場合 | `rag_stats` は source_store セクションを「未設定」と表示する。`rag_rebuild` はエラーを返す |
| `CONVERTED_STORE_DIR` が未設定の場合 | `rag_stats` は converted_store セクションを「未設定」と表示する。`rag_rebuild` はエラーを返す |
| source_store ディレクトリが存在しない場合 | `rag_stats` は source_store の各項目を 0 で表示する。`rag_rebuild` はエラーを返す |
| 再構築中に別の再構築が要求された場合 | 後発の要求にエラーを返す（排他制御） |
| `incremental` モードで `source_type` が指定された場合 | パラメータ検証エラーを返す |
| metadata.db が存在しない場合の `rag_stats` | パイプラインセクションを「未初期化」と表示する。source_store のファイルシステムベースの統計は表示する |
| 再構築中にエラーが発生した場合 | パイプライン制御のエラーハンドリングに従う（エラーファイルをスキップし残りを処理続行。`pipeline_history` に履歴を追加しない） |

## 関連ドキュメント

- [pipeline-controller.md](pipeline-controller.md) — パイプライン制御仕様（再構築の実行ロジック）
- [source-store.md](source-store.md) — source_store 仕様（データ構成・metadata.db スキーマ）
- [rag-knowledge.md](rag-knowledge.md) — RAG ナレッジ仕様（既存の `rag_stats`・設定管理）

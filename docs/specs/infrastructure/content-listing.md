# コンテンツ一覧取得

## 概要

取り込み済みソースを一覧取得する機能を MCP ツールおよび CLI サブコマンドとして提供する。`rag_list_recent`（指定 `source_type` の `published_at` 順）と `rag_list_by_date_range`（`published_at` 日付範囲・`source_type` 任意で横断取得）の 2 ツールを提供する。

スコープ:

- ソース単位の一覧取得（MCP ツール + CLI サブコマンド）
- `source_type` 指定による絞り込み（`rag_list_recent` は必須、`rag_list_by_date_range` は任意）
- `published_at` の日付範囲指定による絞り込み（`rag_list_by_date_range`）
- メタデータフィルタ（`filters`）による絞り込み
- `published_at` によるソート（降順/昇順切り替え可能）
- 取得件数の制限

スコープ外:

- チャンク単位の検索（rag_search の範疇）
- 全文取得（rag_get_document の範疇）
- 統計情報の表示（rag_stats の範疇）

## 背景

- 既存の検索機能（`rag_search`）はクエリベースの検索であり、「最近取り込んだコンテンツを一覧する」用途には対応していない
- 直近の情報を参照しやすくすることで、ナレッジベースの運用・確認が容易になる
- `rag_stats` はドメイン別の集計情報を提供するが、個別ソースの時系列的な一覧は提供していない
- `collected_at`（取込時刻）はインジェスターの処理順に依存するため、ユーザーが期待する「コンテンツの公開日時順」にならないケースがある。source_type ごとの公開日時（`published_at`）でソートすることで、直感的な一覧を提供する

## 制約

- **ソース単位の返却**: チャンク単位ではなくソース単位で返却する。1ソース = 1エントリとして title、source_id、published_at、file_size のメタデータを返す
- **データソースの限定**: 全データを MetadataDB から取得する。VectorStore や BM25 インデックスへの追加クエリは発行しない
- **source_type 必須**: `source_type` パラメータは必須とする。全 source_type 横断の一覧取得は提供しない
- **ソート基準**: `published_at` でソートする。デフォルトは降順（新しい順）。`order` パラメータで昇順に切り替え可能
- **論理削除済みソースの除外**: `status` が `deleted` のソースは一覧に含めない
- **MCP + CLI 両提供**: MCP ツールと CLI サブコマンドの両方で提供する。内部ロジックは共通関数とする
- **レスポンス形式**: MCP ツールのレスポンスはプレーンテキスト形式とする（既存ツールと統一）

## published_at の決定ロジック

`published_at` は source_type ごとの `.meta` フィールドから公開日時を抽出し、MetadataDB の `published_at` 列に格納する。

| source_type | .meta フィールド | 変換 | フォールバック |
|---|---|---|---|
| bluesky | `created_at` | そのまま（ISO 8601） | `collected_at` |
| zenn | `published_at` | そのまま（ISO 8601） | `collected_at` |
| youtube | `upload_date` | YYYYMMDD → ISO 8601 | `collected_at` |
| aozora | — | — | `collected_at` |
| journal | — | — | `collected_at` |
| local | — | — | `collected_at` |
| web | — | — | `collected_at` |

解決ロジックは `rag.store.resolve.resolve_published_at()` に一元化されている。

## インターフェース

### 操作一覧

| 操作 | 種別 | 提供方式 | 概要 |
|------|------|---------|------|
| rag_list_recent | 既存 | MCP ツール + CLI | 指定 source_type のソースを公開日時順で一覧取得する |
| rag_list_by_date_range | 新規 | MCP ツール + CLI | 指定日付範囲のソースを横断取得する（`source_type` 任意） |

### MCP ツール

#### rag_list_recent

指定された `source_type` のソースを `published_at` 順で取得する。

| 項目 | 内容 |
|------|------|
| ツール名 | `rag_list_recent` |
| 説明 | 指定した source_type のソースを公開日時順で一覧取得する |

パラメータ:

| パラメータ | 型 | 必須 | 内容 |
|-----------|-----|------|------|
| `source_type` | str | はい | ソース種別（値は [`_schema/enums.yml`](../../../_schema/enums.yml) の `source_type` を参照） |
| `limit` | int | いいえ | 取得件数。デフォルト: `rag_list_recent_limit`（config.toml） |
| `order` | str | いいえ | ソート順。`"desc"`（新しい順、デフォルト）または `"asc"`（古い順） |
| `filters` | str | いいえ | メタデータフィルタ（`key=value` 形式、カンマ区切りで複数指定可）。`.meta` のカスタムフィールドで絞り込む。完全一致。例: `"repository=rag-knowledge"`。[search-response.md](../search-response.md) の `rag_search` の `filters` と同一形式 |

バリデーション:

- `source_type` が有効値でない場合、エラーメッセージを返す（有効値の一覧を含める）
- `limit` が許容範囲外の場合、エラーメッセージを返す。許容範囲は `src/rag/config.py` の pydantic Field 制約に従う
- `order` が `"asc"` / `"desc"` 以外の場合、エラーメッセージを返す
- `filters` のキー名が不正な場合、エラーメッセージを返す

出力:

プレーンテキスト形式。ヘッダー行にソース種別・件数・ソート順を表示し、各ソースのメタデータを出力する。

```
source_type: web（5件 / 全80件, 新しい順）

1. Sample Documentation Site
   Source: https://example.com/docs
   Published: 2025-06-15T10:30:00+09:00
   Size: 45.2 KB

2. Another Page Title
   Source: https://example.com/guide
   Published: 2025-06-14T08:00:00+09:00
   Size: 12.8 KB
```

- ヘッダー行: `source_type: {type}（{表示件数}件 / 全{該当source_typeの総件数}件, {ソート順}）`
  - 総件数: MetadataDB の `sources` テーブルに対して同一 `source_type` + `status = 'active'` + `filters` 条件の `COUNT(*)` で取得する。`filters` 指定時は絞り込み後の件数を反映する
- 各エントリ: 番号付きリスト。タイトル、source_id、published_at、ファイルサイズを表示
  - ファイルサイズ: MetadataDB の `file_size` カラムから取得する。人間が読みやすい単位（KB / MB）でフォーマットする
- 該当するソースが0件の場合: `source_type: {type}（0件 / 全0件）`（ソート順は省略する）
- Source の値は source_store 内の相対パス（全 source_type 共通）
- クエリコスト: 一覧取得 1 クエリ + 総件数 COUNT 1 クエリの最大 2 クエリで完結する。全データは MetadataDB から取得し、VectorStore への追加クエリは発行しない

#### rag_list_by_date_range

`published_at` の日付範囲で取り込み済みソースを横断取得する。`source_type` 未指定時は全種別を横断する。

| 項目 | 内容 |
|------|------|
| ツール名 | `rag_list_by_date_range` |
| 説明 | 指定日付範囲のソースを横断取得する（JST 解釈、両端 inclusive） |

パラメータ:

| パラメータ | 型 | 必須 | 内容 |
|-----------|-----|------|------|
| `date_from` | str | はい | 開始日（`YYYY-MM-DD` 形式、JST 起点で inclusive） |
| `date_to` | str | はい | 終了日（`YYYY-MM-DD` 形式、JST 起点で inclusive） |
| `source_type` | str | いいえ | ソース種別（値は [`_schema/enums.yml`](../../../_schema/enums.yml) の `source_type` を参照）。未指定時は全種別横断 |
| `limit` | int | いいえ | 取得件数。デフォルト: `rag_list_recent_limit`（config.toml の値を流用） |
| `order` | str | いいえ | ソート順。`"desc"`（新しい順、デフォルト）または `"asc"`（古い順） |
| `filters` | str | いいえ | メタデータフィルタ（`key=value` 形式、カンマ区切り）。`rag_list_recent` の `filters` と同一形式 |

バリデーション:

- `date_from` / `date_to` が `YYYY-MM-DD` 形式でない場合、エラーメッセージを返す
- `date_from` が `date_to` より後の日付の場合、エラーメッセージを返す
- `source_type` 指定時に有効値でない場合、エラーメッセージを返す
- `limit` が許容範囲外の場合、エラーメッセージを返す。許容範囲は `src/rag/config.py` の pydantic Field 制約に従う
- `order` が `"asc"` / `"desc"` 以外の場合、エラーメッセージを返す
- `filters` のキー名が不正な場合、エラーメッセージを返す

範囲解釈:

- `date_from` / `date_to` は JST(`+09:00`) として解釈する
- `published_at` カラムは初回 INSERT 時に **UTC マイクロ秒 6 桁固定の ISO 8601** に正規化される
  （`MetadataDB.register_source` / `update_source` の入口で `normalize_published_at()` を適用）。
  範囲境界も同書式に揃えて SQL の文字列比較で範囲フィルタする
  - 例: `date_from=2026-01-15` → `2026-01-14T15:00:00.000000+00:00`
  - 例: `date_to=2026-01-15` → `2026-01-15T14:59:59.999999+00:00`
- 表記揺れ（マイクロ秒の有無、`Z` vs `+09:00` 等の TZ オフセット表記差）は書き込み入口で吸収されるため、辞書順比較が時系列順比較と一致する
- 既存データは `uv run python -m rag.cli migrate` を 1 回実行することで一括正規化される（初回適用後は冪等）
- 範囲の両端は inclusive（`>=` / `<=`）

出力:

プレーンテキスト形式。ヘッダー行に日付範囲・件数・ソート順・`source_type` フィルタ条件を表示し、各エントリに `Type: {source_type}` を付与したフラットリストを出力する。

```
date_range: 2026-01-15〜2026-01-15（all, 3件 / 全3件, 新しい順）

1. セッション記録
   Type: journal
   Source: journal/example-repo/20260115-101500-list-by-date-range.md
   Published: 2026-01-15T01:15:00.000000+00:00
   Size: 4.2 KB

2. 開発メモ投稿
   Type: bluesky
   Source: bluesky/example.bsky.social/3k...
   Published: 2026-01-15T00:00:00.000000+00:00
   Size: 0.5 KB

3. RAG 解説記事
   Type: zenn
   Source: zenn/example-user/article-slug
   Published: 2026-01-14T22:30:00.000000+00:00
   Size: 12.3 KB
```

- ヘッダー行: `date_range: {date_from}〜{date_to}（{source_type or "all"}, {表示件数}件 / 全{該当総件数}件, {ソート順}）`
  - 総件数: 同じ範囲・`source_type`・`filters` 条件の `COUNT(*)` で取得する
- 各エントリ: 番号付きリスト。タイトル、`Type: {source_type}`、source_id、published_at、ファイルサイズを表示
- 該当ソースが 0 件の場合: `date_range: {date_from}〜{date_to}（{source_type or "all"}, 0件 / 全0件）`（ソート順は省略）
- クエリコスト: 一覧取得 1 クエリ + 総件数 COUNT 1 クエリの最大 2 クエリで完結する

### CLI サブコマンド

#### list-recent コマンド

```
uv run python -m rag.cli list-recent --source-type <TYPE> [--limit <N>] [--order asc|desc] [--filters <FILTERS>]
```

| オプション | 型 | 必須 | 内容 |
|-----------|-----|------|------|
| `--source-type` | str | はい | ソース種別（値は [`_schema/enums.yml`](../../../_schema/enums.yml) の `source_type` を参照） |
| `--limit` | int | いいえ | 取得件数。デフォルト: `rag_list_recent_limit`（config.toml） |
| `--order` | str | いいえ | ソート順。`asc`（古い順）/ `desc`（新しい順、デフォルト） |
| `--filters` | str | いいえ | メタデータフィルタ（`key=value` 形式、カンマ区切り）。MCP ツールの `filters` と同一形式 |

MCP ツール `rag_list_recent` と同じバリデーション・振る舞いを適用する。出力形式も同一。

#### list-by-date-range コマンド

```
uv run python -m rag.cli list-by-date-range --date-from <YYYY-MM-DD> --date-to <YYYY-MM-DD> [--source-type <TYPE>] [--limit <N>] [--order asc|desc] [--filters <FILTERS>]
```

| オプション | 型 | 必須 | 内容 |
|-----------|-----|------|------|
| `--date-from` | str | はい | 開始日（`YYYY-MM-DD` 形式、JST 起点で inclusive） |
| `--date-to` | str | はい | 終了日（`YYYY-MM-DD` 形式、JST 起点で inclusive） |
| `--source-type` | str | いいえ | ソース種別。未指定で全種別横断 |
| `--limit` | int | いいえ | 取得件数。デフォルト: `rag_list_recent_limit`（config.toml） |
| `--order` | str | いいえ | ソート順。`asc`（古い順）/ `desc`（新しい順、デフォルト） |
| `--filters` | str | いいえ | メタデータフィルタ（`key=value` 形式、カンマ区切り）。MCP ツールと同一形式 |

MCP ツール `rag_list_by_date_range` と同じバリデーション・範囲解釈・出力形式を適用する。

### 設定項目

| 設定項目 | 層 | 設計意図 |
|---------|-----|---------|
| `rag_list_recent_limit` | 共通設定値 | `rag_list_recent` / `list-recent` のデフォルト取得件数。運用規模に応じて調整する |

## コンポーネント構成

```mermaid
flowchart TD
    MCP_RECENT["MCP rag_list_recent"]
    MCP_RANGE["MCP rag_list_by_date_range"]
    CLI_RECENT["CLI list-recent"]
    CLI_RANGE["CLI list-by-date-range"]
    VALIDATE["パラメータ検証"]
    SERVICE_RECENT["StatsPort.list_recent / list_recent_sources"]
    SERVICE_RANGE["StatsPort.list_by_date_range / list_sources_by_date_range"]
    METADB["MetadataDB"]
    FORMAT["テキストフォーマット"]
    RESPONSE["レスポンス返却"]

    MCP_RECENT --> VALIDATE
    CLI_RECENT --> VALIDATE
    MCP_RANGE --> VALIDATE
    CLI_RANGE --> VALIDATE
    VALIDATE -->|不正| ERROR["エラー返却"]
    VALIDATE -->|"正常 (rag_list_recent)"| SERVICE_RECENT
    VALIDATE -->|"正常 (rag_list_by_date_range)"| SERVICE_RANGE
    SERVICE_RECENT --> METADB
    SERVICE_RANGE --> METADB
    METADB -->|"フィルタ条件 + ソート + COUNT"| SERVICE_RECENT
    METADB -->|"範囲フィルタ + フィルタ条件 + ソート + COUNT"| SERVICE_RANGE
    SERVICE_RECENT --> FORMAT
    SERVICE_RANGE --> FORMAT
    FORMAT --> RESPONSE
```

### データ取得フロー

#### rag_list_recent

1. MCP ツールまたは CLI からパラメータを受け取る
2. `source_type`、`limit`、`order`、`filters` のバリデーションを実行する
3. `StatsPort.list_recent` Adapter メソッド（または `list_recent_sources` 共通関数）を呼び出す
4. `MetadataDB` から以下の 2 クエリを発行する:
   - 一覧取得: `source_type` + `status = 'active'` + `filters`（指定時）でフィルタし、`published_at` でソート（`order` に応じて昇順/降順）して `limit` 件取得
   - 総件数取得: 同条件の `COUNT(*)` で該当 source_type の全件数を取得（`filters` 指定時は絞り込み後の件数）
5. 結果をテキスト形式にフォーマットして返却する（file_size は人間が読みやすい単位に変換）

#### rag_list_by_date_range

1. MCP ツールまたは CLI からパラメータを受け取る
2. `date_from` / `date_to` を `YYYY-MM-DD` 形式で検証し、`date_from <= date_to` を確認する
3. `source_type`（指定時）、`limit`、`order`、`filters` のバリデーションを実行する
4. 「範囲解釈」節に従い、`date_from` / `date_to` を UTC マイクロ秒 6 桁固定 ISO 8601 の境界文字列に変換する
5. `StatsPort.list_by_date_range` Adapter メソッド（または `list_sources_by_date_range` 共通関数）を呼び出す
6. `MetadataDB` から以下の 2 クエリを発行する:
   - 一覧取得: `status = 'active'` + `published_at >= ?` + `published_at <= ?` + `source_type`（指定時） + `filters`（指定時）でフィルタし、`published_at` でソートして `limit` 件取得
   - 総件数取得: 同条件の `COUNT(*)`
7. 結果をテキスト形式にフォーマットして返却する（各エントリに `Type: {source_type}` を含める）

### 関連ファイル

| ファイル | 役割 |
|---------|------|
| `src/rag/server/tools/listing.py` | MCP ツール定義（`rag_list_recent`, `rag_list_by_date_range`, `rag_stats`）。パラメータ検証と共通関数呼び出し |
| `src/rag/cli.py` | CLI サブコマンド定義（`list-recent`, `list-by-date-range` 等）。パラメータ検証と共通関数呼び出し |
| `src/rag/admin/stats_port.py` | `StatsPort` Protocol + `RealStatsAdapter` + `list_recent_sources` / `list_sources_by_date_range` 関数。日付検証・JST 境界生成・MetadataDB へのクエリ |
| `src/rag/admin/formatting.py` | `format_file_size` 等のフォーマット処理 |
| `src/rag/filter_parser.py` | `parse_filters()` — `key=value` 文字列のパース（`rag_search` と共通） |
| `src/rag/store/metadata_db.py` | MetadataDB。`list_sources` / `list_sources_by_date_range` 等のクエリ |
| `src/rag/store/resolve.py` | `resolve_published_at()` — source_type ごとの公開日時解決 |
| `src/rag/config.py` | `rag_list_recent_limit` 設定の定義（`rag_list_by_date_range` でも流用） |
| `config.toml` | `rag_list_recent_limit` のデフォルト値 |

## エッジケース

### rag_list_recent

| ケース | 振る舞い |
|--------|---------|
| 指定 source_type のソースが0件 | `source_type: {type}（0件 / 全0件）` を返す |
| limit が該当ソース件数より大きい | 全件返却する（エラーにはしない） |
| 無効な source_type を指定 | エラーメッセージを返す（有効値の一覧を含める） |
| limit が許容範囲外 | エラーメッセージを返す |
| 無効な order を指定 | エラーメッセージを返す |
| 論理削除済みソースが存在 | 一覧に含めない（`status = 'active'` のみ対象） |
| `published_at` が同一の複数ソース | ソート順序は不定（同一タイムスタンプ内の順序は保証しない） |
| `published_at` が空文字列（マイグレーション前のデータ） | マイグレーション時に `collected_at` で自動補完される |
| `filters` 指定で該当ソースが0件 | `source_type: {type}（0件 / 全0件）` を返す |
| `filters` のキー名が不正 | エラーメッセージを返す |
| `filters` 未指定 | 既存の動作（全件返却）を維持する |

### rag_list_by_date_range

| ケース | 振る舞い |
|--------|---------|
| 範囲内のソースが 0 件 | `date_range: {date_from}〜{date_to} ({source_type or "all"}, 0件 / 全0件)` を返す |
| `date_from` / `date_to` が `YYYY-MM-DD` 形式でない | エラーメッセージを返す |
| `date_from > date_to` | エラーメッセージを返す |
| `source_type` 未指定 | 全 source_type を横断して取得する |
| `source_type` 指定 | その source_type のみ取得する |
| `published_at` が UTC（`Z` 終端）のソース | 書き込み入口で UTC マイクロ秒 6 桁固定 ISO 8601 に正規化済みのため、範囲境界との文字列比較が時系列順と一致する |
| `published_at` が空文字列のソース | 書き込み入口で `collected_at` を fallback として適用するため通常は発生しない。万一空文字列が残った場合は範囲条件で除外される |
| `published_at` が同一の複数ソース | ソート順序は不定 |
| 同一 `source_id` の再取り込み | `register_source` の ON CONFLICT で `published_at` は **更新されない**（初回 INSERT 時の値が保持される）。再取り込みで `published_at` を更新したい場合は `update_source` で明示的に渡すか、`uv run python -m rag.cli migrate` を再実行する |
| 論理削除済みソース | 一覧に含めない（`status = 'active'` のみ対象） |
| `filters` のキー名が不正 | エラーメッセージを返す |

## 関連ドキュメント

- [検索レスポンス + 全文取得](../search-response.md) — チャンク単位の検索・全文取得
- [再構築・統計・バックアップ](../rebuild-stats.md) — rag_stats による統計情報
- [source_store](../source-store.md) — ソースデータの格納・メタデータ管理
- [インジェスター共通仕様](../ingesters/common.md) — source_type の定義

# Fake モード基盤

## 概要

外部 API・外部ライブラリを必要とするインジェスター（YouTube・BlueSky 等）および Embedding 層（LM Studio / OpenAI）に対し、外部アクセスを行わない代替実装（**Fake Adapter**）を注入できる仕組みを定義する。pytest テスト・QA・運用環境のいずれでも同一の Fake Adapter を使い回し、実外部アクセスをデフォルト無効化する。

Fake Adapter の対象は 2 系統に分類される:

- **インジェスター系 Fake Fetcher**: `RAG_{SOURCE_TYPE}_FAKE_MODE` で切替（例: `RAG_YOUTUBE_FAKE_MODE`）
- **Embedding 系 Fake Embedding**: `RAG_EMBEDDING_FAKE_MODE` で切替（LM Studio / OpenAI 共通）

スコープ:

- インジェスター単位の Fetcher 抽象（Port: Protocol）の定義規約
- Real Fetcher / Fake Fetcher の責務分離規約
- `.env` ベースの fake モード切替機構（pydantic Settings + DI ファクトリ）
- 起動時の状態可視化（ログ・MCP 応答ラベル）
- pytest における Fake Adapter の利用規約（autouse 安全網込み）
- QA スキル運用での fake / 実アクセス選択フロー

スコープ外:

- 個別 source_type の Fake Adapter の実装詳細（[fake-adapters/](fake-adapters/) 配下の個別仕様書で扱う）
- ChromaDB / SQLite 等の永続化層の fake 化
- Vision モデルの fake 化（メディア解析テスト等は別途扱う）
- 個別の L2 Mock E2E テストの実装詳細（subprocess 越境注入の構成・テスト分類・CI 統合方針は [QA 戦略](../workflows/qa-strategy.md) で定義する。本仕様の Fake Adapter を L2 Mock E2E で再利用する）

## 背景

- インジェスターの結合テスト・QA・運用環境での動作確認において、実外部 API へのアクセスは IP ブロック・API 仕様変更・コスト・レート制限等のリスクを伴う
- 過去の取り組み（Issue #683 前回失敗、2026-04-26）では「pytest プロセス内の mock 基盤」のみを作成し、MCP サーバー稼働時 / CLI 実行時には mock が一切効かず、QA 中に実 YouTube アクセスが発生した
- 既存テストはインジェスターの private メソッド（`_fetch_metadata` 等）を `unittest.mock.patch.object` で個別に差し替える方式を採用しているが、以下の課題がある:
  - private メソッド名・シグネチャ変更でテストが破壊されやすい
  - 「実外部アクセスをデフォルトで禁止する」 autouse 安全網が成立しない
  - Test Double が pytest 内部にしか存在せず、production 起動時の QA で再利用できない
  - MCP server / CLI subprocess 越境環境（[Issue #692](https://github.com/becky3/rag-knowledge/issues/692)）では別オブジェクトとなり patch.object が効かない
- インジェスターから外部アクセス処理を Fetcher 抽象（Port）として切り出し、Fake Fetcher を SSoT とすることで、上記課題をすべて解消する

## 制約

### Fetcher 抽象（Port）の定義

各インジェスターは外部アクセス処理を **Fetcher Protocol** として抽象化する。インジェスター本体は Fetcher Protocol 経由でのみ外部データを取得する。

- Fetcher Protocol は `src/rag/pipeline/ingesters/{source_type}_fetcher.py` 等の専用モジュールで定義する
- Real Fetcher（実外部アクセス実装）と Fake Fetcher（fixture 返却実装）の両方を提供する
- Real Fetcher と Fake Fetcher は同じ Protocol を実装し、戻り値の型・構造が完全に一致すること
- インジェスター本体は Protocol 型で Fetcher を受け取り、Real / Fake のいずれが注入されたかを意識しない

### Test Double の SSoT 統一

- **Test Double は Fake Fetcher 1 種類に統一する**: pytest テスト・production fake モード・QA すべてから同じ Fake Fetcher を使う
- pytest テストでも `patch.object(ingester, "_fetch_xxx")` 等の private メソッドパッチは新規追加禁止
- **既存テストの書き換え対象**: 既存の `tests/test_pipeline_ingesters_youtube.py` 等に存在する `patch.object(ingester, "_fetch_*")` 形式のパッチ箇所はすべて本 PR で Fake Fetcher 注入方式に書き換える（暫定的な共存は許容しない）
- pytest 単体テストで個別の振る舞い検証（例: 特定の例外を投げる）が必要な場合、Fake Fetcher にシナリオ切替の機構を持たせる（コンストラクタ引数 / ファクトリ関数等）

### `.env` ベースの切替機構

- 切替は `.env` 経由で pydantic Settings に読み込む形式を SSoT とする
- 環境変数名規約: `RAG_{SOURCE_TYPE}_FAKE_MODE`（例: `RAG_YOUTUBE_FAKE_MODE`）
- **デフォルト値は安全側（fake = true）に倒す**: `.env` に明示記述がない場合は fake モードで起動する。本番運用時のみ `RAG_{SOURCE_TYPE}_FAKE_MODE=false` を `.env` に明示する
- fake モード時の fixture ディレクトリは `RAG_{SOURCE_TYPE}_FAKE_FIXTURE_DIR` 環境変数で指定可能とする（デフォルトは個別 source_type 仕様書で定義）
- Settings の Field 定義が値の SSoT。仕様書には Why のみ記載する
- **既存運用環境への周知**: `.env.example` に本番運用想定値として `RAG_{SOURCE_TYPE}_FAKE_MODE=false` を記述する。既存の `.env` を持つユーザーは本変更後、`.env` への明示追記が必要となる（追記しない場合はデフォルトの fake モードで起動する）。リリース時の CHANGELOG・README で本変更点を周知する

### DI ファクトリ関数

- 各 source_type は `create_{source_type}_fetcher(settings: Settings) -> {SourceType}Fetcher` のファクトリ関数を提供する
- ファクトリ関数は Settings の `*_fake_mode` を見て Real / Fake を選択して返す
- インジェスター生成箇所（CLI / MCP / pytest）は必ずファクトリ関数経由で Fetcher を注入する
- **インジェスター本体は Settings に依存しない**: インジェスター本体は `Fetcher` Protocol のみを依存性として受け取り、Settings の参照や環境変数の直接読み取り（`os.getenv` 等）は行わない

### 起動時の状態可視化

CLI / MCP サーバー / pytest のいずれの起動経路でも、**起動時に 1 回のみ**現在のモードを以下の形で出力する:

- **fake モード**: WARNING レベルでログ出力する。メッセージ冒頭に `[FAKE MODE: <source>]` ラベルを付与し（`<source>` は `youtube` / `embedding` 等の source 識別子）、
  「fake モードで起動中、実外部アクセスは発生しません」「本番運用時は `RAG_{SOURCE_TYPE}_FAKE_MODE=false` を `.env` に設定してください」のガイドを含める
- **real モード**: INFO レベルでログ出力する。「real モードで起動中、実外部アクセスが発生します」と明示

複数 source（インジェスター系・Embedding 系）が存在する場合、各 source ごとに独立して状態を出力する。

**起動時 1 回保証**: Settings はプロセス内シングルトンとして実装し、ログ出力は Settings 初期化時に 1 回のみ発生させる。pytest 実行時も session 開始時の Settings 初期化で 1 回のみ出力される（pytest-xdist 並列実行時は各 worker で 1 回ずつ）。テストごとに繰り返し WARNING が出力されないようにする。

### MCP 応答ラベル

MCP ツールの応答（`rag_add_youtube` / `rag_crawl_youtube` 等の取り込み系ツール）は、fake モード時に応答テキスト冒頭に `[FAKE MODE: <source>]` ラベルを付与する。MCP クライアント側で目視確認可能とする。

- ラベル形式: `[FAKE MODE: <source>]`（`<source>` は `youtube` / `embedding` 等）
- 各 MCP ツールは利用する fake source を明示してラベル付与する。複数 source が同時に有効な場合は、各ラベルを改行で連結して並列出力する（例: `rag_add_youtube` で YouTube fake と Embedding fake が同時有効なら `[FAKE MODE: youtube]\n[FAKE MODE: embedding]\n本文...`）
- 実アクセス時（該当 source の fake モードが false）はそのラベルを省略する。すべての対象 source が real モードならラベル全体を出力しない（通常応答）

### autouse 安全網

`tests/conftest.py` に autouse fixture を配置し、**pytest プロセス内のみ**で対象 source_type の外部ライブラリ層を `_RaiseOnUse` センチネルに差し替える。

- **適用範囲**: pytest プロセス内のみ。CLI / MCP サーバー起動時は適用しない（CLI/MCP では `.env` 設定 + DI ファクトリ関数による Fetcher 選択が安全網となる）
- **差し替え対象の粒度**: クラス単位（例: `yt_dlp.YoutubeDL`、`youtube_transcript_api.YouTubeTranscriptApi`）。ライブラリのモジュール自体や例外クラスの import は阻害しない（テストコード内で `assert isinstance(exc, RequestBlocked)` 等の検証を可能にするため）
- 目的: テストで Fake Fetcher の注入を忘れた場合、即座に `RuntimeError` を発生させて検出する
- Fake Fetcher を注入したテストでは外部ライブラリは呼ばれないため、安全網はテスト動作に影響しない
- 環境変数 `RAG_TESTS_ALLOW_NETWORK=1` 設定時のみ安全網を解除する（手動の本番回帰検証等の特殊用途）
- 安全網の対象ライブラリは個別 source_type 仕様書で列挙する

### QA スキル運用

QA スキル（`/qa`）使用時は以下のフローで fake / 実アクセスを選択する:

1. QA 開始時、AskUserQuestion で「fake モード（推奨）」「実アクセスモード」を選択
2. fake モード選択時: `.env` の `RAG_{SOURCE_TYPE}_FAKE_MODE=true` を一時的に設定し、CLI/MCP を起動して動作確認
3. 実アクセスモード選択時: 二重確認（「実アクセスを発生させますが進めますか？対象ターゲット数: N 件」）の後、`.env` の `RAG_{SOURCE_TYPE}_FAKE_MODE=false` を一時的に設定して実行。実行完了後、取り込まれた実 URL 一覧を Issue / ジャーナルに記録する
4. デフォルトは fake モード。明示的に opt-in した場合のみ実アクセスを発生させる

### Synthetic ID 規約

Fake Fetcher が返すデータ内の識別子は実在の ID と衝突しないよう、source_type ごとに以下の prefix 規約に従う:

| source_type | 識別子種別 | 形式 | 例 |
|---|---|---|---|
| YouTube | video_id | 11 文字、`Test` プレフィックス | `TestVideo01` |
| YouTube | playlist_id | `PLtest` プレフィックス | `PLtest12345` |
| YouTube | channel_id | `UCtest` プレフィックス | `UCtest123456789012345` |
| BlueSky | DID | `did:plc:test` プレフィックス | `did:plc:test01234567` |
| BlueSky | handle | `*.bsky.social` の `test` プレフィックス | `test.bsky.social` |
| BlueSky | rkey | `testrkey` プレフィックス | `testrkey00000` |
| Zenn | slug | `test-` プレフィックス | `test-article-001` |
| Zenn | username | `testuser` プレフィックス | `testuser` |
| Aozora | book_id | `999900` 番台（実在しない範囲） | `999900` / `999901` |
| Aozora | person_id | `99999` 番台 | `99999` |
| Web (scrapy) | URL | `https://test.invalid/` プレフィックス（IANA 予約 TLD、RFC 6761） | `https://test.invalid/page1` |

新たな source_type を追加する際は、本テーブルに synthetic ID 規約を追記すること。

**テスト入力 URL の扱い**: Web (scrapy) では Fake Runner が **入力 URL を無視して fixture URL を返す** ため、テストの入力には IANA 予約・実在ドメイン（`example.com` / `example.org` / `example.net`）を使う。これらは DNS / SSRF 検証を通過し、Fake Runner では実 HTTP リクエストが発生しない。Fake が返却する **出力 URL** のみ `test.invalid` を使う。

### Fake データの配置

- Fake Fetcher と fake データは `src/rag/pipeline/ingesters/_fake/{source_type}/` 配下に集約する
- `data/` サブディレクトリに JSON 形式で配置する
- production パッケージに含まれることを許容する（運用環境での fake モード動作のため）
- pytest テストからは `from rag.pipeline.ingesters._fake.{source_type} import Fake{SourceType}Fetcher` で参照する
- `tests/fixtures/` 配下には fake データを配置しない（src 配下が SSoT）

### Fake Embedding（Embedding 層の Fake 実装）

Embedding 層は外部の LM Studio / OpenAI API への HTTP 通信を伴うため、インジェスター系 Fake Fetcher と独立した Fake 実装 `FakeEmbedding` を提供する。

- **配置**: `src/rag/embedding/_fake/__init__.py` に `FakeEmbedding(EmbeddingProvider)` を集約する
- **既存 ABC への準拠**: 既存の `src/rag/embedding/base.py` の `EmbeddingProvider` ABC を継承し、必須抽象メソッド（`embed` / `is_available`）を実装する。`embed_documents` / `embed_query` は ABC の default 実装（`embed` への委譲）を再利用する
- **決定論性**: `embed` の戻り値は入力テキストに対して決定論的に決まる。同じ入力テキストには同じベクトルを返し、L2 正規化済みの float リストとする。決定論性により、テスト・CI で「特定クエリが特定文書と類似する」アサーションが安定する
- **生成方式**: SHA-256(text) を seed として固定ベクトルを構成する。実装詳細は `src/rag/embedding/_fake/__init__.py` を SSoT とする（仕様書には方式の本質のみ記述）
- **次元数**: pydantic Field `rag_embedding_fake_dimensions` で指定する。デフォルトは Real Embedding モデル（`text-embedding-nomic-embed-text-v2-moe`）の次元と整合させる。テスト時は柔軟に変更可能
- **切替**: `RAG_EMBEDDING_FAKE_MODE`（pydantic Settings）で切替する。
  `get_embedding_provider(settings, provider_setting)` の冒頭で `settings.rag_embedding_fake_mode` を確認し、true なら `FakeEmbedding` を返す。
  `provider_setting`（`local` / `online`）の判定より優先する
- **production パッケージへの含有**: `_fake/` ディレクトリは production パッケージに含まれることを許容する（運用環境での fake モード動作のため）
- **synthetic ID 規約**: Embedding は ID を返さないため、本仕様の synthetic ID 規約（YouTube 等の prefix 規約）への追加は不要

#### Embedding Fake モードの設定項目

`src/rag/config.py` の Settings に以下の項目を追加する:

| 項目名 | 層 | 設計意図 |
|---|---|---|
| `rag_embedding_fake_mode` | 環境依存値 | Embedding fake モード切替。デフォルトは安全側（fake 有効）。本番運用時のみ false を `.env` で明示する |
| `rag_embedding_fake_dimensions` | 環境依存値 | Fake Embedding が生成するベクトルの次元数。Real モデルの次元数に合わせる |

具体値（デフォルト・許容範囲）は pydantic Field が SSoT。

#### pytest 安全網（Embedding 層）

`tests/conftest.py` に session スコープの autouse fixture を追加し、テスト中は `RAG_EMBEDDING_FAKE_MODE=true` を環境変数で強制する。

- **適用範囲**: pytest プロセス内および subprocess 越境テスト（e2e）。subprocess 起動時に環境変数が引き継がれることで、子プロセス内の `factory.get_embedding_provider` も Fake を選択する
- **解除条件**: `RAG_TESTS_ALLOW_NETWORK=1` 設定時のみ強制を解除する（手動の本番回帰検証等の特殊用途）
- **個別テストの上書き**: Real Embedding を要求する個別テストは `monkeypatch.setenv("RAG_EMBEDDING_FAKE_MODE", "false")` で上書きできる
- インジェスター系 autouse 安全網（YouTube ライブラリの `_RaiseOnUse` ブロック）とは独立して機能する

#### pytest 安全網（BlueSky 層）

`tests/conftest.py` の session スコープ autouse fixture（`_force_bluesky_fake_mode`）でテスト中は `RAG_BLUESKY_FAKE_MODE=true` を環境変数で強制する。

- **適用範囲**: pytest プロセス内および subprocess 越境テスト（e2e）。subprocess 起動時に環境変数が引き継がれることで、子プロセス内の `create_bluesky_fetcher` / `create_bluesky_media_downloader` も Fake を選択する
- **解除条件**: `RAG_TESTS_ALLOW_NETWORK=1` 設定時のみ強制を解除する
- **個別テストの上書き**: Real Adapter を要求する個別テストは factory に Real を直接渡すか、Settings インスタンスに `rag_bluesky_fake_mode=False` を渡す
- **httpx クラス全体の `_RaiseOnUse` 差し替えは採用しない**: bluesky 以外で `httpx` を使う既存コードを誤爆させるため。`.env` + DI ファクトリ経由で Fake を選択させる本機構（production fake モードと同じ経路）に揃える
- インジェスター系 autouse 安全網（YouTube ライブラリの `_RaiseOnUse` ブロック）とは独立して機能する

## インターフェース

### 環境変数

| 環境変数 | 値 | デフォルト | 振る舞い |
|---|---|---|---|
| `RAG_{SOURCE_TYPE}_FAKE_MODE` | `true` / `false` | `true` | true で Fake Fetcher を注入。false で Real Fetcher を注入 |
| `RAG_{SOURCE_TYPE}_FAKE_FIXTURE_DIR` | パス | source_type ごとに個別仕様書で定義 | Fake Fetcher が読み込む fixture ディレクトリ |
| `RAG_WEB_FAKE_MODE` | `true` / `false` | `true` | **上位スイッチ**。site-ingest（scrapy）の fake モード切替を司る。内部の `rag_scrapy_fake_mode` の既定値として派生する（個別 env が `.env` で明示されていない場合） |
| `RAG_TESTS_ALLOW_NETWORK` | `1` | 未設定 | autouse 安全網を解除する（pytest 専用、特殊用途） |

#### 上位スイッチの派生階層

`RAG_WEB_FAKE_MODE` は web 系の fake モードを統括する上位スイッチ。`.env` で公開される唯一の web 系フラグであり、内部 Settings の個別フィールドへ派生する:

| 上位 env | 内部 Settings フィールド | 派生先のサブシステム |
|---|---|---|
| `RAG_WEB_FAKE_MODE` | `rag_scrapy_fake_mode` | site-ingest（Scrapy subprocess） |

派生ロジックは `_EnvLoader` の `model_validator(mode="after")` に集約される。個別 env（例えば将来導入される `RAG_SCRAPY_FAKE_MODE`）が `.env` で明示されている場合はそちらが優先される構造を維持する（現段階では個別 env は未公開）。

### Fetcher Protocol（共通形式）

各 source_type は以下の形式で Fetcher Protocol を定義する。詳細は個別 source_type 仕様書で定義する。

- Protocol クラス名: `{SourceType}Fetcher`（例: `YoutubeFetcher`）
- 配置先: `src/rag/pipeline/ingesters/{source_type}_fetcher.py`
- メソッド: 各インジェスターが行う外部アクセス処理を抽象化したもの。シグネチャ・戻り値の型は個別仕様書で定義
- Real 実装クラス名: `Real{SourceType}Fetcher`
- Fake 実装クラス名: `Fake{SourceType}Fetcher`
- ファクトリ関数: `create_{source_type}_fetcher(settings: Settings) -> {SourceType}Fetcher`

### pydantic Settings の追加項目

各 source_type について、`src/rag/config.py` の Settings に以下の項目を追加する:

| 項目名 | 層 | 設計意図 |
|---|---|---|
| `{source_type}_fake_mode` | 環境依存値 | fake モード切替。デフォルトは安全側（fake 有効）に倒し、本番運用時のみ `.env` で false を明示する |
| `{source_type}_fake_fixture_dir` | 環境依存値 | Fake Fetcher が読み込む fixture ディレクトリ。デフォルトは個別 source_type 仕様書で定義 |

具体値（デフォルト・許容範囲）は pydantic Field が SSoT。本仕様書には Why のみ記述する。

## コンポーネント構成

```mermaid
flowchart TB
    subgraph Entry["エントリポイント"]
        CLI["CLI<br/>rag.cli"]
        MCP["MCP server<br/>rag.server"]
        TESTS["pytest"]
    end

    subgraph Factory["DI ファクトリ"]
        SETTINGS["pydantic Settings<br/>(.env)"]
        FACTORY["create_xxx_fetcher()"]
    end

    subgraph Ingester["インジェスター"]
        ING["XxxIngester<br/>(Protocol 経由でのみ外部アクセス)"]
    end

    subgraph Fetchers["Fetcher 実装"]
        REAL["RealXxxFetcher<br/>(yt-dlp / API クライアント等)"]
        FAKE["FakeXxxFetcher<br/>(JSON 返却 + シナリオ切替)"]
    end

    subgraph Data["Fake データ"]
        SRC["src/.../_fake/xxx/data/<br/>*.json"]
    end

    subgraph Safety["pytest 安全網"]
        AUTO["tests/conftest.py<br/>autouse fixture"]
        BLOCK["_RaiseOnUse<br/>(外部ライブラリブロック)"]
    end

    CLI -->|起動時| FACTORY
    MCP -->|起動時| FACTORY
    TESTS -->|テスト時| FACTORY
    SETTINGS -->|fake_mode 値| FACTORY
    FACTORY -->|fake_mode=true| FAKE
    FACTORY -->|fake_mode=false| REAL
    FAKE --> SRC
    ING -->|Protocol 経由| Fetchers
    AUTO -->|autouse| BLOCK
    BLOCK -.実外部アクセスをブロック.-> REAL
```

### コンポーネント一覧

| コンポーネント | 役割 |
|---|---|
| `src/rag/config.py` の Settings | `*_fake_mode` / `*_fake_fixture_dir` を pydantic Field で SSoT 管理 |
| `src/rag/pipeline/ingesters/{source_type}_fetcher.py` | Fetcher Protocol + Real 実装 + ファクトリ関数 |
| `src/rag/pipeline/ingesters/_fake/{source_type}/` | Fake Fetcher + fake データ JSON |
| `src/rag/pipeline/ingesters/{source_type}.py` | インジェスター本体（Fetcher Protocol を依存性注入で受け取る） |
| エントリポイント（CLI / MCP / pytest）| ファクトリ関数経由で Fetcher を生成しインジェスターに注入 |
| `tests/conftest.py` | autouse 安全網（外部ライブラリ層を `_RaiseOnUse` でブロック）+ 共通 fixture |

### Fake モード起動の流れ

1. ユーザーが `.env` に `RAG_YOUTUBE_FAKE_MODE=true`（または未設定）を記述する
2. CLI / MCP サーバー起動時、pydantic Settings が `.env` を読み込む
3. インジェスター生成箇所がファクトリ関数を呼び出す: `fetcher = create_youtube_fetcher(settings)`
4. ファクトリ関数が Settings の `youtube_fake_mode=True` を確認し、`FakeYoutubeFetcher(fixture_dir)` を返す
5. インジェスターは `fetcher` を Protocol 型で受け取り、外部アクセス時に Fake Fetcher を呼び出す
6. 起動直後にログ出力: `WARNING: [FAKE MODE: youtube] YouTube は FAKE モードで起動中（fixture: src/rag/pipeline/ingesters/_fake/youtube/data）`
7. MCP ツール応答時、応答テキスト冒頭に `[FAKE MODE: youtube]` ラベルを付与（Embedding fake も同時有効なら `[FAKE MODE: embedding]` も並列出力）

## 想定プロファイル

本仕様書は外部 API 通信を行わない（fake モード機構の定義）ため、想定プロファイルは該当しない。Real Fetcher 実装の想定プロファイルは個別 source_type のインジェスター仕様書を参照。

## 外部連携

本仕様書自体は外部連携を行わない（fake モード機構の定義）。Real Fetcher 実装が個別の外部 API と連携する。連携先・制約は個別 source_type 仕様書および対応するインジェスター仕様書で定義する。

## エッジケース

| ケース | 振る舞い |
|---|---|
| `.env` に `RAG_{SOURCE_TYPE}_FAKE_MODE` が未設定 | デフォルト値 `true`（fake モード）で起動。WARNING ログ出力 |
| `RAG_{SOURCE_TYPE}_FAKE_FIXTURE_DIR` で指定したパスが存在しない | 起動時に FileNotFoundError で fail-fast。エラーメッセージで設定を促す |
| Fake Fetcher の戻り値構造が Real と乖離 | 個別 source_type の契約検証テストで検出する |
| pytest テストで Fake Fetcher 注入を忘れた | autouse 安全網の `_RaiseOnUse` が `RuntimeError` を発生させる。エラーメッセージで Fake Fetcher の使用を促す |
| QA スキルで実アクセスを誤選択 | 二重確認フローで「実アクセス開始しますが本当に進めますか？」を表示。ユーザー確認後にのみ実行 |
| 複数 source_type を同時に扱うインジェスター（例: BlueSky の URL 自動取り込み）| 各 source_type ごとに `*_fake_mode` を独立して評価する。BlueSky は real、YouTube は fake のような混合モードも許容する |
| 運用環境での fake モード動作 | `_fake/` ディレクトリは production パッケージに含まれるため、運用環境でも fake モードで動作可能 |

## 関連ドキュメント

- [YouTube Fake Adapter](fake-adapters/youtube.md) — 最初の対象 source_type 個別仕様
- [BlueSky Fake Adapter](fake-adapters/bluesky.md) — 2 番目の対象 source_type 個別仕様（Issue #704、U2 で実装）
- [Scrapy Fake Adapter](fake-adapters/scrapy.md) — site-ingest 用 Fake Runner の個別仕様（Issue #705）
- [Zenn Fake Adapter](fake-adapters/zenn.md) — Zenn API 用 Fake Fetcher の個別仕様（Issue #705）
- [Aozora Fake Adapter](fake-adapters/aozora.md) — 青空文庫用 Fake Fetcher の個別仕様（Issue #705）
- [YouTube インジェスター](../ingesters/youtube.md) — Fetcher 抽象化対象のインジェスター仕様
- [BlueSky インジェスター](../ingesters/bluesky.md) — Fetcher 抽象化対象のインジェスター仕様（Issue #704）
- [Zenn インジェスター](../ingesters/zenn.md) — 将来の水平展開対象
- [RAG ナレッジ](../rag-knowledge.md) — Embedding 層の Real 実装（LM Studio / OpenAI）の SSoT
- [Issue #692（MCP 取り込み系ツールの E2E mock 基盤）](https://github.com/becky3/rag-knowledge/issues/692) — 本仕様の Fake Adapter を subprocess 越境環境で再利用する E2E 基盤
- [Issue #697（QA 頻度削減のための CI 自動 E2E 基盤整備）](https://github.com/becky3/rag-knowledge/issues/697) — Fake Embedding を本仕様に追加

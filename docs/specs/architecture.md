# rag-knowledge アーキテクチャ

rag-knowledge プロジェクトのアーキテクチャ説明・採用方針・構造判断 SSoT。
個別仕様書の上位にあたる構造判断はここに集約する。

用語の定義は agent-commons の `architecture-guide.md`（語彙集）を参照する。
本仕様書は語彙の上に乗る形で rag-knowledge 固有の採用方針・構造判断を記述する。

## 記述方針

本仕様書では rot 防止のため、以下の列挙ルールを適用する:

- **記述する**: クラス名・Protocol 名（構造判断の語彙）/ `source_type` 値（公開 API 契約・概念）
- **記述しない**: 配置パス（ディレクトリ・モジュールパス。実装側コードが SSoT）/ 件数のスナップショット（「7 Ingester」等。追加・rename で rot するため）

クラス名・Protocol 名から実装の物理位置を辿るには `grep -rn "<ClassName>" src/` を使う（コード側が SSoT）。

ただし以下は **例外として配置パスの記載を許容する**:

- **§1 アーキテクチャ概要**: 層と主要モジュールのオリエンテーション目的でモジュールパス（`pipeline/ingesters/<source>` 等のワイルドカード型）を例示する
- **§2 / §2.1 正解パターン**: 新規 Adapter 追加時の参照点として、youtube / bluesky の正解パターンを示すために配置ファイル名を残す（教育的価値を優先）

例外箇所では rot 受容と引き換えに読者の到達性を優先する。例外を増やす際は記述方針側に列挙すること。

## 1. アーキテクチャ概要

rag-knowledge は **Ports and Adapters**（hexagonal）スタイルを採用する。
パイプラインの入出力は Port（Protocol / ABC）で表現し、外部依存（HTTP / ChromaDB / LM Studio / yt-dlp 等）は Adapter として注入する。

主要な層（依存方向は上から下、Dependency Rule に従い逆方向の依存は禁止）:

| 層 | 責務 | 主なモジュール |
|---|---|---|
| Adapter（境界） | 外部システムとの入出力 | `pipeline/ingesters/<source>` の Fetcher / `vector_store.py` / `embedding/` / `scrapy/` |
| パイプライン制御 | 取り込み・変換・索引のオーケストレーション | `pipeline/controller.py` |
| インジェスター | 媒体ごとの取得・配置 | `pipeline/ingesters/*.py` |
| ストア | データ・メタデータの永続化 | `store/source_store.py` / `store/metadata_db.py` / `vector_store.py` |
| 共通契約・モデル | dto / Port / Enum / 設定 | `store/models.py` / `pipeline/models.py` / `pipeline/ingesters/_common.py` / `config.py` |

### Dependency Rule

- 上位層は下位層に依存してよい
- 下位層は上位層に依存しない
- Adapter（境界）は Port（共通契約）にのみ依存し、パイプライン制御層からは Adapter ではなく Port 経由で操作する

## 2. 正解パターン: youtube インジェスター

新規 / 改修するインジェスター・Adapter は **youtube インジェスターを正解パターン** として参照する。
youtube パターンは以下の構造を満たす:

| 要素 | 配置 | 役割 |
|---|---|---|
| インジェスター本体 | `pipeline/ingesters/youtube.py` | 取得結果を `IngestResult` に集約。Fetcher は Protocol 経由で注入される |
| Fetcher Protocol | `pipeline/ingesters/youtube_fetcher.py` の `YoutubeFetcher` Protocol | 外部依存（youtube-transcript-api / yt-dlp）を抽象化する Port |
| Real Fetcher | `pipeline/ingesters/youtube_fetcher.py` の `RealYoutubeFetcher` | 本番用 Adapter（実外部アクセス） |
| Fake Fetcher | `_fake/youtube/` の `FakeYoutubeFetcher` | テスト・Fake モード用 Adapter（実外部アクセスなし、JSON fixture から応答を返却） |
| Fake モード切替 | `RAG_YOUTUBE_FAKE_MODE` 環境変数 + production fake モード基盤 | テスト・QA・運用環境で Adapter 注入を切り替え可能 |

このパターンの本質は **Port を介した Adapter 注入**であり、Real / Fake の切替を構造的に保証することで、L2 Mock E2E テスト（[QA 戦略](workflows/qa-strategy.md)）を成立させる。

### 2.1 適用例: bluesky インジェスター（package 化 + 複数 Port 注入）

新規インジェスターは引き続き **youtube パターンを基本形** として参照する。
bluesky インジェスターは youtube パターンを踏襲しつつ、規模・接続種別の事情に
応じて構成を拡張した適用例である。bluesky のような拡張は、本セクションの
「適用判断」列に該当する場合に検討する。

| 観点 | bluesky の選択 | 適用判断 |
|---|---|---|
| facade の package 化 | `pipeline/ingesters/bluesky/` パッケージ（_facade / feed_fetcher / post_placer / url_routing / delegations 等）に分解 | 単一インジェスターが複数の責務（API / 配置 / URL 分類 / 委譲）を持ち、セクション 4 の LOC 参考値を超える場合 |
| Port の複数分離 | AT Protocol API 用 (`BlueskyFetcher`) と CDN メディア DL 用 (`BlueskyMediaDownloader`) を独立 Port として分離 | 1 インジェスターが性質の異なる複数の外部接続を持つ場合、責務単位の Port として分離する |
| 委譲先の Port 化 | youtube / web への越境直 import を `YoutubeClassifier` / `YoutubeDelegator` / `WebDelegator` Port 経由に置換 | 他インジェスターへの委譲が必要な場合（セクション 4 の越境直 import 系統数の参考値も参照） |
| factory の独立化 | `create_bluesky_fetcher` / `create_bluesky_media_downloader` をそれぞれ独立 factory として提供。ingester 全体の組み立ては起動経路（CLI / MCP）が担う | factory が「Settings → 単一 component」という単純な役割に保たれる |

詳細は [BlueSky インジェスター仕様](ingesters/bluesky.md) と [BlueSky Fake Adapter](infrastructure/fake-adapters/bluesky.md) を参照。

## 3. Ingester / Runner ファミリーの境界

rag-knowledge には外部データ取得経路として 2 系統がある:

- **Ingester ファミリー**: in-process（同一 Python プロセス内）で `source_store.place_file()` を直接呼び出し、`IngestResult` を直接生成する取得・配置クラス
- **Runner ファミリー**: subprocess を起動して別プロセスで取得を行い、結果を bridge 層で `IngestResult` に変換する経路

両ファミリーとも、外部依存（HTTP API / 外部ライブラリ / subprocess）は Protocol 経由で注入され、Real / Fake を切り替えられる（[正解パターン: youtube インジェスター](#2-正解パターン-youtube-インジェスター)）。

### 3.1 Ingester ファミリー

`source_type` ごとに 1 つの Ingester クラスが対応する。各クラスは `BaseIngester` を実装し、共通 Port を介してパイプライン制御層から操作される。

| クラス | 対応 `source_type` |
|---|---|
| `AozoraIngester` | `aozora` |
| `BlueskyIngester` | `bluesky` |
| `JournalIngester` | `journal` |
| `LocalIngester` | `local` |
| `WebIngester` | `web` |
| `YoutubeIngester` | `youtube` |
| `ZennIngester` | `zenn` |

`source_type` の値定義の SSoT は [`_schema/enums.yml`](../../_schema/enums.yml)。各クラスの配置は `grep -rn "<ClassName>" src/` で辿る。

他 Ingester から特定媒体への委譲が必要な場合は、専用 Delegator Protocol（例: `YoutubeDelegator` / `WebDelegator`）経由で実施する。委譲先 Ingester 本体を直接 import せず、Port を介した Adapter 注入の形で構造を揃える（[正解パターン: youtube インジェスター](#2-正解パターン-youtube-インジェスター)と同型）。

#### Ingester の役割と責務境界

Ingester は **取り込み機構** であり、元データの加工を可能な限り行わず raw bytes を `source_store` に
配置することを責務とする。インデックス化向けのテキスト変換・正規化は converter の責務。詳細な責務制約
（無加工保存・metadata.db アクセス禁止・git 操作禁止・ファイル削除禁止）の SSoT は
[ingesters/common.md「責務の限定」](ingesters/common.md#責務の限定) を参照。converter 側から見た責務境界
（source_type 固有処理を converter に集約する根拠・判断フロー）は
[converter.md「責務境界」](converter.md#責務境界) を参照。

### 3.2 Runner ファミリー

subprocess を起動し、Scrapy 等の外部プロセスで取得を行う経路。Runner Protocol 経由で Real / Fake を切り替える。Runner ファミリーは以下の構造を持つ:

- **`ScrapyRunner` Protocol**: subprocess 起動の抽象 Port
- **`RealScrapyRunner` / `FakeScrapyRunner`**: Real / Fake 実装が同 Protocol を実装する
- **bridge 層**: subprocess の出力（クロール結果）を `IngestResult` に変換する中間層。詳細は [site-ingest.md](site-ingest.md) を参照

`ScrapyRunner` は `WebIngester` の Fetcher 相当依存として注入され、subprocess を起動・管理する。Runner ファミリーは Ingester ファミリーから注入される位置関係であり、パイプライン制御層から直接 Runner を操作することはない。

### 3.3 WebIngester の構造

`WebIngester` は `pipeline/ingesters/web/_facade.py` に配置され、site-ingest（Scrapy subprocess による Web ページ取り込み）を実行する。`source_type=web` のソースを生成する Ingester。

- `ScrapyRunner` Protocol を Fetcher 相当依存として注入する（[正解パターン: youtube インジェスター](#2-正解パターン-youtube-インジェスター)と同型の構造）
- bridge 層の中間処理は `WebIngester` 内部の実装詳細として隠蔽され、呼び出し元からは Ingester の戻り値 `IngestResult` のみ見える。中間型・処理フローの詳細は [site-ingest.md](site-ingest.md) を参照
- Fake モードは `RAG_WEB_FAKE_MODE` 環境変数で切り替える（[Fake モード基盤](infrastructure/fake-mode.md) 参照）

他 Ingester から web 取り込みへの委譲は `WebDelegator` Protocol 経由で行う（直接 import を禁止）。委譲経路の構造は youtube への委譲（`YoutubeDelegator`）と同型。

### 3.4 IndexWriteStrategy（インデクサー内部 Port）

`Indexer` は媒体ごとに性質の異なる書き込み戦略を持つインデックス（BM25・Vector Store 等）を保有する。
これらの「バッチ書き込み手順」を `IndexWriteStrategy` Protocol（context manager）で抽象化し、
`Indexer.batch_writes()` が保有する全 strategy を nest して enter/exit する。

| 観点 | 内容 |
|---|---|
| 役割 | バッチ処理時の書き込み戦略をインデックス実装ごとに表現する |
| 各実装の例 | `BM25WriteStrategy`（deferred save → flush）/ Vector Store は逐次 upsert のため独立 strategy を持たない |
| 呼び出し側 | `PipelineController` の `run_incremental` / `run_full_rebuild` / `run_index_only` が `with self._indexer.batch_writes():` 経由で利用 |

`IndexerProtocol` は BM25 等の実装詳細名を露出させない（呼び出し側は `batch_writes()` のみを知る）。
配置・各実装の詳細は [indexer.md「バッチ書き込み戦略（IndexWriteStrategy）」](indexer.md#バッチ書き込み戦略indexwritestrategy) を参照。

### 3.5 BaseIngester 抽象化の対象範囲

- Ingester ファミリーの全クラスが `BaseIngester` 継承対象（対象 `source_type` の SSoT は [`_schema/enums.yml`](../../_schema/enums.yml)、継承の実体は各 Ingester のコードが SSoT）
- `BaseIngester` は Ingester ファミリーの共通 Port（Protocol / ABC）として位置付ける。§1 の Dependency Rule に従い、パイプライン制御層は具象 Ingester ではなく `BaseIngester` 経由で操作する
- `ScrapyRunner` は `WebIngester` の Fetcher 相当依存として位置付け、`BaseIngester` 継承対象外

## 4. 構造判断の定量基準（参考値）

以下は構造問題のシグナルとなる定量値。**閾値超過は即座にリファクタ義務とはせず、レビュー / Issue 起票時の参考値として用いる**。

| 指標 | 参考値 | 構造問題の典型 |
|---|---|---|
| 1 ファイルの LOC | 800 LOC | 単一クラスに複数責務が同居している（HLS DL / URL 分類 / 媒体委譲等） |
| 越境直 import 系統数 | 2 系統 | あるインジェスターが他媒体や Scrapy ランナーを直 import している（Port 化されていない） |
| 単体テスト mock 比 | 2.0（mock 数 / テストケース数） | 構造的に注入できない依存を mock で吸収しているサイン。Adapter 化を検討 |

これらは [Issue #702 のロードマップ](https://github.com/becky3/rag-knowledge/issues/702) で検出された具体的な構造問題（bluesky.py 1319 LOC / mock 比 4.4 等）から導出した経験値である。

## 5. SSoT 階層と所在

設定値・列挙値・契約は SSoT 階層と所在の 2 軸で管理する（agent-commons `spec-driven.md` の SSoT 2 軸併置に従う）。

### 軸 1: 設定値・制約の競合解決

| 優先度 | 情報源 | 役割 |
|---|---|---|
| 1 | pydantic Field 定義（`src/rag/config.py`） | 型・制約値・デフォルト値の SSoT |
| 2 | `_schema/enums.yml` | 横断列挙値の SSoT（`source_type` / `source_status` / `pipeline_mode` / `cli_error_code` / `ingest_error_category`） |
| 3 | py-common-lib `ConstrainedClient` 定数 | 解除不能なハードリミット（リクエスト総数上限・最低リクエスト間隔等） |
| 4 | 仕様書 | 設計意図（Why）・振る舞いの定義 |

`_schema/enums.yml` と Python 側（Literal / Enum）の整合性は [`scripts/validate_enums.py`](../../scripts/validate_enums.py) が CI で検証する。

### 軸 2: 契約の所在

| 契約の種類 | 定義場所 | 表現 |
|---|---|---|
| 内部 dto / モデル | `<package>/models.py`（例: `store/models.py` / `pipeline/models.py`） | dataclass / Enum / TypedDict |
| Port（Adapter が従う interface 契約） | `<package>/<feature>_<role>.py`（例: `pipeline/ingesters/youtube_fetcher.py`）。Protocol と複数実装を 1 ファイルに集約する場合は `<package>/<feature>.py` 形式（例: `indexer/write_strategy.py`）も許容する | Protocol / ABC |
| 公開 API contract | MCP ツール定義（`src/rag/server/tools/`）/ HTTP スキーマ（`pydantic` BaseModel） | pydantic BaseModel / Discriminated Union |

`dict[str, Any]` は境界（外部 API レスポンス・JSON 応答・MCP 応答テキスト等）でのみ許容し、越境後は構造化型（dataclass / Enum / TypedDict）に変換する。

## 6. ConstrainedClient 所有権原則

外部 HTTP アクセスを担う `ConstrainedClient`（[py-common-lib](https://github.com/becky3/py-common-lib/blob/main/src/py_common_lib/httpx/constrained_client.py)）の **生成・破棄を誰が担うか**（所有権）の判断基準を定義する。

`ConstrainedClient` は安全制約（リクエスト総数上限・最低リクエスト間隔・操作全体タイムアウト・サーキットブレーカー）を内包し、1 インスタンスにつき 1 つのバジェット枠とリクエストカウンタを持つ。所有権の置き場所はこのバジェット枠の共有範囲を直接規定するため、構造判断として本セクションで明文化する。

各 Adapter が実際にどのパターンを採用しているかは Real Adapter のコード（クラス docstring + 実装）が SSoT である。本仕様書では現状の帰属を列挙せず、判断軸と各パターンの Why のみを定義する。
現状の採用状況を確認したい場合は `grep -rn "所有権: Pattern" src/` で各 Real Adapter の docstring を辿る。

### 6.1 採用パターンの定義

| ID | 名称 | 採用条件と Why |
|---|---|---|
| A | CLI 所有 | **委譲先 Adapter と同一 `ConstrainedClient` を共有してバジェットを合算する必要があるとき**。委譲経路でのバジェット合算・サーキットブレーカー連動を成立させるため、複数 Adapter の上位に位置する起動経路（CLI / MCP）で生成して各 Adapter の factory に注入する |
| B | Adapter 所有 | **単独 Adapter で完結し、外部に `ConstrainedClient` を共有する必要がないとき**。Adapter のライフサイクル開始時に内部生成し、終了時に破棄する。起動経路に余計なボイラープレートを増やさないため、委譲のないインジェスターはこのパターンを採る |
| C | 都度生成 | **並行・再入時の状態干渉を避ける必要があるとき**。同一インスタンスへの並行呼び出しを許容する Adapter で、1 回の API 呼び出しごとに新規生成して呼び出し終了で破棄する |

### 6.2 採用条件の判断軸

新規 Adapter 追加時は以下のチェックリストでパターンを選定する:

- 質問 1: **他 Adapter（委譲先）と同一の `ConstrainedClient` を共有してバジェットを合算する必要があるか？**
  - Yes → **A: CLI 所有** を採用
  - No → 質問 2 へ
- 質問 2: **同一 Adapter 内で並行・再入による状態干渉のリスクがあるか？**（複数の同時呼び出しが 1 つの client 状態を共有すると問題が起きる設計か）
  - Yes → **C: 都度生成** を採用
  - No → **B: Adapter 所有** を採用

委譲経路でのバジェット共有要件は [ingesters/common.md「インジェスター間の委譲」](ingesters/common.md#インジェスター間の委譲) を参照。

### 6.3 共通制約

採用パターンを問わず以下を遵守する:

- **facade メソッドへの `client` 引数渡しは禁止**: facade はコンストラクタで Adapter（Protocol 型）を受け取り、`ConstrainedClient` は Adapter 内に内包する（[ingesters/common.md「Protocol 注入規約」](ingesters/common.md#protocol-注入規約) 参照）
- **HTTP ステータスチェックは共通ヘルパー経由**: ConstrainedClient 対応の Adapter は HTTP GET を共通ヘルパー経由で実行する
  （[ingesters/common.md「HTTP ステータスチェックの共通ヘルパー」](ingesters/common.md#http-ステータスチェックの共通ヘルパー) 参照）
- **Fake モード切替は factory に閉じる（Fake モード対象 Adapter のみ）**:
  ingester 系 Real / Fake 切替を行う Adapter は factory が Fake モード環境変数を見て Real / Fake を返す。
  Fake は `ConstrainedClient` を持たず、所有権原則の対象外（[Fake モード基盤](infrastructure/fake-mode.md) 参照）。
  Fake モード非対象の Adapter（`SafeBrowsingClient` 等の独立クライアント）は本制約の適用外

## 7. Fake モード基盤

外部 API・ライブラリの実アクセスを排除する仕組み。詳細は [Fake モード基盤仕様](infrastructure/fake-mode.md) を参照。

| 観点 | 採用方針 |
|---|---|
| 注入方法 | Fetcher Protocol 経由で Real / Fake を切替 |
| 切替トリガー | `RAG_<SOURCE>_FAKE_MODE` 環境変数 |
| デフォルト | 安全側（fake 有効） |
| 起動ログ | `[FAKE MODE: <source>]` ラベルを stderr / 応答冒頭に明示 |

新規インジェスターは原則として Fake Adapter を備える（youtube パターン）。Fake Adapter なしでの追加は QA 戦略の L2 Mock E2E テストが書けなくなるため、構造レビューの対象とする。

## 8. 関連ドキュメント

- [全体仕様概要](overview.md) — 機能一覧と仕様書マップ
- [QA 戦略](workflows/qa-strategy.md) — L1 / L2 / L3 の責務分離
- [Fake モード基盤](infrastructure/fake-mode.md) — Fake Adapter 注入の仕様
- agent-commons `architecture-guide.md` — 用語定義（語彙集）
- agent-commons `spec-driven.md` — SSoT 2 軸併置・契約所在ルール

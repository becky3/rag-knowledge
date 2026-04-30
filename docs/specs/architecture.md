# rag-knowledge アーキテクチャ

rag-knowledge プロジェクトのアーキテクチャ説明・採用方針・構造判断 SSoT。
個別仕様書の上位にあたる構造判断はここに集約する。

用語の定義は agent-commons の `architecture-guide.md`（語彙集）を参照する。
本仕様書は語彙の上に乗る形で rag-knowledge 固有の採用方針・構造判断を記述する。

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
| facade の package 化 | `pipeline/ingesters/bluesky/` パッケージ（_facade / feed_fetcher / post_placer / url_routing / delegations 等）に分解 | 単一インジェスターが複数の責務（API / 配置 / URL 分類 / 委譲）を持ち、セクション 3 の LOC 参考値を超える場合 |
| Port の複数分離 | AT Protocol API 用 (`BlueskyFetcher`) と CDN メディア DL 用 (`BlueskyMediaDownloader`) を独立 Port として分離 | 1 インジェスターが性質の異なる複数の外部接続を持つ場合、責務単位の Port として分離する |
| 委譲先の Port 化 | youtube / site-ingest への越境直 import を `YoutubeClassifier` / `YoutubeDelegator` / `SiteIngestRunner` Port 経由に置換 | 他インジェスター・他ランナーへの委譲が必要な場合（セクション 3 の越境直 import 系統数の参考値も参照） |
| factory の独立化 | `create_bluesky_fetcher` / `create_bluesky_media_downloader` をそれぞれ独立 factory として提供。ingester 全体の組み立ては起動経路（CLI / MCP）が担う | factory が「Settings → 単一 component」という単純な役割に保たれる |

詳細は [BlueSky インジェスター仕様](ingesters/bluesky.md) と [BlueSky Fake Adapter](infrastructure/fake-adapters/bluesky.md) を参照。

## 3. 構造判断の定量基準（参考値）

以下は構造問題のシグナルとなる定量値。**閾値超過は即座にリファクタ義務とはせず、レビュー / Issue 起票時の参考値として用いる**。

| 指標 | 参考値 | 構造問題の典型 |
|---|---|---|
| 1 ファイルの LOC | 800 LOC | 単一クラスに複数責務が同居している（HLS DL / URL 分類 / 媒体委譲等） |
| 越境直 import 系統数 | 2 系統 | あるインジェスターが他媒体や Scrapy ランナーを直 import している（Port 化されていない） |
| 単体テスト mock 比 | 2.0（mock 数 / テストケース数） | 構造的に注入できない依存を mock で吸収しているサイン。Adapter 化を検討 |

これらは [Issue #702 のロードマップ](https://github.com/becky3/rag-knowledge/issues/702) で検出された具体的な構造問題（bluesky.py 1319 LOC / mock 比 4.4 等）から導出した経験値である。

## 4. SSoT 階層と所在

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
| Port（Adapter が従う interface 契約） | `<package>/<feature>_<role>.py`（例: `pipeline/ingesters/youtube_fetcher.py`） | Protocol / ABC |
| 公開 API contract | MCP ツール定義（`server.py`）/ HTTP スキーマ（`pydantic` BaseModel） | pydantic BaseModel / Discriminated Union |

`dict[str, Any]` は境界（外部 API レスポンス・JSON 応答・MCP 応答テキスト等）でのみ許容し、越境後は構造化型（dataclass / Enum / TypedDict）に変換する。

## 5. Fake モード基盤

外部 API・ライブラリの実アクセスを排除する仕組み。詳細は [Fake モード基盤仕様](infrastructure/fake-mode.md) を参照。

| 観点 | 採用方針 |
|---|---|
| 注入方法 | Fetcher Protocol 経由で Real / Fake を切替 |
| 切替トリガー | `RAG_<SOURCE>_FAKE_MODE` 環境変数 |
| デフォルト | 安全側（fake 有効） |
| 起動ログ | `[FAKE MODE: <source>]` ラベルを stderr / 応答冒頭に明示 |

新規インジェスターは原則として Fake Adapter を備える（youtube パターン）。Fake Adapter なしでの追加は QA 戦略の L2 Mock E2E テストが書けなくなるため、構造レビューの対象とする。

## 6. 関連ドキュメント

- [全体仕様概要](overview.md) — 機能一覧と仕様書マップ
- [QA 戦略](workflows/qa-strategy.md) — L1 / L2 / L3 の責務分離
- [Fake モード基盤](infrastructure/fake-mode.md) — Fake Adapter 注入の仕様
- agent-commons `architecture-guide.md` — 用語定義（語彙集）
- agent-commons `spec-driven.md` — SSoT 2 軸併置・契約所在ルール

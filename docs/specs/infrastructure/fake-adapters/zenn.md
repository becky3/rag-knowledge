# Zenn Fake Adapter

## 概要

Zenn インジェスターの外部アクセス処理（記事一覧 / スクラップ一覧 / 詳細取得）を抽象化した `ZennFetcher` Protocol と、その Fake 実装 `FakeZennFetcher` を定義する。Fake モード基盤の Zenn 向け具体実装。

スコープ:

- `ZennFetcher` Protocol の定義
- `FakeZennFetcher` のシナリオ切替仕様
- fixture JSON の構造とフィールド規約
- pytest テストでの利用パターン

スコープ外:

- fake モード基盤共通の制約・切替機構（[Fake モード基盤](../fake-mode.md) で定義）
- Real Fetcher の振る舞い（既存 [Zenn インジェスター](../../ingesters/zenn.md) に従う）

## 制約

### 共通制約の継承

Fake モード基盤の制約（[Fake モード基盤](../fake-mode.md)）はすべて適用される。本仕様書は Zenn 固有の制約のみを追記する。

### ZennFetcher Protocol のメソッド定義

Zenn インジェスターの外部アクセス処理を以下のメソッドに抽象化する:

| メソッド | 引数 | 戻り値 | 振る舞い |
|---|---|---|---|
| `list_contents` | `kind: "articles" \| "scraps"`、`username: str`、`page: int` | `dict[str, Any]`（Zenn API 一覧応答） | コンテンツ一覧 API 呼び出し |
| `fetch_content_detail` | `kind: "articles" \| "scraps"`、`slug: str` | `dict[str, Any]`（Zenn API 詳細応答） | コンテンツ詳細 API 呼び出し |

すべて `async` メソッド。Real / Fake で同じシグネチャを実装する。

`ZennFetcher` は `async with` でライフサイクル管理する（Real 実装が内部で `ConstrainedClient` を保持するため）。Fake 実装の `__aenter__` / `__aexit__` は no-op。

### FakeZennFetcher のシナリオ切替

| シナリオ名 | 振る舞い | 用途 |
|---|---|---|
| `happy`（デフォルト）| 全メソッドが正常データを返す | 通常系の動作確認 |
| `empty` | `list_contents` が空配列を返す | 空一覧の検証 |
| `not_found` | `fetch_content_detail` が `RuntimeError` を発生 | 404 相当の検証 |
| `metadata_error` | `list_contents` / `fetch_content_detail` が `RuntimeError` を発生 | エラーハンドリング検証 |

`SCENARIOS` tuple で網羅性を管理。未対応の値は `ValueError`（fail-fast）。

### Fake データ JSON の構造

`src/rag/pipeline/ingesters/_fake/zenn/data/` 配下に以下の JSON ファイルを配置する:

| ファイル | 対応メソッド | 内容 |
|---|---|---|
| `happy_articles_list.json` | `list_contents("articles")` | synthetic 記事一覧（`articles` 配列 + `next_page=null`） |
| `happy_scraps_list.json` | `list_contents("scraps")` | synthetic スクラップ一覧 |
| `happy_articles_detail.json` | `fetch_content_detail("articles", _)` | synthetic 記事詳細（`article` キー含む） |
| `happy_scraps_detail.json` | `fetch_content_detail("scraps", _)` | synthetic スクラップ詳細（`scrap` キー含む） |

JSON のフィールド構造は対応する Real Zenn API レスポンスと同じ。実値は synthetic ID 規約に従う。

### Synthetic ID 規約

| 識別子種別 | 形式 | 例 |
|---|---|---|
| slug | `test-` プレフィックス | `test-article-001` / `test-scrap-001` |
| username | `testuser` 系 | `testuser` |

### 安全網

- `tests/conftest.py` の autouse fixture (`_force_zenn_fake_mode`) で `RAG_ZENN_FAKE_MODE=true` を強制
- env 強制方式（`_RaiseOnUse` クラス差し替えは採用しない、bluesky と同じ）
- `httpx` クラス全体の差し替えは zenn 以外の用途で利用される httpx を誤爆させるため不採用
- `RAG_TESTS_ALLOW_NETWORK=1` で解除可能

### Zenn 設定項目

`src/rag/config.py` の Settings に以下の項目を追加する:

| 項目名 | 層 | 設計意図 |
|---|---|---|
| `rag_zenn_fake_mode` | 環境依存値 | Zenn fake モード切替。デフォルトは安全側（true）。本番運用時のみ false を `.env` で明示 |
| `rag_zenn_fake_fixture_dir` | 環境依存値 | Fake Fetcher が読み込む fixture ディレクトリ |

具体値（デフォルト）は pydantic Field が SSoT。

## インターフェース

### Protocol / 実装クラス

| 種別 | クラス名 | 配置 |
|---|---|---|
| Protocol | `ZennFetcher` | `src/rag/pipeline/ingesters/zenn/fetcher_protocol.py` |
| Real 実装 | `RealZennFetcher` | `src/rag/pipeline/ingesters/zenn/fetcher_protocol.py` |
| Fake 実装 | `FakeZennFetcher` | `src/rag/pipeline/ingesters/_fake/zenn/__init__.py` |
| ファクトリ関数 | `create_zenn_fetcher(settings: RAGSettings) -> ZennFetcher` | `src/rag/pipeline/ingesters/zenn/fetcher_protocol.py` |

### `ZennIngester` のシグネチャ

`ZennIngester.__init__` は `fetcher: ZennFetcher` を必須引数として受け取る。本体メソッド（`crawl_zenn` / `ingest_contents`）から `client` 引数は撤廃済み。

### CLI / MCP からの利用

CLI（`run_crawl_zenn` / `run_ingest_zenn`）は `async with create_zenn_fetcher(settings) as fetcher:` のコンテキストマネージャ内で `ZennIngester(fetcher=fetcher, ...)` を構築する。

## エッジケース

| ケース | 振る舞い |
|---|---|
| Fake モードで運用環境（本番）起動 | `_fake/` ディレクトリが production パッケージに含まれるため動作する。WARNING ログで `[FAKE MODE: zenn]` を明示 |
| `RAG_ZENN_FAKE_FIXTURE_DIR` で指定したパスが存在しない | 起動時に `FileNotFoundError` で fail-fast |
| MCP 応答ラベル | fake モード時、`rag_crawl_zenn` / `rag_add_zenn` 応答冒頭に `[FAKE MODE: zenn]` を付与。Embedding fake も同時有効なら `[FAKE MODE: embedding]` も並列出力 |

## 関連ドキュメント

- [Fake モード基盤](../fake-mode.md) — 共通基盤・切替機構・autouse 安全網・QA 運用を含む横断 SSoT
- [Zenn インジェスター](../../ingesters/zenn.md) — 抽象化対象のインジェスター仕様

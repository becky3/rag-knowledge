# Aozora Fake Adapter

## 概要

青空文庫インジェスターの外部アクセス処理（カタログ ZIP / 作品 XHTML）を抽象化した `AozoraFetcher` Protocol と、その Fake 実装 `FakeAozoraFetcher` を定義する。Fake モード基盤の Aozora 向け具体実装。

スコープ:

- `AozoraFetcher` Protocol の定義（カタログ ZIP / XHTML の bytes 取得）
- `FakeAozoraFetcher` のシナリオ切替仕様
- ZIP fixture（synthetic CSV を含む）と XHTML fixture の構造
- pytest テストでの利用パターン

スコープ外:

- ZIP 解凍 / CSV パース / 著作権チェック / GitHub Raw URL 変換（インジェスター本体の責務）
- Real Fetcher の振る舞い（既存 [青空文庫インジェスター](../../ingesters/aozora.md) に従う）

## 制約

### 共通制約の継承

Fake モード基盤の制約（[Fake モード基盤](../fake-mode.md)）はすべて適用される。本仕様書は Aozora 固有の制約のみを追記する。

### AozoraFetcher Protocol のメソッド定義

| メソッド | 引数 | 戻り値 | 振る舞い |
|---|---|---|---|
| `fetch_catalog_zip` | なし | `bytes` | カタログ ZIP（list_person_all_extended_utf8.zip）の生 bytes を取得 |
| `fetch_xhtml` | `github_url: str` | `bytes` | 指定 GitHub Raw URL から作品 XHTML の生 bytes を取得 |

すべて `async` メソッド。Real / Fake で同じシグネチャを実装する。

`AozoraFetcher` は `async with` でライフサイクル管理する（Real 実装が内部で `ConstrainedClient` を保持するため）。Fake 実装の `__aenter__` / `__aexit__` は no-op。

### 境界の決定

- **Real が返す型**: `httpx.Response.content` の bytes
- **Fake も bytes を返す**: 戻り値型一致を保証
- **ZIP 解凍 / CSV パース**: インジェスター本体（`_extract_csv_from_zip` / `_parse_csv`）が実施
- **GitHub Raw URL 変換**: インジェスター本体の `_to_github_raw_url`
- **著作権チェック**: インジェスター本体（`COL_COPYRIGHT == "なし"` 判定）

「外部境界では bytes、本体で構造化」のパターンに従う。

### FakeAozoraFetcher のシナリオ切替

| シナリオ名 | 振る舞い | 用途 |
|---|---|---|
| `happy`（デフォルト）| 全メソッドが正常データを返す | 通常系の動作確認 |
| `not_found` | `fetch_xhtml` が `RuntimeError` を発生 | 404 相当の検証 |
| `catalog_error` | `fetch_catalog_zip` / `fetch_xhtml` が `RuntimeError` を発生 | カタログ DL 失敗の検証 |

`SCENARIOS` tuple で網羅性を管理。未対応の値は `ValueError`（fail-fast）。

### Fake データの構造

`src/rag/pipeline/ingesters/_fake/aozora/data/` 配下に以下を配置:

| ファイル | 内容 |
|---|---|
| `catalog_happy.zip` | 実 ZIP ファイル（`zipfile` モジュールで生成）。内部の `list_person_all_extended_utf8.csv` には synthetic レコード 3 件（著作権なし 2 件 + 著作権あり 1 件）を含む |
| `xhtml_happy.html` | synthetic 作品 XHTML（実在の青空文庫作品とは関係ない汎用テキスト） |

ZIP fixture は実ファイルとして git にコミットする（テキストではなくバイナリ）。CSV 内容は ASCII の synthetic データのみで著作権リスクを回避する。

### Synthetic ID 規約

| 識別子種別 | 形式 | 例 |
|---|---|---|
| book_id | `999900` 番台（実在しない範囲） | `999900` / `999901` / `999902` |
| person_id | `99999` 番台 | `99999` |

### 安全網

- `tests/conftest.py` の autouse fixture (`_force_aozora_fake_mode`) で `RAG_AOZORA_FAKE_MODE=true` を強制
- env 強制方式（`_RaiseOnUse` クラス差し替えは採用しない、bluesky / zenn と同方式）
- `RAG_TESTS_ALLOW_NETWORK=1` で解除可能

### Aozora 設定項目

`src/rag/config.py` の Settings に以下の項目を追加する:

| 項目名 | 層 | 設計意図 |
|---|---|---|
| `rag_aozora_fake_mode` | 環境依存値 | Aozora fake モード切替。デフォルトは安全側（true） |
| `rag_aozora_fake_fixture_dir` | 環境依存値 | Fake Fetcher が読み込む fixture ディレクトリ |

## インターフェース

### Protocol / 実装クラス

| 種別 | クラス名 | 配置 |
|---|---|---|
| Protocol | `AozoraFetcher` | `src/rag/pipeline/ingesters/aozora/fetcher_protocol.py` |
| Real 実装 | `RealAozoraFetcher` | `src/rag/pipeline/ingesters/aozora/fetcher_protocol.py` |
| Fake 実装 | `FakeAozoraFetcher` | `src/rag/pipeline/ingesters/_fake/aozora/__init__.py` |
| ファクトリ関数 | `create_aozora_fetcher(settings: RAGSettings) -> AozoraFetcher` | `src/rag/pipeline/ingesters/aozora/fetcher_protocol.py` |

### `AozoraIngester` のシグネチャ

`AozoraIngester.__init__` は `fetcher: AozoraFetcher` を必須引数として受け取る。本体メソッド（`update_catalog` / `add_work` / `crawl_author` / `_ingest_work`）から `client` 引数は撤廃済み。

### CLI / MCP からの利用

CLI（`run_update_aozora_catalog` / `run_ingest_aozora` / `run_ingest_aozora_author` / `run_search_aozora`）は `async with create_aozora_fetcher(settings) as fetcher:` のコンテキストマネージャ内で `AozoraIngester(fetcher=fetcher, ...)` を構築する。

## エッジケース

| ケース | 振る舞い |
|---|---|
| Fake モードで運用環境（本番）起動 | `_fake/` ディレクトリが production パッケージに含まれるため動作する。WARNING ログで `[FAKE MODE: aozora]` を明示 |
| `RAG_AOZORA_FAKE_FIXTURE_DIR` で指定したパスが存在しない | 起動時に `FileNotFoundError` で fail-fast |
| MCP 応答ラベル | fake モード時、`rag_update_aozora_catalog` / `rag_add_aozora` / `rag_crawl_aozora` 応答冒頭に `[FAKE MODE: aozora]` を付与 |

## 関連ドキュメント

- [Fake モード基盤](../fake-mode.md) — 共通基盤・切替機構・autouse 安全網・QA 運用を含む横断 SSoT
- [青空文庫インジェスター](../../ingesters/aozora.md) — 抽象化対象のインジェスター仕様

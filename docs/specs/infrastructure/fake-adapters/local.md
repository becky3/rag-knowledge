# Local Fake Adapter

## 概要

Local インジェスターのファイルシステムアクセスを抽象化した `LocalFetcher` Protocol と、その Fake 実装 `FakeLocalFetcher` を定義する。Fake モード基盤の Local 向け具体実装。

スコープ:

- `LocalFetcher` Protocol の定義（filesystem 抽象化のみ）
- `FakeLocalFetcher` のシナリオ切替仕様
- fixture ディレクトリ構造（synthetic Markdown / text ファイル）
- pytest テストでの利用パターン

スコープ外:

- **PDF テキスト抽出 / AsciiDoc → Markdown 変換**: converter 層の責務（[#713](https://github.com/becky3/rag-knowledge/issues/713) で Fake 化対応予定）
- HTTP モード allowed_dirs 検証（セキュリティ境界としてインジェスター本体に残す）
- Real Fetcher の振る舞い（既存 [Local インジェスター](../../ingesters/local.md) に従う）

## 制約

### 共通制約の継承

Fake モード基盤の制約（[Fake モード基盤](../fake-mode.md)）はすべて適用される。本仕様書は Local 固有の制約のみを追記する。

### LocalFetcher Protocol のメソッド定義

| メソッド | 引数 | 戻り値 | 振る舞い |
|---|---|---|---|
| `discover_files` | `dir_path: str`、`pattern: str`、`extensions: list[str]` | `list[Path]`（ソート済み） | 指定ディレクトリ配下のファイルを glob パターンと拡張子で列挙 |
| `read_bytes` | `path: Path` | `bytes` | 指定ファイルを bytes として読み込む |
| `get_file_size` | `path: Path` | `int` | ファイルサイズ（bytes） |

`discover_files` / `read_bytes` / `get_file_size` は同期メソッド。`async with` は no-op（filesystem 操作にコンテキスト不要）。

### FakeLocalFetcher のシナリオ切替

| シナリオ名 | 振る舞い | 用途 |
|---|---|---|
| `happy`（デフォルト）| fixture 配下に複数ファイル（synthetic Markdown / text）を配置し正常に列挙・読み込み | 通常系の動作確認 |
| `empty` | `discover_files` が空配列を返す | 空ディレクトリの検証 |
| `unsupported_ext` | fixture に対応外拡張子のファイルのみを配置 | 拡張子フィルタの検証 |

### Fake の動作の特徴

- 入力 `dir_path` は **無視される**（Fake は常に固定の fixture を返す）
- fixture 内のファイルは実 I/O で読み込む（fake だが実 filesystem 操作）
- ユーザー指定の任意ディレクトリへのアクセスを排除する目的（テスト隔離）

### Fake データの構造

```
src/rag/pipeline/ingesters/_fake/local/data/
├── happy/
│   ├── test_doc1.md
│   └── test_doc2.txt
├── empty/                # 空ディレクトリ（discover_files が空配列を返す）
└── unsupported_ext/      # 対応外拡張子のみ含む（必要なら追加）
```

### Synthetic ID 規約

| 識別子種別 | 形式 | 例 |
|---|---|---|
| ファイル名 | `test_` プレフィックス | `test_doc1.md` / `test_doc2.txt` |

### 安全網

- `tests/conftest.py` の autouse fixture (`_force_local_fake_mode`) で `RAG_LOCAL_FAKE_MODE=true` を強制
- env 強制方式（`_RaiseOnUse` クラス差し替えは採用しない）
- 標準ライブラリ（`pathlib.Path` / `open` / `os.walk`）のクラス差し替えは他モジュールへの誤爆リスクが高いため不採用
- 既存 tmp_path ベース単体テストは `make_local_ingester` のデフォルト RealLocalFetcher 注入で互換維持
- `RAG_TESTS_ALLOW_NETWORK=1` で解除可能

### Local 設定項目

`src/rag/config.py` の Settings に以下の項目を追加する:

| 項目名 | 層 | 設計意図 |
|---|---|---|
| `rag_local_fake_mode` | 環境依存値 | Local fake モード切替。デフォルトは安全側（true） |
| `rag_local_fake_fixture_dir` | 環境依存値 | Fake Fetcher が読み込む fixture ディレクトリ |

## QA 速度向上について（重要）

Local Fake Adapter は **filesystem 抽象化のみ** を担うため、Local インジェスター由来の処理が直接律速する場合（filesystem 走査・bytes 読み込み）に対してのみ効果がある。

Local 取り込みで主要な律速要因である **PDF テキスト抽出（pymupdf4llm / MinerU）** および **AsciiDoc → Markdown 変換** は **converter 層の責務** であり、本 Fake Adapter のスコープ外。

QA イテレーション速度を改善するための converter Fake 化は [Issue #713](https://github.com/becky3/rag-knowledge/issues/713)（epic #702 子 Issue）で対応する。

## インターフェース

### Protocol / 実装クラス

| 種別 | クラス名 | 配置 |
|---|---|---|
| Protocol | `LocalFetcher` | `src/rag/pipeline/ingesters/local/fetcher_protocol.py` |
| Real 実装 | `RealLocalFetcher` | `src/rag/pipeline/ingesters/local/fetcher_protocol.py` |
| Fake 実装 | `FakeLocalFetcher` | `src/rag/pipeline/ingesters/_fake/local/__init__.py` |
| ファクトリ関数 | `create_local_fetcher(settings: RAGSettings) -> LocalFetcher` | `src/rag/pipeline/ingesters/local/fetcher_protocol.py` |

### `LocalIngester` のシグネチャ

`LocalIngester.__init__` は `fetcher: LocalFetcher` を必須引数として受け取る。本体メソッド（`add_document` / `crawl_documents` / `_collect_files`）は filesystem 操作を `self._fetcher.discover_files / read_bytes / get_file_size` 経由で実施する。

`_check_http_mode_access`（HTTP モード allowed_dirs 検証）はセキュリティ境界として本体に残す。

### CLI / MCP からの利用

CLI（`run_add_document` / `run_crawl_documents`）は `create_local_fetcher(settings)` で生成した Fetcher を `LocalIngester(fetcher=..., ...)` に注入する。

## エッジケース

| ケース | 振る舞い |
|---|---|
| Fake モードで運用環境（本番）起動 | `_fake/` ディレクトリが production パッケージに含まれるため動作する。WARNING ログで `[FAKE MODE: local]` を明示 |
| `RAG_LOCAL_FAKE_FIXTURE_DIR` で指定したパスが存在しない | 起動時に `FileNotFoundError` で fail-fast |
| MCP 応答ラベル | fake モード時、`rag_add_document` / `rag_crawl_documents` 応答冒頭に `[FAKE MODE: local]` を付与 |
| 既存 tmp_path ベース単体テスト | `make_local_ingester` がデフォルトで `RealLocalFetcher` を注入するため互換維持される（実 I/O テストはそのまま動作） |

## 関連ドキュメント

- [Fake モード基盤](../fake-mode.md) — 共通基盤・切替機構・autouse 安全網・QA 運用を含む横断 SSoT
- [Local インジェスター](../../ingesters/local.md) — 抽象化対象のインジェスター仕様
- [Issue #713](https://github.com/becky3/rag-knowledge/issues/713) — converter Fake 化（QA 速度向上の対応先）

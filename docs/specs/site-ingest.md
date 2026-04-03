# サイト一括取り込み（Scrapy subprocess）

## 概要

Scrapy を subprocess 方式で起動し、Web ページを一括取り込みする機能。2 つの動作モードを提供する:

- **クロールモード**（単一 URL）: 開始 URL からリンクを辿り、数千ページ規模の大規模サイトを一括取り込みする
- **複数 URL モード**（複数 URL）: 指定された URL のみを取得する（リンク辿りなし）。BlueSky インジェスターの URL 先取り込み等、バッチ取得に使用する

Scrapy の汎用 Spider でページを取得し、HTML ファイルとメタデータ JSONL をブリッジ層で source_store に変換した後、パイプライン制御で converter → indexer を実行する。

MCP ツール `rag_site_ingest` と CLI コマンド `site-ingest` の 2 つのインターフェースを提供する。

スコープ:

- Scrapy subprocess によるサイトクロール / 複数 URL 一括取得
- 汎用 Spider のパラメータ化（URL、ドメイン制約、URL パターン）
- JSONL メタデータ + HTML ファイルの一時保存
- ブリッジ層による一時保存データから source_store への変換・配置
- JOBDIR による中断再開（クロールモードのみ）
- MCP ツール `rag_site_ingest` と CLI コマンド `site-ingest` の提供

スコープ外:

- テキスト変換・チャンキング・インデックス構築（コンバーター・インデクサーの範疇）
- git 操作（パイプライン制御の範疇）
- 検索機能（rag_search 等）

## 背景

- Scrapy をライブラリとして組み込む方式は、Twisted の `ReactorNotRestartable` 問題（reactor はプロセスごとに 1 回しか起動できない）により、MCP サーバー（長時間稼働 asyncio プロセス）での複数回クロールが不可能。subprocess 方式で根本回避する
- #308 の検証で、Scrapy subprocess 方式が Unity Manual 3,503 ページ / 16 分 / データ品質 12/12 完全一致の実績を確認済み

## 制約

### Scrapy の subprocess 実行

- Scrapy は `asyncio.create_subprocess_exec` で別プロセスとして起動する。MCP サーバーの asyncio イベントループと Twisted の reactor を分離する
- ConstrainedClient は使用しない。Scrapy が HTTP リクエストを直接管理する
- 現行の raw HTTP チェック（`check-raw-http` ワークフロー）は aiohttp/httpx/requests/urllib.request の直接利用のみを検出対象としており、Scrapy の利用は検出対象外のため、`# safety:allowed` コメントによる CI 例外指定は不要である

### ドメイン制約

- **クロールモード**: Scrapy の `allowed_domains` により、クロール対象を開始 URL と同一ドメインに制限する。リンク辿りで発見された URL は同一ドメイン制約で自動的に制限される
- **複数 URL モード**: 全 URL のドメインの和集合を `allowed_domains` に設定する。リンク辿りは行わないため、指定 URL 以外のページは取得されない

### 複数 URL モード

- `urls` パラメータで 2 つ以上の URL を指定した場合、複数 URL モードで動作する
- 指定された URL のみを取得し、リンク辿り（クロール）は行わない
- `url_pattern` は無視される（パターンフィルタ不要）
- `max_pages` は無視される（取得ページ数 = 指定 URL 数）
- JOBDIR によるレジュームは使用しない（指定 URL を毎回取得する）
- `--force` は無効（JOBDIR が存在しないため）
- 単一 URL 指定時（`url` パラメータ、または `urls` が 1 件のみ）は既存のクロール動作を維持する

### パスプレフィックス制約

- `url_pattern` が未指定の場合、開始 URL のパスプレフィックスから正規表現パターンを自動生成する
- 例: `https://example.com/docs/` → `^https://example\.com/docs/`
- パスが `/` のみの場合はパターンを生成しない（ドメイン全体が対象）
- ユーザーが `url_pattern` を明示的に指定した場合はその値を優先する

### Safe Browsing チェック

- 数千件規模の URL に対する Google Safe Browsing API 呼び出しは非現実的なためスキップする
- SSRF チェックは 2 層で実施する:
  1. **初回 URL チェック**: ユーザー入力の開始 URL に対して、クロール開始前に `check_ssrf` で検証する
  2. **per-request チェック**: Scrapy Downloader Middleware で、各リクエストの送信前に DNS 解決 → IP 検証を実行する。DNS リバインディング攻撃（初回解決時はパブリック IP、実際のリクエスト時にプライベート IP に切り替わる手法）に対応する

### robots.txt

- Scrapy の `ROBOTSTXT_OBEY = True` に委譲する

### ページ数上限

- `site_ingest_max_pages`（config.toml）でクロール対象ページ数を制限する（デフォルト: 500）
- Spider 内で 200 OK レスポンスのみをカウントし、上限に達したら `close_spider` で自前クローズする
- 404 等の非 200 レスポンスはカウントに含めない（Scrapy の `CLOSESPIDER_PAGECOUNT` は全レスポンスをカウントするため使用しない）

### クロール順序

- BFS（幅優先探索）でクロールする。同一 depth のページを優先的に取得するため、`max_pages` に達した際にトップページ付近のページが網羅される
- Scrapy の `DEPTH_PRIORITY = 1` + FIFO キュー設定で制御する

### リクエスト間隔

- `site_ingest_delay_sec`（config.toml）でリクエスト間隔を制御する（デフォルト: 0.1 秒）
- Scrapy の `DOWNLOAD_DELAY` 設定で制御する

### 操作全体タイムアウト

- `site_ingest_timeout_sec`（config.toml）でクロール全体のタイムアウトを制御する（デフォルト: 7,200 秒 = 2 時間）
- Scrapy の `CLOSESPIDER_TIMEOUT` 設定で制御する
- タイムアウト後も JOBDIR が残存するため、再実行で続きから取得できる

### エラー停止閾値

- `site_ingest_error_count`（config.toml）でエラー数による停止閾値を制御する（デフォルト: 10）
- Scrapy の `CLOSESPIDER_ERRORCOUNT` 設定で制御する
- 閾値到達後も JOBDIR が残存するため、再実行で続きから取得できる

### Windows エンコーディング

- cp932 コンソールでの `UnicodeEncodeError` 対策として、subprocess 起動時に `PYTHONIOENCODING=utf-8` を環境変数に設定する

### バリデーション

- **バリデーションエラー（拒否）**: 空文字列の URL、無効なスキーム（http/https 以外）、ホスト名なし、SSRF チェック違反
- **クランプ（警告ログ付き）**: `max_pages` が許容範囲外の場合、範囲内にクランプする

## 想定プロファイル

| 項目 | 内容 |
|------|------|
| 最悪ケースリクエスト数 | `site_ingest_max_pages`（デフォルト 500、許容上限 1,000）+ robots.txt 取得 1 件。Scrapy の重複フィルタにより実際のリクエスト数は対象サイトのユニーク URL 数に依存する |
| 最悪ケース所要時間 | 1,000 × 0.1 秒（デフォルト間隔）= 100 秒。`site_ingest_download_timeout`（デフォルト 30 秒）× リクエスト数分の接続待ち時間が加算される可能性あり。Scrapy プロセスの終了で操作が完了する |
| 想定エラー率 | サイト依存。Scrapy のリトライミドルウェア（デフォルト: 2 回リトライ）が対象ステータスコード（500, 502, 503, 504, 522, 524, 408, 429）に適用される |

## 安全制約

| 制約名 | 種別 | 値 | 解除可否 |
|--------|------|-----|---------|
| SSRF チェック（初回 URL） | ハードリミット | クロール開始前に開始 URL を検証。プライベート IP・ローカルホストを拒否 | 無効化不可 |
| SSRF チェック（per-request） | ハードリミット | Downloader Middleware で各リクエストの DNS 解決結果を検証。プライベート IP を拒否 | 無効化不可 |
| ドメイン制約 | ハードリミット | `allowed_domains` で初回 URL と同一ドメインに制限 | 無効化不可 |
| robots.txt 遵守 | ハードリミット | `ROBOTSTXT_OBEY = True`（固定） | 無効化不可 |
| ページ数上限 | 設定値 | 許容範囲 1〜1,000、デフォルト 500 | 範囲内で変更可 |
| リクエスト間隔 | 設定値 | 許容範囲 0.05〜60 秒、デフォルト 0.1 秒 | 範囲内で変更可 |
| ダウンロードタイムアウト | 設定値 | 許容範囲 1〜300 秒、デフォルト 30 秒 | 範囲内で変更可 |
| 操作全体タイムアウト | 設定値 | 許容範囲 60〜86,400 秒、デフォルト 7,200 | 範囲内で変更可 |
| エラー停止閾値 | 設定値 | 許容範囲 1〜1,000、デフォルト 10 | 範囲内で変更可 |

テスト実行時の安全な値: `max_pages: 3`, `delay_sec: 1.0`

## インターフェース

### MCP ツール

| ツール | 入力 | 振る舞い |
|--------|------|---------|
| `rag_site_ingest` | `url` または `urls`（排他）、`url_pattern`（任意）、`max_pages`（任意）、`force`（任意）、`download_only`（任意） | Scrapy subprocess でページを取得し、HTML を source_store に配置後、パイプライン処理を実行する（`download_only` 時はパイプライン処理をスキップ）。結果サマリー（取得ページ数、エラー数、所要時間）を返す |

#### `rag_site_ingest` パラメータ

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| `url` | str | `""` | クロール開始 URL（クロールモード）。`urls` と排他 |
| `urls` | list[str] | `[]` | 取得対象 URL のリスト（複数 URL モード）。`url` と排他。2 件以上でクロール無効 |
| `url_pattern` | str | なし | クロール対象 URL のフィルタパターン（正規表現）。クロールモードのみ有効。未指定時は開始 URL のパスプレフィックスから自動生成する（例: `https://example.com/docs/` → `^https://example\.com/docs/`）。パスが `/` のみの場合はパターンなし（ドメイン全体が対象）。明示的に指定した場合はその値を優先する |
| `max_pages` | int | `site_ingest_max_pages` | ページ数上限。クロールモードのみ有効。config.toml の値を上書き可能 |
| `force` | bool | `false` | `true` の場合、クロールディレクトリ全体（JOBDIR、HTML、JSONL）を削除して最初からクロールする。クロールモードのみ有効 |
| `download_only` | bool | `false` | `true` の場合、Scrapy クロール + Bridge（source_store 配置 + git commit）まで実行し、パイプライン処理（converter + indexer）をスキップする。source_store への commit は実行されるため、後から `rag_rebuild`（incremental）で差分処理可能 |

`url` と `urls` の入力規則:

- `url` のみ指定: クロールモード（既存動作）
- `urls` のみ指定（2 件以上）: 複数 URL モード（リンク辿りなし）
- `urls` が 1 件のみ: クロールモード（`url` 指定と同等）
- 両方指定: バリデーションエラー
- 両方未指定: バリデーションエラー

### CLI コマンド

| コマンド | 振る舞い |
|---------|---------|
| `site-ingest` | MCP ツール `rag_site_ingest` と同等の機能を CLI で提供する |

#### `site-ingest` オプション

| オプション | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| `url`（位置引数） | str（1 つ以上） | — | 取得対象 URL。1 件: クロールモード、2 件以上: 複数 URL モード |
| `--url-pattern` | str | なし | URL フィルタパターン（クロールモードのみ） |
| `--max-pages` | int | `site_ingest_max_pages` | ページ数上限（クロールモードのみ） |
| `--force` | フラグ | `false` | クロールディレクトリ全体を削除して再クロール（クロールモードのみ） |
| `--download-only` | フラグ | `false` | Scrapy クロール + Bridge（source_store 配置 + git commit）まで実行し、パイプライン処理をスキップ |

### 取り込み結果の出力形式

既存の取り込みツール共通のサマリー形式（source_store 配置結果 + パイプライン処理結果）に加え、以下の情報を含む:

| フィールド | 内容 |
|-----------|------|
| クロール統計 | Scrapy が取得したページ数、エラー数 |
| 所要時間 | クロール開始から完了までの経過時間 |
| source_store 配置結果 | 配置ファイル数、上書き数、スキップ数（重複等）、エラー数 |
| パイプライン処理結果 | コンバート・インデックス構築の処理件数 |

## コンポーネント構成

### 全体フロー

```mermaid
flowchart TD
    USER["MCP / CLI"]
    CMD["SiteIngestCommand"]
    VALID["URL バリデーション + SSRF チェック"]
    SPIDER["Scrapy Spider（subprocess）"]
    TMPDIR["一時保存ディレクトリ"]
    BRIDGE["ブリッジ層"]
    SS["source_store"]
    PC["PipelineController"]
    CONV["コンバーター"]
    IDX["インデクサー"]
    RESULT["結果サマリー"]

    USER -->|"rag_site_ingest(url, ...)"| CMD
    CMD --> VALID
    VALID --> SPIDER
    SPIDER -->|"HTML ファイル + JSONL"| TMPDIR
    TMPDIR --> BRIDGE
    BRIDGE -->|"ファイル配置 + .meta 生成"| SS
    BRIDGE --> PC
    PC --> CONV
    CONV --> IDX
    IDX --> RESULT
    RESULT --> USER
```

### コンポーネント一覧

| コンポーネント | ファイル | 役割 |
|-------------|---------|------|
| Spider | `src/rag/scrapy/spider.py` | 汎用 Scrapy Spider。URL・ドメイン制約・URL パターンをパラメータで受け取り、HTML ファイルを一時保存ディレクトリに保存する |
| Runner | `src/rag/scrapy/runner.py` | subprocess ラッパー。Scrapy プロセスの起動・監視・終了判定を行う |
| Bridge | `src/rag/scrapy/bridge.py` | JSONL + HTML を source_store に変換・配置する。.meta サイドカーファイルを生成する |
| SSRF Middleware | `src/rag/scrapy/middleware.py` | Downloader Middleware。各リクエストの DNS 解決結果を検証し、プライベート IP へのアクセスを拒否する |
| パッケージ初期化 | `src/rag/scrapy/__init__.py` | パッケージ初期化 |

### Spider

汎用の Scrapy Spider。以下のパラメータで動作をカスタマイズする。

| パラメータ | 用途 |
|-----------|------|
| `start_url` | クロール開始 URL（クロールモード時。`start_urls` と排他） |
| `start_urls` | 取得対象 URL のリスト（複数 URL モード時。JSON 配列文字列。`start_url` と排他） |
| `allowed_domains` | ドメイン制約（URL から自動導出） |
| `url_pattern` | URL フィルタ（正規表現、任意。クロールモードのみ） |
| `output_dir` | HTML ファイルの保存先ディレクトリ |
| `max_pages` | ページ数上限（200 OK カウント）。外部インターフェースでは許容範囲 1〜1,000 でクランプされる。Spider 内部では 0 を無制限として扱うが、CLI/MCP からは入力されない |
| `no_follow` | リンク辿りを無効化するフラグ（複数 URL モード時に `true`） |

Spider の振る舞い:

- **クロールモード**（`no_follow` が `false`）: `start_url` からクロールを開始し、ページ内のリンクを辿る。リンク辿り時はクエリ文字列を除去して canonical URL に正規化する（静的サイトを主要ユースケースとする設計判断。`?page=2` 等のクエリでページが区別されるサイトでは一部ページが欠落する可能性がある）
- **複数 URL モード**（`no_follow` が `true`）: `start_urls` の全 URL を取得するが、ページ内のリンクは辿らない
- `allowed_domains` に含まれないドメインへのリクエストは自動的にフィルタされる
- `url_pattern` が指定されている場合、パターンに一致する URL のみ取得・保存する（クロールモードのみ）
- 取得した HTML をファイルとして `output_dir` に保存する。URL パスのディレクトリ構造を維持する（例: `https://example.com/docs/api/auth.html` → `output_dir/docs/api/auth.html`）
- URL パスが `.html`, `.htm` 等の Web 系拡張子で終わっている場合は `.html` を付加しない。拡張子がないパス（`/docs/api/` 等）のみ `.html` を付加する
- `start_requests` をオーバーライドし `dont_filter=False` でリクエストを発行する。これにより start_url のフィンガープリントが重複フィルタに記録され、リンク辿りでの再取得を防止する
- FEEDS 機能で JSONL にメタデータ（url, title, status, depth, collected_at）を出力する
- 非テキストレスポンス（Content-Type がテキスト系でない場合）はスキップする
- 200 OK レスポンスのみをページ数としてカウントし、`max_pages` に達したら `close_spider` で自前クローズする

### Runner

Scrapy プロセスの subprocess ラッパー。

振る舞い:

- `run()` メソッドはクロールモード（`start_url` 引数）と複数 URL モード（`start_urls` 引数）の 2 つの呼び出し方をサポートする
- `asyncio.create_subprocess_exec` で Scrapy を起動し、インラインスクリプト内で `CrawlerProcess(settings=...)` を構成する
- stdin は `DEVNULL` に設定する（MCP stdio モードでの親プロセス stdin 干渉を防止）
- stderr はファイルにリダイレクトする（Twisted の子プロセス/スレッドが stderr パイプを継承し、メインプロセス終了後もパイプが閉じない Windows 固有の問題を回避）
- JOBDIR を指定して中断再開に対応する
- 環境変数 `PYTHONIOENCODING=utf-8` を設定する
- `process.wait()` でプロセスの終了を待機し、exit code で成否を判定する
- exit code 0 以外の場合はエラーログを出力する（stderr ファイルの末尾を読み取り）

Scrapy に渡す設定:

| Scrapy 設定 | 値の由来 |
|-------------|---------|
| `ROBOTSTXT_OBEY` | `True`（固定） |
| `DOWNLOAD_DELAY` | `site_ingest_delay_sec`（config.toml） |
| `DOWNLOAD_TIMEOUT` | `site_ingest_download_timeout`（config.toml） |
| `DEPTH_PRIORITY` | `1`（固定、BFS） |
| `SCHEDULER_DISK_QUEUE` | `scrapy.squeues.PickleFifoDiskQueue`（固定、BFS） |
| `SCHEDULER_MEMORY_QUEUE` | `scrapy.squeues.FifoMemoryQueue`（固定、BFS） |
| `CLOSESPIDER_TIMEOUT` | `site_ingest_timeout_sec`（config.toml） |
| `CLOSESPIDER_ERRORCOUNT` | `site_ingest_error_count`（config.toml） |
| `JOBDIR` | クロールディレクトリ内の `jobdir/`（`{domain}/{crawl_key}/jobdir/`） |
| `FEEDS` | JSONL 出力パス |
| `DOWNLOADER_MIDDLEWARES` | SSRF Middleware を有効化（優先度 50） |
| `LOG_LEVEL` | `INFO` |

### SSRF Middleware

Scrapy Downloader Middleware として動作し、各リクエストの送信前に SSRF チェックを実行する。

動作フロー:

1. Scrapy がリクエストを発行する
2. Middleware がリクエスト URL のホスト名を DNS 解決する
3. 解決された全 IP アドレスがプライベート IP レンジに該当しないか検証する
4. 検証に通過した場合、リクエストを次の Middleware（または Downloader）に渡す
5. 検証に失敗した場合、リクエストを `IgnoreRequest` 例外で拒否し、警告ログを出力する

拒否対象（`rag.utils.url.check_ssrf` と同一の判定基準）:

| アドレス範囲 | 区分 |
|-------------|------|
| `127.0.0.0/8` | IPv4 ループバック |
| `10.0.0.0/8` | RFC 1918 プライベート |
| `172.16.0.0/12` | RFC 1918 プライベート |
| `192.168.0.0/16` | RFC 1918 プライベート |
| `169.254.0.0/16` | リンクローカル |
| `::1/128` | IPv6 ループバック |
| `fc00::/7` | IPv6 ユニークローカル |
| `fe80::/10` | IPv6 リンクローカル |
| `localhost` / `localhost.localdomain` | ホスト名文字列マッチ |

DNS リバインディング対策:

- Middleware は Scrapy の Downloader が HTTP 接続を確立する直前に DNS 解決を行い、その結果を検証する。初回 URL チェック時の DNS 結果をキャッシュして再利用するのではなく、リクエストごとに独立して DNS 解決を実行する
- これにより、初回 DNS 解決時にパブリック IP を返し、後続の解決でプライベート IP に切り替える DNS リバインディング攻撃を検出する

Middleware の優先度:

- `DOWNLOADER_MIDDLEWARES` で優先度 50 に設定する。Scrapy のデフォルト Middleware（`HttpCompressionMiddleware`: 590、`RedirectMiddleware`: 600 等）より先に実行されるため、プライベート IP への接続自体を防止する

判定ロジックの共有:

- IP アドレスの判定ロジックは `rag.utils.url.check_ssrf` を使用する。初回 URL チェックと per-request チェックで同一の判定基準を適用する

### Bridge

JSONL + HTML から source_store への変換・配置。

処理手順:

1. JSONL ファイルを行単位で読み込む
2. 各行の URL に対応する HTML ファイルを一時保存ディレクトリから読み取る
3. `SourceStore.place_file_from_url` で source_store に配置する
4. .meta サイドカーファイルを生成する（JSONL のメタデータから変換）

#### .meta 生成

JSONL の各行から .meta サイドカーファイルへの変換:

| JSONL フィールド | .meta フィールド | 説明 |
|-----------------|----------------|------|
| `url` | `source_id` | ソース識別子 |
| — | `source_type` | `"web"`（固定） |
| `title` | `title` | ページタイトル |
| `collected_at` | `collected_at` | 取得日時（ISO 8601） |
| `url` | `url` | ページ URL |

`status`, `depth` は .meta に含めない（source_store メタデータとして不要）。

### 中断再開

- JOBDIR にスケジューラキューと重複フィルタが永続化される
- 同じ JOBDIR で再実行すると、処理済み URL をスキップして続きから取得
- JOBDIR のレジュームは「重複スキップ付き再クロール」として動作する（Scrapy Issue #4106）。プロセス強制終了時にキューが失われる場合がある
- `--force` オプション指定時はクロールディレクトリ全体（JOBDIR、HTML、JSONL）を削除して最初からクロールする

### JOBDIR の分離

一時保存ディレクトリは `{domain}/{crawl_key}/` の 2 階層で管理する。

- **クロールモード**: `crawl_key` は `start_url` と effective `url_pattern`（自動生成後の値）の SHA-256 先頭 16 文字。同じパラメータなら同じディレクトリでレジューム可能
- **複数 URL モード**: `crawl_key` はソート済み URL リストの SHA-256 先頭 16 文字。`domain` は `_multi_` 固定（複数ドメインの場合があるため）。JOBDIR は使用しない（レジューム不要）

### 一時保存ディレクトリ

| 内容 | パス |
|------|------|
| HTML ファイル | `{SITE_INGEST_TEMP_DIR}/{domain}/{crawl_key}/html/` |
| JSONL メタデータ | `{SITE_INGEST_TEMP_DIR}/{domain}/{crawl_key}/metadata.jsonl` |
| JOBDIR | `{SITE_INGEST_TEMP_DIR}/{domain}/{crawl_key}/jobdir/` |

`SITE_INGEST_TEMP_DIR` のデフォルト値は `.tmp/site_ingest`。`crawl_key` の詳細は「JOBDIR の分離」セクションを参照。

### 正常完了後のクリーンアップ

Scrapy 正常完了 + bridge 完了後、クロールディレクトリ（`{domain}/{crawl_key}/`）全体を削除する。bridge 完了後は全データが source_store に配置済みであり、一時ファイルは不要。大規模サイトのクロールを繰り返すとディスクを圧迫するため、自動削除する。`download_only` の場合も bridge は完了しているため同様に削除する。

クリーンアップの実行条件:

| 条件 | クリーンアップ |
|------|-------------|
| Scrapy 正常完了 + bridge 完了 | 実行する（`download_only` の有無を問わない。bridge 完了時点で source_store に配置済み） |
| Scrapy 異常終了（exit_code != 0） | 実行しない（JOBDIR によるレジュームを維持） |
| クリーンアップ自体が失敗（Windows ファイルロック等） | 警告ログを出力し、処理全体は成功扱いとする |

### 処理フロー

```mermaid
sequenceDiagram
    participant USER as MCP/CLI
    participant CMD as SiteIngestCommand
    participant RUNNER as Runner
    participant SPIDER as Scrapy Spider
    participant BRIDGE as Bridge
    participant SS as source_store
    participant PC as PipelineController

    USER->>CMD: rag_site_ingest(url, ...)
    CMD->>CMD: URL バリデーション + SSRF チェック
    CMD->>CMD: 一時保存ディレクトリ準備
    CMD->>RUNNER: Scrapy 起動要求
    RUNNER->>SPIDER: subprocess 起動
    SPIDER->>SPIDER: クロール実行（HTML 保存 + JSONL 出力）
    SPIDER->>RUNNER: プロセス終了（exit code）
    RUNNER->>CMD: クロール結果
    CMD->>BRIDGE: JSONL + HTML → source_store 変換
    BRIDGE->>SS: ファイル配置 + .meta 生成
    BRIDGE->>CMD: 配置結果
    alt download_only = false
        CMD->>PC: パイプライン処理（git commit + converter + indexer、MCP: CLI サブプロセス経由）
        PC->>CMD: パイプライン処理結果
    else download_only = true
        CMD->>PC: git commit（source_store のみ）
        Note over CMD: パイプライン処理（converter + indexer）をスキップ
    end
    CMD->>CMD: クロールディレクトリ削除（Scrapy 正常完了時）
    CMD->>USER: 結果サマリー
```

### 設定項目

| 設定項目 | 型 | 保管先 | デフォルト | 許容範囲 | 説明 |
|---------|-----|--------|-----------|---------|------|
| `SITE_INGEST_TEMP_DIR` | str | `.env` | `.tmp/site_ingest` | — | 一時保存ディレクトリ |
| `site_ingest_delay_sec` | float | `config.toml` | `0.1` | 0.05〜60 | リクエスト間隔（秒） |
| `site_ingest_max_pages` | int | `config.toml` | `500` | 1〜1,000 | ページ数上限 |
| `site_ingest_download_timeout` | int | `config.toml` | `30` | 1〜300 | 1 リクエストのタイムアウト（秒） |
| `site_ingest_timeout_sec` | float | `config.toml` | `7200` | 60〜86,400 | 操作全体タイムアウト（秒） |
| `site_ingest_error_count` | int | `config.toml` | `10` | 1〜1,000 | エラー停止閾値 |

## 外部連携

| 連携先 | 用途 | 接続方式 |
|--------|------|---------|
| 対象 Web サイト | クロール対象 | HTTP/HTTPS（Scrapy が直接管理） |
| Scrapy | サイトクロールエンジン | subprocess 起動 |

Scrapy は独立した Python パッケージとして `pyproject.toml` に依存を追加する。Twisted / lxml / pyOpenSSL 等の推移的依存が追加される。

## エッジケース

| ケース | 振る舞い |
|--------|---------|
| 非テキストレスポンス（動画埋め込み等） | Spider 内で Content-Type を検査し、テキスト系でないレスポンスはスキップする |
| Scrapy プロセスが異常終了した場合 | Runner がエラーログを出力し、JSONL が部分的に出力されていれば、出力済み分を source_store に配置する。未出力分は次回再実行時に取得される（JOBDIR が残存している場合） |
| JOBDIR が破損している場合 | Scrapy がエラーで終了する。`--force` で JOBDIR を削除して再実行する |
| 一時保存ディレクトリのディスク容量不足 | Scrapy プロセスが I/O エラーで終了する。エラーログに記録する |
| JSONL に記載されているが HTML ファイルが存在しない場合 | ブリッジ層で当該エントリをスキップし、エラーとして計上（`ingest.errors` / `error_details`）してエラーログを出力する |
| 同一 URL が既に source_store に存在する場合 | `SourceStore.place_file_from_url` が既存ファイルを上書きする（通常の重複検出動作） |
| `url_pattern` が無効な正規表現の場合 | バリデーションエラーとして拒否する |
| Windows でのファイルロック | Scrapy プロセス終了後に JOBDIR のファイルがロックされている場合、`--force` による JOBDIR 削除が失敗する可能性がある。リトライまたは手動削除を案内する |
| DNS リバインディングによるプライベート IP への誘導 | SSRF Middleware が各リクエストの DNS 解決結果を検証し、プライベート IP へのアクセスを `IgnoreRequest` で拒否する。該当リクエストは Scrapy の統計に失敗として記録される |
| SSRF Middleware での DNS 解決失敗 | DNS 解決に失敗した場合、そのリクエストを `IgnoreRequest` で拒否する。ネットワーク障害等による一時的な DNS エラーは Scrapy のリトライ対象外となる |
| `download_only` 指定時にパイプライン処理が必要な場合 | MCP: `rag_rebuild`（mode: full, source_type: web）、CLI: `uv run python -m rag.cli rebuild --mode full --source-type web` で後からパイプライン処理を実行する。incremental モードでも可（source_store への配置が git commit されていれば差分検知される） |
| 同一ドメインへの異なるパラメータでの複数回クロール | クロールキー（`start_url` + effective `url_pattern` のハッシュ）により JOBDIR が分離されるため、前回クロールの URL キューが残留しない |
| 複数 URL モードで無効なスキームの URL が混在 | バリデーションで全 URL を検証し、不正な URL があればエラーを返す |
| 複数 URL モードで SSRF 対象の URL が混在 | 全 URL に対して SSRF チェックを実行し、1 つでも違反があればエラーを返す |
| 複数 URL モードで一部の URL が取得失敗 | 取得可能な URL のみ処理する。失敗した URL はエラーとして計上する |
| `url` と `urls` の両方が指定された場合 | バリデーションエラーとして拒否する |
| 正常完了後のクリーンアップ失敗（Windows ファイルロック等） | 警告ログを出力し、処理全体は成功扱いとする。一時ディレクトリは手動削除が必要 |

## 関連ドキュメント

- [rag-knowledge.md](rag-knowledge.md) — RAG ナレッジ全体仕様
- [source-store.md](source-store.md) — source_store 仕様
- [pipeline-controller.md](pipeline-controller.md) — パイプライン制御仕様
- [converter.md](converter.md) — コンバーター仕様
- [ingesters/common.md](ingesters/common.md) — インジェスター共通仕様
- [ingesters/bluesky.md](ingesters/bluesky.md) — BlueSky インジェスター仕様（複数 URL モードの利用元）

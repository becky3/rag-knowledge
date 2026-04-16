# source_store

## 概要

source_store は RAG Knowledge の全データの根源（Single Source of Truth）となるストレージである。各媒体から取得したオリジナルデータを無加工で保存し、git リポジトリとして管理する。

metadata.db、converted_store、検索インデックスは全て source_store から派生する。

スコープ:

- オリジナルデータの保存・管理
- .meta サイドカーファイルによるメタデータ管理
- metadata.db によるメタデータの索引
- source_id の決定規則
- URL とファイルパスの双方向変換
- 削除（物理削除 + 論理削除）

スコープ外:

- converted_store の管理（コンバーター仕様の範疇）
- git 操作の実行（パイプライン制御の範疇）
- 検索インデックスの構築・更新（インデクサーの範疇）

## 背景

- Embedding モデルやチャンクパラメータの変更時に、外部アクセスなしで再構築したい
- 取得したオリジナルデータを無加工で保持し、将来の変換方式変更に備えたい
- 取り込み・変換・インデックス構築の責務分離において、データの根源となるストレージが必要

## 制約

### データ保全

- オリジナルデータは無加工で保存する。メタデータの埋め込み等の加工を行わない
- source_store は git 管理下にあるため、物理削除されたファイルも `git checkout` で復旧可能。この復旧可能性を前提に、物理削除を許容する
- 物理削除は source_store の git リポジトリ内でのみ行う。`remove_file` は metadata.db から取得した `file_path` を使い、source_store ルートからの相対パスで操作する

### git 管理

- source_store は独立した git リポジトリとして管理する（プロジェクト本体の git リポジトリとは別）
- git 操作（init、add、commit、diff）はパイプライン制御層が実行する。各ステージ（インジェスター等）は git 操作を行わない
- .meta ファイルもオリジナルデータと同様に git 管理対象に含める（再構築の根拠として重要）
- metadata.db は git 管理対象外とする（`.gitignore` で除外）。バイナリファイルのため差分管理に適さず、source_store のファイルと .meta から再構築可能

### Windows パス制約

- Windows のファイルパス禁止文字は全角文字で代替する（変換規則は「URL パス変換」を参照）
- Windows はファイルパスの大文字小文字を区別しないが、大文字小文字の表現は保持される。URL 通りのケースでファイル名を作成し、パスから URL への逆算を可能にする
- 長いパスの利用は `LongPathsEnabled` レジストリが有効であることを前提とする

### .meta サイドカーファイル

- 自動取り込み媒体（[`_schema/enums.yml`](../../_schema/enums.yml) の `source_type` のうち `local` 以外）のファイルには `.meta` サイドカーファイルを同階層に配置する
- local 媒体は `.meta` 不要。sources テーブルの各フィールドは以下から導出する:
  - `title`: ファイル名（拡張子除去）
  - `collected_at`: git の初回コミット日時
  - `updated_at`: git の最終コミット日時
  - `content_hash`: ファイル内容から算出
  - `file_size`: ファイルシステムから取得
- `.meta` ファイルの形式は YAML とする
- `.meta` ファイルはメタデータの原本である。metadata.db はパフォーマンス向上のための索引であり、`.meta` から再構築可能。コンバーター等のパイプラインコンポーネントが `.meta` を直接読み取ってメタデータを取得することを許容する（metadata.db 経由を強制しない）

### metadata.db

- source_store ディレクトリ内に配置する
- SQLite WAL モードで運用する
- metadata.db が破損した場合、source_store のファイルと `.meta` からの再構築が可能であること
- バックアップ時は metadata.db の WAL をフラッシュするため、事前に `PRAGMA wal_checkpoint(TRUNCATE)` を実行すること
- スキーマ変更（マイグレーション）は CLI `migrate` コマンドで明示的に実行する。`initialize()` はテーブル作成（`CREATE TABLE IF NOT EXISTS`）のみ行い、スキーマ変更は行わない

本コンポーネントは外部 API 通信を行わないため、想定プロファイル・安全制約セクションは省略する。

## インターフェース

### ストア操作

| 操作 | 入力 | 出力 | 振る舞い |
|------|------|------|---------|
| ファイル配置 | source_type、ファイルデータ、メタデータ | 配置先パス | source_type に応じたディレクトリにファイルを配置し、.meta を生成する（local 以外） |
| .meta 読み取り | ファイルパス | メタデータ辞書 | 指定ファイルの .meta サイドカーを YAML として読み取る |
| .meta 書き込み | ファイルパス、メタデータ辞書 | なし | 指定ファイルの .meta サイドカーを YAML として書き込む |
| ファイル一覧 | source_type（任意） | ファイルパスのリスト | source_store 内のファイルを列挙する。source_type 指定時はそのディレクトリのみ。`.meta`、`metadata.db`、`.git/`、`.gitignore`、ロックファイル（`.ingest.lock`、`.rebuild.lock`）は除外する |
| ファイル削除 | source_id | なし | source_id に対応するファイルと .meta サイドカーをディスクから削除する。BlueSky 投稿の場合は対応する `media/{rkey}/` サブディレクトリも再帰削除する。metadata.db の更新は行わない（パイプライン制御が git diff 経由で処理する）。呼び出し後にパイプライン制御の取り込み実行（[pipeline-controller.md](pipeline-controller.md) 参照）を実行することで、git commit → パイプラインによる論理削除・インデックス削除が行われる |
| 論理削除 | source_id | なし | metadata.db のステータスを `deleted` に変更する。パイプライン制御の内部処理で使用 |
| 論理削除解除 | source_id | なし | metadata.db のステータスを `active` に戻す |
| ファイル取得 | source_id | ファイルデータ + メタデータ / `None` | source_id に対応するファイルと .meta を返す。物理削除済み（ファイル欠落）の場合は警告ログを出力して `None` を返す（復元が必要な場合は source_store の git リポジトリから `git checkout` で復元する） |

### metadata.db 操作

| 操作 | 入力 | 出力 | 振る舞い |
|------|------|------|---------|
| ソース登録 | ソースメタデータ | なし | sources テーブルにレコードを挿入する |
| ソース更新 | source_id、更新フィールド | なし | 指定レコードを更新する |
| ソース検索 | 検索条件 | ソースメタデータのリスト | 条件に合致するレコードを返す |
| パイプライン履歴追加 | from_commit_id、to_commit_id、mode、processed_at | なし | pipeline_history テーブルに実行履歴を追加する |
| 最終コミット ID 取得 | なし | コミット ID | pipeline_history の最新行の `to_commit_id` を返す。履歴がない場合は null commit hash を返す |
| DB 再構築 | なし | なし | source_store のファイルと .meta をスキャンし、metadata.db を再構築する |

### URL パス変換

| 操作 | 入力 | 出力 | 振る舞い |
|------|------|------|---------|
| URL → パス | URL 文字列 | source_store 内の相対パス | URL をディレクトリ構成に変換する。禁止文字は全角代替する |
| パス → URL | source_store 内の相対パス | URL 文字列 | パスから元の URL を復元する。全角文字を半角に戻す |

URL パス変換は純粋な文字列変換であり、拡張子の付加は行わない。インジェスターがコンバーターの変換方式決定のために拡張子を付加する場合がある（例: site_ingest の Bridge 層は拡張子なし、または未知の拡張子の URL に `.html` を付与する）。詳細は各インジェスターの仕様書を参照。

## コンポーネント構成

### ディレクトリ構成

```mermaid
flowchart TD
    SS["source_store/"]
    SS --> META_DB["metadata.db"]
    SS --> LOCAL["local/"]
    SS --> WEB["web/"]
    SS --> BS["bluesky/"]
    SS --> ZENN["zenn/"]
    SS --> YT["youtube/"]
    SS --> AZ["aozora/"]
    SS --> JNL["journal/"]
    SS --> DOT_GIT[".git/"]

    LOCAL --> L_USER["my-notes/ 等"]
    WEB --> W_PROTO["https/ または http/"]
    W_PROTO --> W_DOMAIN["example.com/"]
    W_DOMAIN --> W_PATH["docs/"]
    W_PATH --> W_FILE["guide.html"]
    W_PATH --> W_META["guide.html.meta"]
    BS --> BS_DID["did：plc：xxx/"]
    BS_DID --> BS_YEAR["2026/"]
    BS_YEAR --> BS_MONTH["03/"]
    BS_MONTH --> BS_POST["rkey.json"]
    BS_MONTH --> BS_POST_META["rkey.json.meta"]
    ZENN --> Z_USER["username/"]
    Z_USER --> Z_ART["articles/"]
    Z_USER --> Z_SCR["scraps/"]
    Z_ART --> Z_SLUG["slug.json"]
    Z_ART --> Z_SLUG_META["slug.json.meta"]
    YT --> YT_CH["{channel_id}/"]
    YT_CH --> YT_VID["{video_id}.json"]
    YT_CH --> YT_VID_META["{video_id}.json.meta"]
    AZ --> AZ_CAT["catalog.csv"]
    AZ --> AZ_CAT_META["catalog.csv.meta"]
    AZ --> AZ_PERSON["{person_id}/"]
    AZ_PERSON --> AZ_BOOK["{book_id}.html"]
    AZ_PERSON --> AZ_BOOK_META["{book_id}.html.meta"]
    JNL --> JNL_REPO["repository/"]
    JNL_REPO --> JNL_ENTRY["entry_id.md"]
    JNL_REPO --> JNL_META["entry_id.md.meta"]
```

- **source_store/**: ルートディレクトリ。パスは `.env` の設定値で指定
- **metadata.db**: メタデータ索引。source_store ルート直下に配置
- **.git/**: git リポジトリ管理ディレクトリ
- **local/**: ユーザーが手動で自由にファイル・フォルダを配置する領域
- **web/**: site_ingest が URL ベースのパス構成で自動配置
- **bluesky/**: BlueSky インジェスターが DID + 年月で階層化して自動配置
- **zenn/**: Zenn インジェスターがユーザー名 + コンテンツ種別（articles/scraps）で階層化して自動配置
- **youtube/**: YouTube インジェスターがチャンネル ID で階層化して自動配置
- **aozora/**: 青空文庫インジェスターがカタログ + 著者 ID で階層化して自動配置
- **journal/**: Journal インジェスターがリポジトリ名で階層化して自動配置

### converted_store のディレクトリ構成

converted_store は source_store のディレクトリ構成をミラーする。source_store 内の相対パスがそのまま converted_store 内の相対パスに対応する（拡張子は変換後の形式に変わる）。

変換不要なファイル（md/txt/adoc）もそのまま converted_store にコピーする。これによりインデクサーは常に converted_store のみを参照すればよい。

### source_id の決定方式

source_id は全媒体共通で **source_store 内の相対パス（file_path）** を使用する。source_store のディレクトリ構造により source_type ごとに名前空間が分離されているため、構造的に一意性が保証される。

source_store 層では `file_path` として、metadata_db 以上の層では `source_id` として同じ値を扱う。

| 媒体 | source_id（= file_path） | 例 |
|------|--------------------------|-----|
| local | `local/{ユーザー指定パス}` | `local/my-notes/memo.md` |
| web | `web/{スキーム}/{ホスト}/{パス}` | `web/https/example.com/docs/guide.html` |
| bluesky | `bluesky/{escaped_did}/{年}/{月}/{rkey}.json` | `bluesky/did：plc：xxx/2026/03/rkey.json` |
| zenn | `zenn/{username}/{articles\|scraps}/{slug}.json` | `zenn/alice/articles/sample-article.json` |
| youtube | `youtube/{channel_id}/{video_id}.json` | `youtube/UCxxxxxxxx/xxxxxxxxxxx.json` |
| aozora | `aozora/{person_id}/{book_id}.html` / `aozora/catalog.csv` | `aozora/000035/001567.html` |
| journal | `journal/{repository}/{entry_id}.md` | `journal/rag-knowledge/20260323-143000-session-summary.md` |

元 URL は .meta の `url` フィールド（web, zenn, youtube, aozora, bluesky）に保持される。bluesky は追加で `at_uri` フィールド（AT Protocol 識別子）も持つ。これらは検索インデックスでは `custom:url` / `custom:at_uri` のメタデータキーとして保持される。

### URL パス変換規則

Web URL を source_store のファイルパスに変換する際、以下の規則を適用する。

#### プロトコル分離

URL のスキームに応じてトップレベルディレクトリを分ける。

| URL スキーム | ディレクトリ |
|-------------|------------|
| `https://` | `web/https/` |
| `http://` | `web/http/` |

#### 禁止文字の全角代替

Windows のファイルパス禁止文字を全角文字に置換する。逆変換（パス → URL）時は全角を半角に戻す。

| 半角（URL 内） | 全角（パス内） | Unicode |
|---------------|---------------|---------|
| `:` | `：` | U+FF1A |
| `?` | `？` | U+FF1F |
| `*` | `＊` | U+FF0A |
| `<` | `＜` | U+FF1C |
| `>` | `＞` | U+FF1E |
| `\|` | `｜` | U+FF5C |
| `"` | `＂` | U+FF02 |

#### 変換例

URL: `https://example.com/docs/guide?lang=ja`

パス: `web/https/example.com/docs/guide？lang=ja`

逆変換: パスから `web/https/` を除去し、全角文字を半角に戻して `https://` を付与する。

#### ポート番号の扱い

URL にポート番号が含まれる場合（例: `localhost:8080`）、`:` を全角 `：` に置換する。

URL: `http://localhost:8080/api/docs`

パス: `web/http/localhost：8080/api/docs`

### .meta サイドカーファイル

#### ファイル命名規則

元ファイル名に `.meta` サフィックスを付加する。

| 元ファイル | .meta ファイル |
|-----------|---------------|
| `guide.html` | `guide.html.meta` |
| `post_xyz.json` | `post_xyz.json.meta` |
| `article-slug.html` | `article-slug.html.meta` |

#### 共通フィールド

全媒体（local 以外）の .meta に含まれるフィールド。

| フィールド | 型 | 内容 |
|-----------|-----|------|
| `source_type` | str | 媒体種別（値は [`_schema/enums.yml`](../../_schema/enums.yml) の `source_type` を参照。`local` は .meta を持たないため含まない） |
| `title` | str | コンテンツのタイトル |
| `collected_at` | str | 取り込みタイムスタンプ（ISO 8601） |

#### 媒体別フィールド

**web:**

| フィールド | 型 | 内容 |
|-----------|-----|------|
| `url` | str | 元の URL |

**bluesky:**

| フィールド | 型 | 内容 |
|-----------|-----|------|
| `at_uri` | str | 投稿の AT URI |
| `handle` | str | 投稿者のハンドル |
| `did` | str | 投稿者の DID |
| `rkey` | str | 投稿の Record Key |
| `url` | str | 投稿の Web URL |
| `created_at` | str | 投稿日時（ISO 8601） |
| `has_images` | bool | 画像添付の有無 |
| `has_video` | bool | 動画添付の有無 |
| `has_external_link` | bool | 外部リンクの有無 |
| `is_reply` | bool | リプライかどうか |
| `is_repost` | bool | リポストかどうか |

**zenn:**

| フィールド | 型 | 内容 |
|-----------|-----|------|
| `url` | str | Zenn 記事/スクラップの URL |
| `slug` | str | コンテンツスラッグ |
| `content_type` | str | コンテンツ種別（`article` または `scrap`） |
| `article_type` | str | 記事種別（`tech`, `idea` 等）。スクラップでは空文字列 |
| `published_at` | str | 公開日時（ISO 8601）。スクラップでは `created_at` を使用 |
| `liked_count` | int | いいね数 |
| `topics` | list | トピックタグのリスト |
| `comments_count` | int | コメント数（スクラップのみ。記事では 0） |
| `closed` | bool | クローズ状態（スクラップのみ。記事では `false`） |
| `username` | str | 著者のユーザー名 |

**youtube:**

| フィールド | 型 | 内容 |
|-----------|-----|------|
| `url` | str | YouTube 動画 URL |
| `video_id` | str | YouTube 動画 ID |
| `channel_id` | str | チャンネル ID |
| `uploader` | str | 投稿者名 |
| `upload_date` | str | アップロード日（`YYYYMMDD` 形式） |
| `duration` | int | 動画の長さ（秒） |
| `transcript_source` | str | 字幕ソース（`manual`, `auto`, `whisper`） |
| `playlist_id` | str | プレイリスト ID（プレイリスト経由の取り込み時のみ） |

**aozora:**

| フィールド | 型 | 内容 |
|-----------|-----|------|
| `url` | str | 青空文庫の作品 URL |
| `book_id` | str | 作品 ID |
| `person_id` | str | 著者 ID |
| `author` | str | 著者名 |
| `author_kana` | str | 著者名カナ |
| `copyright_expired` | bool | 著作権切れフラグ |

**journal:**

| フィールド | 型 | 内容 |
|-----------|-----|------|
| `repository` | str | リポジトリ名 |

#### .meta ファイルの形式例

**web:**

```yaml
source_type: web
title: "Guide Title"
collected_at: "2026-01-15T10:30:00+09:00"
url: "https://example.com/docs/guide"
```

**bluesky:**

```yaml
source_type: bluesky
title: "Sample post text"
collected_at: "2026-01-15T10:30:00+09:00"
at_uri: "at://did:plc:abc123/app.bsky.feed.post/xyz789"
handle: "alice.bsky.social"
did: "did:plc:abc123"
rkey: "xyz789"
url: "https://bsky.app/profile/alice.bsky.social/post/xyz789"
created_at: "2026-01-15T09:00:00Z"
has_images: false
has_video: false
has_external_link: true
is_reply: false
is_repost: false
```

**zenn（記事）:**

```yaml
source_type: zenn
url: "https://zenn.dev/alice/articles/sample-article"
title: "Sample Article Title"
collected_at: "2026-01-15T10:30:00+09:00"
slug: "sample-article"
content_type: "article"
article_type: "tech"
published_at: "2026-01-10T12:00:00+09:00"
liked_count: 42
topics:
  - "Python"
  - "FastAPI"
comments_count: 0
closed: false
username: "alice"
```

**zenn（スクラップ）:**

```yaml
source_type: zenn
url: "https://zenn.dev/alice/scraps/f0b53bc3944bb3"
title: "Sample Scrap Title"
collected_at: "2026-01-15T10:30:00+09:00"
slug: "f0b53bc3944bb3"
content_type: "scrap"
article_type: ""
published_at: "2026-02-14T20:48:17+09:00"
liked_count: 0
topics: []
comments_count: 3
closed: false
username: "alice"
```

**youtube:**

```yaml
source_type: youtube
url: "https://www.youtube.com/watch?v=xxxxxxxxxxx"
title: "Sample Video Title"
collected_at: "2026-03-25T10:00:00+09:00"
video_id: "xxxxxxxxxxx"
channel_id: "UCxxxxxxxxxxxxxxxxxxxxxxxx"
uploader: "Alice Channel"
upload_date: "20260320"
duration: 600
transcript_source: "auto"
```

**aozora:**

```yaml
source_type: aozora
url: "https://www.aozora.gr.jp/cards/000035/files/1567_14913.html"
title: "Sample Title"
collected_at: "2026-03-23T10:00:00+09:00"
book_id: "001567"
person_id: "000035"
author: "Alice Bob"
author_kana: "Sample Kana"
copyright_expired: true
```

**journal:**

```yaml
source_type: journal
title: "Session Summary: Pipeline Migration"
collected_at: "2026-03-23T14:30:00+00:00"
repository: rag-knowledge
```

### metadata.db スキーマ

#### sources テーブル

source_store 内の全ファイルのメタデータ索引。

| カラム | 型 | 制約 | 内容 |
|--------|-----|------|------|
| `source_id` | TEXT | PRIMARY KEY | ソース識別子（= source_store 内の相対パス） |
| `source_type` | TEXT | NOT NULL | 媒体種別（値は [`_schema/enums.yml`](../../_schema/enums.yml) の `source_type` を参照） |
| `title` | TEXT | NOT NULL | コンテンツのタイトル |
| `status` | TEXT | NOT NULL, DEFAULT 'active' | `active` または `deleted` |
| `content_hash` | TEXT | NOT NULL | ファイル内容の SHA-256 ハッシュ |
| `file_size` | INTEGER | NOT NULL | ファイルサイズ（バイト） |
| `collected_at` | TEXT | NOT NULL | 初回取り込み日時（ISO 8601） |
| `updated_at` | TEXT | NOT NULL | 最終更新日時（ISO 8601）。`register_source` の呼び出し時に現在時刻で設定される（新規登録・再取り込み時の上書きの両方） |
| `published_at` | TEXT | NOT NULL, DEFAULT '' | 公開日時（ISO 8601）。source_type ごとの `.meta` フィールドから解決する。空の場合は `collected_at` を使用する |
| `meta` | TEXT | NOT NULL, DEFAULT '{}' | `.meta` ファイルの内容を JSON 文字列として格納する索引。`json_extract()` でフィルタ可能。`.meta` が原本であり、本カラムは検索用の索引 |

#### pipeline_history テーブル

パイプライン実行履歴。最新行の `to_commit_id` が現在の `last_commit_id` に相当する。

| カラム | 型 | 制約 | 内容 |
|--------|-----|------|------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 連番 |
| `from_commit_id` | TEXT | NOT NULL | 処理前のコミット ID。初回は null commit hash（40文字ゼロ） |
| `to_commit_id` | TEXT | NOT NULL | 処理後のコミット ID |
| `processed_at` | TEXT | NOT NULL | 処理日時（ISO 8601） |
| `mode` | TEXT | NOT NULL | 実行モード: `full`, `convert`, `index`, `incremental` |

- `last_commit_id` の取得: `SELECT to_commit_id FROM pipeline_history ORDER BY id DESC LIMIT 1`
- 初回実行時の `from_commit_id` には git の null commit hash `0000000000000000000000000000000000000000`（40文字ゼロ）を使用する
- この値は git の慣例で「コミットなし」を意味するため、初回処理であることが自明

### 設定項目

| 設定項目 | 層 | 設計意図 |
|---------|-----|---------|
| `SOURCE_STORE_DIR` | 環境依存値 | source_store のディレクトリパス。環境ごとにストレージ配置が異なる |

## エッジケース

| ケース | 振る舞い |
|--------|---------|
| metadata.db が破損・消失した場合 | source_store のファイルと .meta をスキャンして再構築する。pipeline_history も消失するため、再構築後の初回パイプライン実行は null commit hash（初回扱い）となり、全ファイルが処理対象になる |
| .meta ファイルが欠落している場合（自動取り込み媒体） | 警告ログを出力し、ファイルパスから導出可能な情報で metadata.db に登録する。導出不可能なフィールドは空とする |
| local ファイルが source_store 外から参照された場合 | source_store 内の相対パスのみを受け付ける。外部パスはエラーとする |
| URL の大文字小文字が異なる同一パスへのアクセス | Windows は大文字小文字を区別しないため、先に配置されたファイルのケースが保持される。URL の逆算時はファイルシステム上のケースを使用する |
| URL にクエリパラメータが含まれる場合 | クエリパラメータも含めてパスに変換する（`?` → `？` の全角変換）。同一パスで異なるクエリの URL は異なるファイルとして扱う |
| source_store の git リポジトリが未初期化の場合 | パイプライン制御が初回実行時に `git init` を行う |
| 同一 source_id のファイルが既に存在する場合 | 上書きする（再取り込み時の更新動作） |
| 物理削除済みファイルの再取り込み | `place_file` でファイルを配置し、`ingest_and_index` を実行する。git diff が `A`（追加）として検知し、パイプラインが変換・インデックス追加・metadata.db 登録を行う |
| metadata.db の `status` が `deleted` のファイルへの再取り込み | `place_file` が `register_source` でステータスを `active` に戻し、ファイルを上書きする |
| URL にフラグメント（`#section`）が含まれる場合 | パス変換前にフラグメント部分を除去する。フラグメント違いの URL は同一ファイルとして扱う |
| `LongPathsEnabled` が無効で長いパスの操作に失敗した場合 | OS エラーをそのまま伝播し、エラーログに `LongPathsEnabled` の有効化を促すメッセージを出力する |
| パス内の全角代替文字がユーザーの手動配置で使用された場合（local 媒体） | local 媒体は URL 逆変換を行わないため影響なし。web 媒体のパス配下にユーザーが手動でファイルを配置することは想定しない |

## 関連ドキュメント

- [pipeline-controller.md](pipeline-controller.md) — パイプライン制御仕様
- [rag-knowledge.md](rag-knowledge.md) — RAG ナレッジ仕様（既存）

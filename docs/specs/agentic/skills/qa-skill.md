# QA スキル

## 概要

本スキルは **L3 本番相当 QA**（実 LM Studio・実 ChromaDB・実外部 API 接続による検証）を実行するための手順を提供する。L1 Unit Test / L2 Mock E2E で検出できない「環境込みの動作」「実 API 構造変更」「人間評価」を担う。

MCP ツール・CLI コマンド・HTTP API の機能を実プロセス・実依存環境で実行し、本番相当環境での正常動作を検証する。

QA 戦略 3 レイヤーの位置づけ・L1（pytest 単体）/L2（Mock E2E）との責務分離・実施頻度ポリシーは [QA 戦略](../../workflows/qa-strategy.md) を参照。実施タイミング基準も同仕様書の [L3 実施タイミング基準](../../workflows/qa-strategy.md#l3-実施タイミング基準) セクションが SSoT である。

スコープ:

- L3 本番相当 QA の実行手順（取り込み・検索・一覧・削除・再構築・統計）
- 取り込み後の実データ検証（source_store ファイル・Git 状態・検索結果）
- グループ単位の選択実行

スコープ外:

- L1 自動テストの実行（`/test-run` の範疇）
- L2 Mock E2E の実行（`tests/e2e/` の範疇）
- 異常系・エッジケースの網羅的テスト（L1/L2 の範疇）
- パフォーマンス計測

## 実施タイミング基準

L3 本番相当 QA の実施有無の判定基準（必須 / 推奨 / 不要）は [QA 戦略](../../workflows/qa-strategy.md#l3-実施タイミング基準) が SSoT である。本スキルを呼び出す前に必ず参照すること。

ここでは判定基準を転記せず、qa-strategy.md を一次情報源として運用する。

## 背景

- 実装セッションで動作確認を行うと、「テスト通過 = 動く」という確信バイアスにより検証が形骸化する
- 品質ゲートの手順として明記しても、実装者が手順を省略・簡略化する問題が繰り返し発生した
- 実装と検証を別セッションに分離し、検証手順をスキルとして固定することでバイアスと省略を排除する
- 本スキルは L3 本番相当 QA を担い、PR ごとの regression 検出は L1（pytest 単体）・L2（Mock E2E）に委譲することで実施頻度を抑える

## 制約

以下は L3 本番相当 QA 固有の制約。L1/L2 共通の制約は [QA 戦略](../../workflows/qa-strategy.md) を参照。

- **worktree 環境での実行**: 本番ストレージに影響を与えないよう、worktree 環境で実行する。ストレージパスは worktree 内の絶対パスを使用する
- **MCP サーバーの分離**: MCP ツール検証時は MCP サーバーを HTTP モードで起動する。CLI 検証時は停止を推奨する（HttpClient + ファイルベースロックにより同時アクセスは技術的に可能だが、検証環境の単純化のため分離する）
- **HTTP モード非対応ツール**: `crawl-documents`（`rag_crawl_documents`）は HTTP モードでは実行不可（クライアントとサーバーが別マシンの可能性がありローカルパスを解決できないため）。MCP テスト時はスキップし、CLI テスト時のみ実行する
- **大きいバイナリファイルの MCP テスト制約**: MCP の `content` パラメータに大きいファイル（PDF 等）を渡す場合、クライアント側のパラメータサイズ制約により失敗することがある。MCP テスト時は小さいファイルを使用するか、CLI で代替する
- **YouTube の実行制限**: YouTube グループ（D）を選択した場合、実行直前にユーザー確認を必ず挟む。IP ブロックリスクがあるため
- **外部 API への最小アクセス**: 外部 API を使用するグループでは、取り込み件数を最小限に制限する（具体値は検証リソース定義に従う）
- **エラー時の継続動作**: 各ステップでエラーが発生した場合、NG として記録し次のステップに進む。グループ全体を中断しない
- **取り込み後の共通検証フロー**: 取り込み・再構築を行うステップの後には、必ず共通検証フローを実行する（詳細は [QA 実行スキル](qa-execute-skill.md) に定義）
- **実行パラメータの明示**: 全ステップで実行するコマンド・パラメータを表示してから実行する
- **qa-execute による実行制御**: 各検証ステップの実行は [QA 実行スキル](qa-execute-skill.md) に委譲する。プロダクトコマンドの直接実行・共通検証フローの省略は禁止（ホワイトリストに定義された環境確認・準備・片付けは例外）

## コマンド体系

### 呼び出し

| コマンド | 用途 |
|---------|------|
| `/qa` | 動作確認を開始（引数なし、対話的にグループ・インターフェースを選択） |

### 検証グループ

データ取り込み系グループを先に実行し、Core を最後に配置する。Core は他グループの取り込みデータを使ってパイプライン基盤を検証する。

| ID | グループ | 内容 | 外部 API | インターフェース |
|----|---------|------|---------|----------------|
| A | Local | add-document, crawl-documents, add-journal, migrate-journal | 不要 | CLI / MCP |
| B | Web | site-ingest（クロール + 複数URL） | Web アクセス | CLI / MCP |
| C | SNS | crawl-zenn, crawl-bluesky | Zenn/BlueSky API | CLI / MCP |
| D | YouTube | ingest-youtube, ingest-youtube-playlist | YouTube API | CLI / MCP |
| E | Aozora | update-aozora-catalog, search-aozora, ingest-aozora | 青空文庫 | CLI / MCP |
| F | Upload | POST /upload/document, POST /upload/journal | 不要 | HTTP 固定 |
| G | Eval | init-test-db, evaluate | 不要 | CLI 固定 |
| H | Core | stats, list-recent, search, get-document, delete, rebuild | 不要（Embedding のみ） | CLI / MCP |
| J | Log | MCP サーバーのログファイル出力検証 | 不要 | MCP 固定 |

### インターフェース選択

| 選択肢 | 説明 |
|--------|------|
| CLI | CLI コマンドのみで検証 |
| MCP | MCP ツールのみで検証 |
| both | CLI と MCP の両方で検証 |

Upload（F）は HTTP 固定、Eval（G）は CLI 固定、Log（J）は MCP 固定のため、インターフェース選択に関わらず固定方式で実行する。

## 処理フロー

### ステップ 1: グループ選択

一覧を表示し、ユーザーに検証対象を問い合わせる。

表示形式:

```
QA 検証グループ:

  A) Local    — add-document, crawl-documents, add-journal, migrate-journal
  B) Web      — site-ingest（クロール + 複数URL）
  C) SNS      — crawl-zenn, crawl-bluesky
  D) YouTube  — ingest-youtube, ingest-youtube-playlist（実行前にユーザー確認）
  E) Aozora   — update-aozora-catalog, search-aozora, ingest-aozora
  F) Upload   — HTTP Upload API（HTTP モード固定）
  G) Eval     — init-test-db, evaluate（フィクスチャ必要）
  H) Core     — stats, list-recent, search, get-document, delete, rebuild
  J) Log      — ログファイル出力の検証（MCP フェーズのみ）

どのグループを検証しますか？（例: A B D, all）
```

ユーザーの回答を受け取り、対象グループを確定する。

### ステップ 2: インターフェース選択

```
インターフェースを選択してください:
  1) CLI のみ
  2) MCP のみ
  3) both（CLI + MCP）

※ Upload(F) は HTTP 固定、Eval(G) は CLI 固定です
```

### ステップ 3: worktree セットアップ

本番ストレージに影響を与えないよう、worktree 環境で QA を実行する。

1. ベースブランチ（develop）を最新に更新する
2. worktree を作成する（配置: リポジトリの親ディレクトリ、命名: `<リポジトリ名>-wt-<Issue番号>`）
3. メインリポジトリから `.env` をコピーし、ストレージパスを worktree 内の絶対パスに変更する。以下も設定する:
   - `CHROMADB_SERVER_PORT=8001`（メインリポジトリの MCP サーバーとのポート競合回避）
   - `CHROMADB_AUTO_START` はデフォルト（`true`）のままでよい（`ChromaDBServerManager` は起動前に heartbeat チェックを行い、既存インスタンスが応答すれば起動をスキップするため競合しない）
4. LM Studio のセットアップ・モデルロード手順は `docs/specs/infrastructure/lmstudio-operation.md` を参照する
5. `.tmp` ディレクトリを作成する
6. メインリポジトリから `.qa/` ディレクトリをコピーする（`.qa/` は `.gitignore` 対象のため worktree に含まれない）
7. ChromaDB サーバーを手動起動する: `uv run chroma run --path <persist_dir> --port <CHROMADB_SERVER_PORT>`（初回は venv 構築のため起動に時間がかかる。目安: 10〜20 秒。heartbeat 確認前に十分待機すること）
8. MCP フェーズを含む場合、worktree で MCP サーバーを HTTP モードで起動する: `cd <worktree-path> && uv run python -m rag.server &`。メインリポジトリの MCP サーバーが起動中の場合はポート競合するため先に停止すること。`.mcp.json` は `url` ベースのため変更不要

以降の全コマンドは worktree ディレクトリで実行する。

### ステップ 4: 環境確認

- 現在のブランチ・ディレクトリを確認する
- MCP サーバーの状態を確認する（MCP フェーズでは HTTP モードで起動中かつ `/mcp` で enabled であること、CLI 検証時は停止推奨）。disabled の場合はユーザーに有効化を依頼する
- ChromaDB サーバー疎通確認: `curl http://localhost:<CHROMADB_SERVER_PORT>/api/v2/heartbeat` で応答を確認する
- LM Studio の状態確認（Embedding API が必要なグループの場合）
  - `lms server status` でサーバー起動を確認
  - `.env` の `LMSTUDIO_BASE_URL` のホスト/ポートと `lms server status` のリッスン先が一致することを確認
    - 不一致だと `lms load` してもアプリは別サーバーへ接続し続けるため要注意
  - `lms ps` で `lmstudio.toml` の `models.embedding.key` がロード中であることを確認、未ロードなら `lms load <key>`
  - 詳細手順: `docs/specs/infrastructure/lmstudio-operation.md`
- LM Studio Vision モデルの確認（グループ A でメディア解析ステップを実行する場合）
  - `lms ps` で `lmstudio.toml` の `models.vision.key` がロード中であることを確認、未ロードなら `lms load <key>`
- ffmpeg の確認（メディア解析の動画処理を検証する場合）: `ffmpeg -version` で利用可能か確認する
- `.env` のストレージパスが worktree 内の絶対パスを指していることを確認する

### ステップ 5: 選択グループの順次実行

選択されたグループを ID 順（A→J）に実行する。各グループの詳細手順はグループ別検証手順に定義する。

#### ステップ実行ループ

グループ内の各ステップは、以下のループで逐次実行する:

1. グループ別検証手順のステップテーブルから、次の 1 行を取得する
2. その 1 行の情報（コマンド・期待結果・検証種別）を [QA 実行スキル](qa-execute-skill.md) に渡して呼び出す
3. qa-execute から結果（OK / NG / SKIP / STOP）が返るまで待機する。結果が返る前に次のステップに進んではならない
4. 結果を記録し、次の行に進む（STOP の場合はループを中断する）

**同一ターン制約:** qa-execute が継続シグナルを返し、かつステップテーブルに未実行の次ステップが残っている場合に限り、同一レスポンス内で即座に次の qa-execute を呼び出す。詳細は [QA スキル SKILL.md](../../../../.claude/skills/qa/SKILL.md) を参照。

qa スキル自身はステップ間の制御（次ステップへの遷移、STOP 時の中断、結果の蓄積）のみを担う。各ステップにおけるコマンド実行・検証・ユーザー確認は全て qa-execute 側で完結する（グループ開始前のユーザー確認など、ステップ外のグループレベル操作のみ qa 側で実施する）。

取り込み系ステップは検証種別 `ingest`、削除系ステップは `delete`、それ以外は `none` を指定する。

qa-execute がステップ結果として「STOP」を返した場合、残りのステップ・グループをすべてスキップし、ステップ 6（結果サマリー）に進む。STOP までに完了したステップの結果はサマリーに含める。

#### qa スキルが直接実行してよい操作（ホワイトリスト）

qa スキルが qa-execute を経由せず直接実行してよい操作を以下に限定する。ホワイトリスト外の Bash / CLI 実行（プロダクトコマンド: 取り込み・検索・削除・再構築等）は全て qa-execute 経由とする。

| カテゴリ | 許可する操作 | 例 |
|---------|------------|-----|
| 環境確認 | 読み取り専用の状態確認コマンド | `git branch --show-current`, `curl http://localhost:<CHROMADB_SERVER_PORT>/api/v2/heartbeat`, `curl http://localhost:<LM_STUDIO_PORT>/v1/models` |
| グループ準備・片付け | `.env` 変更、サーバー起動・停止、worktree セットアップ・クリーンアップ | `uv run chroma run ...`, `taskkill`, `git worktree remove` |
| YouTube 事前確認 | グループ D 実行前の AskUserQuestion | IP ブロックリスクの確認表示 |

#### インターフェース選択に応じた実行方針

- **CLI のみ**: MCP サーバーを停止し、全グループを CLI で実行する
- **MCP のみ**: MCP サーバーが HTTP モードで起動中であることを確認し、全グループを MCP で実行する
- **both**: 2フェーズで実行する
  1. **CLI フェーズ**: MCP サーバーを停止し、全グループを CLI で実行する
  2. **MCP フェーズ**: MCP サーバーを HTTP モードで起動し、全グループを MCP で実行する

MCP フェーズでの追加確認（書き込み系ツール実行時）:

- MCP ツールの実行結果が正常に返されること（CLI サブプロセス経由の JSON パースが成功していることの間接確認）
- エラー発生時にエラーメッセージが適切に返されること

YouTube（D）選択時は、グループ実行直前に以下を表示してユーザー確認を取る（qa-execute の都度確認とは別に、グループレベルで事前確認する）:

```
YouTube 検証を実行します。
YouTube API へのアクセスにより IP ブロックのリスクがあります。
実行しますか？ (y/n)
```

### ステップ 6: 結果サマリー

全グループの実行完了後（STOP による中止を含む）、結果サマリーを表示する。

#### NG 扱い基準

結果サマリーの集計にあたり、以下の条件に該当するステップは NG として扱う:

- qa-execute から NG として返されたステップ
- 検証フロー（検証種別 `ingest` / `delete`）を省略・簡略化して実行したステップ
- 期待結果と異なる条件で再検証して OK としたステップ（例: パラメータを変えて再実行し、その結果で OK とする）

ユーザーが qa-execute の進行確認で明示的に OK と判断した場合のみ例外とする。この場合、結果テーブルに「OK（ユーザー判断）」と記載する。

#### 表示形式

```
QA 検証結果:

| グループ | インターフェース | 結果 |
|---------|----------------|------|
| A Local | CLI            | OK   |
| B Web   | CLI            | OK   |
| C SNS   | CLI            | STOP — ユーザーにより中止 |
| D YouTube | ---          | 未実行 |
| ...     | ...            | ...  |

NG 項目: N 件
```

STOP で中止されたグループは「STOP — ユーザーにより中止」、STOP 以降の未実行グループは「未実行」と表示する。

NG が検出された場合、Issue 起票を提案する。

## グループ別検証手順

### 共通検証フロー

共通検証フロー（取り込み後・削除後）の詳細は [QA 実行スキル仕様](qa-execute-skill.md) のステップ 4 に定義する。各グループの検証種別テーブルで `ingest` / `delete` を指定したステップで自動的に実行される。

### A) Local

目的: ローカルファイル取り込み（ドキュメント・ジャーナル）の動作確認。

各ステップを qa-execute に委譲する:

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `add-document --file README.md` | Markdown 取り込み成功 | `ingest` |
| 2 | `add-document --file .qa/pdf_add_test.pdf` | PDF 取り込み成功 | `ingest` |
| 3 | LM Studio Vision モデルを `lms unload` した状態（Embedding モデルは load 中）で `add-document --file .qa/image_add_test.jpg` | 取り込み成功するがメディア解析テキストなし（フォールバック動作）。エラーで中断しないこと | `ingest` |
| 4a | `cp .qa/image_replace_test.jpg .qa/image_add_test.jpg`（A-3 と内容バイトが異なる別画像で上書きするための事前準備） | コピー成功（同名ファイルが別バイトに置き換わる） | `none` |
| 4b | LM Studio Vision モデルを `lms load <key>` した状態で `add-document --file .qa/image_add_test.jpg --upload-mode replace` | 上書き成功（`overwritten=1`）。pipeline が変更検知して Vision 解析を実行。メディア解析テキストが生成されること。search 結果に新画像の解析テキストが含まれることを確認する | `ingest` |
| 5 | `add-document --file .qa/video_add_test.mp4` | 動画取り込み成功。ffmpeg フレーム抽出 → Vision モデルで各フレーム解析 → タイムスタンプ付きテキスト生成。search 結果に動画の解析テキストが含まれること | `ingest` |
| 6 | `add-document --file README.md --upload-mode replace` | 上書き成功、エラーなし | `none` |
| 7 | `crawl-documents docs/specs/`（`dir_path` は positional 引数） | ディレクトリ一括取り込み成功 | `ingest` |
| 8 | `add-journal --title "コンテンツ一覧取得機能の実装" --file .qa/journal_add_test.md --repository rag-knowledge` | ジャーナル登録成功 | `ingest` |
| 9a | `migrate-journal --dir .qa/journals --repository rag-knowledge` | ジャーナル一括配置成功 | `none` |
| 9b | `rebuild --mode incremental` | 再構築成功、migrate 分がインデックスに反映 | `ingest` |

CLI / MCP 対応:

| CLI コマンド | MCP ツール |
|------------|-----------|
| `add-document --file <path>` | `rag_add_document` |
| `crawl-documents <dir>` | `rag_crawl_documents`（HTTP モード非対応、CLI のみ） |
| `add-journal --title <t> --file <f> --repository <r>` | `rag_add_journal` |
| `migrate-journal --dir <d> --repository <r>` | （CLI のみ） |

**MCP テスト時の注意:**

- A-2 (PDF): 大きい PDF は MCP パラメータサイズ制約で失敗する場合がある。失敗時は CLI で代替実行する
- A-3 (フォールバック): Vision モデル unload が必要。`lms unload <key>` で実施する
  - 手順: `docs/specs/infrastructure/lmstudio-operation.md`
- A-4 (画像), A-5 (動画): LM Studio Vision モデルが未ロードの場合はスキップする（`lms ps` で確認）
- A-7 (crawl-documents): HTTP モード非対応のため MCP テスト時はスキップする

### B) Web

目的: Web ページ取り込み（クロールモード + 複数 URL モード）の動作確認。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `site-ingest https://www.stat.go.jp/ --max-pages 20` | Scrapy クロールモード一括取り込み成功 | `ingest` |
| 2 | `site-ingest https://www.stat.go.jp/data/jinsui/ https://www.stat.go.jp/data/roudou/` | 複数 URL モードで 2 件取得成功（クロールなし） | `ingest` |

CLI / MCP 対応: `rag_site_ingest`（`url` パラメータ / `urls` パラメータ）

### C) SNS

目的: Zenn・BlueSky インジェスターの動作確認。BlueSky メディア DL と --force オプション、および投稿内 URL 自動取り込み（`rag_crawl_bluesky` / `rag_add_bluesky` 経由）の検証を含む。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `crawl-zenn rhythmcan --max-articles 1` | Zenn 記事 1 件取り込み成功 | `ingest` |
| 2 | `crawl-bluesky rhythmcan.bsky.social --max-posts 5` | BlueSky 投稿取り込み成功。メディア付き投稿がある場合、source_store の `bluesky/{did}/{year}/{month}/media/{rkey}/` にメディアファイル（`image_0.{ext}` / `video_0.ts`）が配置されていること。投稿内に外部リンクを含む投稿がある場合、site_ingest 経由で URL 先が取得され `URL 自動取り込み: Web N件` が表示されること | `ingest` |
| 3 | `crawl-bluesky rhythmcan.bsky.social --max-posts 1 --force` | `--force` による上書き再取得成功。既存投稿が上書きされ、CLI に `完了: N件配置`（N > 0）と表示されること | `ingest` |
| 4 | `ingest-bluesky <Web リンク付き投稿 URL>` | 投稿 1 件配置 + `URL 自動取り込み: Web N件` が表示される。site_ingest が呼ばれ URL 先 Web ページが source_store に配置されること（ピンポイント修復シナリオ） | `ingest` |
| 5 | 同 URL で `ingest-bluesky` を再実行 | 投稿 1 件上書き（`overwritten=1`）。`URL 自動取り込み: Web N件` が再度表示され、site_ingest Bridge が URL 先 Web ページを上書き配置すること | `ingest` |

CLI / MCP 対応: `rag_crawl_zenn` / `rag_crawl_bluesky`（`force=True` で --force 相当）/ `rag_add_bluesky`（C-4/C-5 用、`urls` パラメータに投稿 URL リストを渡す）

**MCP テスト時の追加確認:**

- C-4 / C-5: MCP 応答テキストに `URL 自動取り込み: Web N件` のフォーマット文字列が含まれること（`_format_ingest_response` の url_follow 反映を確認）

### D) YouTube

目的: YouTube インジェスターの動作確認。グループ実行前にユーザー確認を必須とする（qa-execute の都度確認とは別）。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `ingest-youtube https://www.youtube.com/watch?v=GuFBDpzH3ck` | 動画 1 件取り込み成功 | `ingest` |
| 2 | `ingest-youtube-playlist https://www.youtube.com/playlist?list=PLaFZvPBpvhKKgHIDI16ja0jwEG_vIH55K --max-videos 1` | プレイリストから 1 件取り込み成功 | `ingest` |

CLI / MCP 対応: `rag_add_youtube` / `rag_crawl_youtube`

### E) Aozora

目的: 青空文庫インジェスターの動作確認。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `update-aozora-catalog` | カタログ CSV が source_store に配置される | `none` |
| 2 | `search-aozora --author 太宰 --limit 300` | 結果に `001567`（走れメロス）が含まれる | `none` |
| 3 | `ingest-aozora 001567` | 作品 1 件取り込み成功 | `ingest` |

CLI / MCP 対応: `rag_update_aozora_catalog` / `rag_search_aozora` / `rag_add_aozora`

著者一括（`rag_crawl_aozora` / `ingest-aozora-author`）は単一取り込みで検証十分なため、QA ではスキップする。

### F) Upload

目的: HTTP Upload API の動作確認。MCP サーバーが HTTP モードで起動中であること（worktree セットアップで起動済み）。

#### グループ準備

1. API キーが keyring に登録済みか確認する:

   ```bash
   uv run python -c "
   from rag.config import UPLOAD_API_KEY_SERVICE, UPLOAD_API_KEY_NAME
   import keyring
   key = keyring.get_password(UPLOAD_API_KEY_SERVICE, UPLOAD_API_KEY_NAME)
   print('OK: API key found' if key else 'NG: API key not found')
   "
   ```

   未登録の場合は `uv run python -m rag.cli generate-api-key --save` で生成・保存する

3. API キーをテンポラリファイルに書き出す（curl コマンドで使用）:

   ```bash
   uv run python -c "
   from rag.config import UPLOAD_API_KEY_SERVICE, UPLOAD_API_KEY_NAME
   import keyring
   print(keyring.get_password(UPLOAD_API_KEY_SERVICE, UPLOAD_API_KEY_NAME))
   " > /tmp/qa_api_key.txt
   chmod 600 /tmp/qa_api_key.txt
   ```

   起動確認は F-1（最初のリクエスト）で HTTP 200 が返ることをもって行う。

ベース URL: `http://localhost:<RAG_HTTP_PORT>`（デフォルト: `8081`）

全リクエストに API キーヘッダー（`-H "X-API-Key: $(cat /tmp/qa_api_key.txt)"`）を付与する。

| # | コマンド（curl） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `curl -X POST http://localhost:<RAG_HTTP_PORT>/upload/document -H "X-API-Key: $(cat /tmp/qa_api_key.txt)" -F "file=@.qa/upload_doc_test.md"` | HTTP 200、`status: ok` と `source_id` が返る | `ingest` |
| 2 | `curl -X POST http://localhost:<RAG_HTTP_PORT>/upload/document -H "X-API-Key: $(cat /tmp/qa_api_key.txt)" -F "file=@.qa/upload_doc_test.md"` | HTTP 409（重複検出エラー、`upload_mode` デフォルト `fail`） | `none` |
| 3 | `curl -X POST http://localhost:<RAG_HTTP_PORT>/upload/document -H "X-API-Key: $(cat /tmp/qa_api_key.txt)" -F "file=@.qa/upload_doc_test.md" -F "upload_mode=replace"` | HTTP 200 | `none` |
| 4 | `curl -X POST http://localhost:<RAG_HTTP_PORT>/upload/journal -H "X-API-Key: $(cat /tmp/qa_api_key.txt)" -F "file=@.qa/journal_upload_test.md" -F "title=QA スキルの仕様書・スキル定義作成" -F "repository=rag-knowledge"` | HTTP 200、`status: ok` と `source_id` が返る | `ingest` |
| 5 | `curl -X POST http://localhost:<RAG_HTTP_PORT>/upload/journal -H "X-API-Key: $(cat /tmp/qa_api_key.txt)" -F "file=@.qa/journal_upload_test.md" -F "repository=rag-knowledge"` | HTTP 400（必須フィールド `title` 欠落） | `none` |
| 6 | 下記の並行リクエストコマンドを実行 | いずれか一方が HTTP 409 Conflict（ロック競合） | `none` |

F-6 並行リクエストコマンド（API キーをファイル経由で共有し、バックグラウンドプロセスへの変数伝搬問題を回避する）:

```bash
curl -s -w "\nBG: %{http_code}\n" -X POST http://localhost:<RAG_HTTP_PORT>/upload/document \
  -H "X-API-Key: $(cat /tmp/qa_api_key.txt)" -F "file=@.qa/upload_doc_test.md" -F "upload_mode=replace" \
  > /tmp/upload_bg.txt 2>&1 &
bg_pid=$!
curl -s -w "\nFG: %{http_code}\n" -X POST http://localhost:<RAG_HTTP_PORT>/upload/document \
  -H "X-API-Key: $(cat /tmp/qa_api_key.txt)" -F "file=@.qa/upload_doc_test.md" -F "upload_mode=replace"
wait "$bg_pid"
cat /tmp/upload_bg.txt
```

#### グループ片付け

1. テンポラリファイルを削除する: `rm -f /tmp/qa_api_key.txt /tmp/upload_bg.txt`

### G) Eval

目的: 評価パイプラインの動作確認。評価フィクスチャが必要。

前提: テストドキュメントフィクスチャ（JSON）と評価データセット（JSON）がローカルに存在すること。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `init-test-db --chunk-size 200 --chunk-overlap 30 --bm25-k1 1.5 --bm25-b 0.75 --persist-dir .tmp/eval_chroma_db --bm25-persist-dir .tmp/eval_bm25_index --fixture <フィクスチャパス>` | ChromaDB・BM25 ディレクトリが作成される | `none` |
| 2 | `evaluate --chunk-size 200 --chunk-overlap 30 --bm25-k1 1.5 --bm25-b 0.75 --vector-weight 0.6 --persist-dir .tmp/eval_chroma_db --output-dir .tmp/rag-evaluation --fixture <フィクスチャパス> --dataset <データセットパス>` | レポートファイル（report.json, report.md）が生成される | `none` |

### H) Core

目的: パイプライン基盤（統計・一覧・検索・全文取得・削除・再構築）の動作確認。A〜G の取り込みデータを使用する。

前提: H 単独実行時は、先にテスト用 Markdown を add-document で取り込んでからテストする。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `stats` | 統計情報が表示される | `none` |
| 2 | `list-recent --source-type local` | 取り込み済み local ソースが一覧に表示される | `none` |
| 3 | `list-recent --source-type journal` | 取り込み済み journal ソースが一覧に表示される | `none` |
| 4 | `search --query <取り込み内容に関連するワード>` | ベクトル検索・BM25 の両方で結果が返る | `none` |
| 5a | `get-document <source_id> --format text`（`source_id` は positional 引数） | A) の README.md の全文がテキスト形式で取得できる | `none` |
| 5b | `get-document <source_id> --format original` | A) の README.md の全文がオリジナル形式で取得できる | `none` |
| 6 | `delete <source_id>`（`source_id` は positional 引数） | A) の README.md が削除される | `delete` |
| 7 | `rebuild --mode incremental` | 差分再構築が成功する | `none` |
| 7a | `rebuild --mode convert` | コンバートのみ再実行が成功する | `none` |
| 7b | `rebuild --mode index` | インデックスのみ再構築が成功する | `none` |
| 7c | `rebuild --mode full` | 全再構築が成功する | `none` |
| 8 | `stats` | 再構築後の統計が更新されている | `none` |

CLI / MCP 対応: `rag_stats` / `rag_list_recent` / `rag_search` / `rag_get_document` / `rag_delete` / `rag_rebuild`

`rag_rebuild` の mode 指定: `rag_rebuild(mode="incremental")` / `rag_rebuild(mode="convert")` / `rag_rebuild(mode="index")` / `rag_rebuild(mode="full")`

### J) Log（ログファイル出力、MCP 固定）

目的: MCP サーバーのログファイル出力（`SessionRotatingFileHandler`）が想定どおり動作していることの検証。サーバー起動ログ・MCP ツール呼び出しログ・CLI 転送ログの 3 経路が同一ファイルに記録されることを確認する。

対象実装: `py_common_lib.logging.SessionRotatingFileHandler`（py-common-lib 提供）、`src/rag/server.py` の `_write_cli_lines_to_handlers` およびサーバーフォーマッタ。

前提:

- MCP サーバーが HTTP モードで起動中であること（worktree セットアップで起動済み）
- `.env` の `RAG_LOG_DIR` が worktree 内の絶対パスを指していること（未設定の場合はデフォルトパスを使用）

CLI フェーズではサーバー側のログファイルは生成されない（CLI 直接実行経路はファイルハンドラを経由しない）ため、本グループは MCP フェーズでのみ実行する。CLI フェーズが選択されている場合は全ステップをスキップする。

| # | コマンド | 期待結果 | 検証種別 |
|---|---------|---------|---------|
| 1 | ログディレクトリ内のセッションログファイル一覧を表示する | サーバー起動後に `rag-server-YYYYMMDD-HHMMSS-00001.log` が存在する | `none` |
| 2 | セッションログファイル内で `[MCP]` プレフィックス付きの `Starting MCP server` 行を確認する | 起動ログが記録されている | `none` |
| 3 | MCP ツール `rag_stats` を呼び出した後、ログファイルに `[MCP]` と `[CLI]` の両方のプレフィックス行が記録されていることを確認する | サーバーログと CLI サブプロセス stderr 由来のログが同一ファイルに混在して記録される | `none` |
| 4 | サーバーログの各行が `[MCP] YYYY-MM-DD HH:MM:SS,mmm - logger.name - LEVEL - message` 形式であることを確認する | サーバー側フォーマッタが正しく適用されている | `none` |
| 5 | CLI 転送ログの各行が `[CLI] YYYY-MM-DD HH:MM:SS,mmm - logger.name - LEVEL - message` 形式であることを確認する（CLI 側のタイムスタンプをそのままパススルーしており、二重のタイムスタンプにならない） | CLI 転送経路のフォーマッタ差し替え（`_write_cli_lines_to_handlers`）が正しく動作している | `none` |
| 6 | ローリング検証: MCP サーバーを停止し、`config.toml` の `rag_log_file_max_bytes` を `5000` に一時変更する。ログディレクトリ内の既存 `rag-server-*.log` を削除したうえで MCP サーバーを再起動する（`/mcp` reconnect が必要）。`rag_stats` を 4〜5 回呼び出し、ログディレクトリに `00002.log` が作成され `00001.log` が保持されていることを確認する。検証後 `config.toml` を元の値に復元し、MCP サーバーを再起動する | `SessionRotatingFileHandler` のサイズ超過時ファイルローリングが正しく動作している | `none` |

補足:

- ステップ 3 の検証ツールに `rag_stats` を用いるのは、サーバー側の副作用を発生させずに CLI サブプロセス経路（`_run_cli_subprocess` → `_write_cli_lines_to_handlers`）を通すため。取り込み系ツールは副作用が残るため非推奨
- 対象ログファイルは「セッション最新」の `rag-server-*-00001.log`（ロールオーバー発生時は最大連番のファイル）を使用する

### ステップ 7: クリーンアップ

QA 完了後、worktree 環境を片付ける。ChromaDB・HTTP サーバー等のプロセスがファイルをロックしているため、先にプロセスを停止する必要がある。

1. worktree パスを参照する全プロセスを特定・停止する（`wmic process where "CommandLine like '%<worktree-path>%'" get ProcessId,CommandLine` で特定し、`taskkill //PID <pid> //T //F` でプロセスツリーごと停止）
2. worktree 内の未コミット変更を確認する（`git -C "<worktree-path>" status --porcelain`）。未コミット変更がある場合はユーザーに警告し、削除の確認を取る。ユーザーが拒否した場合は worktree の削除をスキップする
3. worktree ディレクトリを削除する: `git worktree remove "<worktree-path>"`。未コミット変更ありでユーザーが削除を承認した場合のみ `--force` を使用する。未確認の状態で `--force` を使用してはならない
4. worktree の参照をクリーンアップする: `git worktree prune`

## 入出力

### 入力

- ユーザーによるグループ選択（A〜J、all）
- ユーザーによるインターフェース選択（CLI / MCP / both）
- YouTube 実行時のユーザー確認（y/n）

### 出力

- 各ステップの実行コマンド・パラメータ表示
- 各ステップの実行結果と検証結果
- 全グループ完了後の結果サマリー（OK/NG テーブル）
- NG 検出時の Issue 起票提案

## 関連ドキュメント

- [QA 戦略](../../workflows/qa-strategy.md) — 3 レイヤー設計（L1/L2/L3）と実施頻度ポリシー（L3 実施タイミング基準の SSoT）
- [QA 実行スキル](qa-execute-skill.md) — 各検証ステップの実行制御
- 品質ゲート（`~/.claude/rules/quality-gate.md`）— QA フェーズの位置づけ
- 仕様駆動開発（`~/.claude/rules/spec-driven.md`）— QA フェーズの原則
- [RAG Knowledge 全体仕様](../../overview.md) — 機能一覧
- [RAG ナレッジ](../../rag-knowledge.md) — ChromaDB client/server 構成、MCP 薄層アダプターパターン
- [content-upload](../../infrastructure/content-upload.md) — Upload HTTP API 仕様
- [content-listing](../../infrastructure/content-listing.md) — コンテンツ一覧取得仕様
- [メディア解析](../../infrastructure/media-analysis.md) — 画像・動画の Vision モデルによるテキスト変換
- [BlueSky インジェスター](../../ingesters/bluesky.md) — メディア DL・--force オプション

---
name: qa
description: MCP・CLI・HTTP API の動作確認（グループ選択式）
user-invocable: true
allowed-tools: Bash, Read, Grep, Glob, AskUserQuestion
argument-hint: ""
---

## タスク

マージ後の動作確認スキル。MCP ツール・CLI コマンド・HTTP API の機能を実際に実行し、正常動作を検証する。仕様書: `docs/specs/agentic/skills/qa-skill.md`

各検証ステップの実行は `/qa-execute` スキル（`.claude/skills/qa-execute/SKILL.md`）に委譲する。プロダクトコマンドの直接実行・共通検証フローの省略は禁止（ホワイトリストに定義された環境確認・準備・片付けは例外）。

## 処理手順

### 1. グループ選択

以下を表示し、ユーザーに検証対象を問い合わせる:

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
  I) Obs      — 観測性（構造化エラー情報・exit code 体系の検証）
  J) Log      — ログファイル出力の検証（MCP フェーズのみ）

どのグループを検証しますか？（例: A B D, all）
```

### 2. インターフェース選択

```
インターフェースを選択してください:
  1) CLI のみ
  2) MCP のみ
  3) both（CLI + MCP）

※ Upload(F) は HTTP 固定、Eval(G) は CLI 固定です
```

### 3. worktree セットアップ

本番ストレージに影響を与えないよう、worktree 環境で QA を実行する。

1. ベースブランチ（develop）を最新に更新する
2. worktree を作成する: `git worktree add -b qa/qa-skill-<Issue番号> <worktree-path> develop`
   - 配置: リポジトリの親ディレクトリ、命名: `<リポジトリ名>-wt-<Issue番号>`
3. メインリポジトリから `.env` をコピーする
4. ストレージパスは相対パスのため worktree ディレクトリ基準で解決される（書き換え不要）。以下を確認・設定する:
   - `CHROMADB_AUTO_START=false`（worktree では手動起動するため。`true` のままだと MCP サーバー起動時に競合する可能性がある）
   - ポート競合時（前セッションの停止漏れ等）は `CHROMADB_SERVER_PORT` を変更すること
5. LM Studio の接続先を確認し、必要に応じて `localhost` に変更する
6. `.tmp` ディレクトリを作成する
7. メインリポジトリから `.qa/` ディレクトリをコピーする（`.qa/` は `.gitignore` 対象のため worktree に含まれない）
8. ChromaDB サーバーを手動起動する: `uv run chroma run --path <persist_dir> --port <CHROMADB_SERVER_PORT>`（初回は venv 構築のため起動に時間がかかる。目安: 10〜20 秒。heartbeat 確認前に十分待機すること）
9. MCP フェーズを含む場合、worktree で MCP サーバーを HTTP モードで起動する: `cd <worktree-path> && uv run python -m rag.server &`。メインリポジトリの MCP サーバーが起動中の場合はポート競合するため先に停止すること。`.mcp.json` は `url` ベース（`http://localhost:<RAG_HTTP_PORT>/mcp`）のため変更不要

以降の全コマンドは worktree ディレクトリで実行する。

### 4. 環境確認

- 現在のブランチ・作業ディレクトリを表示
- MCP サーバーの状態確認（CLI 検証時は停止推奨、MCP 検証時は HTTP モードで起動中かつ `/mcp` で enabled であることを確認）。disabled の場合はユーザーに有効化を依頼する
- ChromaDB サーバー疎通確認: `curl http://localhost:<CHROMADB_SERVER_PORT>/api/v2/heartbeat` で応答を確認
- LM Studio の接続確認（Embedding API が必要なグループの場合）
- LM Studio Vision モデルの確認（グループ A でメディア解析ステップを実行する場合）: `curl http://localhost:1234/v1/models` で Vision 対応モデルがロードされているか確認する
- ffmpeg の確認（メディア解析の動画処理を検証する場合）: `ffmpeg -version` で利用可能か確認する
- 問題があればユーザーに報告し、解決してから続行

### 5. グループ実行

選択されたグループを ID 順（A→J）に実行する。

**ステップ実行ループ:**

グループ内の各ステップは以下のループで逐次実行する:

1. ステップテーブルから次の 1 行を取得する
2. その 1 行の情報（コマンド・期待結果・検証種別）を `/qa-execute` に渡して呼び出す
3. qa-execute から結果（OK / NG / SKIP / STOP）が返るまで待機する。結果が返る前に次のステップに進んではならない
4. 結果を記録し、次の行に進む（STOP の場合は当該グループのループを中断し、残りのステップ・グループをすべてスキップして結果サマリーへ進む）

**同一ターン制約:** 次のステップが残っている場合に限り、qa-execute から結果（OK/NG/SKIP）が返ったそのレスポンス内で、即座に次の `/qa-execute` の Skill 呼び出しを行うこと。
STOP が返った場合、または最終ステップが完了した場合は、次の `/qa-execute` を呼び出してはならず、テキスト出力でターンを終了してよい。
qa-execute の step 6 が継続/中止の判断を完結させているため、qa スキル側で追加のユーザー確認やテキスト出力を単独で行ってはならない
（テキスト出力のみのレスポンスはターンを終了させ、ユーザー入力待ちになるため）。
結果の記録が必要な場合は、次のステップへ継続する際に限り、次の Skill 呼び出しと同一レスポンスに含める。

qa スキル自身はステップ間の制御（次ステップへの遷移、STOP 時の QA 全体の中断と結果サマリーへの遷移、結果の蓄積）のみを担う。各ステップにおけるコマンド実行・検証・ユーザー確認は全て qa-execute 側で完結する（グループ開始前のユーザー確認など、ステップ外のグループレベル操作のみ qa 側で実施する）。

**qa スキルが直接実行してよい操作（ホワイトリスト）:**

以下のみ qa-execute を経由せず直接実行できる。それ以外（プロダクトコマンド: 取り込み・検索・削除・再構築等）は全て qa-execute 経由:

- 環境確認: `git branch --show-current`, `curl http://localhost:<CHROMADB_SERVER_PORT>/api/v2/heartbeat`, `curl http://localhost:<LM_STUDIO_PORT>/v1/models` 等の読み取り専用コマンド
- グループ準備・片付け: `.env` 変更、サーバー起動・停止、worktree セットアップ・クリーンアップ
- YouTube 事前確認: グループ D 実行前の AskUserQuestion

インターフェース選択に応じた実行方針:

- **CLI のみ**: MCP サーバーが停止していることを確認し、全グループを CLI で実行
- **MCP のみ**: MCP サーバーが HTTP モードで起動中であることを確認し、全グループを MCP で実行
- **both**: 2フェーズで実行
  1. CLI フェーズ: 全グループを CLI で実行
  2. MCP フェーズ: MCP サーバーが HTTP モードで起動中であることを確認 → 全グループを MCP で実行

**全ステップ共通ルール:**

- 外部 API を使用するグループでは取り込み件数を最小限に制限する（検証リソースの定義値に従う）
- 取り込み系ステップは検証種別 `ingest`、削除系ステップは検証種別 `delete`、それ以外は `none` を指定する

**YouTube（D）選択時の必須確認:**

グループ D の実行直前に以下を表示し、ユーザー確認を取る。確認なしに実行してはならない（qa-execute の都度確認とは別に、グループレベルで事前確認する）:

```
YouTube 検証を実行します。
YouTube API へのアクセスにより IP ブロックのリスクがあります。
実行しますか？ (y/n)
```

**MCP フェーズでの追加確認:**

MCP フェーズで書き込み系ツール（取り込み・削除・再構築）を実行する際、以下を追加で確認する:

- MCP ツールの実行結果が正常に返されること（CLI サブプロセス経由の JSON パースが成功していることの間接確認）
- エラー発生時にエラーメッセージが適切に返されること

### 6. 結果サマリー

全グループ完了後（STOP による中止を含む）に結果テーブルを表示する。

**NG 扱い基準:**

以下に該当するステップは NG として集計する:

- qa-execute から NG として返されたステップ
- 検証フロー（検証種別 `ingest` / `delete`）を省略・簡略化して実行したステップ
- 期待結果と異なる条件で再検証して OK としたステップ

ユーザーが明示的に OK と判断した場合のみ例外（結果テーブルに「OK（ユーザー判断）」と記載）。

**表示形式:**

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

NG を検出した場合、Issue 起票を提案する。

## 検証リソース

各グループで使用するテストデータ・URL・アカウントを以下に定義する。実行者が毎回判断する余地をなくし、再現性を担保する。

`.qa/` ディレクトリはテストリソースの置き場（git 管理外）。以下のファイルを事前に配置しておくこと:

- `.qa/pdf_add_test.pdf` — PDF 取り込みテスト用
- `.qa/image_add_test.jpg` — 画像取り込みテスト用（メディア解析検証）
- `.qa/video_add_test.mp4` — 動画取り込みテスト用（メディア解析検証、ffmpeg 必要）
- `.qa/journal_add_test.md` — add-journal テスト用
- `.qa/journal_upload_test.md` — Upload journal テスト用
- `.qa/upload_doc_test.md` — Upload document テスト用
- `.qa/journals/` — migrate-journal テスト用（ジャーナル 10 件）

| グループ | リソース | 値 |
|---------|---------|-----|
| A) Local | add-document（Markdown） | リポジトリの `README.md` |
| A) Local | add-document（PDF） | `.qa/pdf_add_test.pdf` |
| A) Local | add-document（画像） | `.qa/image_add_test.jpg`（メディア解析検証。LM Studio Vision が必要） |
| A) Local | add-document（動画） | `.qa/video_add_test.mp4`（メディア解析検証。LM Studio Vision + ffmpeg が必要） |
| A) Local | add-document（上書き） | `README.md` を再取り込み（CLI: `--upload-mode replace` / MCP: `upload_mode="replace"`） |
| A) Local | crawl-documents | リポジトリの `docs/specs/` ディレクトリ全体 |
| A) Local | add-journal | `.qa/journal_add_test.md`（`--title "コンテンツ一覧取得機能の実装"` `--repository rag-knowledge`） |
| A) Local | migrate-journal | `.qa/journals/`（古いジャーナル 10 件、`--repository rag-knowledge`） |
| B) Web | site-ingest（クロール） | `https://www.stat.go.jp/`（`--max-pages 20`） |
| B) Web | site-ingest（複数URL） | `https://www.stat.go.jp/data/jinsui/` と `https://www.stat.go.jp/data/roudou/` |
| C) SNS | Zenn ユーザー | `rhythmcan` |
| C) SNS | BlueSky ハンドル | `rhythmcan.bsky.social` |
| C) SNS | BlueSky --max-posts | `5`（メディア付き投稿を含むため増加） |
| C) SNS | BlueSky --force | C-2 の後に `--max-posts 1 --force` で上書き再取得 |
| C) SNS | BlueSky 単一投稿 URL（Web リンク付き） | `rhythmcan.bsky.social` の最近の投稿で外部リンク（リンクカードまたは facets）を含むものを 1 件選定する。実行時に `crawl-bluesky` 結果から `has_external_link: true` の投稿を確認 |
| D) YouTube | 動画 URL | `https://www.youtube.com/watch?v=GuFBDpzH3ck` |
| D) YouTube | プレイリスト URL | `https://www.youtube.com/playlist?list=PLaFZvPBpvhKKgHIDI16ja0jwEG_vIH55K`（`--max-videos 1`） |
| E) Aozora | 著者検索キーワード | `太宰`（「太宰 治」にマッチ） |
| E) Aozora | person_id | `000035`（太宰治） |
| E) Aozora | book_id | `001567`（走れメロス） |
| F) Upload | document | `.qa/upload_doc_test.md`（Upload 専用テストファイル） |
| F) Upload | journal | `.qa/journal_upload_test.md`（`title: "QA スキルの仕様書・スキル定義作成"` `repository: rag-knowledge`） |
| G) Eval | フィクスチャ | メモリ `reference_eval_fixtures.md` を参照 |
| G) Eval | init-test-db パラメータ | `--chunk-size 200 --chunk-overlap 30 --bm25-k1 1.5 --bm25-b 0.75` |
| G) Eval | evaluate パラメータ | 上記 + `--vector-weight 0.6` |
| H) Core | search クエリ | 直前に取り込んだ内容に関連するワード（固定値なし） |
| H) Core | get-document / delete 対象 | A) で取り込んだ README.md の source_id |
| J) Log | 検証対象 MCP ツール | `rag_stats`（副作用なしで CLI サブプロセス経路を通すため） |
| J) Log | 対象ログディレクトリ | `.env` の `RAG_LOG_DIR` 配下（`rag-server-*.log`） |

## グループ別の検証内容

仕様書 `docs/specs/agentic/skills/qa-skill.md` の「グループ別検証手順」セクションに従う。

各ステップは qa-execute に委譲する。以下のテーブルの各行が 1 回の qa-execute 呼び出しに対応する。

### A) Local

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `add-document --file README.md` | Markdown 取り込み成功 | `ingest` |
| 2 | `add-document --file .qa/pdf_add_test.pdf` | PDF 取り込み成功 | `ingest` |
| 3 | LM Studio を停止した状態で `add-document --file .qa/image_add_test.jpg` | 取り込み成功するがメディア解析テキストなし（フォールバック動作）。エラーで中断しないこと | `ingest` |
| 4 | LM Studio を起動した状態で `add-document --file .qa/image_add_test.jpg --upload-mode replace` | 画像取り込み成功。メディア解析テキストが生成されること。search 結果に画像の解析テキストが含まれることを確認する | `ingest` |
| 5 | `add-document --file .qa/video_add_test.mp4` | 動画取り込み成功。ffmpeg フレーム抽出 → Vision モデルで各フレーム解析 → タイムスタンプ付きテキスト生成。search 結果に動画の解析テキストが含まれること | `ingest` |
| 6 | `add-document --file README.md --upload-mode replace` | 上書き成功、エラーなし | `none` |
| 7 | `crawl-documents docs/specs/`（`dir_path` は positional 引数） | ディレクトリ一括取り込み成功 | `ingest` |
| 8 | `add-journal --title "コンテンツ一覧取得機能の実装" --file .qa/journal_add_test.md --repository rag-knowledge` | ジャーナル登録成功 | `ingest` |
| 9a | `migrate-journal --dir .qa/journals --repository rag-knowledge` | ジャーナル一括配置成功 | `none` |
| 9b | `rebuild --mode incremental` | 再構築成功、migrate 分がインデックスに反映 | `ingest` |

MCP 対応コマンド:

| CLI コマンド | MCP ツール |
|------------|-----------|
| `add-document --file <path>` | `rag_add_document` |
| `crawl-documents <dir>` | `rag_crawl_documents`（HTTP モード非対応、CLI のみ） |
| `add-journal --title <t> --file <f> --repository <r>` | `rag_add_journal` |
| `migrate-journal --dir <d> --repository <r>` | （CLI のみ） |

**MCP テスト時の注意:**

- A-2 (PDF): 大きい PDF は MCP パラメータサイズ制約で失敗する場合がある。失敗時は CLI で代替実行する
- A-3 (フォールバック): LM Studio の停止・再起動はユーザーの手動操作が必要。MCP テスト時はスキップする
- A-4 (画像), A-5 (動画): LM Studio Vision モデルが未ロードの場合はスキップする
- A-7 (crawl-documents): HTTP モード非対応のため MCP テスト時はスキップする

### B) Web

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `site-ingest https://www.stat.go.jp/ --max-pages 20` | Scrapy クロールモード一括取り込み成功 | `ingest` |
| 2 | `site-ingest https://www.stat.go.jp/data/jinsui/ https://www.stat.go.jp/data/roudou/` | 複数 URL モードで 2 件取得成功（クロールなし） | `ingest` |

MCP 対応: `rag_site_ingest`（`url` パラメータ / `urls` パラメータ）

### C) SNS

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `crawl-zenn rhythmcan --max-articles 1` | Zenn 記事 1 件取り込み成功 | `ingest` |
| 2 | `crawl-bluesky rhythmcan.bsky.social --max-posts 5` | BlueSky 投稿取り込み成功。メディア付き投稿がある場合、source_store の `bluesky/{did}/{year}/{month}/media/{rkey}/` にメディアファイル（`image_0.{ext}` / `video_0.ts`）が配置されていること。投稿内に外部リンクを含む投稿がある場合、site_ingest 経由で URL 先が取得され `URL 自動取り込み: Web N件` が表示されること | `ingest` |
| 3 | `crawl-bluesky rhythmcan.bsky.social --max-posts 1 --force` | `--force` による上書き再取得成功。既存投稿が上書きされ、CLI に `完了: N件配置`（N > 0）と表示されること | `ingest` |
| 4 | `ingest-bluesky <Web リンク付き投稿 URL>` | 投稿 1 件配置 + `URL 自動取り込み: Web N件` が表示される。site_ingest が呼ばれ、URL 先 Web ページが source_store に配置されること | `ingest` |
| 5 | 同 URL で `ingest-bluesky <同じ Web リンク付き投稿 URL>` を再実行 | 投稿 1 件上書き（`overwritten=1`）。`URL 自動取り込み: Web N件` が再度表示され、site_ingest Bridge が URL 先 Web ページを上書き配置することを確認（ピンポイント修復シナリオ） | `ingest` |

MCP 対応: `rag_crawl_zenn` / `rag_crawl_bluesky`（`force=True` で --force 相当） / `rag_add_bluesky`（C-4/C-5 用、`urls` パラメータに投稿 URL リストを渡す）

**MCP テスト時の追加確認:**

- C-4 / C-5: MCP 応答テキストに `URL 自動取り込み: Web N件` のフォーマット文字列が含まれること（`_format_ingest_response` の url_follow 反映を確認）

### D) YouTube

グループ実行前にユーザー確認（必須）を行った上で、各ステップを qa-execute に委譲する。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `ingest-youtube https://www.youtube.com/watch?v=GuFBDpzH3ck` | 動画 1 件取り込み成功 | `ingest` |
| 2 | `ingest-youtube-playlist https://www.youtube.com/playlist?list=PLaFZvPBpvhKKgHIDI16ja0jwEG_vIH55K --max-videos 1` | プレイリストから 1 件取り込み成功 | `ingest` |

MCP 対応: `rag_add_youtube` / `rag_crawl_youtube`

### E) Aozora

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `update-aozora-catalog` | カタログ CSV が source_store に配置される | `none` |
| 2 | `search-aozora --author 太宰 --limit 300` | 結果に `001567`（走れメロス）が含まれる | `none` |
| 3 | `ingest-aozora 001567` | 作品 1 件取り込み成功 | `ingest` |

MCP 対応: `rag_update_aozora_catalog` / `rag_search_aozora` / `rag_add_aozora`

### F) Upload（HTTP モード固定）

#### グループ準備

MCP サーバーが HTTP モードで起動中であること（worktree セットアップで起動済み）。

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

### G) Eval（CLI 固定）

評価フィクスチャのパスはメモリ `reference_eval_fixtures.md` を参照（メモリが利用できない場合はユーザーにパスを確認する）:

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `init-test-db --chunk-size 200 --chunk-overlap 30 --bm25-k1 1.5 --bm25-b 0.75 --persist-dir .tmp/eval_chroma_db --bm25-persist-dir .tmp/eval_bm25_index --fixture <フィクスチャパス>` | ChromaDB・BM25 ディレクトリが作成される | `none` |
| 2 | `evaluate --chunk-size 200 --chunk-overlap 30 --bm25-k1 1.5 --bm25-b 0.75 --vector-weight 0.6 --persist-dir .tmp/eval_chroma_db --output-dir .tmp/rag-evaluation --fixture <フィクスチャパス> --dataset <データセットパス>` | レポートファイル（report.json, report.md）が生成される | `none` |

### H) Core

A〜G の取り込みデータを使ってパイプライン基盤を検証する。H 単独実行時は、先に `README.md` を add-document で取り込んでからステップ 2 以降を実行する。

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | `stats` | 統計情報が表示される | `none` |
| 2 | `list-recent --source-type local` | 取り込み済み local ソースが一覧に表示される | `none` |
| 3 | `list-recent --source-type journal` | 取り込み済み journal ソースが一覧に表示される | `none` |
| 3a | `list-recent --source-type journal --filters repository=rag-knowledge` | journal がリポジトリ名で絞り込まれる | `none` |
| 4 | `search --query <取り込み内容に関連するワード>` | ベクトル検索・BM25 の両方で結果が返る | `none` |
| 5a | `get-document <source_id> --format text`（`source_id` は positional 引数） | A) の README.md の全文がテキスト形式で取得できる | `none` |
| 5b | `get-document <source_id> --format original` | A) の README.md の全文がオリジナル形式で取得できる | `none` |
| 6 | `delete <source_id>`（`source_id` は positional 引数） | A) の README.md が削除される | `delete` |
| 7 | `rebuild --mode incremental` | 差分再構築が成功する | `none` |
| 7a | `rebuild --mode convert` | コンバートのみ再実行が成功する | `none` |
| 7b | `rebuild --mode index` | インデックスのみ再構築が成功する | `none` |
| 7c | `rebuild --mode full` | 全再構築が成功する | `none` |
| 8 | `stats` | 再構築後の統計が更新されている | `none` |

MCP 対応: `rag_stats` / `rag_list_recent` (+ filters) / `rag_search` / `rag_get_document` / `rag_delete` / `rag_rebuild`

`rag_rebuild` の mode 指定: `rag_rebuild(mode="incremental")` / `rag_rebuild(mode="convert")` / `rag_rebuild(mode="index")` / `rag_rebuild(mode="full")`

> MCP `rag_list_recent` の filters 確認: `rag_list_recent(source_type="journal", filters="repository=rag-knowledge")` で journal がリポジトリ名で絞り込まれること。

### I) Obs（観測性）

PR #596 の観測性機能（構造化エラー情報）が production 出力経路（CLI JSON / MCP レスポンス）に届いていること、および #605 で確定した CLI exit code 体系（2 値）・MCP 応答契約（result line 優先）が正しく動作することを検証する。

仕様:

- [docs/specs/pipeline-controller.md](../../../docs/specs/pipeline-controller.md) の `PipelineSummary.errors`
- [docs/specs/rebuild-stats.md](../../../docs/specs/rebuild-stats.md) の「CLI exit code 体系」「MCP 応答契約」

#### グループ準備

1. 事前に何らかのインジェストを実行し、source_store を非空にしておく（グループ A や C の実行後を想定）
2. 壊れた JSON を投入するための `source_store/zenn` ディレクトリが存在することを確認する

| # | コマンド（CLI） | 期待結果 | 検証種別 |
|---|----------------|---------|---------|
| 1 | 壊れた JSON を `<SOURCE_STORE_DIR>/zenn/obs_test/articles/broken.json` に配置（`echo '<!DOCTYPE html>' > …`）し、source_store で `git add -A && git commit` | 壊れた JSON の配置・コミットが成功 | `none` |
| 2 | `rebuild --mode incremental --output json` を実行し、最終 JSON 行を `tail -1 \| jq '.errors'` で確認 | JSON 出力の `errors` が構造化 dict のリストで、`path` / `size_bytes` / `message` / `phase` を含む | `none` |
| 3 | 直前コマンドの `echo $?` を実行 | **exit code = 0**（workload で `errors > 0` であっても CLI は完走扱い。2 値契約に従う） | `none` |
| 4 | 壊れた JSON を削除し source_store で `git add -A && git commit` | 片付け成功 | `none` |
| 5 | バリデーション失敗コマンドを実行: `rebuild --mode incremental --source-type web --output json`（`_output_error` 経路を通る組合せ違反） | stdout に `{"type": "error", "message": "incremental モードでは source_type を指定できません"}` が出力される | `none` |
| 6 | 直前コマンドの `echo $?` を実行 | **exit code = 1**（2 値契約に従う。argparse バリデーション失敗も `_JsonAwareArgumentParser` で exit 1 に統一される） | `none` |

MCP 対応:

MCP 経路では `echo $?` を直接検証できないため、代替として以下を確認する:

- ステップ 2: `rag_rebuild(mode="incremental")` のレスポンステキストに `path`・`size_bytes`・`message`・`phase` が含まれることで確認する（CLI JSON → MCP テキスト変換経路の動作確認）
- ステップ 3 の代替: MCP 経路では `_run_cli_subprocess` が result 行を優先してパースするため、workload の `errors > 0` でも MCP クライアントは構造化サマリを受け取る（例外として失敗しない）。ステップ 2 でサマリが正しく返れば exit code 契約の動作も保証される
- ステップ 5 の代替: `rag_rebuild(mode="incremental", source_type="web")` が `CLISubprocessError` として伝播し、エラーメッセージが MCP レスポンスに含まれる

### J) Log（ログファイル出力、MCP フェーズのみ）

MCP サーバーのログファイル出力（`SessionRotatingFileHandler`）が想定どおり動作していることを検証する。サーバー起動ログ・MCP ツール呼び出しログ・CLI 転送ログの 3 経路が同一ファイルに記録されることを確認する。

仕様:

- [docs/specs/rag-knowledge.md](../../../docs/specs/rag-knowledge.md) の「MCP サーバーのロガー設定 / ログファイル出力」
- 実装: `py_common_lib.logging.SessionRotatingFileHandler`（py-common-lib 提供）/ `src/rag/server.py` の `_write_cli_lines_to_handlers`

#### グループ準備

1. `.env` の `RAG_LOG_DIR` を確認する（未設定の場合はデフォルトパスを使用）
2. MCP サーバーが HTTP モードで起動中であること（worktree セットアップで起動済み）

| # | コマンド | 期待結果 | 検証種別 |
|---|---------|---------|---------|
| 1 | `ls -1 <RAG_LOG_DIR>/rag-server-*.log \| tail -1` | サーバー起動後にログファイル（`rag-server-YYYYMMDD-HHMMSS-00001.log`）が存在する | `none` |
| 2 | 直前のログファイルに対し `grep -E "^\[MCP\].*Starting MCP server" <logfile>` | `[MCP]` プレフィックス付きで `Starting MCP server: transport=http` の行がマッチする | `none` |
| 3 | MCP ツール `rag_stats` を呼び出し、その後ログファイルを再確認（`cat <logfile>`） | `[MCP]` プレフィックスのサーバーログと、CLI サブプロセス stderr 由来の `[CLI]` プレフィックス行の両方がファイルに記録されている | `none` |
| 4 | `grep -E "^\[MCP\] [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} - [^ ]+ - (INFO\|DEBUG\|WARNING\|ERROR) - " <logfile>` | サーバーログが `[MCP] YYYY-MM-DD HH:MM:SS,mmm - logger.name - LEVEL - message` 形式で記録されている | `none` |
| 5 | `grep -E "^\[CLI\] [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} - [^ ]+ - (INFO\|DEBUG\|WARNING\|ERROR) - " <logfile>` | CLI 転送ログが `[CLI] YYYY-MM-DD HH:MM:SS,mmm - logger.name - LEVEL - message` 形式で記録されている（CLI 側の stderr 行をそのままパススルーしており、二重のタイムスタンプにならない） | `none` |
| 6 | ローリング検証（下記手順を参照） | 00001.log が `rag_log_file_max_bytes` を超えた時点で 00002.log にローリングが発生し、旧ファイルが保持される | `none` |

**ステップ 6 ローリング検証手順:**

1. MCP サーバーを停止する
2. `config.toml` の `rag_log_file_max_bytes` の元の値を記録し、`5000` に一時変更する
3. ログディレクトリ内の `rag-server-*.log` を削除する
4. MCP サーバーを再起動する（ユーザーに `/mcp` reconnect を依頼）
5. `rag_stats` を 4〜5 回呼び出し、ログデータを蓄積する
6. ログディレクトリを確認する:
   - `rag-server-*-00002.log` が作成されていること（ローリング発生）
   - `rag-server-*-00001.log` が削除されず保持されていること
   - 両ファイルに `[MCP]` / `[CLI]` プレフィックスのログが記録されていること
7. `config.toml` の `rag_log_file_max_bytes` を手順 2 で記録した元の値に復元する
8. MCP サーバーを停止→再起動する（復元した設定で稼働させるため。ユーザーに `/mcp` reconnect を依頼）

**注意:**

- CLI フェーズではサーバー側でログファイルを生成しない（CLI 直接実行経路はファイルハンドラを経由しない）ため、本グループは MCP フェーズでのみ実行する。CLI フェーズが選択されている場合は全ステップをスキップする
- ステップ 3 で `rag_stats` を選ぶのはサーバー副作用を発生させずに CLI サブプロセス経路（`_run_cli_subprocess` → `_write_cli_lines_to_handlers`）を通すため。他のツールでも代替可能だが、取り込み系は副作用が残るため非推奨
- 対象ログファイルは「セッション最新」の `rag-server-*-00001.log`（ロールオーバーが発生していれば最大連番のファイル）を使用する

### 7. クリーンアップ

QA 完了後、worktree 環境を片付ける。ChromaDB・HTTP サーバー等のプロセスがファイルをロックしているため、先にプロセスを停止する必要がある。

1. worktree パスを参照する全プロセスを特定・停止する:

   ```bash
   # worktree のプロセスを特定（Windows）
   wmic process where "CommandLine like '%<worktree-path>%'" get ProcessId,CommandLine
   # プロセスツリーごと停止（//T で子プロセスも含む）
   taskkill //PID <pid> //T //F
   ```

2. worktree 内の未コミット変更を確認する:

   ```bash
   git -C "<worktree-path>" status --porcelain
   ```

   - **出力が空の場合**: 未コミット変更なし。ステップ 3 に進む
   - **出力がある場合**: 未コミット変更あり。以下の警告をユーザーに表示し、確認を取る:

     ```
     worktree に未コミットの変更があります:
     <git status --porcelain の出力>

     この worktree を削除すると上記の変更は失われます。
     削除しますか？ (y/n)
     ```

     - ユーザーが `n` を選択した場合: worktree の削除をスキップし、パスを表示して手動対応を促す
     - ユーザーが `y` を選択した場合: ステップ 3 に進む（`--force` の使用を許可）

3. worktree ディレクトリを削除する:

   ```bash
   # 未コミット変更がない場合
   git worktree remove "<worktree-path>"

   # 未コミット変更ありでユーザーが削除を承認した場合のみ
   git worktree remove --force "<worktree-path>"
   ```

   `--force` はステップ 2 でユーザーが明示的に削除を承認した場合にのみ使用する。未確認の状態で `--force` を使用してはならない。

4. worktree の参照をクリーンアップする:

   ```bash
   git worktree prune
   ```

## 制約

- テストデータの作成時、実在の著作物・キャラクター情報を使用しない（`~/.claude/rules/invariants.md`）
- 各グループの実行中にエラーが発生した場合、そのステップを NG として記録し、次のステップに進む。グループ全体を中断しない
- MCP ツールの検証では、MCP サーバーが HTTP モードで起動中であることを確認してから実行する。起動していない場合はユーザーに起動を依頼する
- **MCP と CLI の共存**: HttpClient + ファイルベースロックにより MCP と CLI の同時アクセスは技術的に可能だが、QA では検証環境の単純化のため CLI 検証時は MCP サーバーを停止推奨
- **HTTP モード非対応ツール**: `crawl-documents` は HTTP モードでは実行不可（クライアントとサーバーが別マシンの可能性がありローカルパスを解決できないため）。MCP テスト時はスキップし、CLI テスト時のみ実行する
- **大きいバイナリファイルの MCP テスト制約**: MCP の `content` パラメータに大きいファイル（PDF 等）を base64 で渡す場合、クライアント側のパラメータサイズ制約により失敗することがある。MCP テスト時は小さいファイルを使用するか、スキップして CLI で代替する

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

## 処理フロー・グループ別検証手順

処理フロー（グループ選択 → インターフェース選択 → worktree セットアップ → 環境確認 → 順次実行 → 結果サマリー → クリーンアップ）の具体ステップ、
および各検証グループ（A〜J）の検証ステップテーブル（コマンド・期待結果・検証種別）の SSoT は
[.claude/skills/qa/SKILL.md](../../../../.claude/skills/qa/SKILL.md) に置く。

本仕様書では設計判断・制約・コマンド体系の概念定義のみを SSoT として保持し、実行手順・検証ステップの追加・修正は SKILL.md 側で行う。仕様書側で重複記述しない（観点 #9 — SKILL.md と仕様書の SSoT 分離）。

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

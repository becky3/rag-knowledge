# QA 戦略

## 概要

本仕様書は RAG Knowledge プロジェクトの QA を 3 レイヤー（L1 / L2 / L3）に分割し、各レイヤーの責務・実施頻度・実装手段の境界を定義する。

PR ごとの自動 regression 検出（L1 + L2）と本番相当検証（L3）の責務を分離することで、PR ごとの手動 QA 実施頻度を削減しつつ、外部 API 仕様変更などの実環境固有の問題を取り逃さない検証戦略を確立する。

スコープ:

- 3 レイヤーの定義・責務分離
- レイヤー間の境界軸（特に L2 と L3 の交換不能性）
- L2 Mock E2E の構成方針（実プロセス起動・subprocess 越境注入・テスト分類・CI 統合）
- 各レイヤーの失敗時対応・スキップ条件
- L3 実施タイミング基準（SSoT）

スコープ外:

- 個別テストの実装詳細（テストコード自体が SSoT）
- 他 source_type への L2 横展開計画（別 Issue で追跡）
- Fake Adapter 機構そのもの（[Fake モード基盤](../infrastructure/fake-mode.md) で定義）
- `/qa` スキルの操作手順（`.claude/skills/qa/SKILL.md` が SSoT）

## 背景

- 取り込み系ツール（`rag_add_*` / `rag_crawl_*`）の動作確認は PR ごとに `/qa` スキルで手動実施しており、セッションごとの QA 実施コストが大きい
- 過去に「MCP サーバー起動時のみ顕在化するバグ」（Issue #683 / #686）を見落とした事例があり、pytest プロセス内の単体テストでは subprocess 越境環境の regression を検出できないことが判明している
- [Fake モード基盤](../infrastructure/fake-mode.md) と [YouTube Fake Adapter](../infrastructure/fake-adapters/youtube.md) で `.env` + DI ファクトリ経由の Fake Adapter 注入機構が確立されている。
  これを subprocess 越境 E2E テストで再利用すれば PR ごとの手動 QA を CI 自動化できる
- 一方で、実 API の仕様変更（外部サービスのレスポンス構造変化等）は Fake Adapter では検出できないため、本番相当の検証手段は独立して保持する必要がある

## 制約

本セクションは QA 戦略全体に共通する制約を定義する。各レイヤー固有の制約・実装詳細は対応するレイヤー定義セクションまたは個別仕様書（[`qa-skill.md`](../agentic/skills/qa-skill.md) / [`fake-mode.md`](../infrastructure/fake-mode.md) 等）に記述する。

### レイヤー定義の遵守

PR ごとの regression 検出は L1 + L2 のみで担保し、L3 を PR ごとの必須工程に組み込まない。L3 は本仕様書の「L3 実施タイミング基準」に該当する場合にのみ実施する。

### Test Double 注入の経路統一

L2 における Test Double 注入は `.env`（または環境変数）+ DI ファクトリ経由のみとする。
`unittest.mock.patch.object` 等の in-process パッチは subprocess 越境では伝播しないため、L2 では使用しない。
これは [Fake モード基盤](../infrastructure/fake-mode.md) で確立された「Test Double は Fake Adapter 1 種類に統一する」原則の subprocess 越境環境への適用である。

### e2e marker の付与とデフォルト除外

L2 テストには `@pytest.mark.e2e` を全ファイルで付与する。`pyproject.toml` の `addopts` でデフォルト除外し、CI および明示的な `-m e2e` 指定でのみ実行する。理由: L2 は実プロセス起動を伴い実行時間が L1 より長いため、開発時の高速 feedback loop を保つ目的。

### Fake Adapter 自体の変更時は L3 必須

Fake Adapter（`src/rag/pipeline/ingesters/_fake/` 配下）の戻り値構造・シナリオを変更する PR では、L2 が緑であっても L3 を実施する。理由: Fake Adapter の変更は「実 API との同期がずれる」リスクを含むため、本番相当検証で実 API 構造との一致を確認する必要がある。

### L3 実施タイミング基準の SSoT

L3 の実施タイミング基準は本仕様書の「L3 実施タイミング基準」セクションを SSoT とする。`/qa` スキル定義（`.claude/skills/qa/SKILL.md`）、`docs/specs/agentic/skills/qa-skill.md`、`CLAUDE.md`、`docs/specs/overview.md` 等は本仕様書への参照リンクで関連付け、転記による二重管理を行わない。

### `RAG_TESTS_ALLOW_NETWORK` の取り扱い

`RAG_TESTS_ALLOW_NETWORK=1` は pytest プロセス内 autouse 安全網（`tests/conftest.py`）の解除フラグであり、L2 テストの subprocess 起動には影響しない。L2 で実 API アクセスを発生させたい場合は `RAG_*_FAKE_MODE=false` を環境変数として subprocess に渡すことで実現する（ただし通常運用では使用しない、本番回帰検証等の特殊用途に限る）。

## QA レイヤー定義

| Layer | 目的 | 実装手段 | 実施頻度 | プロセス境界 |
|---|---|---|---|---|
| L1 Unit Test | 関数・クラス単位の論理検証 | pytest（デフォルト範囲）| PR ごと（CI 自動）| in-process のみ |
| L2 Mock E2E | パイプライン全体の regression 検出 | pytest + `e2e` marker + Fake Adapter | PR ごと（CI 自動）| MCP / CLI / ChromaDB の subprocess 起動 |
| L3 本番相当 QA | 実 API・実環境での動作確認 | `/qa` スキル（人間による実施）| 実施タイミング基準に該当時のみ | 本番相当の全プロセス |

### L1 Unit Test

関数単位・クラス単位の論理を検証する。in-process で完結し、外部ライブラリは [Fake モード基盤](../infrastructure/fake-mode.md) の Fake Adapter および autouse 安全網（`_RaiseOnUse`）で遮断される。

実装手段:

- `tests/` 配下の `test_*.py`（`tests/e2e/` 配下を除く）
- `pyproject.toml` の `addopts` のデフォルト範囲（`-m 'not slow and not e2e'`）で実行される
- `pytest-xdist` による並列実行

検証対象例:

- URL パース、バリデーションロジック
- チャンキングアルゴリズム
- メタデータ整形
- インジェスター本体のフロー（Fake Fetcher 注入で外部アクセスなし）

失敗時対応は [L1 失敗時](#l1-失敗時) を参照。

### L2 Mock E2E

MCP server / CLI / ChromaDB を実プロセスで起動し、外部 API・Embedding を Fake Adapter で置換した状態でパイプライン全体の regression を検出する。subprocess 越境環境固有のバグ（プロセス間通信・環境変数伝播・ファイルロック・エンコーディング等）を検出することが本レイヤーの本質的目的。

実装手段:

- `tests/e2e/` 配下の `test_*.py`
- 全ファイルに `@pytest.mark.e2e` を付与
- 通常 `pytest` 実行ではデフォルト除外、CI または `uv run pytest tests/e2e/ -m e2e` で実行

検証対象例:

- MCP ツール経由のインジェスト → search の往復
- CLI 経由のインジェスト → search の往復
- 取り込み完了応答に含まれる `[FAKE MODE: <source>]` ラベル（`<source>` は `youtube` / `embedding` 等）
- 重複検出・上書きモード（`overwritten` カウント）の subprocess 越境動作
- ファイルベースロック競合の振る舞い
- 応答テキスト中の文字化けマーカー（U+FFFD）の不在検証（subprocess 越境 encoding 違反の silent 発生検出）
- 子プロセス stderr の WARNING / ERROR / CRITICAL 行の不在検証（想定外ログレベル混入による silent regression 検出。`[FAKE MODE: ...]` 等の意図的 WARNING は allowlist で除外）

構成詳細は [L2 Mock E2E の構成](#l2-mock-e2e-の構成) を参照。失敗時対応は [L2 失敗時](#l2-失敗時) を参照。

### L3 本番相当 QA

実 LM Studio・実 ChromaDB・実外部 API（YouTube / Zenn / BlueSky 等）に接続して、本番相当の環境で動作確認を行う。L1 / L2 では検出できない外部 API 仕様変更・本番固有設定の問題を検出する。

実装手段:

- `.claude/skills/qa/SKILL.md` に従って人間が実施
- 検証項目は仕様書 `docs/specs/agentic/skills/qa-skill.md` に従う
- worktree 環境で本番ストレージから分離して実施

検証対象例:

- 実 API のレスポンス構造が現在のインジェスター実装と整合していること
- 実 LM Studio の Embedding 応答との連携
- HTTP モード固有の動作（Upload API・DNS rebinding protection 等）

実施タイミング基準は [L3 実施タイミング基準](#l3-実施タイミング基準) を参照。失敗時対応は本仕様書では定義しない（人間実施のため、[QA スキル仕様](../agentic/skills/qa-skill.md) のエラー時継続動作制約に従う）。

## レイヤー間の境界軸

### L1 と L2 の境界（プロセス境界）

L1 は in-process でのみ動作する。L2 は MCP server / CLI / ChromaDB を subprocess として起動し、プロセス境界をまたいだ通信・状態共有を検証する。

この境界が技術的に不可逆である理由:

- `unittest.mock.patch.object` 等の in-process パッチは subprocess に伝播しない
- ファイルベースロック（`fcntl` / `msvcrt`）はプロセス間の競合状態でのみ振る舞いが変わる
- 環境変数の伝播・パス解決は subprocess 起動時にのみ発生する

L1 と L2 を統合せず別レイヤーとして保持する根拠は、上記の **「プロセス境界をまたぐ振る舞い」を検証可能なのは L2 のみ** という非対称性にある。

### L2 と L3 の境界（外部 API 仕様変更検出の独立性）

L2 は Fake Adapter を使うため、実 API のレスポンス構造変化を検出できない。L3 のみが実 API に接続し、構造変化を検出可能である。

この境界が技術的に不可逆である理由:

- Fake Adapter の戻り値は実 API のレスポンス構造を **写し取った瞬間の構造** を保持しており、実 API 側の変更は Fake Adapter を更新しない限り反映されない
- Fake Adapter の更新は人間が実 API 構造を観察してコードに反映する作業であり、自動同期する仕組みは存在しない
- したがって「Fake Adapter 経由の検証で緑」と「実 API 経由で緑」は独立した命題であり、前者が後者を保証しない

L2 と L3 を統合せず別レイヤーとして保持する根拠は、上記の **「外部 API 仕様変更検出」を交換不能な独立軸** として持つ点にある。L3 を PR ごとの必須工程から外しつつ独立レイヤーとして保持する設計は、この交換不能性を踏まえた合理的な構造である。

### 検出能力の対応関係

| 検出対象 | L1 | L2 | L3 |
|---|---|---|---|
| 関数単位の論理エラー | ○ | （L1 で十分）| （L1 で十分）|
| プロセス間通信・環境変数伝播の問題 | × | ○ | ○ |
| ファイルベースロック競合の振る舞い | × | ○ | ○ |
| Fake Adapter / インジェスター本体 / DI ファクトリの regression | × | ○ | ○ |
| 外部 API のレスポンス構造変化 | × | × | ○ |
| 実 LM Studio との連携 | × | ×（Fake Embedding 使用）| ○ |
| 本番固有設定（HTTP モード認証・DNS rebinding 等）| 部分的 | 部分的 | ○ |

## L2 Mock E2E の構成

### 実プロセス起動方式

| コンポーネント | 起動方式 | テスト中の役割 |
|---|---|---|
| MCP server | subprocess（HTTP モード）| MCP ツール経路の検証 |
| CLI | subprocess（コマンドごと短命起動）| CLI 経路の検証 |
| ChromaDB | subprocess（テスト用ディレクトリ・テスト用ポート）| 永続化層の実体 |
| 外部 API（YouTube 等）| Fake Adapter（in-process / subprocess 内 DI 経由）| 実アクセス排除 |
| Embedding（LM Studio）| Fake Embedding（subprocess 内 DI 経由）| 実 LM Studio 不要化 |

ChromaDB のテスト用ディレクトリ・ポートは pytest fixture が動的に確保し、テスト終了時にクリーンアップする。本番ストレージへの干渉を防ぐ。

### subprocess 越境での Test Double 注入

[Fake モード基盤](../infrastructure/fake-mode.md) で確立された「`.env` + DI ファクトリ」の機構をそのまま流用する。pytest fixture が subprocess 起動時に以下の環境変数を渡し、子プロセス側の DI ファクトリが Fake Adapter を選択する:

| 環境変数 | 役割 |
|---|---|
| `RAG_{SOURCE_TYPE}_FAKE_MODE=true` | 各 source_type の Fake Fetcher を選択 |
| `RAG_EMBEDDING_FAKE_MODE=true` | Fake Embedding を選択 |
| `RAG_TRANSPORT=http` | MCP server を HTTP モードで起動 |
| `RAG_HTTP_PORT` | テスト用 MCP HTTP ポート（fixture が動的取得）|
| `CHROMADB_SERVER_PORT` | テスト用 ChromaDB ポート（fixture が動的取得）|
| `CHROMADB_PERSIST_DIR` | テスト用 ChromaDB ディレクトリ |

具体的な環境変数名のデフォルト値・許容範囲は pydantic Field（`src/rag/config.py`）が SSoT であり、本仕様書には設計意図のみ記述する。

設計上の本質: subprocess 越境では in-process のメモリ状態を共有できないため、Test Double 注入点は「子プロセス起動時の環境」しかありえない。`.env` + DI ファクトリは pytest 実行と production 実行の両方で同じ機構を使うことにより、Test Double を 1 種類に統一する効果も得られる。

### テスト分類とディレクトリ構造

| 配置 | 内容 | marker |
|---|---|---|
| `tests/` 直下 | L1 Unit Test | なし（または `slow`）|
| `tests/e2e/` | L2 Mock E2E | `@pytest.mark.e2e` 必須 |

`tests/e2e/conftest.py` には L2 専用の fixture（subprocess 起動・停止・環境変数伝播・ChromaDB ディレクトリ確保等）を集約する。

### CI 統合方針

既存 Quality Check workflow（`.github/workflows/quality-check.yml`）に新 job として L2 実行を追加する。既存の単体テスト・lint・型チェック job と並列実行し、いずれかが失敗した場合は workflow 全体が失敗することで PR をブロックする。

CI 実行時のコマンド: `uv run pytest tests/e2e/ -m e2e`

## L1 / L2 失敗時対応

### L1 失敗時

PR をブロックする。修正後に push する。L1 の失敗は単体ロジックの regression を意味するため、原因のソース変更を確認してから修正する。

### L2 失敗時

PR をブロックする。L2 の失敗は以下のいずれかを意味する:

- Fake Adapter / インジェスター本体 / DI ファクトリの regression
- subprocess 越境環境固有のバグ（環境変数伝播漏れ・パス解決ミス・エンコーディング等）
- pytest fixture 自体のバグ

修正方針: 失敗の subprocess ログ（stdout / stderr）と pytest 出力を確認し、上記のどれに該当するかを特定してから修正する。

## L3 実施タイミング基準

以下の区分に該当する PR では L3 を必須または推奨とする。判断に迷う場合は実施する側に倒す。

| 区分 | 実施有無 | 根拠 |
|---|---|---|
| 新インジェスター追加 / 新 source_type 追加 | 必須 | 実 API 接続・本番設定の検証が L1/L2 では行えない |
| 設定変更（`.env` / `config.toml` / ChromaDB / LM Studio 接続先 / HTTP/stdio モード切替）| 必須 | 本番相当環境固有の設定問題は L2 では検出できない |
| Fake Adapter 自体の変更 | 必須 | Fake と実 API の構造同期を確認する必要がある |
| 大幅なリファクタ（パイプライン構造変更等）| 推奨 | L2 では拾いきれない統合問題のリスクがある |
| 既存ロジックの軽微な修正で L2 が緑 | 不要 | L1 + L2 の検出能力で十分 |
| 判断に迷う | 実施する側に倒す | スキップによる本番事故のコストが実施コストを上回る |

本セクションは L3 実施判断の SSoT である。`/qa` スキル定義（`.claude/skills/qa/SKILL.md`）、[`qa-skill.md`](../agentic/skills/qa-skill.md)、`CLAUDE.md`、[`overview.md`](../overview.md)、`README.md` 等は本セクションへの参照リンクで関連付ける。

なお、agent-commons 側の品質ゲート規定（`~/.claude/rules/quality-gate.md` Phase 4「QA 実施確認」）は実装フェーズ全体の手続きを定めるものであり、本セクションの L3 実施タイミング基準とは別の判断軸である。両者は独立した基準として併存し、Phase 4 の判定で「実施する」となった場合に、本セクションの基準で L3 を実施するかどうかを決定する関係となる。

## エッジケース

| ケース | 振る舞い |
|---|---|
| Fake Adapter の戻り値構造が実 API と乖離 | L2 は緑のまま、L3 で初めて検出される。Fake Adapter 変更時は L3 必須とすることで早期検出する |
| L2 subprocess 起動失敗（ポート競合・依存プロセス未起動等）| pytest fixture が起動失敗を `RuntimeError` として伝播し、テストを fail させる。原因を切り分けて環境を整備してから再実行する |
| `RAG_TESTS_ALLOW_NETWORK=1` を設定した状態で L2 を実行 | 制約セクション「`RAG_TESTS_ALLOW_NETWORK` の取り扱い」を参照（L2 subprocess には影響しない）|
| L1 緑・L2 失敗 | subprocess 越境環境固有の問題が示唆される。pytest プロセス内では再現しない問題のため、L2 のログを優先して原因特定する |
| L1 / L2 緑・L3 失敗 | 実 API 仕様変更 or 本番固有設定の問題。Fake Adapter / 設定値の更新 PR を別途立てる |
| 既存テストのうち subprocess 越境を検証していたもの | 段階的に `tests/e2e/` へ移動し `@pytest.mark.e2e` を付与する。本仕様書の対象範囲は YouTube 1 種であり、他 source_type の移行は [Issue #698](https://github.com/becky3/rag-knowledge/issues/698) で追跡する |

## 関連ドキュメント

- [Fake モード基盤](../infrastructure/fake-mode.md) — Test Double 注入機構・autouse 安全網・QA 運用の SSoT
- [YouTube Fake Adapter](../infrastructure/fake-adapters/youtube.md) — L2 で最初に対象とする source_type の Fake Adapter 仕様
- [QA スキル仕様](../agentic/skills/qa-skill.md) — `/qa` スキルの仕様 SSoT。L3 の検証グループ・実施手順を定義
- [QA 実行スキル仕様](../agentic/skills/qa-execute-skill.md) — `/qa` の内部スキル。検証ステップごとの実行・確認制御を定義
- [`/qa` スキル定義](../../../.claude/skills/qa/SKILL.md) — L3 の操作手順
- [全体仕様概要](../overview.md) — プロジェクト全体の機能一覧
- agent-commons 品質ゲート（`~/.claude/rules/quality-gate.md`）Phase 4「QA 実施確認」 — 実装フェーズ全体の品質ゲート規定。本仕様書の L3 実施タイミング基準とは独立した判断軸として併存する
- [Issue #697（QA 頻度削減のための CI 自動 E2E 基盤整備）](https://github.com/becky3/rag-knowledge/issues/697) — 本仕様書を作成する Issue
- [Issue #683（YouTube Fake Adapter 基盤整備）](https://github.com/becky3/rag-knowledge/issues/683) — 本仕様書が再利用する基盤
- [Issue #686（二重 subprocess ロック競合修正）](https://github.com/becky3/rag-knowledge/issues/686) — 本 Issue の派生元

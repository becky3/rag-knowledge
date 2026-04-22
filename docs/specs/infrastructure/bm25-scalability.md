# BM25 スケーラビリティ

## 概要

BM25 インデックスのメモリ消費・永続化・rebuild 性能を改善し、820k チャンク規模での安定運用を可能にする。

スコープ:

- 4 辞書（`_documents`, `_doc_source_map`, `_doc_source_type_map`, `_doc_metadata_map`）の SQLite 外部化
- metadata.json 廃止と SQLite ベースの永続化への移行
- トークン化結果のキャッシュによる rebuild 高速化

スコープ外:

- bm25s ライブラリの差分更新（API が存在しないため対象外）
- BM25 の検索精度チューニング（既存仕様の範疇）
- Embedding・ChromaDB 側のスケーラビリティ

## 背景

現在の BM25 インデックス実装は全チャンクのテキスト・メタデータを Python 辞書でオンメモリ保持し、永続化時に JSON 一括シリアライズする。133k チャンク（metadata.json 873KB）では問題ないが、820k チャンク規模では以下の問題が顕在化する。

| 問題 | 現在（133k） | 見込み（820k） |
|------|-------------|---------------|
| metadata.json サイズ | 873 KB | 約 500 MB |
| テキストメモリ | 約 25 MB | 約 150 MB |
| 辞書オーバーヘッド含む合計 | 軽微 | 数百 MB |
| `_save()` JSON シリアライズ | 瞬時 | OOM リスク |
| `_rebuild_index()` トークン化 | 数秒 | 数時間 |

MCP サーバー起動時に全データをメモリにロードするため、サーバー再起動失敗として顕在化するリスクがある。

## 制約

- **bm25s は差分更新不可**: `bm25s.BM25.index()` が唯一のインデックス構築メソッドであり、全コーパスのトークン列を受け取る。add/delete 後のインデックス再構築は全件 rebuild が必須
- **rebuild 時のサービス中断なし**: rebuild 中も検索リクエストに応答する。旧インデックスを保持し、rebuild 完了後にアトミックスワップする既存方式を維持する
- **既存データの移行**: `rebuild --mode full` で新フォーマットに移行する。旧 metadata.json からのマイグレーションパスは設けない
- **BM25 の独立性**: BM25 インデックスは ChromaDB に依存しない。テキスト・メタデータの取得に ChromaDB へのアクセスを要求しない

## インターフェース

外部インターフェース（MCP ツール・CLI コマンド）の変更はない。BM25Index クラスの公開メソッドシグネチャも変更しない。

内部的な変更:

| 変更 | 内容 |
|------|------|
| 永続化形式 | metadata.json → SQLite（`bm25_store.db`）+ bm25s ネイティブファイル |
| メモリモデル | 全データオンメモリ → SQLite に委譲、検索時に必要なデータのみ取得 |
| rebuild | 全チャンクトークン化 → トークンキャッシュから読み出し、未キャッシュ分のみトークン化 |

## コンポーネント構成

### ストレージ構成

BM25 永続化ディレクトリ（`BM25_PERSIST_DIR`）の構成を以下に変更する。

変更前:

```
bm25_index/
  metadata.json      # 全辞書の JSON ダンプ
  bm25s/             # bm25s ネイティブファイル
```

変更後:

```
bm25_index/
  bm25_store.db      # SQLite（チャンクデータ + トークンキャッシュ）
  bm25s/             # bm25s ネイティブファイル
```

### SQLite スキーマ（`bm25_store.db`）

2 テーブルで構成する。

#### `chunks` テーブル

旧 4 辞書の代替。チャンクのテキスト・メタデータを格納する。

| カラム | 型 | 内容 |
|--------|---|------|
| `doc_id` | TEXT PRIMARY KEY | チャンク ID |
| `text` | TEXT NOT NULL | チャンク本文（BM25 検索入力テキスト） |
| `source_id` | TEXT NOT NULL | ソース識別子 |
| `source_type` | TEXT NOT NULL | 媒体種別 |
| `metadata` | TEXT NOT NULL DEFAULT '{}' | チャンクメタデータ（JSON） |

インデックス: `source_id`, `source_type`

#### `token_cache` テーブル

トークン化結果のキャッシュ。rebuild 時にテキスト未変更のチャンクのトークン化をスキップする。

| カラム | 型 | 内容 |
|--------|---|------|
| `doc_id` | TEXT PRIMARY KEY | チャンク ID |
| `text_hash` | TEXT NOT NULL | テキストの SHA-256 ハッシュ（先頭 16 文字） |
| `tokens` | TEXT NOT NULL | トークン化結果（JSON 配列） |

### データフロー

#### add_documents

1. SQLite の `chunks` テーブルに INSERT OR REPLACE
2. `_needs_rebuild = True` を設定
3. 遅延 save モードでなければ rebuild + bm25s 永続化

#### search

1. `_needs_rebuild` なら rebuild を実行
2. bm25s で検索し、doc_id リストを取得
3. 検索結果の doc_id に対応する `text`・`source_type`・`metadata` を SQLite からバッチ取得
4. source_type フィルタ・metadata フィルタはアプリケーション側で適用

#### delete_by_source

1. SQLite の `chunks` テーブルから `source_id` で DELETE
2. SQLite の `token_cache` テーブルから `doc_id` で DELETE（chunks の doc_id と紐付け）
3. `_needs_rebuild = True` を設定

#### rebuild（_rebuild_index）

1. SQLite の `chunks` テーブルから全 `doc_id` と `text` を取得
2. 各チャンクのテキストハッシュを算出
3. `token_cache` からハッシュが一致するトークンを取得（キャッシュヒット）
4. キャッシュミスのチャンクのみ `tokenize_japanese()` を実行
5. 新規トークン化結果を `token_cache` に INSERT OR REPLACE
6. 全トークン列を `bm25s.BM25.index()` に渡してインデックス構築
7. bm25s ネイティブファイルを永続化（アトミックスワップ）
8. `_needs_rebuild = False`

#### _save の廃止

現行の `_save()` は metadata.json への JSON 一括ダンプと bm25s の保存を行う。SQLite 外部化後は:

- チャンクデータの永続化: add/delete 時に SQLite へ即時書き込み（トランザクション制御）
- bm25s の永続化: rebuild 完了時にアトミックスワップで保存
- metadata.json の一括ダンプは廃止

#### _load の廃止

現行の `_load()` は metadata.json から全辞書をメモリに復元する。SQLite 外部化後は:

- 起動時に SQLite の接続を確立するのみ（全データのメモリ読み込みは行わない）
- bm25s モデルはネイティブファイルからロード（既存方式を維持）

### メモリモデルの変化

| データ | 変更前 | 変更後 |
|--------|--------|--------|
| チャンクテキスト（`_documents`） | 全件オンメモリ | SQLite に格納。検索結果分のみ取得 |
| source_id マッピング（`_doc_source_map`） | 全件オンメモリ | SQLite `chunks.source_id` |
| source_type マッピング（`_doc_source_type_map`） | 全件オンメモリ | SQLite `chunks.source_type` |
| メタデータ（`_doc_metadata_map`） | 全件オンメモリ | SQLite `chunks.metadata` |
| doc_id リスト（`_doc_ids`） | 全件オンメモリ | rebuild 時に SQLite から取得。bm25s のインデックス順序に対応するためオンメモリ保持を維持 |
| bm25s モデル | オンメモリ | オンメモリ（変更なし。bm25s の内部構造は CSR スパース行列） |
| トークンキャッシュ | なし | SQLite `token_cache` |

## エッジケース

- **bm25_store.db 破損時**: `rebuild --mode full` で復旧する。bm25_store.db を削除して rebuild すれば、converted_store から全データが再構築される
- **トークンキャッシュの不整合**: `token_cache` のエントリが `chunks` に存在しない場合、rebuild 時に無視される。逆に `chunks` にあるが `token_cache` にない場合はキャッシュミスとしてトークン化を実行する。不整合が蓄積した場合は `token_cache` テーブルの全行削除で解消可能
- **deferred_save モード**: SQLite への書き込みは即時コミットし、bm25s の rebuild + 永続化を flush() まで遅延する
- **空インデックス**: チャンク 0 件の場合、bm25s ディレクトリを削除し bm25_store.db は空テーブルの状態を維持する

## 関連ドキュメント

- [インデクサー仕様](../indexer.md) — BM25 インデックスのデータ構造・変更種別ごとの処理
- [再構築・統計・バックアップ](../rebuild-stats.md) — rebuild コマンドの仕様
- [Issue #649](https://github.com/becky3/rag-knowledge/issues/649) — BM25 オンメモリ保持解消
- [Issue #650](https://github.com/becky3/rag-knowledge/issues/650) — rebuild 全チャンク一括トークン化の改善

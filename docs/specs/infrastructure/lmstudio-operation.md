# LM Studio 運用手順

## 概要

RAG Knowledge は Embedding（ローカル）と Vision（メディア解析）に LM Studio を使用する。本書は Claude が実施する運用手順を集約する。

`lms` CLI コマンド・API パラメータ等の仕様参照は [lmstudio-reference.md](lmstudio-reference.md) を参照。

## 責務分担

| 担当 | 責務 |
|---|---|
| ユーザー | LM Studio 本体のインストール、必要モデル（Embedding / Vision）のダウンロード、初回 GUI セットアップ、per-model preset 設定（context_length / gpu_offload / ttl_seconds / parallel 等のチューニング）、`lmstudio.toml` の key 編集（モデル選定・切替） |
| Claude | サーバー起動・停止、`lmstudio.toml` の key を見たモデルロード/unload、状態確認、トラブル時の一次対応 |

ロードパラメータ（context_length / gpu_offload 等）は LM Studio の per-model preset に委譲する。`lms load` の引数では指定しない。

## ユーザーセットアップ

以下の項目は [LM Studio 公式ドキュメント](https://lmstudio.ai/docs) を参照する。本プロジェクトは公式手順に独自の追加要件を持たない。

- LM Studio 本体のインストール
- 必要モデル（`lmstudio.toml` の `models.*.key` で指定）のダウンロード
- 初回 GUI セットアップ
- per-model preset 設定

## 運用手順（Claude 実施）

### サーバーの起動・停止

```bash
lms server start       # 起動
lms server status      # 状態確認
lms server stop        # 停止
```

### 接続先の確認

`lms` CLI はローカルの LM Studio インスタンスを操作するが、ランタイム（本プロジェクト）が実際に接続する先は `.env` の `LMSTUDIO_BASE_URL` である。
両者が一致していないと、`lms load` でモデルをロードしてもアプリは別のサーバーへ接続し続けて疎通失敗の原因となる。

操作前に以下を確認する:

- `.env` の `LMSTUDIO_BASE_URL` のホスト/ポート
- `lms server status` の出力に表示される LM Studio サーバーのリッスン先

両者のホスト/ポートが一致しない場合、`.env` を修正するか、`lms` CLI 側でリモート接続を構成する必要がある。

### モデルのロード・unload

`lmstudio.toml` の `models.embedding.key` / `models.vision.key` を参照して `lms load <key>` を実行する。
追加のロードパラメータを指定しないため、per-model preset に従ってロードされる。

```bash
# lmstudio.toml で指定されている key を読み出してロード
lms load <embedding-key>
lms load <vision-key>

# unload
lms unload <key>
lms unload --all
```

### 状態確認

```bash
lms ps                 # ロード中モデルの一覧（IDENTIFIER / SIZE / CONTEXT / PARALLEL / TTL）
lms ls                 # ダウンロード済みモデルの一覧
lms server status      # サーバー起動状態
```

### パラメータ調整

LM Studio GUI の per-model preset 設定に従う（CLI 側からは指定しない）。

## トラブルシュート

| 症状 | 原因 | 対処 |
|---|---|---|
| `lms load` でエラー | モデル未ダウンロード | `lms ls` で確認後、ユーザーに対応を依頼する（モデルダウンロードはユーザー責務） |
| API 呼び出しで `ConnectError` | サーバー停止中 | `lms server start` で起動 |
| Vision API 呼び出しで `Model has crashed` | 入力画像が小さすぎる（1x1 等） | 720x720 以上の実用サイズで再試行 |
| 同時ロードで VRAM 不足 | GPU メモリ超過 | `lms unload <key>` で不要モデルを開放してから再ロード |
| `lms load` を再実行すると `:2` サフィックス付きインスタンスが追加される | `lms load` に冪等性なし | 事前に `lms ps` でロード済みを確認し、未ロード時のみ load する |

## 関連ドキュメント

- [lmstudio-reference.md](lmstudio-reference.md) — `lms` CLI 仕様参照
- [media-analysis.md](media-analysis.md) — メディア解析（Vision モデル）
- [../indexer.md](../indexer.md) — インデクサー（Embedding）
- [LM Studio 公式](https://lmstudio.ai/docs)

# Raw HTTP クライアント検出

## 概要

本仕様書は `scripts/check_raw_http.py` の振る舞い（検出パターン・許可リスト・除外ルール）を定義する。

`src/` 配下の Python コードで [`ConstrainedClient`][cc] を経由しない直接 HTTP クライアント利用を CI で検出する。検出器は GitHub Actions ワークフロー `.github/workflows/check-raw-http.yml` から呼び出される。

[cc]: https://github.com/becky3/py-common-lib/blob/main/src/py_common_lib/httpx/constrained_client.py

スコープ:

- 検出対象パターンの定義
- 許可リストの仕様
- 誤検出を構造的に防ぐための除外ルール

スコープ外:

- ConstrainedClient 自体の所有権原則（[architecture.md §6](../architecture.md#6-constrainedclient-所有権原則) で定義）
- 他 CI ワークフローの統合方針

## 背景

PR #734（Issue #726）で、`src/rag/safe_browsing.py` の docstring 内に説明目的で `httpx.AsyncClient` の literal を記述したところ、当時の `scripts/check_raw_http.sh` の単純文字列マッチが docstring 内 literal を誤検出し、CI が失敗した。

`# safety:allowed` コメントは行末のみに作用し、複数行 docstring 内の literal を救済できないため、当時は docstring 表記を不自然な形に書き換えて回避する workaround で対処した。今後 docstring/コメントで HTTP クライアント名を説明する箇所が増えると同じ問題が再発するため、検出器側を文字列リテラル・docstring・コメントを構造的に認識できる形に改善する必要があった。

Issue #735 で Python AST ベースの検出器に置換し、`Import` / `ImportFrom` / `Attribute` ノードのみを検査することで誤検出を構造的に排除した。

## 制約

### 検出対象は `src/` 配下の `.py` ファイルに限定

検出器は `<project_root>/src/**/*.py` に対して検査する。`tests/` `scripts/` `_schema/` 等は対象外。

### 検査は AST ベース

`ast.parse` で Python ソースをパースし、以下の AST ノードのみを検査する。docstring・コメント・文字列リテラル内の literal は AST 上 `Constant` ノードに包まれるため、構造的に除外される。

| AST ノード | 検査内容 |
|---|---|
| `ast.Import` | `import urllib.request` のみ違反として記録する。`import httpx` / `import aiohttp` / `import requests` のような単体 import は違反にならず、`httpx.Client(...)` のような属性アクセス時点で `ast.Attribute` ノードとして検出する |
| `ast.ImportFrom` | `from <module> import <name>` 形式で禁止対象を検出 |
| `ast.Attribute` | `<simple-name>.<attr>` 形式の属性アクセスで禁止対象を検出。`<simple-name>` は単純な名前ノード（`ast.Name`）に限り、関数戻り値経由（`get_client().Client()`）や別名 import（`import httpx as h; h.Client()`）は対象外（現行の bash + grep ベース実装と振る舞い等価） |

### 検出パターン

| カテゴリ | 検出例 |
|---|---|
| `aiohttp.ClientSession` | `aiohttp.ClientSession()` / `from aiohttp import ClientSession` |
| `httpx.Client` / `httpx.AsyncClient` | `httpx.Client()` / `httpx.AsyncClient()` / `from httpx import Client` / `from httpx import AsyncClient` |
| `requests.<HTTP メソッド>` | `requests.get(...)` / `requests.post(...)` / `requests.put(...)` / `requests.delete(...)` / `requests.patch(...)` / `requests.head(...)` / `requests.options(...)` / `requests.session(...)` / `requests.Session(...)` / `from requests import <任意の名前>` |
| `urllib.request` | `import urllib.request` / `from urllib.request import <任意の名前>` / `urllib.request.urlopen(...)` |

検出パターンの SSoT は `scripts/check_raw_http.py` の `_BANNED_ATTRIBUTES` / `_BANNED_FROM_IMPORT` / `_BANNED_DIRECT_IMPORT` 定数。本仕様書は概念レベルでの記述に留め、具体的なモジュール名・属性名は検出器のコードを参照する。

### 許可リスト

`# safety:allowed` マーカーを持つコメントが付与された行は、検出パターンに合致しても違反として扱わない。

- 厳密一致: コメント先頭の `#` 直後（空白許容）に `safety:allowed` がある場合のみ許可。マーカー直後はホワイトスペースまたはコメント終端である必要がある（追加文脈は空白を挟んで記述可）。例:
  - 許可: `# safety:allowed`、`# safety:allowed - reason here`、`#safety:allowed`
  - 不許可: `# not safety:allowed`、`# safety:allowed-but`、`# safety:allowed_var`
- 同一行コメント方式: 違反候補となる AST ノードの開始行（`lineno` 属性）に同じ行番号で `# safety:allowed` コメントが存在すれば許可
- 複数行呼び出しの注意: `httpx.Client(\n    timeout=...\n)` のように呼び出しが複数行に跨る場合は、開始行（`httpx` または `httpx.Client` が出現する行）に `# safety:allowed` コメントを付与する必要がある。末尾行（閉じ括弧の行）に付与しても尊重されない

### 終了コード

| コード | 意味 |
|---|---|
| `0` | 違反なし（または許可リストでカバー済み） |
| `1` | 違反あり（標準エラー出力に詳細を表示） |

## 運用

### 違反を検出した場合の対処

検出された違反に対しては、以下のいずれかで対応する。

1. py-common-lib の `ConstrainedClient` 経由に書き換える
2. 直接利用が妥当な場合は同一行に `# safety:allowed` コメントを付与する

直接利用が妥当な例（`# safety:allowed` 許容）:

- 制約を適用すべきでないローカルヘルスチェック（例: `src/rag/infrastructure/chromadb_manager.py` の ChromaDB サーバー heartbeat）
- 並行性要件のため都度生成が必要な箇所（例: `src/rag/safe_browsing.py` の Pattern C 採用箇所）

判断軸の詳細は [architecture.md §6](../architecture.md#6-constrainedclient-所有権原則) を参照。

### 検出パターンの追加・変更

検出パターンを追加・変更する場合は `scripts/check_raw_http.py` 内の定数を更新し、対応するテスト `tests/test_check_raw_http.py` に検出ケースを追加する。本仕様書のカテゴリ表は、追加した検出パターンが新しいカテゴリの場合のみ更新する（同一カテゴリ内の属性追加では本仕様書の更新は不要）。

## 関連ドキュメント

- [architecture.md §6](../architecture.md#6-constrainedclient-所有権原則) — ConstrainedClient 所有権原則
- [`scripts/check_raw_http.py`](../../../scripts/check_raw_http.py) — 検出器本体（検出パターンの SSoT）
- [`tests/test_check_raw_http.py`](../../../tests/test_check_raw_http.py) — 検出器のテスト
- [`.github/workflows/check-raw-http.yml`](../../../.github/workflows/check-raw-http.yml) — CI 統合

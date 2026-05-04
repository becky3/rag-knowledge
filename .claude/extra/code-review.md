# rag-knowledge: code-review 追加観点

agent-commons の `code-review` skill から読み込まれる、rag-knowledge 固有の構造変化検出パターン定義。
観点 #16「実装と仕様の整合性」で参照する。特に `(d) architecture spec 更新` での利用を主目的とする。

## rag-knowledge 構造変化検出パターン

`docs/specs/architecture.md` を SSoT とする構造判断との整合を検出する。コードの構造変化が architecture spec に反映されていない場合、レビュー結果として警告する。

### 検出パターン一覧

| 変化パターン | 確認対象 spec | 確認観点 |
|---|---|---|
| `src/rag/pipeline/ingesters/` 配下のクラス追加・削除・package 化 | `docs/specs/architecture.md`「3. Ingester / Runner ファミリーの境界」セクション | Ingester ファミリーのメンバー表（3.1）にクラスが過不足なく列挙されているか。追加クラスが `BaseIngester` 抽象化対象範囲（3.4）と整合しているか |
| `src/rag/scrapy/` 配下のファイル追加・削除 | `docs/specs/architecture.md`「3.2 Runner ファミリー」セクション、`docs/specs/site-ingest.md` | Runner ファミリーのメンバー表（3.2）にエントリが過不足なく列挙されているか。site-ingest.md の Scrapy subprocess 構成記述と矛盾していないか |
| 新規 Protocol 定義（`class.*\(Protocol\)` パターン）の追加 | `docs/specs/architecture.md`「5. SSoT 階層と所在」セクション（軸 2: 契約の所在）、および新規 Protocol が Port 注入される §3.2（Runner ファミリー）/ §3.3（WebIngester の構造） | 新規 Protocol が Port として位置付けられているか。Port の定義場所（`<package>/<feature>_<role>.py`）の規約に従っているか。Port が注入される側のセクションでも位置付けが明記されているか |
| 新規抽象基底クラス（`class.*\(ABC\)` / `ABCMeta` 派生）の追加 | `docs/specs/architecture.md`「1. アーキテクチャ概要」セクション（Dependency Rule / 主要な層） | 新規抽象が層構造の中で位置付けられているか。Dependency Rule（上位層は下位層に依存可、逆方向禁止）に違反していないか |

### 照合方向

仕様駆動開発では spec が先行して記述されるため、「spec にあって実装にないもの」は作業途中の通常状態である。照合は **実装側の構造変化が spec に反映されているか** を確認する方向で行う（実装 → spec の整合確認）。すなわち「実装にあって spec にないもの（spec 未更新）」を検出対象とする。行番号・LOC 等の数値差分は照合対象外とする。

### 検出時のレビュー出力形式

検出した場合、レビューコメントとして以下の構造で報告する:

```text
[impl-spec-drift] <変化パターン名>
- 検出した変化: <ファイルパス + 変化内容>
- 確認対象 spec: <spec パス + セクション>
- 推奨アクション: <spec 更新 / 新規 Issue 起票 / 構造判断レビュー依頼>
```

skill 本体（agent-commons `code-review`）の観点 #16 `(d) architecture spec 更新` の報告に統合する。

## 関連

- `docs/specs/architecture.md` — rag-knowledge アーキテクチャ採用方針 SSoT
- agent-commons `code-review` skill — 本拡張ファイルを読み込む側の skill
- agent-commons `architecture-guide.md` — 用語集（Core / Port / Adapter / dto）

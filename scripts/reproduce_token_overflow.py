"""#321 再現スクリプト: 実際のトークナイザーでチャンクのトークン数を計測する.

nomic-embed-text-v2-moe の実際のトークナイザーを使い、
indexer が生成するチャンクのトークン数を正確に計測する。

使い方:
    uv run python scripts/reproduce_token_overflow.py
"""

from __future__ import annotations

import sys

from tokenizers import Tokenizer

from rag.chunker import chunk_text
from rag.content_detector import ContentType, detect_content_type
from rag.heading_chunker import chunk_by_headings
from rag.table_chunker import chunk_table_data

CHUNK_SIZE = 200
CHUNK_OVERLAP = 30
DOCUMENT_PREFIX = "search_document: "
CONTEXT_LENGTH = 512


def load_tokenizer() -> Tokenizer:
    """nomic-embed-text-v2-moe のトークナイザーをロードする.

    Hugging Face Hub からダウンロードする。ネットワーク未接続・キャッシュ未作成の
    場合はエラーメッセージを表示して終了する。
    """
    model_id = "nomic-ai/nomic-embed-text-v2-moe"
    print(f"Loading tokenizer: {model_id} ...")
    try:
        tok = Tokenizer.from_pretrained(model_id)
    except Exception as e:
        print(f"\n[ERROR] Failed to load tokenizer: {model_id}")
        print("  Possible causes:")
        print("    - Network unavailable")
        print("    - Hugging Face Hub cache not created")
        print(f"  {type(e).__name__}: {e}")
        sys.exit(1)
    print("  OK\n")
    return tok


def count_tokens(tok: Tokenizer, text: str) -> int:
    """トークン数を計測する."""
    return len(tok.encode(text).ids)


def chunk_like_indexer(text: str) -> tuple[str, list[str]]:
    """indexer._chunk_text の content 部分のみを取得する. (content_type_name, chunks) を返す."""
    content_type = detect_content_type(text)

    if content_type == ContentType.TABLE:
        table_chunks = chunk_table_data(text)
        chunks = [c.content for c in table_chunks] if table_chunks else []
        return content_type.name, chunks

    if content_type in (ContentType.HEADING, ContentType.MIXED):
        heading_chunks = chunk_by_headings(
            text,
            max_chunk_size=CHUNK_SIZE,
            min_chunk_size=max(1, CHUNK_SIZE // 4),
        )
        chunks = [c.content for c in heading_chunks] if heading_chunks else []
        return content_type.name, chunks

    chunks = chunk_text(text, chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    return content_type.name, chunks


def analyze(label: str, source_text: str, tok: Tokenizer) -> None:
    """チャンクを生成してトークン数を計測する."""
    ct_name, chunks = chunk_like_indexer(source_text)

    print(f"{'=' * 70}")
    print(f"[{label}]")
    print(f"  content_type: {ct_name}, source: {len(source_text)} chars, chunks: {len(chunks)}")
    print(f"{'=' * 70}")

    over_limit = 0
    for i, chunk in enumerate(chunks):
        with_prefix = DOCUMENT_PREFIX + chunk
        n_chars = len(with_prefix)
        n_tokens = count_tokens(tok, with_prefix)
        flag = " *** OVER 512 ***" if n_tokens > CONTEXT_LENGTH else ""
        if n_tokens > CONTEXT_LENGTH:
            over_limit += 1

        # 先頭プレビュー（改行をスペースに置換）
        preview = chunk[:60]
        for ch in "\n\r":
            preview = preview.replace(ch, " ")
        if len(chunk) > 60:
            preview += "..."

        print(f"  [{i}] {n_chars:>4} chars, {n_tokens:>4} tokens{flag}")
        print(f"      {preview}")

    if over_limit:
        print(f"\n  *** {over_limit}/{len(chunks)} chunks EXCEED {CONTEXT_LENGTH} tokens ***")
    else:
        print(f"\n  All {len(chunks)} chunks within {CONTEXT_LENGTH} tokens")
    print()


def main() -> None:
    """メイン処理."""
    tok = load_tokenizer()

    # prefix のトークン数を確認
    prefix_tokens = count_tokens(tok, DOCUMENT_PREFIX)
    print(f"Document prefix: '{DOCUMENT_PREFIX}' = {prefix_tokens} tokens")

    # 基本的なトークン/文字比率を確認
    test_texts = [
        ("Japanese prose", "人工知能技術の進歩により自然言語処理の精度が大幅に向上している。"),
        ("English prose", "The quick brown fox jumps over the lazy dog."),
        ("Mixed JP+EN", "ChromaDBを使用したベクトル検索エンジンの設計方針について説明する。"),
        ("Markdown table row", "| framework | Hugging Face | Python | Apache 2.0 |"),
    ]

    print(f"\nToken/char ratios:")
    for label, text in test_texts:
        n_tokens = count_tokens(tok, text)
        ratio = n_tokens / len(text) if text else 0
        print(f"  {label}: {len(text)} chars -> {n_tokens} tokens (ratio: {ratio:.2f})")
    print()

    # --- テストケース ---

    # 1. Markdown テーブル（カラム数多い）
    analyze(
        "Markdown table (many columns, JP description)",
        """\
| framework | developer | language | license | stars | description | features | year |
|-----------|-----------|----------|---------|-------|-------------|----------|------|
| Transformers | Hugging Face | Python | Apache 2.0 | 120k | 大規模言語モデルの統合的な管理と配布を実現するフレームワーク | 事前学習・ファインチューニング・推論・モデルハブ連携 | 2018 |
| PyTorch | Meta AI | Python/C++ | BSD | 80k | 動的計算グラフによる柔軟な深層学習フレームワーク | 自動微分・GPU加速・分散学習・TorchScript | 2016 |
| TensorFlow | Google | Python/C++ | Apache 2.0 | 180k | 産業グレードの機械学習プラットフォーム | TensorBoard・SavedModel・TFLite・TPU対応 | 2015 |
| LangChain | LangChain Inc | Python/JS | MIT | 90k | 大規模言語モデルを活用したアプリケーション構築フレームワーク | チェーン・エージェント・メモリ・ツール連携・RAG | 2022 |
| spaCy | Explosion | Python/Cython | MIT | 30k | 産業用途向けの高速自然言語処理ライブラリ | 固有表現認識・係り受け解析・テキスト分類・パイプライン | 2015 |
""",
        tok,
    )

    # 2. Markdown テーブル（日本語の値が非常に長い）
    analyze(
        "Markdown table (very long JP values)",
        """\
| 項目 | 説明 | 備考 |
|------|------|------|
| ベクトル検索 | ChromaDBを使用した高次元ベクトル空間での類似度検索。コサイン類似度に基づいてクエリに最も関連性の高いドキュメントチャンクを返却する仕組み | ベクトル次元数はEmbeddingモデルに依存する。現在のnomic-embed-text-v2-moeモデルでは768次元 |
| ハイブリッド検索 | ベクトル検索とBM25キーワード検索のスコアを重み付き統合して最終スコアを算出する検索方式。意味的類似度とキーワード一致の両方を考慮できる | ベクトル重み0.90、BM25パラメータk1=2.5,b=0.50で運用中 |
| チャンキング | 長いドキュメントを検索に適したサイズに分割する処理。見出しベース・テーブルベース・プレーンテキストの3種類の戦略を自動選択する | chunk_size=200文字、overlap=30文字が現在の設定 |
""",
        tok,
    )

    # 3. 見出し付きドキュメント（深いネスト）
    analyze(
        "Heading document (deep nesting, long content)",
        """\
# RAG Knowledge システム概要

## アーキテクチャ

### パイプライン構成

#### ステージ1: インジェスト

外部ソースからデータを取得し、source_storeに格納する。Web、Zenn、BlueSky、YouTube、ローカルファイルなど多様なソースに対応している。各インジェスターは共通インターフェースを実装し、パイプライン制御から統一的に呼び出される。取得したデータはsource_store内にファイルとして保存され、メタデータは.metaサイドカーファイルに記録される。

#### ステージ2: コンバート

source_storeのファイルをプレーンテキストまたはMarkdown形式に変換する。HTML、PDF、AsciiDocなどの形式に対応し、メタデータを保持したまま変換を行う。変換結果はconverted_storeに格納される。

#### ステージ3: インデックス

変換済みテキストをチャンキングし、Embeddingベクトルを生成してChromaDBとBM25インデックスに格納する。コンテンツタイプに応じた適切なチャンキング戦略を自動選択する。
""",
        tok,
    )

    # 4. プレーンテキスト（本番相当）
    analyze(
        "Plain prose (Japanese, ~200 chars)",
        "人工知能技術の進歩により自然言語処理の精度が大幅に向上している。"
        "特に大規模言語モデルは文脈理解や生成タスクにおいて従来手法を凌駕する性能を示す。"
        "ベクトル検索技術を組み合わせることで関連情報の高速な検索が可能となり、"
        "知識管理システムの構築において重要な役割を果たしている。"
        "埋め込みモデルはテキストを高次元のベクトル空間にマッピングし、"
        "意味的な類似度を数値として定量化することができる。",
        tok,
    )

    # 5. 極端に長いテーブル行（再現狙い）
    analyze(
        "Extreme: wide table with long JP cells",
        """\
| 機能名 | 概要 | 技術スタック | 入力形式 | 出力形式 | エラーハンドリング | パフォーマンス特性 | セキュリティ考慮事項 | 依存関係 | 設定項目 |
|--------|------|------------|----------|----------|------------------|------------------|-------------------|---------|---------|
| Webクロール | 外部Webページを取得しHTMLを解析してテキストを抽出する機能。robots.txtを遵守しクロールディレイを設定可能 | httpx,BeautifulSoup4,markdownify | URL文字列またはURLリスト | Markdown形式のテキストファイル | タイムアウト・接続エラー・HTTP4xx/5xxはリトライ後スキップ。サーキットブレーカーで連続エラー時は中断 | 同時接続数制限あり。大規模サイトはScrapyサブプロセスに委譲 | URL安全性チェック（Google Safe Browsing API）を実施。DNSリバインディング保護 | py-common-lib ConstrainedClient | rag_max_crawl_pages,rag_crawl_delay_sec,rag_crawl_default_depth |
| ベクトル検索 | ChromaDBによる高次元ベクトル空間でのコサイン類似度検索。Embeddingモデルでテキストをベクトル化し近傍探索を実行 | ChromaDB,OpenAI SDK | 検索クエリ文字列 | スコア付きドキュメントチャンクリスト | Embedding接続エラー時はConnectionError送出。空クエリは空結果を返却 | インメモリインデックスで高速応答。大規模データはバッチ処理 | APIキーはOSセキュアストレージで管理。通信はHTTPS | OpenAI SDK,chromadb | embedding_model_local,rag_retrieval_count |
""",
        tok,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

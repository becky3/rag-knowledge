"""Wikipedia APIを使ったRAG評価データ収集スクリプト.

Wikipedia API (MediaWiki API) で公開ドキュメントを収集し、
RAGパラメータスイープ用の拡充評価データを構築する。

Usage:
    uv run python scripts/collect_evaluation_data.py
    uv run python scripts/collect_evaluation_data.py --config scripts/eval_data_config.json
    uv run python scripts/collect_evaluation_data.py --config scripts/eval_data_config.json --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import aiohttp

WIKI_API_JA = "https://ja.wikipedia.org/w/api.php"
WIKI_API_EN = "https://en.wikipedia.org/w/api.php"
WIKI_PAGE_JA = "https://ja.wikipedia.org/wiki/"
WIKI_PAGE_EN = "https://en.wikipedia.org/wiki/"

DEFAULT_CONFIG = "scripts/eval_data_config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def fetch_wikipedia_page(
    session: aiohttp.ClientSession,
    title: str,
    lang: str,
    max_length: int,
) -> dict[str, str] | None:
    """Wikipedia APIでページ本文を取得する.

    Args:
        session: aiohttp セッション
        title: Wikipedia ページタイトル
        lang: 言語コード ("ja" or "en")
        max_length: テキストの最大文字数

    Returns:
        {"source_url": ..., "title": ..., "content": ...} or None
    """
    api_url = WIKI_API_JA if lang == "ja" else WIKI_API_EN
    page_base = WIKI_PAGE_JA if lang == "ja" else WIKI_PAGE_EN

    params = {
        "action": "query",
        "titles": title,
        "prop": "extracts",
        "explaintext": "",
        "format": "json",
        "redirects": "1",
    }

    try:
        async with session.get(api_url, params=params) as resp:
            if resp.status != 200:
                logger.warning("HTTP %d for %s (%s)", resp.status, title, lang)
                return None

            data = await resp.json()
            pages = data.get("query", {}).get("pages", {})

            for page_id, page_data in pages.items():
                if page_id == "-1":
                    logger.warning("Page not found: %s (%s)", title, lang)
                    return None

                extract = page_data.get("extract", "")
                if not extract or len(extract.strip()) < 100:
                    logger.warning("Content too short: %s (%s)", title, lang)
                    return None

                # テキストを max_length 文字に制限
                content = extract[:max_length].strip()

                # 最後の文が途中で切れていたら、最後の句点で切る
                if len(extract) > max_length:
                    for sep in ["。", ".\n", ". ", "\n\n"]:
                        last_sep = content.rfind(sep)
                        if last_sep > max_length * 0.6:
                            content = content[: last_sep + len(sep)].strip()
                            break

                resolved_title = page_data.get("title", title)
                source_url = page_base + resolved_title.replace(" ", "_")

                return {
                    "source_url": source_url,
                    "title": resolved_title,
                    "content": content,
                }

    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning("Request failed for %s (%s): %s", title, lang, e)
        return None

    return None


async def collect_documents(
    config: dict,
    dry_run: bool = False,
) -> list[dict[str, str]]:
    """設定ファイルに基づきドキュメントを収集する."""
    max_length = config.get("max_content_length", 2000)
    delay = config.get("request_delay_sec", 1.0)
    topics = config.get("topics", {})

    # 収集対象を展開
    requests: list[tuple[str, str, str]] = []  # (cluster, title, lang)
    for cluster_name, cluster in topics.items():
        for title in cluster.get("ja", []):
            requests.append((cluster_name, title, "ja"))
        for title in cluster.get("en", []):
            requests.append((cluster_name, title, "en"))

    total = len(requests)
    logger.info("Total topics to fetch: %d", total)

    if dry_run:
        for cluster, title, lang in requests:
            print(f"  [{cluster}] {title} ({lang})")
        return []

    documents: list[dict[str, str]] = []

    async with aiohttp.ClientSession() as session:
        for i, (cluster, title, lang) in enumerate(requests, 1):
            logger.info("[%d/%d] Fetching: %s (%s) [%s]", i, total, title, lang, cluster)

            doc = await fetch_wikipedia_page(session, title, lang, max_length)
            if doc:
                doc["cluster"] = cluster
                doc["lang"] = lang
                documents.append(doc)
                logger.info(
                    "  -> OK: %s (%d chars)",
                    doc["title"],
                    len(doc["content"]),
                )
            else:
                logger.warning("  -> SKIP: %s (%s)", title, lang)

            # レート制限対策
            if i < total:
                await asyncio.sleep(delay)

    return documents


def save_documents(documents: list[dict[str, str]], output_path: Path) -> None:
    """収集ドキュメントをJSON形式で保存する."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # クラスタ・言語統計
    cluster_counts: dict[str, int] = {}
    lang_counts: dict[str, int] = {}
    for doc in documents:
        cluster = doc.get("cluster", "unknown")
        lang = doc.get("lang", "unknown")
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        lang_counts[lang] = lang_counts.get(lang, 0) + 1

    # 出力用にcluster/langフィールドを除去（ドキュメントスキーマに合わせる）
    clean_docs = [
        {
            "source_url": doc["source_url"],
            "title": doc["title"],
            "content": doc["content"],
        }
        for doc in documents
    ]

    output = {
        "description": "RAG評価用拡充ドキュメント - Wikipedia公開データ (CC BY-SA 4.0)",
        "version": "1.0.0",
        "documents": clean_docs,
        "metadata": {
            "created_at": "2026-02-19",
            "source": "Wikipedia (CC BY-SA 4.0)",
            "total_documents": len(clean_docs),
            "cluster_counts": cluster_counts,
            "language_counts": lang_counts,
            "related_issues": ["#512", "#516"],
            "notes": [
                "Wikipedia APIで自動収集したドキュメント群",
                "各ドキュメントは先頭2000文字程度に制限",
                "既存の rag_test_documents.json（勇者の冒険テーマ）とは別管理",
                "init-test-db コマンドで ChromaDB にベクトル化・登録できる形式",
            ],
        },
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info("Saved %d documents to %s", len(clean_docs), output_path)
    logger.info("Cluster breakdown: %s", cluster_counts)
    logger.info("Language breakdown: %s", lang_counts)


def main() -> None:
    """エントリポイント."""
    parser = argparse.ArgumentParser(
        description="Wikipedia APIでRAG評価データを収集する",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"トピック設定ファイルのパス（デフォルト: {DEFAULT_CONFIG}）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="出力ディレクトリ（デフォルト: 設定ファイルの output_dir）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="収集対象の一覧を表示するだけで実際には取得しない",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    output_dir = Path(args.output or config.get("output_dir", "tests/fixtures/rag_evaluation_extended"))
    output_path = output_dir / "rag_test_documents_extended.json"

    documents = asyncio.run(collect_documents(config, dry_run=args.dry_run))

    if args.dry_run:
        return

    if not documents:
        logger.error("No documents collected!")
        sys.exit(1)

    save_documents(documents, output_path)

    # サマリー表示
    print(f"\n{'=' * 60}")
    print("Collection Summary")
    print(f"{'=' * 60}")
    print(f"Total documents: {len(documents)}")
    print(f"Output: {output_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()

"""RAG MCP サーバー

仕様: docs/specs/rag-knowledge.md
独立リポジトリとして動作する。

FastMCP を使用して 10 個の RAG ツールを公開する:
- rag_search: ナレッジベースから関連情報を検索
- rag_add: 単一ページをナレッジベースに取り込み
- rag_crawl: リンク集ページからクロール＆一括取り込み
- rag_crawl_preview: クロール対象ページのプレビュー（タイトル・URL一覧）
- rag_crawl_zenn: Zenn 記事の一括取り込み
- rag_crawl_bluesky: BlueSky 投稿の一括取り込み
- rag_add_local: ローカルファイルをナレッジベースに取り込み
- rag_crawl_local: ディレクトリ内ファイルを一括取り込み
- rag_delete: ソースURL指定でナレッジから削除
- rag_stats: ナレッジベースの統計情報を表示
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os

# ChromaDB テレメトリを無効化（import 前に設定する必要がある）
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

# RAG モジュールをモジュールレベルで import する。
# 重要: asyncio.to_thread のワーカースレッド内で import すると、
# anyio イベントループの import lock とデッドロックするため、
# サーバー起動時（メインスレッド）に全て import しておく。
# bm25s が "resource module not available on Windows" を stdout に print する
# 問題への対策として、import 時に stdout を抑制する。
from .config import ensure_utf8_streams

with contextlib.redirect_stdout(io.StringIO()):
    from .bm25_index import BM25Index
    from .config import get_settings
    from .embedding.factory import get_embedding_provider
    from .ingesters.bluesky import BlueskyIngester
    from .ingesters.local_file import LocalFileIngester
    from .ingesters.web import WebIngester
    from .ingesters.zenn import ZennIngester
    from .rag_knowledge import RAGKnowledgeService
    from .safe_browsing import create_safe_browsing_client
    from .vector_store import VectorStore
    from .web_crawler import WebCrawler

from py_common_lib.httpx import ConstrainedClient  # safety:allowed

from mcp.server.fastmcp import FastMCP

# Windows 環境で stderr が cp932 等の場合に UTF-8 へ再構成する
# stdout は MCP stdio プロトコルが使うため変更しない
ensure_utf8_streams()

logger = logging.getLogger(__name__)

mcp = FastMCP("rag")

# --- 遅延初期化 ---

_rag_service = None
_init_lock = asyncio.Lock()


def _reset_rag_service() -> None:
    """グローバルな RAGKnowledgeService をリセットする（テスト用）."""
    global _rag_service
    _rag_service = None


async def _get_rag_service() -> RAGKnowledgeService:
    """RAGKnowledgeService を遅延初期化して返す."""
    global _rag_service
    if _rag_service is not None:
        return _rag_service

    async with _init_lock:
        # ダブルチェック: Lock 待ちの間に別タスクが初期化済みの場合
        if _rag_service is not None:
            return _rag_service

        # ChromaDB / BM25 のオブジェクト構築は同期的でブロッキング。
        # イベントループをブロックすると MCP stdio 通信が途絶えるため、
        # ワーカースレッドで実行する。
        # 注意: import は全てモジュールレベルで完了済み。ワーカースレッド内で
        # import するとデッドロックする（anyio イベントループとの import lock 競合）。
        _rag_service = await asyncio.to_thread(_build_rag_service)
        logger.info("RAG service initialized")
        return _rag_service


def _build_rag_service() -> RAGKnowledgeService:
    """RAGKnowledgeService を構築する（ワーカースレッド用、import なし）."""
    settings = get_settings()

    embedding_provider = get_embedding_provider(settings, settings.embedding_provider)

    # bm25s が stdout に直接 print する問題への対策（MCP stdio プロトコル保護）
    with contextlib.redirect_stdout(io.StringIO()):
        vector_store = VectorStore(
            embedding_provider=embedding_provider,
            persist_directory=settings.chromadb_persist_dir,
        )
        web_crawler = WebCrawler(
            max_pages=settings.rag_max_crawl_pages,
            crawl_delay=settings.rag_crawl_delay_sec,
            respect_robots_txt=settings.rag_respect_robots_txt,
            robots_txt_cache_ttl=settings.rag_robots_txt_cache_ttl,
        )
        safe_browsing_client = create_safe_browsing_client(settings)

        # BM25は準Agentic Search（生結果個別返却）で常時使用するため、
        # rag_hybrid_search_enabled に関係なく常時初期化する。
        # 既存の hybrid_search_enabled フラグと HybridSearchEngine は従来のまま保持。
        bm25_index = BM25Index(
            k1=settings.rag_bm25_k1,
            b=settings.rag_bm25_b,
            persist_dir=settings.bm25_persist_dir,
        )

    web_ingester = WebIngester(
        web_crawler=web_crawler,
        safe_browsing_client=safe_browsing_client,
    )

    return RAGKnowledgeService(
        vector_store=vector_store,
        web_crawler=web_crawler,
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
        similarity_threshold=settings.rag_similarity_threshold,
        safe_browsing_client=safe_browsing_client,
        bm25_index=bm25_index,
        hybrid_search_enabled=settings.rag_hybrid_search_enabled,
        vector_weight=settings.rag_vector_weight,
        min_combined_score=settings.rag_min_combined_score,
        debug_log_enabled=settings.rag_debug_log_enabled,
        web_ingester=web_ingester,
    )


# --- MCP ツール定義 ---


@mcp.tool()
async def rag_search(query: str, n_results: int | None = None) -> str:
    """[rag-knowledge] RAG search - ナレッジベース検索。挨拶・雑談以外の質問では必ずこのツールを最初に呼び出すこと。

    knowledge base, vector search, BM25, retrieval-augmented generation.
    ナレッジベースにはゲーム攻略情報・技術文書等が格納されている。
    蓄積データの詳細は rag_stats ツールで確認できる。
    知らない用語や固有名詞を含む質問でも必ず検索すること。

    Args:
        query: 検索クエリ（ユーザーの質問からキーワードを抽出して構成する）
        n_results: 各エンジンから取得する結果数（未指定時は設定値を使用）

    Returns:
        検索結果テキスト。ベクトル検索結果とBM25検索結果をセクション分けして返す。
        ヒットしたチャンクのページ全文を返却し、同一URLの重複は参照テキストで省略する。
        結果が0件の場合は「該当する情報が見つかりませんでした」を返す。
        RAG_MAX_RESPONSE_CHARS 設定時、累積文字数を追跡し上限到達後はページ全文取得を
        早期打ち切りする。末尾にトランケート通知が付記される。未設定時は無制限。
    """
    service = await _get_rag_service()
    if n_results is None:
        n_results = get_settings().rag_retrieval_count

    raw = await service.retrieve_raw_results(query, n_results=n_results)

    if not raw.vector_results and not raw.bm25_results:
        return "該当する情報が見つかりませんでした"

    max_chars = get_settings().rag_max_response_chars

    # ページ全文キャッシュ（同一URLの多重DB問い合わせ防止）
    page_cache: dict[str, str] = {}
    # URL初出記録: url -> (セクション名, Result番号)
    url_first_seen: dict[str, tuple[str, int]] = {}

    async def _get_page_text(url: str) -> str:
        if url not in page_cache:
            page_cache[url] = await service.get_full_page_text(url)
        return page_cache[url]

    parts: list[str] = []
    # 累積文字数を追跡（改行セパレータ分も含む）
    current_chars = 0
    budget_exceeded = False

    def _append_part(text: str) -> None:
        """parts にテキストを追加し、累積文字数を更新する."""
        nonlocal current_chars, budget_exceeded
        # 改行セパレータ分を加算（最初の要素以外）
        sep_len = 1 if parts else 0
        new_chars = sep_len + len(text)

        if max_chars is not None and current_chars + new_chars > max_chars:
            # 空セパレータは装飾目的なので、超過しても打ち切りとみなさずスキップ
            if not text:
                return
            # 残り文字数分だけ追加してトランケート
            remaining = max_chars - current_chars - sep_len
            if remaining > 0:
                parts.append(text[:remaining])
                current_chars = max_chars
            budget_exceeded = True
            return

        parts.append(text)
        current_chars += new_chars

    # ベクトル検索結果
    if raw.vector_results:
        _append_part("## ベクトル検索結果 (意味的類似度)\n")
        for i, vec_item in enumerate(raw.vector_results, start=1):
            if budget_exceeded:
                break
            _append_part(f"### Result {i} [distance={vec_item.distance:.3f}]")
            if budget_exceeded:
                break
            _append_part(f"Source: {vec_item.source_url}")
            if budget_exceeded:
                break

            if vec_item.source_url not in url_first_seen:
                url_first_seen[vec_item.source_url] = ("ベクトル検索結果", i)
                full_text = await _get_page_text(vec_item.source_url)
                _append_part(full_text)
            else:
                section, num = url_first_seen[vec_item.source_url]
                _append_part(
                    f"（この URL のページ全文は{section} Result {num} に掲載済み）"
                )
            if budget_exceeded:
                break
            _append_part("")

    # BM25検索結果
    if raw.bm25_results and not budget_exceeded:
        _append_part("## BM25検索結果 (キーワード一致)\n")
        for i, bm25_item in enumerate(raw.bm25_results, start=1):
            if budget_exceeded:
                break
            _append_part(f"### Result {i} [score={bm25_item.score:.3f}]")
            if budget_exceeded:
                break
            _append_part(f"Source: {bm25_item.source_url}")
            if budget_exceeded:
                break

            if bm25_item.source_url not in url_first_seen:
                url_first_seen[bm25_item.source_url] = ("BM25検索結果", i)
                full_text = await _get_page_text(bm25_item.source_url)
                _append_part(full_text)
            else:
                section, num = url_first_seen[bm25_item.source_url]
                _append_part(
                    f"（この URL のページ全文は{section} Result {num} に掲載済み）"
                )
            if budget_exceeded:
                break
            _append_part("")

    response = "\n".join(parts).rstrip()

    if budget_exceeded:
        response += "\n\n…（レスポンスが上限の{:,}文字を超えたため切り詰めました）".format(
            max_chars
        )

    return response


@mcp.tool()
async def rag_add(url: str) -> str:
    """[rag-knowledge] RAG add - 単一ページをナレッジベースに取り込む.

    knowledge base, ingest, web page, crawl single URL.

    Args:
        url: 取り込むページのURL

    Returns:
        取り込み結果のメッセージ
    """
    service = await _get_rag_service()
    try:
        chunks = await service.ingest_page(url)
        if chunks <= 0:
            return f"エラー: ページの取り込みに失敗しました。URL: {url}"
        return f"ページを取り込みました: {url} ({chunks}チャンク)"
    except ValueError as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to add page: %s", url)
        return f"エラー: ページの取り込みに失敗しました。URL: {url}"


@mcp.tool()
async def rag_crawl(url: str, pattern: str = "") -> str:
    """[rag-knowledge] RAG crawl - リンク集ページからクロール＆一括取り込み.

    knowledge base, bulk ingest, web crawl, link index.

    Args:
        url: リンク集ページのURL
        pattern: URLフィルタリング用の正規表現パターン（任意）

    Returns:
        クロール結果のサマリー
    """
    service = await _get_rag_service()
    try:
        result = await service.ingest_from_index(url, url_pattern=pattern)
        pages = result["pages_crawled"]
        chunks = result["chunks_stored"]
        errors = result["errors"]
        return f"完了: {pages}ページ / {chunks}チャンク / エラー: {errors}件"
    except ValueError as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to crawl: %s", url)
        return f"エラー: クロールに失敗しました。URL: {url}"


@mcp.tool()
async def rag_crawl_preview(url: str, pattern: str = "") -> str:
    """[rag-knowledge] RAG crawl preview - クロール対象ページのプレビュー.

    knowledge base, crawl preview, dry run, link list.
    実際の取り込み（チャンキング・ベクトル化）は行わず、
    クロール対象となるページのタイトルとURLの一覧を返す。

    Args:
        url: リンク集ページのURL
        pattern: URLフィルタリング用の正規表現パターン（任意）

    Returns:
        クロール対象ページの一覧テキスト
    """
    service = await _get_rag_service()
    try:
        pages = await service.crawl_preview(url, url_pattern=pattern)
        if not pages:
            return "対象ページが見つかりませんでした"

        lines: list[str] = [f"クロール対象: {len(pages)}ページ", ""]
        for i, page in enumerate(pages, start=1):
            title = page.title or "(タイトル取得不可)"
            lines.append(f"{i}. {title}")
            lines.append(f"   {page.url}")
        return "\n".join(lines)
    except ValueError as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to preview crawl: %s", url)
        return f"エラー: プレビューに失敗しました。URL: {url}"


@mcp.tool()
async def rag_crawl_zenn(username: str, max_articles: int | None = None) -> str:
    """[rag-knowledge] RAG crawl Zenn - Zenn 記事を API 経由で取得し一括取り込み.

    knowledge base, Zenn, ingest, articles, API.
    指定ユーザーの Zenn 記事を API 経由で取得し、ナレッジベースに取り込む。
    同一記事の再取り込み時は既存の知識を最新に置き換える。

    Args:
        username: Zenn ユーザー名
        max_articles: 取得する最大記事数（未指定時は設定値を使用、許容範囲: 1〜100）

    Returns:
        取り込み結果のサマリーテキスト（取得記事数、チャンク数、エラー数）
    """
    service = await _get_rag_service()
    settings = get_settings()

    # max_articles のデフォルト解決: 未指定時は設定値を使用
    if max_articles is None:
        max_articles = settings.rag_zenn_max_articles

    # max_articles のバリデーション（MCP ツール入力として）
    if not isinstance(max_articles, int) or isinstance(max_articles, bool):
        return f"エラー: max_articles は整数で指定してください（入力値: {max_articles!r}）"
    if max_articles <= 0:
        return f"エラー: max_articles は正の整数で指定してください（入力値: {max_articles}）"

    if not username or not username.strip():
        return "エラー: username を指定してください"

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_zenn_request_timeout,
            request_interval=settings.rag_zenn_request_interval,
        ) as client:
            ingester = ZennIngester(
                client=client,
                max_articles=max_articles,
            )

            # 記事一覧を走査
            slugs = await ingester.discover(username.strip())
            if not slugs:
                return f"記事が見つかりませんでした（ユーザー: {username}）"

            # 各記事を取得してナレッジベースに取り込む
            total_chunks = 0
            errors = 0
            skipped = 0
            ingested_count = 0

            for slug in slugs:
                try:
                    content = await ingester.fetch_single(slug)
                    if content is None:
                        skipped += 1
                        continue
                    chunks = await service.ingest_content(content)
                    total_chunks += chunks
                    ingested_count += 1
                except Exception:
                    logger.exception("Failed to ingest Zenn article: %s", slug)
                    errors += 1

            parts = [
                f"完了: {ingested_count}記事 / {total_chunks}チャンク",
            ]
            if skipped > 0:
                parts.append(f"スキップ: {skipped}件")
            if errors > 0:
                parts.append(f"エラー: {errors}件")
            parts.append(f"（ユーザー: {username}）")

            return " / ".join(parts)
    except (ValueError, TypeError) as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to crawl Zenn articles for user: %s", username)
        return f"エラー: Zenn 記事の取り込みに失敗しました（ユーザー: {username}）"


@mcp.tool()
async def rag_crawl_bluesky(
    handle: str,
    max_posts: int | None = None,
    include_reposts: bool | None = None,
) -> str:
    """[rag-knowledge] RAG crawl BlueSky - BlueSky 投稿を AT Protocol API 経由で取得し一括取り込み.

    knowledge base, BlueSky, Bluesky, ingest, posts, AT Protocol.
    指定ユーザーの BlueSky 投稿を AT Protocol API 経由で取得し、ナレッジベースに取り込む。
    BlueSky は投稿編集不可のため、既存の投稿はスキップする（上書き不要）。

    Args:
        handle: BlueSky ハンドル（例: user.bsky.social）。DID 形式は不可
        max_posts: 取得する最大投稿数（未指定時は設定値を使用、許容範囲: 1〜1000）
        include_reposts: リポストを取得対象に含めるか（未指定時は設定値を使用）

    Returns:
        取り込み結果のサマリーテキスト（取得投稿数、スキップ数、チャンク数、エラー数）
    """
    service = await _get_rag_service()
    settings = get_settings()

    # max_posts のデフォルト解決: 未指定時は設定値を使用
    if max_posts is None:
        max_posts = settings.rag_bluesky_max_posts

    # include_reposts のデフォルト解決
    if include_reposts is None:
        include_reposts = settings.rag_bluesky_include_reposts

    # max_posts のバリデーション（MCP ツール入力として）
    if not isinstance(max_posts, int) or isinstance(max_posts, bool):
        return f"エラー: max_posts は整数で指定してください（入力値: {max_posts!r}）"
    if max_posts <= 0:
        return f"エラー: max_posts は正の整数で指定してください（入力値: {max_posts}）"

    if not handle or not handle.strip():
        return "エラー: handle を指定してください"

    handle = handle.strip()
    if handle.startswith("did:"):
        return (
            f"エラー: DID 形式は使用できません: {handle!r}。"
            "ハンドル（例: user.bsky.social）を指定してください"
        )

    try:
        async with ConstrainedClient(
            request_timeout=settings.rag_bluesky_request_timeout,
            request_interval=settings.rag_bluesky_request_interval,
        ) as client:
            ingester = BlueskyIngester(
                client=client,
                pds_url=settings.rag_bluesky_pds_url,
                max_posts=max_posts,
            )

            # 投稿を一括取得
            contents = await ingester.crawl(
                handle,
                include_reposts=include_reposts,
            )

            if not contents:
                return f"投稿が見つかりませんでした（ハンドル: {handle}）"

            # 各投稿をナレッジベースに取り込む
            total_chunks = 0
            errors = 0
            skipped = 0
            ingested_count = 0

            for content in contents:
                # 既存 source_id チェック（スキップ判定）
                # BlueSky は投稿編集不可のため、既存投稿はスキップする
                existing = await service.get_full_page_text(content.source_id)
                if existing:
                    skipped += 1
                    continue

                try:
                    chunks = await service.ingest_content(content)
                    total_chunks += chunks
                    ingested_count += 1
                except Exception:
                    logger.exception(
                        "Failed to ingest BlueSky post: %s", content.source_id
                    )
                    errors += 1

            parts = [
                f"完了: {ingested_count}投稿 / {total_chunks}チャンク",
            ]
            if skipped > 0:
                parts.append(f"スキップ: {skipped}件")
            if errors > 0:
                parts.append(f"エラー: {errors}件")
            parts.append(f"（ハンドル: {handle}）")

            return " / ".join(parts)
    except (ValueError, TypeError) as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception(
            "Failed to crawl BlueSky posts for handle: %s", handle
        )
        return f"エラー: BlueSky 投稿の取り込みに失敗しました（ハンドル: {handle}）"


def _create_local_ingester() -> LocalFileIngester:
    """設定に基づいて LocalFileIngester を生成する."""
    settings = get_settings()
    extensions = [
        ext.strip() if ext.strip().startswith(".") else f".{ext.strip()}"
        for ext in settings.rag_local_supported_extensions.split(",")
        if ext.strip()
    ]
    return LocalFileIngester(supported_extensions=extensions)


@mcp.tool()
async def rag_add_local(file_path: str) -> str:
    """[rag-knowledge] RAG add local - ローカルファイルをナレッジベースに取り込む.

    knowledge base, ingest, local file, document.
    ローカルファイル（Markdown、テキスト、PDF、AsciiDoc）を読み取り、
    ナレッジベースに取り込む。同一ファイルの再取り込み時は既存の知識を最新に置き換える。
    stdio モード専用。HTTP モードでは無効。

    Args:
        file_path: 取り込み対象ファイルのパス（絶対パスまたは相対パス）

    Returns:
        取り込み結果のメッセージ（ファイル名、チャンク数）
    """
    if get_settings().rag_transport == "http":
        return "エラー: rag_add_local は HTTP モードでは無効です（セキュリティ上の制約）"

    service = await _get_rag_service()
    ingester = _create_local_ingester()

    try:
        content = await ingester.fetch_single(file_path)
        if content is None:
            return f"エラー: ファイルの取り込みに失敗しました。パス: {file_path}"
        chunks = await service.ingest_content(content)
        if chunks <= 0:
            return f"エラー: ファイルの取り込みに失敗しました。パス: {file_path}"
        return f"ファイルを取り込みました: {file_path} ({chunks}チャンク)"
    except ValueError as e:
        return f"エラー: {e}"
    except Exception:
        logger.exception("Failed to add local file: %s", file_path)
        return f"エラー: ファイルの取り込みに失敗しました。パス: {file_path}"


@mcp.tool()
async def rag_crawl_local(dir_path: str, pattern: str = "**/*") -> str:
    """[rag-knowledge] RAG crawl local - ディレクトリ内のファイルを一括取り込み.

    knowledge base, ingest, local directory, bulk import, glob.
    指定ディレクトリ内のファイルを glob パターンで検索し、
    一括でナレッジベースに取り込む。同一ファイルの再取り込み時は
    既存の知識を最新に置き換える。
    stdio モード専用。HTTP モードでは無効。

    Args:
        dir_path: 取り込み対象ディレクトリのパス（絶対パスまたは相対パス）
        pattern: glob パターン（デフォルト: ``**/*`` で再帰的に全対応ファイルを検索）

    Returns:
        取り込み結果のサマリーテキスト（処理ファイル数、総チャンク数、スキップ数、エラー数）
    """
    if get_settings().rag_transport == "http":
        return "エラー: rag_crawl_local は HTTP モードでは無効です（セキュリティ上の制約）"

    service = await _get_rag_service()
    ingester = _create_local_ingester()

    try:
        files = ingester.collect_files(dir_path, pattern)
    except ValueError as e:
        return f"エラー: {e}"

    if not files:
        return f"対象ファイルが見つかりませんでした（ディレクトリ: {dir_path}）"

    total_chunks = 0
    errors = 0
    skipped = 0
    ingested_count = 0

    for file in files:
        try:
            content = await ingester.fetch_single(str(file))
            if content is None:
                skipped += 1
                continue
            chunks = await service.ingest_content(content)
            total_chunks += chunks
            ingested_count += 1
        except Exception:
            logger.exception("Failed to ingest local file: %s", file)
            errors += 1

    parts = [
        f"完了: {ingested_count}ファイル / {total_chunks}チャンク",
    ]
    if skipped > 0:
        parts.append(f"スキップ: {skipped}件")
    if errors > 0:
        parts.append(f"エラー: {errors}件")
    parts.append(f"（ディレクトリ: {dir_path}）")

    return " / ".join(parts)


@mcp.tool()
async def rag_delete(url: str) -> str:
    """[rag-knowledge] RAG delete - ソースURL指定でナレッジから削除.

    knowledge base, remove source, delete document.

    Args:
        url: 削除するソースURL

    Returns:
        削除結果のメッセージ
    """
    service = await _get_rag_service()
    try:
        count = await service.delete_source(url)
        if count == 0:
            return f"該当するソースが見つかりませんでした: {url}"
        return f"削除しました: {url} ({count}チャンク)"
    except Exception:
        logger.exception("Failed to delete: %s", url)
        return f"エラー: 削除に失敗しました。URL: {url}"


@mcp.tool()
async def rag_stats() -> str:
    """[rag-knowledge] RAG stats - ナレッジベースの統計情報と蓄積データ概要を表示.

    knowledge base, statistics, chunk count, source count, source list.
    蓄積されているナレッジの概要（ソースURL一覧とタイトル）を返す。
    検索前にこのツールを呼ぶことで、ナレッジベースの内容を把握し
    適切な検索キーワードを構成できる。

    Returns:
        統計情報と蓄積データ概要のテキスト
    """
    service = await _get_rag_service()
    try:
        stats = await service.get_stats()
        total_chunks = stats.get("total_chunks", 0)
        source_count = stats.get("source_count", 0)
        sources = stats.get("sources", [])

        parts: list[str] = [
            "ナレッジベース統計:",
            f"  総チャンク数: {total_chunks}",
            f"  ソースURL数: {source_count}",
        ]

        if sources and isinstance(sources, list):
            max_sources = get_settings().rag_stats_max_sources
            parts.append("")
            parts.append("蓄積データ概要:")

            displayed = 0
            hit_limit = False
            for group in sources:
                if not isinstance(group, dict):
                    continue
                domain = group.get("domain", "unknown")
                pages = group.get("pages", [])
                if not isinstance(pages, list):
                    continue

                if displayed >= max_sources:
                    hit_limit = True
                    break

                page_count = len(pages)
                parts.append("")
                parts.append(f"[{domain}] ({page_count}ページ)")

                shown_in_domain = 0
                for page in pages:
                    if displayed >= max_sources:
                        hit_limit = True
                        remaining = page_count - shown_in_domain
                        if remaining > 0:
                            parts.append(f"  ... 他 {remaining} ページ")
                        break
                    if not isinstance(page, dict):
                        continue
                    title = page.get("title", "") or "(タイトル取得不可)"
                    url = page.get("url", "")
                    parts.append(f"  - {title} ({url})")
                    displayed += 1
                    shown_in_domain += 1

                if hit_limit:
                    break

            if hit_limit:
                parts.append("")
                parts.append(
                    f"(表示上限 {max_sources} 件に達したため省略されたソースがあります)"
                )

        return "\n".join(parts)
    except Exception:
        logger.exception("Failed to get stats")
        return "エラー: 統計情報の取得に失敗しました。"


def _configure_and_run() -> None:
    """トランスポート設定に基づいて MCP サーバーを起動する."""
    settings = get_settings()
    transport = settings.rag_transport

    if transport == "http":
        mcp.settings.host = settings.rag_http_host
        mcp.settings.port = settings.rag_http_port
        if not settings.rag_dns_rebinding_protection:
            if mcp.settings.transport_security is not None:
                mcp.settings.transport_security.enable_dns_rebinding_protection = (
                    False
                )
            else:
                logger.warning(
                    "transport_security is None; "
                    "cannot disable DNS rebinding protection"
                )

    try:
        if transport == "http":
            mcp.run(transport="streamable-http")
        else:
            mcp.run()
    except KeyboardInterrupt:
        logger.info("MCP server shut down")
        raise SystemExit(130)
    else:
        logger.info("MCP server shut down")


if __name__ == "__main__":
    _configure_and_run()

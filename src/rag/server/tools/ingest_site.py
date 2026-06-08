"""rag_site_ingest / rag_site_crawl MCP tools.

仕様: docs/specs/site-ingest.md

Issue #797 により、`site-ingest` の 2 つの責務（リンク辿りクロール vs 指定 URL 取得）を
別ツールに物理分離した:

- ``rag_site_ingest``: 指定 URL リストの取得（リンク辿りなし）。複数 URL 可
- ``rag_site_crawl``: 単一 URL を起点にリンクを辿るクロール。クロール固有オプションあり
"""

from __future__ import annotations

import logging

from .. import cli_subprocess
from .._mcp import mcp
from ..cli_subprocess import CLISubprocessError, MCPContext
from .. import safe_browsing_wiring
from ..fake_labels import _SITE_INGEST_FAKE_SOURCES, _fake_mode_labels
from ...safe_browsing import SafeBrowsingConfigError, SafetyCheckError

logger = logging.getLogger("rag.server")

# rag_site_crawl も web + embedding の fake source を利用する。再エイリアスで明示する
_SITE_CRAWL_FAKE_SOURCES = _SITE_INGEST_FAKE_SOURCES


@mcp.tool()
async def rag_site_ingest(
    urls: list[str],
    skip_pipeline: bool = False,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG site ingest - 指定 URL のページを取得（リンク辿りなし）.

    knowledge base, bulk ingest, fetch pages, multi url, no follow.
    指定された URL の Web ページを取得し source_store に配置する。
    **リンクは辿らない**（Issue #797 で導入された安全側デフォルト）。
    クロール（リンク辿り）が必要な場合は ``rag_site_crawl`` を使用する。

    並行実行非対応: Bridge が source_store へのファイル配置をロック保護外で
    実行するため、同じ URL 群に対する同時実行はデータ競合のリスクがある。

    Args:
        urls: 取得対象 URL のリスト（1 件以上必須）
        skip_pipeline: True の場合、Scrapy 取得 + Bridge まで実行し、
            パイプライン処理（コンバート・インデックス構築）をスキップする。
            CLI の --skip-pipeline フラグと等価。共通仕様は
            docs/specs/ingesters/common.md「--skip-pipeline フラグ共通仕様」を参照

    Returns:
        取り込み結果のサマリー
    """
    from ...utils.url import check_ssrf, validate_url

    label = _fake_mode_labels(_SITE_INGEST_FAKE_SOURCES)

    if not urls:
        return label + "エラー: urls には 1 件以上の URL を指定してください"

    validated_urls: list[str] = []
    for u in urls:
        try:
            validated = validate_url(u)
            check_ssrf(validated)
            validated_urls.append(validated)
        except ValueError as e:
            return label + f"エラー: {e}"

    # CLI subprocess に委譲
    # オプション注入対策: ユーザー入力の positional 群（validated_urls）は `--` 以降に置く。
    cli_args: list[str] = []
    if skip_pipeline:
        cli_args.append("--skip-pipeline")
    cli_args.append("--")
    cli_args.extend(validated_urls)

    display_url = (
        validated_urls[0]
        if len(validated_urls) == 1
        else f"{len(validated_urls)} URLs"
    )

    try:
        result = await cli_subprocess._run_cli_subprocess("site-ingest", cli_args, ctx=ctx)
        return label + cli_subprocess._format_cli_ingest_result(
            result, context=f"サイト: {display_url}",
        )
    except CLISubprocessError as e:
        return label + e.format_mcp_error(
            f"サイト取り込みに失敗しました（{display_url}）",
        )


@mcp.tool()
async def rag_site_crawl(
    url: str,
    url_pattern: str = "",
    max_pages: int | None = None,
    restart: bool = False,
    skip_pipeline: bool = False,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG site crawl - 単一 URL 起点でサイトをクロール（リンク辿りあり）.

    knowledge base, bulk ingest, site crawl, large scale, scrapy, follow links.
    Scrapy subprocess で開始 URL からリンクを辿り、サイト配下のページを一括取り込みする。
    リンク辿りなしの取得のみが必要な場合は ``rag_site_ingest`` を使用する。

    並行実行非対応: Bridge が source_store へのファイル配置をロック保護外で
    実行するため、同一サイトに対する同時実行はデータ競合のリスクがある。

    Args:
        url: クロール開始 URL（単一）
        url_pattern: URL フィルタパターン（正規表現）。未指定時は開始 URL の
            パスプレフィックスから自動生成する
        max_pages: ページ数上限（未指定時は設定値を使用）
        restart: True の場合、JOBDIR + 一時 HTML/JSONL を削除して最初から再クロール
        skip_pipeline: True の場合、Scrapy クロール + Bridge まで実行し、
            パイプライン処理（コンバート・インデックス構築）をスキップする

    Returns:
        取り込み結果のサマリー
    """
    from ...utils.url import check_ssrf, validate_url

    label = _fake_mode_labels(_SITE_CRAWL_FAKE_SOURCES)

    if not url:
        return label + "エラー: url を指定してください"

    try:
        validated_url = validate_url(url)
        check_ssrf(validated_url)
    except ValueError as e:
        return label + f"エラー: {e}"

    # Safe Browsing チェック（起点 URL のみ）
    try:
        sb_client = safe_browsing_wiring._get_safe_browsing_client()
        if sb_client is not None:
            sb_result = await sb_client.check_url(validated_url)
            if not sb_result.is_safe:
                threat_types = ", ".join(
                    t.threat_type.value for t in sb_result.threats
                )
                return label + (
                    f"エラー: 起点URLが安全でないと判定されました: "
                    f"{threat_types} — {validated_url}"
                )
    except SafeBrowsingConfigError:
        logger.warning(
            "Safe Browsing の設定エラーのためチェックをスキップします: %s",
            validated_url,
        )
    except SafetyCheckError as e:
        return label + f"エラー: URL安全性チェックに失敗しました: {e}"

    # url_pattern バリデーション
    if url_pattern:
        import re
        try:
            re.compile(url_pattern)
        except re.error as e:
            return label + f"エラー: 無効な正規表現パターン: {e}"

    # CLI subprocess に委譲
    cli_args: list[str] = []
    if url_pattern:
        cli_args.extend(["--url-pattern", url_pattern])
    if max_pages is not None:
        cli_args.extend(["--max-pages", str(max_pages)])
    if restart:
        cli_args.append("--restart")
    if skip_pipeline:
        cli_args.append("--skip-pipeline")
    cli_args.append("--")
    cli_args.append(validated_url)

    try:
        result = await cli_subprocess._run_cli_subprocess(
            "site-crawl", cli_args, ctx=ctx,
        )
        return label + cli_subprocess._format_cli_ingest_result(
            result, context=f"サイト: {validated_url}",
        )
    except CLISubprocessError as e:
        return label + e.format_mcp_error(
            f"サイトクロールに失敗しました（{validated_url}）",
        )

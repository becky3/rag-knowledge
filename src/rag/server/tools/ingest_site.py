"""rag_site_ingest MCP tool.

仕様: docs/specs/site-ingest.md
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


@mcp.tool()
async def rag_site_ingest(
    url: str = "",
    urls: list[str] | None = None,
    url_pattern: str = "",
    max_pages: int | None = None,
    force: bool = False,
    skip_pipeline: bool = False,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG site ingest - Scrapy でサイトを一括取り込み.

    knowledge base, bulk ingest, site crawl, large scale, scrapy, multi url.
    Scrapy subprocess で Web ページを一括取り込みする。
    単一 URL: リンクを辿るクロールモード（大規模サイト向け）。
    複数 URL: 指定 URL のみ取得する複数 URL モード。

    並行実行非対応: Bridge が source_store へのファイル配置をロック保護外で
    実行するため、同一サイトに対する同時実行はデータ競合のリスクがある。

    Args:
        url: クロール開始 URL（クロールモード、urls と排他）
        urls: 取得対象 URL のリスト（複数 URL モード、url と排他）
        url_pattern: URL フィルタパターン（正規表現、クロールモードのみ）
        max_pages: ページ数上限（クロールモードのみ、未指定時は設定値を使用）
        force: True の場合、JOBDIR を削除して最初からクロール（クロールモードのみ）
        skip_pipeline: True の場合、Scrapy クロール + Bridge まで実行し、
            パイプライン処理（コンバート・インデックス構築）をスキップする。
            CLI の --skip-pipeline フラグと等価。共通仕様は
            docs/specs/ingesters/common.md「--skip-pipeline フラグ共通仕様」を参照

    Returns:
        取り込み結果のサマリー
    """
    from ...utils.url import check_ssrf, validate_url

    label = _fake_mode_labels(_SITE_INGEST_FAKE_SOURCES)

    # url / urls の排他チェック
    effective_urls = urls or []
    if url and effective_urls:
        return label + "エラー: url と urls は排他です。どちらか一方のみ指定してください"
    if not url and not effective_urls:
        return label + "エラー: url または urls を指定してください"

    # 単一 URL モード → リストに統一
    if url:
        effective_urls = [url]

    multi_url_mode = len(effective_urls) >= 2

    # 全 URL バリデーション
    validated_urls: list[str] = []
    for u in effective_urls:
        try:
            validated = validate_url(u)
            check_ssrf(validated)
            validated_urls.append(validated)
        except ValueError as e:
            return label + f"エラー: {e}"

    # Safe Browsing チェック（クロールモードのみ: 起点 URL）
    # 複数 URL モードでは数百件の URL に対する Google Safe Browsing API 呼び出しは
    # 非現実的なためスキップする（仕様: site-ingest.md「Safe Browsing チェック」）
    if not multi_url_mode:
        try:
            sb_client = safe_browsing_wiring._get_safe_browsing_client()
            if sb_client is not None:
                sb_result = await sb_client.check_url(validated_urls[0])
                if not sb_result.is_safe:
                    threat_types = ", ".join(t.threat_type.value for t in sb_result.threats)
                    return label + f"エラー: 起点URLが安全でないと判定されました: {threat_types} — {validated_urls[0]}"
        except SafeBrowsingConfigError:
            logger.warning("Safe Browsing の設定エラーのためチェックをスキップします: %s", validated_urls[0])
        except SafetyCheckError as e:
            return label + f"エラー: URL安全性チェックに失敗しました: {e}"

    # CLI subprocess に委譲
    # オプション注入対策: ユーザー入力の positional 群（validated_urls）は `--` 以降に置く。
    # validate_url + check_ssrf を通過済みだが、一貫性とフォールバック安全性のため separator を入れる。
    cli_args: list[str] = []
    if not multi_url_mode:
        if url_pattern:
            import re
            try:
                re.compile(url_pattern)
            except re.error as e:
                return label + f"エラー: 無効な正規表現パターン: {e}"
            cli_args.extend(["--url-pattern", url_pattern])
        if max_pages is not None:
            cli_args.extend(["--max-pages", str(max_pages)])
        if force:
            cli_args.append("--force")
    if skip_pipeline:
        cli_args.append("--skip-pipeline")
    cli_args.append("--")
    cli_args.extend(validated_urls)

    display_url = validated_urls[0] if not multi_url_mode else f"{len(validated_urls)} URLs"

    try:
        result = await cli_subprocess._run_cli_subprocess("site-ingest", cli_args, ctx=ctx)
        return label + cli_subprocess._format_cli_ingest_result(result, context=f"サイト: {display_url}")
    except CLISubprocessError as e:
        return label + e.format_mcp_error(f"サイト取り込みに失敗しました（{display_url}）")

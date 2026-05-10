"""rag_add_document / rag_crawl_documents / rag_add_journal MCP tool.

仕様: docs/specs/ingesters/local.md
     docs/specs/ingesters/journal.md
"""

from __future__ import annotations

from .. import cli_subprocess
from .._mcp import mcp
from ..cli_subprocess import CLISubprocessError, MCPContext
from ..fake_labels import (
    _JOURNAL_INGEST_FAKE_SOURCES,
    _LOCAL_INGEST_FAKE_SOURCES,
    _fake_mode_labels,
)
from ... import config
from ...upload import sanitize_filename as sanitize_upload_filename


def _get_supported_extensions() -> list[str]:
    """設定からサポート拡張子リストを取得する."""
    settings = config.get_settings()
    return [
        (ext.strip() if ext.strip().startswith(".") else f".{ext.strip()}").lower()
        for ext in settings.rag_document_supported_extensions.split(",")
        if ext.strip()
    ]


@mcp.tool()
async def rag_add_document(
    content: str,
    filename: str,
    encoding: str = "text",
    upload_mode: str = "fail",
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG add document - ドキュメントファイルをナレッジベースに取り込む.

    knowledge base, ingest, document, file, text, upload.
    ドキュメントファイル（Markdown、テキスト、PDF、AsciiDoc）のコンテンツを受け取り、
    ナレッジベースに取り込む。stdio・HTTP 両モードで使用可能。

    Args:
        content: ファイルのコンテンツ。encoding=text の場合は UTF-8 文字列、
            encoding=base64 の場合は base64 エンコードされた文字列
        filename: 元ファイルのファイル名（例: resume.pdf, notes.md）。
            拡張子バリデーションおよびファイル配置先の命名に使用する
        encoding: コンテンツのエンコーディング。"text"（デフォルト）または "base64"。
            テキストファイルは "text"、バイナリファイル（PDF 等）は "base64" を使用する
        upload_mode: 同名ファイル存在時の動作。"fail"（エラー、デフォルト）または "replace"（上書き）

    Returns:
        取り込み結果のメッセージ
    """
    label = _fake_mode_labels(_LOCAL_INGEST_FAKE_SOURCES)

    # MCP 側バリデーション（仕様: content-upload.md）
    if encoding not in ("text", "base64"):
        return label + f"エラー: 無効な encoding: {encoding!r}（有効値: text, base64）"
    if not content:
        return label + "エラー: content が空です"

    try:
        sanitized_filename = sanitize_upload_filename(filename)
    except ValueError as e:
        return label + f"エラー: {e}"

    args: list[str] = ["--stdin", "--filename", sanitized_filename]
    if encoding != "text":
        args.extend(["--encoding", encoding])
    if upload_mode != "fail":
        args.extend(["--upload-mode", upload_mode])

    try:
        result = await cli_subprocess._run_cli_subprocess(
            "add-document", args, ctx=ctx, stdin_data=content,
        )
        return label + cli_subprocess._format_cli_ingest_result(result, context=sanitized_filename)
    except CLISubprocessError as e:
        return label + e.format_mcp_error(f"ファイルの取り込みに失敗しました: {sanitized_filename}")


@mcp.tool()
async def rag_add_journal(
    title: str,
    content: str,
    filename: str,
    repository: str,
    entry_id: str | None = None,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG add journal - ジャーナルエントリをナレッジベースに登録する.

    journal, session log, work record, development diary.
    セッションごとの作業記録（ジャーナル）をナレッジベースに追加する。
    stdio・HTTP 両モードで使用可能。

    Args:
        title: エントリタイトル
        content: ジャーナル本文（Markdown）。MCP クライアントがファイルを読み込んでテキスト文字列として渡す
        filename: 元ファイルのファイル名（例: session-summary.md）。.md 拡張子であることの確認に使用する
        repository: リポジトリ名（例: rag-knowledge）
        entry_id: エントリ識別子（更新時に使用。未指定時は自動生成。命名規則: YYYYMMDD-HHMMSS-topic）

    Returns:
        取り込み結果のメッセージ
    """
    label = _fake_mode_labels(_JOURNAL_INGEST_FAKE_SOURCES)

    try:
        sanitized_filename = sanitize_upload_filename(filename)
    except ValueError as e:
        return label + f"エラー: {e}"

    if not sanitized_filename.lower().endswith(".md"):
        return label + f"エラー: filename の拡張子が .md ではありません: {sanitized_filename!r}"

    args: list[str] = ["--stdin", "--title", title, "--repository", repository]
    if entry_id:
        args.extend(["--entry-id", entry_id])

    try:
        result = await cli_subprocess._run_cli_subprocess(
            "add-journal", args, ctx=ctx, stdin_data=content,
        )
        return label + cli_subprocess._format_cli_ingest_result(
            result, context=f"journal: {repository}/{entry_id or title}",
        )
    except CLISubprocessError as e:
        return label + e.format_mcp_error(f"ジャーナルエントリの登録に失敗しました: {title}")


@mcp.tool()
async def rag_crawl_documents(
    dir_path: str,
    pattern: str = "**/*",
    upload_mode: str = "fail",
    skip_pipeline: bool = False,
    ctx: MCPContext | None = None,
) -> str:
    """[rag-knowledge] RAG crawl documents - ディレクトリ内のドキュメントを一括取り込み.

    knowledge base, ingest, document directory, bulk import, glob.
    指定ディレクトリ内のドキュメントファイルを glob パターンで検索し、
    一括でナレッジベースに取り込む。
    stdio モード専用。HTTP モードではクライアントとサーバーが別マシンの可能性があり、
    ローカルパスを解決できないため無効。

    Args:
        dir_path: 取り込み対象ディレクトリのパス（絶対パスまたは相対パス）
        pattern: glob パターン（デフォルト: ``**/*`` で再帰的に全対応ファイルを検索）
        upload_mode: 同名ファイル存在時の動作。"fail"（スキップ、デフォルト）または "replace"（上書き）
        skip_pipeline: True の場合、後段のパイプライン処理（converter + indexer）を
            スキップする。CLI の `--skip-pipeline` と等価

    Returns:
        取り込み結果のサマリーテキスト
    """
    if config.get_settings().rag_transport == "http":
        return "エラー: rag_crawl_documents は HTTP モードでは無効です（クライアントとサーバーが別マシンの可能性があり、ローカルパスを解決できないため）"

    args: list[str] = [dir_path]
    if pattern != "**/*":
        args.extend(["--pattern", pattern])
    if upload_mode != "fail":
        args.extend(["--upload-mode", upload_mode])
    if skip_pipeline:
        args.append("--skip-pipeline")

    label = _fake_mode_labels(_LOCAL_INGEST_FAKE_SOURCES)
    try:
        result = await cli_subprocess._run_cli_subprocess("crawl-documents", args, ctx=ctx)
        return label + cli_subprocess._format_cli_ingest_result(result, context=f"ディレクトリ: {dir_path}")
    except CLISubprocessError as e:
        return label + e.format_mcp_error(f"ドキュメントの取り込みに失敗しました（ディレクトリ: {dir_path}）")

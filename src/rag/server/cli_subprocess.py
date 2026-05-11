"""CLI サブプロセス起動・JSON Lines パース・共有フォーマッター.

仕様: docs/specs/rag-knowledge.md「MCP 薄層アダプターパターン」「server 構造」
     docs/specs/rebuild-stats.md「MCP 応答契約」
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import sys
from typing import Any

from .logging_setup import _sanitize_log_value, _write_cli_lines_to_handlers

# bm25s が "resource module not available on Windows" を stdout に print する
# 問題への対策として、import 時に stdout を抑制する。
with contextlib.redirect_stdout(io.StringIO()):
    from ..pipeline.ingesters._common import IngestResult
    from ..pipeline.models import (
        PipelineMode,
        PipelineSummary,
        format_pipeline_error as _format_pipeline_error,
        format_pipeline_warning as _format_pipeline_warning,
    )

# MCP Context の具象型パラメータ（ツール関数では型パラメータ不要のため Any で統一）
from mcp.server.fastmcp import Context  # noqa: E402

MCPContext = Context[Any, Any, Any]

logger = logging.getLogger("rag.server")


# SEGFAULT を示す exit code
# Windows: 0xC0000005 は signed (-1073741819) / unsigned (3221225477) 両方で返りうる
# Unix: SIGSEGV=11, shell: 128+11=139
_SEGFAULT_EXIT_CODES: frozenset[int] = frozenset({-1073741819, 3221225477, -11, 139})

# asyncio StreamReader の行バッファ上限。
# デフォルト 64KiB では rag_get_document が 64KiB 超のドキュメントで
# `Separator is found, but chunk is longer than limit` で失敗するため、
# 1 行あたり 10MiB まで許容する。
_STDOUT_LINE_BUFFER_LIMIT: int = 10 * 1024 * 1024


class CLISubprocessError(Exception):
    """CLI サブプロセスの実行エラー."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        lock_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.lock_type = lock_type

    @property
    def lock_conflict(self) -> bool:
        """ロック競合エラーであるかを判定する."""
        return self.code == "LOCK_CONFLICT"

    def format_mcp_error(self, context: str = "") -> str:
        """MCP ツール用のエラーメッセージを生成する.

        全エラーで「エラー: <context>\\n原因: <cause>」形式に統一する。
        メッセージには再試行を促す文言を含めない（再試行タイミングは
        HTTP API では Retry-After ヘッダで伝達し、MCP ツールでは
        状態の事実のみを伝える）。
        """
        if self.lock_conflict:
            if self.lock_type == "rebuild":
                cause = "再構築処理中のためロックが取れません"
            elif self.lock_type == "write":
                cause = "別の取り込みが実行中のためロックが取れません"
            else:
                cause = "別のプロセスがロックを保持しています"
            return f"エラー: ロック競合により処理を受け付けられません\n原因: {cause}"
        if context:
            return f"エラー: {context}\n原因: {self}"
        return f"エラー: {self}"


async def _run_cli_subprocess(
    command: str,
    args: list[str] | None = None,
    *,
    ctx: MCPContext | None = None,
    stdin_data: str | None = None,
) -> dict[str, Any]:
    """CLI コマンドをサブプロセスで実行し結果を返す.

    MCP 薄層アダプターの中核関数。書き込み系ツールを CLI サブプロセスとして実行し、
    C 拡張（BM25s 等）の SEGFAULT からサーバープロセスを隔離する。

    Args:
        command: CLI サブコマンド名（例: "add", "crawl", "rebuild"）
        args: サブコマンド引数のリスト
        ctx: MCP Context（進捗通知用、任意）
        stdin_data: stdin に書き込むデータ（add-journal, add-document 用）

    Returns:
        CLI が出力した result JSON の dict

    Raises:
        CLISubprocessError: サブプロセスの異常終了・クラッシュ・結果パース失敗時。
            code 属性に CLI エラーコード（CliErrorCode の値）を保持する。
    """
    cmd = [
        sys.executable, "-m", "rag.cli",
        command, "--output", "json",
        *(args or []),
    ]

    logger.info("CLI subprocess: %s %s", command, _sanitize_log_value(" ".join(args or [])))

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    stdin_mode = asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stdin=stdin_mode,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=_STDOUT_LINE_BUFFER_LIMIT,
    )

    # stdin にデータを書き込んでクローズする
    if stdin_data is not None:
        assert process.stdin is not None  # noqa: S101
        try:
            process.stdin.write(stdin_data.encode("utf-8"))
            await process.stdin.drain()
        finally:
            process.stdin.close()
            await process.stdin.wait_closed()

    async def _read_stdout() -> tuple[str, str]:
        """stdout を行単位で読み、progress を処理し、最終 result 行・error 行を返す."""
        _result_line = ""
        _error_line = ""
        assert process.stdout is not None  # noqa: S101
        while True:
            try:
                raw = await process.stdout.readline()
            except ValueError as exc:
                # _STDOUT_LINE_BUFFER_LIMIT を超える 1 行は asyncio.StreamReader が
                # `Separator is found, but chunk is longer than limit` で
                # ValueError を投げる。MCP ツール側で扱えるよう CLISubprocessError
                # にラップする（仕様: docs/specs/search-response.md, rag-knowledge.md）。
                limit_mib = _STDOUT_LINE_BUFFER_LIMIT // (1024 * 1024)
                raise CLISubprocessError(
                    f"応答が上限 ({limit_mib}MiB) を超えました。"
                    f"CLI の get-document コマンド（--output-file オプション）で全文取得できます",
                ) from exc
            if not raw:
                break
            line = raw.decode("utf-8").strip()
            if not line:
                continue

            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    msg_type = data.get("type")
                    if msg_type == "progress":
                        if ctx is not None:
                            processed = data.get("processed", 0)
                            total = data.get("total", 0)
                            current = data.get("current", "")
                            await ctx.report_progress(
                                float(processed), float(total),
                                message=current,
                            )
                        continue
                    if msg_type == "error":
                        _error_line = line
                    elif msg_type == "result":
                        _result_line = line
            except json.JSONDecodeError:
                pass
        return _result_line, _error_line

    async def _drain_stderr() -> bytes:
        """stderr を並行に drain する（パイプバッファ溢れ防止）."""
        if process.stderr is None:
            return b""
        return await process.stderr.read()

    # stdout と stderr を並行に読み取る（パイプバッファのデッドロック防止）
    try:
        (result_line, error_line), stderr_bytes = await asyncio.gather(
            _read_stdout(),
            _drain_stderr(),
        )
    except asyncio.CancelledError:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(asyncio.CancelledError):
                await process.wait()
        raise
    except CLISubprocessError:
        # _read_stdout が ValueError をラップした CLISubprocessError を送出した場合、
        # 子プロセスが残留・ゾンビ化しないよう kill()+wait() で後始末する
        # （CancelledError 経路と同じパターン）。
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(asyncio.CancelledError):
                await process.wait()
        raise

    await process.wait()

    assert process.returncode is not None  # noqa: S101
    exit_code = process.returncode

    # stderr 側は decode 失敗で本来のエラー行が落ちないよう replace を許容する。
    # encoding 違反自体は U+FFFD として stdout 応答に残り e2e の assert_no_mojibake で検出する。
    stderr_text = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    stderr_lines = stderr_text.rstrip().splitlines()
    stderr_tail = "\n".join(stderr_lines[-10:])

    if stderr_lines:
        _write_cli_lines_to_handlers(stderr_lines)
    logger.debug("CLI subprocess %s exit_code=%d", command, exit_code)

    # 判定は優先順位「SEGFAULT > error line > result line > 出力なし」。
    # exit_code は判定に使わない（CLI の 2 値契約: 0=完走 / 1=致命、
    # workload の errors/aborted は result JSON で表現する）。
    if exit_code in _SEGFAULT_EXIT_CODES:
        logger.error(
            "CLI subprocess %s crashed: SEGFAULT exit_code=%d",
            command, exit_code,
        )
        raise CLISubprocessError(
            f"CLI サブプロセスがクラッシュしました (SEGFAULT, exit_code={exit_code})"
        )

    if error_line:
        try:
            error_data = json.loads(error_line)
        except json.JSONDecodeError as exc:
            logger.error(
                "CLI subprocess %s failed: error line parse failed: %s",
                command, error_line,
            )
            raise CLISubprocessError(
                f"CLI サブプロセスのエラー行パースに失敗: {error_line}"
            ) from exc
        error_msg = error_data.get("message", "不明なエラー")
        error_code = error_data.get("code")
        error_details = error_data.get("details")
        lock_type: str | None = None
        if isinstance(error_details, dict):
            candidate = error_details.get("lock_type")
            if candidate in ("write", "rebuild"):
                lock_type = candidate
        logger.warning(
            "CLI subprocess %s failed: %s (code=%s, lock_type=%s)",
            command, error_msg, error_code, lock_type,
        )
        raise CLISubprocessError(
            error_msg,
            code=error_code,
            lock_type=lock_type,
        )

    if not result_line:
        logger.error(
            "CLI subprocess %s failed: empty output (exit_code=%d)",
            command, exit_code,
        )
        raise CLISubprocessError(
            f"CLI サブプロセスの出力が空です (exit_code={exit_code})\n{stderr_tail}"
        )

    try:
        result: dict[str, Any] = json.loads(result_line)
    except json.JSONDecodeError as exc:
        logger.error(
            "CLI subprocess %s failed: result parse failed: %s",
            command, result_line,
        )
        raise CLISubprocessError(
            f"CLI サブプロセスの結果パースに失敗: {result_line}"
        ) from exc

    logger.info("CLI subprocess completed: %s (exit_code=%d)", command, exit_code)
    return result


# --- 共有フォーマッター（複数ツールで共用） ---


def _format_ingest_response(
    ingest_result: IngestResult,
    pipeline_summary: PipelineSummary | None,
    *,
    context: str = "",
    url_follow: dict[str, int] | None = None,
) -> str:
    """IngestResult + PipelineSummary を統合レスポンスに変換する."""
    parts = [ingest_result.summary(context=context)]
    if pipeline_summary is not None:
        parts.append(f"パイプライン: {pipeline_summary.processed}件処理")
        if pipeline_summary.warnings:
            details = "; ".join(
                _format_pipeline_warning(w) for w in pipeline_summary.warnings[:5]
            )
            parts.append(f"パイプライン警告: {len(pipeline_summary.warnings)}件 ({details})")
        if pipeline_summary.errors:
            details = "; ".join(
                _format_pipeline_error(e) for e in pipeline_summary.errors[:5]
            )
            parts.append(f"パイプラインエラー: {len(pipeline_summary.errors)}件 ({details})")
    if url_follow:
        web_placed = url_follow.get("web_placed", 0)
        web_overwritten = url_follow.get("web_overwritten", 0)
        yt_placed = url_follow.get("youtube_placed", 0)
        yt_overwritten = url_follow.get("youtube_overwritten", 0)
        err_n = url_follow.get("errors", 0)
        web_total = web_placed + web_overwritten
        yt_total = yt_placed + yt_overwritten
        if web_total > 0 or yt_total > 0 or err_n > 0:
            seg = [
                f"URL 自動取り込み: Web {web_total}件"
                f" (新規 {web_placed}, 上書き {web_overwritten}),"
                f" YouTube {yt_total}件"
                f" (新規 {yt_placed}, 上書き {yt_overwritten})",
            ]
            if err_n > 0:
                seg.append(f"エラー {err_n}件")
            parts.append(" ".join(seg))
    return " / ".join(parts)


def _format_cli_ingest_result(
    result: dict[str, Any],
    *,
    context: str = "",
) -> str:
    """CLI サブプロセスの result dict を MCP レスポンス文字列に変換する.

    _run_cli_subprocess の戻り値（IngestResult + PipelineSummary の dict 表現）を
    既存の _format_ingest_response と同等のフォーマットに変換する。

    IngestResult の全観測性フィールド（partial_failures / aborted 等）は
    CLI が JSON に含める契約（仕様: docs/specs/ingesters/common.md）に従い、
    ここでは常にキーが存在する前提で再構築する。
    """
    ingest_result = IngestResult(
        placed=result.get("placed", 0),
        skipped=result.get("skipped", 0),
        overwritten=result.get("overwritten", 0),
        errors=result.get("errors", 0),
        error_details=result.get("error_details", []),
        partial_failures=result.get("partial_failures", 0),
        partial_failure_details=result.get("partial_failure_details", []),
        aborted=result.get("aborted", False),
        abort_reason=result.get("abort_reason"),
    )

    pipeline_data = result.get("pipeline")
    pipeline_summary = _parse_pipeline_summary(pipeline_data) if pipeline_data else None

    url_follow_data = result.get("url_follow")
    url_follow: dict[str, int] | None = None
    if isinstance(url_follow_data, dict):
        url_follow = {
            "web_placed": int(url_follow_data.get("web_placed", 0)),
            "web_overwritten": int(url_follow_data.get("web_overwritten", 0)),
            "youtube_placed": int(url_follow_data.get("youtube_placed", 0)),
            "youtube_overwritten": int(
                url_follow_data.get("youtube_overwritten", 0),
            ),
            "errors": int(url_follow_data.get("errors", 0)),
        }

    return _format_ingest_response(
        ingest_result, pipeline_summary, context=context, url_follow=url_follow,
    )


def _parse_pipeline_summary(data: dict[str, Any]) -> PipelineSummary | None:
    """JSON dict から PipelineSummary を復元する.

    `errors` は `{path, size_bytes, message, phase}` スキーマを持つ dict の
    リストとして渡される前提（仕様: docs/specs/pipeline-controller.md）。
    パース失敗時は None を返す。
    """
    try:
        return PipelineSummary(
            mode=PipelineMode(data.get("mode", "incremental")),
            total_files=data.get("total_files", 0),
            processed=data.get("processed", 0),
            errors=data.get("errors", []),
            warnings=data.get("warnings", []),
        )
    except ValueError:
        return None


def _format_chunk_position(chunk_index: int, total_chunks: int) -> str:
    """チャンク位置を表示用文字列にフォーマットする."""
    pos = chunk_index + 1
    if total_chunks > 0:
        return f"{pos}/{total_chunks}"
    return f"{pos}/?"


def _format_phase_summary(phase: str, summary: PipelineSummary) -> list[str]:
    """1フェーズの PipelineSummary をテキスト行リストに変換する."""
    parts = [
        f"  [{phase}]",
        f"    処理件数: {summary.processed}",
        f"    警告: {len(summary.warnings)}",
        f"    エラー: {len(summary.errors)}",
    ]
    if summary.warnings:
        parts.append("    警告詳細:")
        for warn in summary.warnings[:10]:
            parts.append(f"      - {_format_pipeline_warning(warn)}")
        if len(summary.warnings) > 10:
            parts.append(f"      ... 他 {len(summary.warnings) - 10} 件")
    if summary.errors:
        parts.append("    エラー:")
        for entry in summary.errors[:10]:
            parts.append(f"      - {_format_pipeline_error(entry)}")
        if len(summary.errors) > 10:
            parts.append(f"      ... 他 {len(summary.errors) - 10} 件")
    return parts


def _format_rebuild_summary(summary: PipelineSummary, elapsed: float) -> str:
    """PipelineSummary をテキストに変換する."""
    mode_names = {
        PipelineMode.CONVERT_ONLY: "コンバートのみ再実行",
        PipelineMode.INDEX_ONLY: "インデックスのみ再構築",
        PipelineMode.INCREMENTAL: "差分更新",
    }
    mode_name = mode_names.get(summary.mode, str(summary.mode.value))

    parts = [
        f"再構築完了 ({mode_name})",
        f"  処理件数: {summary.processed}",
        f"  警告: {len(summary.warnings)}",
        f"  エラー: {len(summary.errors)}",
        f"  所要時間: {elapsed:.1f} 秒",
    ]
    if summary.warnings:
        parts.append("  警告詳細:")
        for warn in summary.warnings[:10]:
            parts.append(f"    - {_format_pipeline_warning(warn)}")
        if len(summary.warnings) > 10:
            parts.append(f"    ... 他 {len(summary.warnings) - 10} 件")
    if summary.errors:
        parts.append("  エラー:")
        for entry in summary.errors[:10]:
            parts.append(f"    - {_format_pipeline_error(entry)}")
        if len(summary.errors) > 10:
            parts.append(f"    ... 他 {len(summary.errors) - 10} 件")

    return "\n".join(parts)


def _format_full_rebuild_summary(
    convert: PipelineSummary,
    index: PipelineSummary,
    elapsed: float,
) -> str:
    """FullRebuildResult の2フェーズ結果をテキストに変換する."""
    parts = ["再構築完了 (全再構築)"]
    parts.extend(_format_phase_summary("Convert", convert))
    parts.extend(_format_phase_summary("Index", index))
    parts.append(f"  所要時間: {elapsed:.1f} 秒")
    return "\n".join(parts)

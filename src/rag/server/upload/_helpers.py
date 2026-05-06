"""Upload HTTP API のレスポンス生成・multipart フォーム値デコード・ファイル読み取り.

仕様: docs/specs/infrastructure/content-upload.md
"""

from __future__ import annotations

import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..cli_subprocess import CLISubprocessError
from ... import config


def _upload_error(
    status_code: int,
    message: str,
    *,
    retry_after: int | None = None,
) -> JSONResponse:
    """Upload API のエラーレスポンスを生成する.

    retry_after が指定された場合、`Retry-After` ヘッダを付与する。
    クライアントへの再試行間隔のヒントとして使う（ロック競合時等）。
    """
    headers: dict[str, str] | None = None
    if retry_after is not None:
        headers = {"Retry-After": str(retry_after)}
    return JSONResponse(
        {"status": "error", "message": message},
        status_code=status_code,
        headers=headers,
    )


def _extract_pipeline_response(cli_result: dict[str, Any]) -> dict[str, Any]:
    """CLI subprocess の result dict から Upload API の pipeline ブロックを抽出する.

    Upload API は単一ファイル投入が前提のため、batch 用フィールド（partial_failures
    等）と固定値フィールド（pipeline.mode / total_files）は含めない。
    `placed`・`overwritten` は IngestResult、`processed`・`warnings`・`errors` は
    PipelineSummary 由来。CLI 由来の `warnings` / `errors` は `PipelineWarningEntry` /
    `PipelineErrorEntry` の dict 配列だが、Upload API では境界で `message` 文字列のみを
    抽出してクライアントに返す（内部 path / phase 等の構造を公開契約に固定しないため）。
    """
    pipeline = cli_result.get("pipeline") or {}
    return {
        "placed": cli_result.get("placed", 0),
        "overwritten": cli_result.get("overwritten", 0),
        "processed": pipeline.get("processed", 0),
        "warnings": _extract_messages(pipeline.get("warnings", [])),
        "errors": _extract_messages(pipeline.get("errors", [])),
    }


def _extract_messages(entries: list[Any]) -> list[str]:
    """PipelineWarningEntry / PipelineErrorEntry の配列から message 文字列のみを抽出する.

    要素が dict（`{path, message, phase}` 等）の場合は `message` フィールドを取り出す。
    既に文字列の場合（テスト mock や互換性のため）はそのまま返す。
    """
    result: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            msg = entry.get("message", "")
            if isinstance(msg, str):
                result.append(msg)
        elif isinstance(entry, str):
            result.append(entry)
    return result


def _upload_success(
    message: str,
    source_id: str,
    *,
    cli_result: dict[str, Any] | None = None,
) -> JSONResponse:
    """Upload API の成功レスポンスを生成する.

    cli_result が指定された場合、`pipeline` ブロックをレスポンスに含める。
    """
    body: dict[str, Any] = {"status": "ok", "message": message, "source_id": source_id}
    if cli_result is not None:
        body["pipeline"] = _extract_pipeline_response(cli_result)
    return JSONResponse(body)


def _upload_lock_conflict_response(e: CLISubprocessError) -> JSONResponse:
    """ロック競合時の Upload API 応答を生成する.

    lock_type に応じて HTTP ステータスと Retry-After を切り替える:
    - write 競合: HTTP 429 + `rag_upload_retry_after_write_sec`
    - rebuild 競合: HTTP 503 + `rag_upload_retry_after_rebuild_sec`
    - lock_type 不明（後方互換）: HTTP 429 + write 値（安全側の短めヒント）

    仕様: docs/specs/infrastructure/content-upload.md の HTTP ステータスコード表
    """
    settings = config.get_settings()
    if e.lock_type == "rebuild":
        return _upload_error(
            503,
            "再構築処理中のため受け付けられません",
            retry_after=settings.rag_upload_retry_after_rebuild_sec,
        )
    return _upload_error(
        429,
        "別の取り込みが実行中のため受け付けられません",
        retry_after=settings.rag_upload_retry_after_write_sec,
    )


_CJK_PATTERN = re.compile(r"[぀-ゟ゠-ヿ一-鿿]")
"""ひらがな・カタカナ・CJK 漢字の検出パターン."""


def _decode_form_value(value: str) -> str:
    """Starlette の Latin-1 フォールバックで壊れたフォーム値を UTF-8/cp932 に復元する.

    Starlette の MultiPartParser は非 UTF-8 バイト列を受信すると UTF-8 デコードに
    失敗し Latin-1 にフォールバックする。この関数は Latin-1 文字列をバイト列に戻し、
    UTF-8 → cp932 の順で再デコードすることで元の文字列を復元する。

    主な発生パターン:
    - Windows curl（cp932 コンソール）から日本語を送信した場合
    - UTF-8 バイト列が Latin-1 にフォールバックした場合

    cp932 誤判定の防止:
    - cp932 デコード後に日本語文字（ひらがな・カタカナ・CJK 漢字）が含まれない場合、
      正当な Latin-1 入力（例: "¡Hola!"）と判断し元の値を返す。
    """
    try:
        raw_bytes = value.encode("latin-1")
    except UnicodeEncodeError:
        return value
    # UTF-8 を優先（cp932 の一部バイト列が偶然 UTF-8 として解釈されるのを防ぐ）
    try:
        return raw_bytes.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        pass
    # cp932: デコード成功しても日本語文字が含まれなければ誤判定とみなす
    try:
        decoded = raw_bytes.decode("cp932")
        if _CJK_PATTERN.search(decoded):
            return decoded
    except (UnicodeDecodeError, ValueError):
        pass
    return value


async def _read_upload_file(
    request: Request,
    max_size_bytes: int,
) -> tuple[bytes, str] | JSONResponse:
    """multipart/form-data からファイルを読み取る.

    Returns:
        (data, filename) タプル、またはエラー時は JSONResponse
    """
    # Content-Length による事前チェック
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_size_bytes:
                max_mb = max_size_bytes // (1024 * 1024)
                return _upload_error(
                    413,
                    f"ファイルサイズが上限を超えています（上限: {max_mb} MB）",
                )
        except ValueError:
            pass

    form = await request.form()
    file_field = form.get("file")
    if file_field is None or not hasattr(file_field, "read"):
        return _upload_error(400, "file フィールドが未指定です")

    filename = _decode_form_value(getattr(file_field, "filename", "") or "")

    # ストリーミング読み取りでサイズチェック
    data = bytearray()
    total = 0
    while True:
        chunk = await file_field.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size_bytes:
            max_mb = max_size_bytes // (1024 * 1024)
            return _upload_error(
                413,
                f"ファイルサイズが上限を超えています（上限: {max_mb} MB）",
            )
        data.extend(chunk)

    if total == 0:
        return _upload_error(400, "アップロードファイルが空です（0 バイト）")

    return bytes(data), filename

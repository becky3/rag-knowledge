"""Scrapy JSONL + HTML → source_store 変換ブリッジ.

仕様: docs/specs/site-ingest.md

Scrapy Spider が出力した JSONL メタデータと HTML ファイルを読み込み、
source_store にファイルを配置して .meta サイドカーファイルを生成する。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from rag.pipeline.ingesters._common import IngestResult
from rag.store.path_converter import url_to_path

# .html 付与をスキップする拡張子
# converter が認識する拡張子 + Spider が Web 系と見なす拡張子の和集合
# Spider._WEB_EXTENSIONS と整合させること
_KNOWN_WEB_EXTENSIONS: frozenset[str] = frozenset(
    {".html", ".htm", ".xhtml", ".shtml", ".php", ".asp", ".aspx", ".jsp",
     ".pdf", ".json", ".md", ".txt", ".adoc"},
)


def _needs_html_extension(url: str) -> bool:
    """URL パスが既知の拡張子を持たない場合に True を返す.

    converter が認識する拡張子（.html, .pdf 等）を既に持つ URL には
    .html を付与しない。拡張子がないか未知の場合のみ .html を付与する。
    """
    path = urlparse(url).path
    ext = PurePosixPath(path).suffix.lower()
    return ext not in _KNOWN_WEB_EXTENSIONS

if TYPE_CHECKING:
    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)

# JSONL に必須のフィールド
_REQUIRED_FIELDS: frozenset[str] = frozenset({"url", "title", "collected_at"})

# JSONL ファイルのデフォルト名
JSONL_FILENAME = "metadata.jsonl"


@dataclass(frozen=True)
class JsonlRecord:
    """JSONL の1行を表すレコード."""

    url: str
    title: str
    status: int
    depth: int
    collected_at: str
    filepath: str


@dataclass
class BridgeResult:
    """Bridge 処理の結果."""

    ingest: IngestResult = field(default_factory=IngestResult)
    total_lines: int = 0
    parse_errors: int = 0


def _parse_jsonl_line(line: str, line_num: int) -> JsonlRecord | None:
    """JSONL の1行をパースする.

    Args:
        line: JSONL の1行（改行を含まない）
        line_num: 行番号（ログ用、1-indexed）

    Returns:
        パース結果。不正な行の場合は None。
    """
    stripped = line.strip()
    if not stripped:
        return None

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("JSONL 行 %d: JSON パースエラー", line_num)
        return None

    if not isinstance(data, dict):
        logger.warning("JSONL 行 %d: 辞書型ではありません", line_num)
        return None

    # 必須フィールドチェック
    missing = _REQUIRED_FIELDS - data.keys()
    if missing:
        logger.warning(
            "JSONL 行 %d: 必須フィールドが不足: %s", line_num, missing,
        )
        return None

    return JsonlRecord(
        url=str(data["url"]),
        title=str(data["title"]),
        status=int(data.get("status", 200)),
        depth=int(data.get("depth", 0)),
        collected_at=str(data["collected_at"]),
        filepath=str(data.get("filepath", "")),
    )


def _build_meta(record: JsonlRecord) -> dict[str, str]:
    """JSONL レコードから .meta 辞書を構築する.

    仕様: docs/specs/site-ingest.md「.meta 生成」セクション
    """
    return {
        "source_id": record.url,
        "source_type": "web",
        "title": record.title,
        "collected_at": record.collected_at,
        "url": record.url,
    }


def import_to_source_store(
    *,
    jsonl_path: Path,
    html_dir: Path,
    source_store: SourceStore,
) -> BridgeResult:
    """JSONL + HTML を source_store に変換・配置する.

    仕様: docs/specs/site-ingest.md「Bridge」セクション

    Args:
        jsonl_path: JSONL メタデータファイルのパス
        html_dir: HTML ファイルが保存されたディレクトリ
        source_store: 配置先の SourceStore

    Returns:
        配置結果
    """
    result = BridgeResult()

    if not jsonl_path.exists():
        logger.warning("JSONL ファイルが見つかりません: %s", jsonl_path)
        return result

    with open(jsonl_path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            result.total_lines += 1

            record = _parse_jsonl_line(line, line_num)
            if record is None:
                # 空行はエラーとしてカウントしない
                if line.strip():
                    result.parse_errors += 1
                continue

            _process_record(
                record=record,
                html_dir=html_dir,
                source_store=source_store,
                result=result,
                line_num=line_num,
            )

    logger.info(
        "Bridge 完了: %d 行処理, %d 件新規配置, %d 件上書き, %d 件スキップ, %d 件エラー",
        result.total_lines,
        result.ingest.placed,
        result.ingest.overwritten,
        result.ingest.skipped,
        result.ingest.errors,
    )
    return result


def _process_record(
    *,
    record: JsonlRecord,
    html_dir: Path,
    source_store: SourceStore,
    result: BridgeResult,
    line_num: int,
) -> None:
    """1レコードを処理して source_store に配置する."""
    # 非200はスキップ（防御的チェック）
    if record.status != 200:
        logger.info(
            "JSONL 行 %d: 非200ステータス (%d) をスキップ: %s",
            line_num, record.status, record.url,
        )
        result.ingest.skipped += 1
        return

    # HTML ファイルの特定
    html_path = _resolve_html_path(record, html_dir)
    if html_path is None or not html_path.exists():
        logger.warning(
            "JSONL 行 %d: HTML ファイルが見つかりません: %s (URL: %s)",
            line_num,
            html_path or "(filepath 未指定)",
            record.url,
        )
        result.ingest.errors += 1
        result.ingest.error_details.append(
            f"HTML not found: {record.url}",
        )
        return

    try:
        data = html_path.read_bytes()
    except OSError:
        logger.exception(
            "JSONL 行 %d: HTML ファイル読み込みエラー: %s",
            line_num, html_path,
        )
        result.ingest.errors += 1
        result.ingest.error_details.append(
            f"Read error: {record.url}",
        )
        return

    # .meta 辞書の構築
    metadata = _build_meta(record)

    # source_store に配置（新規 vs 上書きを区別してカウント）
    try:
        ext = ".html" if _needs_html_extension(record.url) else ""
        # NOTE: パス構築は SourceStore.place_file_from_url と同一ロジック。
        # place_file_from_url のパス導出が変更された場合はここも追従すること。
        rel_path = url_to_path(record.url)
        if ext and not rel_path.endswith(ext):
            rel_path += ext
        is_new = not (source_store.root_dir / rel_path).exists()

        source_store.place_file_from_url(
            url=record.url,
            data=data,
            metadata=metadata,
            extension=ext,
        )
        if is_new:
            result.ingest.placed += 1
        else:
            result.ingest.overwritten += 1
    except Exception:
        logger.exception(
            "JSONL 行 %d: source_store 配置エラー: %s",
            line_num, record.url,
        )
        result.ingest.errors += 1
        result.ingest.error_details.append(
            f"Place error: {record.url}",
        )


def _resolve_html_path(record: JsonlRecord, html_dir: Path) -> Path | None:
    """JSONL レコードから HTML ファイルのパスを解決する.

    filepath フィールドがある場合はそれを使用し、
    ない場合は None を返す。
    パストラバーサル対策として html_dir 内に収まることを検証する。
    """
    if record.filepath:
        resolved = (html_dir / record.filepath).resolve()
        if not resolved.is_relative_to(html_dir.resolve()):
            logger.warning(
                "パストラバーサル検出: filepath=%s が html_dir の外を参照しています",
                record.filepath,
            )
            return None
        return resolved
    return None

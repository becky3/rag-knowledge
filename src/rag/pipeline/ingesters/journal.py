"""Journal インジェスター — ジャーナルエントリを source_store に配置.

仕様: docs/specs/ingesters/journal.md
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from rag.pipeline.ingesters._common import IngestResult

if TYPE_CHECKING:
    from rag.store.source_store import SourceStore

logger = logging.getLogger(__name__)

MAX_FILES_HARD_LIMIT = 500
_ENTRY_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-")
_TOPIC_MAX_LENGTH = 50


class JournalIngester:
    """ジャーナルエントリ取り込み用インジェスター.

    仕様: docs/specs/ingesters/journal.md
    """

    def __init__(self, source_store: SourceStore) -> None:
        self._store = source_store
        self.last_entry_id: str | None = None

    def add_entry(
        self,
        title: str,
        body: str,
        repository: str,
        entry_id: str | None = None,
    ) -> IngestResult:
        """単一ジャーナルエントリを source_store に配置する.

        Args:
            title: エントリタイトル
            body: 本文（Markdown）
            repository: リポジトリ名
            entry_id: エントリ識別子（未指定時は自動生成）
        """
        result = IngestResult()
        try:
            self._validate_text(title, "title")
            self._validate_text(body, "body")
            self._validate_repository(repository)

            if entry_id is None:
                entry_id = self._generate_entry_id(title)
            else:
                self._validate_entry_id(entry_id)

            rel_path = f"journal/{repository}/{entry_id}.md"
            now_iso = datetime.now(timezone.utc).isoformat()

            metadata = {
                "source_type": "journal",
                "title": title,
                "collected_at": now_iso,
                "repository": repository,
            }

            data = body.encode("utf-8")
            self._store.place_file(
                source_type="journal",
                data=data,
                rel_path=rel_path,
                metadata=metadata,
            )
            result.placed = 1
            self.last_entry_id = entry_id
        except ValueError as e:
            result.errors = 1
            result.error_details.append({
                "category": "placement",
                "target": entry_id or title,
                "message": str(e),
            })
        except OSError as e:
            logger.exception("Failed to place journal entry")
            result.errors = 1
            result.error_details.append({
                "category": "placement",
                "target": entry_id or title,
                "message": str(e),
            })
        return result

    def import_directory(
        self,
        dir_path: str,
        repository: str,
    ) -> IngestResult:
        """既存ジャーナルファイルを一括で source_store に配置する.

        Args:
            dir_path: ジャーナルディレクトリパス
            repository: リポジトリ名
        """
        result = IngestResult()
        try:
            self._validate_text(dir_path, "dir_path")
            self._validate_repository(repository)
        except ValueError as e:
            result.errors = 1
            result.error_details.append({
                "category": "placement",
                "target": dir_path,
                "message": str(e),
            })
            return result

        resolved_dir = Path(dir_path.strip()).resolve()
        if not resolved_dir.exists():
            result.errors = 1
            result.error_details.append({
                "category": "placement",
                "target": str(resolved_dir),
                "message": f"Directory not found: {resolved_dir}",
            })
            return result
        if not resolved_dir.is_dir():
            result.errors = 1
            result.error_details.append({
                "category": "placement",
                "target": str(resolved_dir),
                "message": f"Path is not a directory: {resolved_dir}",
            })
            return result

        files = sorted(resolved_dir.glob("*.md"), key=lambda p: str(p))
        logger.info(
            "Journal import: %d files found (repository=%s)",
            len(files), repository,
        )
        if len(files) > MAX_FILES_HARD_LIMIT:
            logger.warning(
                "File count %d exceeds limit %d, clamping to %d",
                len(files), MAX_FILES_HARD_LIMIT, MAX_FILES_HARD_LIMIT,
            )
            files = files[:MAX_FILES_HARD_LIMIT]

        for fp in files:
            try:
                if fp.stat().st_size == 0:
                    logger.warning("Skipping empty file (0 bytes): %s", fp)
                    result.skipped += 1
                    continue

                entry_id = fp.stem
                title = self._parse_title_from_filename(entry_id)
                data = fp.read_bytes()

                # collected_at: ファイル名の日時 > mtime のフォールバック
                collected_at = _parse_datetime_from_entry_id(entry_id)
                if collected_at is None:
                    mtime = fp.stat().st_mtime
                    collected_at = datetime.fromtimestamp(
                        mtime, tz=timezone.utc,
                    ).isoformat()

                rel_path = f"journal/{repository}/{entry_id}.md"
                metadata = {
                    "source_type": "journal",
                    "title": title,
                    "collected_at": collected_at,
                    "repository": repository,
                }

                self._store.place_file(
                    source_type="journal",
                    data=data,
                    rel_path=rel_path,
                    metadata=metadata,
                )
                result.placed += 1
            except OSError as exc:
                logger.exception("Failed to import journal file: %s", fp)
                result.errors += 1
                result.error_details.append({
                    "category": "placement",
                    "target": str(fp),
                    "message": str(exc),
                })

        logger.info(
            "Journal import completed: placed=%d, skipped=%d, errors=%d",
            result.placed, result.skipped, result.errors,
        )
        return result

    @staticmethod
    def _validate_text(value: str, name: str) -> None:
        """文字列パラメータのバリデーション."""
        if not value or not value.strip():
            raise ValueError(f"{name} must not be empty")

    @staticmethod
    def _validate_repository(repository: str) -> None:
        """リポジトリ名のバリデーション."""
        if not repository or not repository.strip():
            raise ValueError("repository must not be empty")
        repo = repository.strip()
        if ".." in repo or "/" in repo or "\\" in repo:
            raise ValueError(
                f"repository に不正な文字が含まれています: {repo!r}"
            )

    @staticmethod
    def _validate_entry_id(entry_id: str) -> None:
        """entry_id のバリデーション."""
        if not entry_id or not entry_id.strip():
            raise ValueError("entry_id must not be empty")
        eid = entry_id.strip()
        if ".." in eid or "/" in eid or "\\" in eid:
            raise ValueError(
                f"entry_id に不正な文字が含まれています: {eid!r}"
            )

    @staticmethod
    def _generate_entry_id(title: str) -> str:
        """title からエントリ ID を自動生成する."""
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y%m%d-%H%M%S")
        topic = _sanitize_topic(title)
        if topic:
            return f"{timestamp}-{topic}"
        return timestamp

    @staticmethod
    def _parse_title_from_filename(entry_id: str) -> str:
        """ファイル名（拡張子除去済み）からタイトルを抽出する."""
        if _ENTRY_ID_PATTERN.match(entry_id):
            # YYYYMMDD-HHMMSS- プレフィックスを除去
            return entry_id[16:]
        return entry_id


def _parse_datetime_from_entry_id(entry_id: str) -> str | None:
    """ファイル名の YYYYMMDD-HHMMSS プレフィックスから ISO 8601 日時を生成する.

    Returns:
        ISO 8601 形式の日時文字列。パースできない場合は None。
    """
    if not _ENTRY_ID_PATTERN.match(entry_id):
        return None
    try:
        dt = datetime.strptime(entry_id[:15], "%Y%m%d-%H%M%S")  # noqa: DTZ007
        dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None


def _sanitize_topic(title: str) -> str:
    """タイトルをサニタイズしてトピック文字列を生成する."""
    # NFKC 正規化
    normalized = unicodedata.normalize("NFKC", title.strip().lower())
    # 英数字・ハイフン以外を空白に変換
    cleaned = re.sub(r"[^a-z0-9\-\s]", " ", normalized)
    # 連続空白をハイフンに
    topic = re.sub(r"\s+", "-", cleaned.strip())
    # 連続ハイフンを1つに
    topic = re.sub(r"-+", "-", topic)
    # 先頭・末尾のハイフン除去
    topic = topic.strip("-")
    # 長さ制限
    if len(topic) > _TOPIC_MAX_LENGTH:
        topic = topic[:_TOPIC_MAX_LENGTH].rstrip("-")
    return topic

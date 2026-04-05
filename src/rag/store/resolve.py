"""ソース識別・タイトル解決の共通ユーティリティ.

title / published_at の解決ロジックを一元化する。
SourceStore と PipelineController の両方から呼び出される。

source_id は source_store 内の相対パス（= file_path）をそのまま使用するため、
解決関数は不要。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def resolve_title(
    source_type: str,
    rel_path: str,
    metadata: dict[str, Any] | None,
) -> str:
    """タイトルを決定する.

    .meta に title があればそれを使用し、
    なければファイル名（拡張子除去）をフォールバックとして使用する。

    Args:
        source_type: 媒体種別
        rel_path: source_store 内の相対パス
        metadata: .meta から読み取ったメタデータ辞書

    Returns:
        解決されたタイトル
    """
    if metadata and "title" in metadata:
        return str(metadata["title"])
    return Path(rel_path).stem


# source_type ごとの公開日時フィールド名
_PUBLISHED_AT_FIELDS: dict[str, str] = {
    "bluesky": "created_at",
    "zenn": "published_at",
    "youtube": "upload_date",
}


def resolve_published_at(
    source_type: str,
    metadata: dict[str, Any] | None,
    collected_at: str,
) -> str:
    """公開日時を決定する.

    source_type ごとに .meta の公開日時フィールドを参照する。
    該当フィールドがない source_type（aozora, journal, local, web）は
    collected_at をそのまま使用する。

    youtube の upload_date は YYYYMMDD 形式のため ISO 8601 に変換する。

    Args:
        source_type: 媒体種別
        metadata: .meta から読み取ったメタデータ辞書
        collected_at: 取込時刻（フォールバック用）

    Returns:
        ISO 8601 形式の公開日時文字列
    """
    field_name = _PUBLISHED_AT_FIELDS.get(source_type)
    if field_name is None or metadata is None:
        return collected_at

    raw_value = metadata.get(field_name)
    if not raw_value:
        return collected_at

    raw_str = str(raw_value)

    # youtube の upload_date: YYYYMMDD → ISO 8601
    if source_type == "youtube" and len(raw_str) == 8 and raw_str.isdigit():
        return f"{raw_str[:4]}-{raw_str[4:6]}-{raw_str[6:8]}T00:00:00+00:00"

    return raw_str

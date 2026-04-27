"""path フィルタの正規化・エスケープ・バリデーション.

仕様: docs/specs/rebuild-stats.md / docs/specs/pipeline-controller.md

rebuild の `--path` / MCP `rag_rebuild` の `path` パラメータが下層
（metadata_db / source_store / converter / indexer / vector_store / bm25_index）
に渡る前段で正規化・検証を行う共通ユーティリティ。

空文字列やパストラバーサル (`..`)・絶対パスを多層で防御することで、
converted_store / source_store のルート外を誤って削除・走査する事故を防ぐ。
"""

from __future__ import annotations

from typing import get_args

from rag.store.models import SourceType

# source_type の SSoT は _schema/enums.yml。SourceType Literal 自体は CI で
# enums.yml と同期検証されるため、本モジュールでは Literal から導出する
_VALID_ROOT_SOURCE_TYPES: frozenset[str] = frozenset(get_args(SourceType))


class PathFilterError(ValueError):
    """path 引数のバリデーションエラー."""


def normalize_path_prefix(path: str) -> str:
    """path 引数を POSIX 形式に正規化する.

    - バックスラッシュをスラッシュに変換
    - 先頭/末尾のスラッシュを除去
    - 空文字列・絶対パス・`..` を含むパスは PathFilterError

    Args:
        path: ユーザー入力の path 文字列

    Returns:
        正規化済みの相対パス（スラッシュ区切り、両端スラッシュなし）

    Raises:
        PathFilterError: バリデーション失敗時
    """
    # path は source_store ルート相対のディレクトリパスでなければならない。
    # 以下のいずれかに該当する入力は受け付けない (ユーザー視点で 1 つの説明に
    # 統一することで、Windows + Git Bash の MSYS パス変換 (`/abs` →
    # `C:/Program Files/Git/abs`) で経路が変わっても同じメッセージで弾く):
    # - 空文字列 / `/` のみのパス
    # - 先頭スラッシュ付きの絶対パス
    # - Windows ドライブレター (`X:`) で始まるパス
    # - 連続スラッシュを含むパス (DB の LIKE や prefix 文字列比較で別文字列扱い
    #   になり、Path 結合は許容しても下流で no-op になるため一律拒否)
    # - `..` / `.` セグメントを含むパス
    # - 先頭セグメントが既知の source_type でないパス
    valid_roots = sorted(_VALID_ROOT_SOURCE_TYPES)
    invalid_msg = (
        f"path は source_store ルート相対のディレクトリパスを指定してください "
        f"(先頭セグメントが {valid_roots} のいずれか、空文字列・絶対パス・"
        f"Windows ドライブレター・連続スラッシュ・`..`/`.` セグメント不可): "
        f"{path!r}"
    )
    if path is None or path == "":
        raise PathFilterError(invalid_msg)
    posix = path.replace("\\", "/")
    if posix.startswith("/"):
        raise PathFilterError(invalid_msg)
    if len(posix) >= 2 and posix[1] == ":":
        raise PathFilterError(invalid_msg)
    stripped = posix.strip("/")
    if stripped == "":
        raise PathFilterError(invalid_msg)
    segments = stripped.split("/")
    if any(seg == "" for seg in segments):
        raise PathFilterError(invalid_msg)
    if any(seg in ("..", ".") for seg in segments):
        raise PathFilterError(invalid_msg)
    root_segment = segments[0]
    if root_segment not in _VALID_ROOT_SOURCE_TYPES:
        raise PathFilterError(invalid_msg)
    return stripped


def derive_source_type(path_prefix: str) -> str:
    """正規化済み path の先頭セグメントから source_type を返す.

    `normalize_path_prefix` を通過した path のみを受け付ける契約。契約違反
    （空文字列・先頭スラッシュ・先頭セグメントが既知 source_type でない）は
    fail-fast のため `PathFilterError` を送出する。
    """
    if not path_prefix or path_prefix.startswith("/"):
        msg = (
            f"derive_source_type には正規化済み path を渡す必要があります: "
            f"{path_prefix!r}"
        )
        raise PathFilterError(msg)
    root = path_prefix.split("/", 1)[0]
    if root not in _VALID_ROOT_SOURCE_TYPES:
        msg = (
            f"derive_source_type には正規化済み path を渡す必要があります "
            f"（先頭セグメントが既知の source_type でない）: {path_prefix!r}"
        )
        raise PathFilterError(msg)
    return root


def escape_like(value: str) -> str:
    """SQLite LIKE のメタ文字をエスケープする (ESCAPE '\\' 前提).

    LIKE のメタ文字は `%` (任意 0+ 文字) と `_` (任意 1 文字)。
    バックスラッシュ自身も併せてエスケープする。
    """
    return value.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")

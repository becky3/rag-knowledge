"""メディア削減ツール共通定義.

仕様: docs/specs/infrastructure/pptx-media-reduction.md
      docs/specs/infrastructure/pdf-media-reduction.md

source_store 配置前の事前処理として提供する削減ツール（CLI reduce-pptx /
reduce-pdf）が共有する定数とパス操作を定義する。削減対象の解析・実体除去は
形式ごとのモジュール（pptx_extractor / pdf_media_reducer）が担う。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# 削減コピーの別名出力サフィックス（--alongside が使用。
# このサフィックスを持つファイルは削減対象の収集から除外される）
REDUCED_STEM_SUFFIX = ".reduced"


def is_reduced_copy(path: Path) -> bool:
    """本ツールが生成した削減コピーか判定する（再削減を防ぐため収集から除外する）."""
    return path.stem.lower().endswith(REDUCED_STEM_SUFFIX)


def alongside_output_path(target: Path) -> Path:
    """原本と同じフォルダへの別名出力パス（``<元名>.reduced.<拡張子>``）を返す."""
    return target.with_name(f"{target.stem}{REDUCED_STEM_SUFFIX}{target.suffix}")


def collect_reduce_targets(
    raw_paths: list[str],
    extensions: frozenset[str],
    *,
    on_skip: Callable[[str], None],
    on_error: Callable[[str], None],
) -> tuple[list[Path], int]:
    """削減対象ファイルを収集する.

    ディレクトリは再帰走査し、対象拡張子かつ削減コピーでないファイルを集める。

    Args:
        raw_paths: CLI で指定されたパス（ファイルまたはディレクトリ）
        extensions: 対象拡張子（小文字・ドット付き）
        on_skip: 対象外ファイルを指定された場合の通知（警告表示用）
        on_error: パスが存在しない場合の通知（エラー表示用）

    Returns:
        (対象ファイルのリスト, パス解決エラー数)
    """

    def _is_target(p: Path) -> bool:
        return p.suffix.lower() in extensions and not is_reduced_copy(p)

    targets: list[Path] = []
    errors = 0
    for raw in raw_paths:
        path = Path(raw).resolve()
        if path.is_dir():
            targets.extend(sorted(
                p for p in path.rglob("*") if p.is_file() and _is_target(p)
            ))
        elif path.is_file():
            if _is_target(path):
                targets.append(path)
            else:
                on_skip(str(path))
        else:
            on_error(str(path))
            errors += 1
    return targets, errors

"""flat module + package の同名衝突を検出する CI 用バリデーションスクリプト.

Python の import 解決順序は flat module（``foo.py``）と同名 package
（``foo/__init__.py``）が共存する場合 implementation-defined になり、
テストでは pass しても本番で予期せぬ挙動を招くリスクがある。pytest 起動時の
ガードに加え、本スクリプトを CI で独立 step として実行することで、テスト
実行前段階で fail-fast を担保する。

検証対象は ``src/`` 配下の Python モジュール。衝突検出時は exit 1 で終了する。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _module_path(py_file: Path, src_root: Path) -> str:
    """Python ファイルの dotted module path を返す.

    ``__init__.py`` の場合は親ディレクトリを、それ以外は ``.py`` を除いた
    相対パスをドット区切りに変換する。
    """
    if py_file.name == "__init__.py":
        rel = py_file.parent.relative_to(src_root)
    else:
        rel = py_file.relative_to(src_root).with_suffix("")
    return str(rel).replace(os.sep, ".")


def find_layout_conflicts(src_root: Path) -> list[tuple[str, Path, Path]]:
    """flat module と package の同名衝突を検出する.

    Args:
        src_root: 走査対象のルート（``src/`` 等）

    Returns:
        衝突のリスト。各要素は (module_path, 先に検出されたファイル, 後に検出されたファイル)
    """
    if not src_root.is_dir():
        return []

    seen: dict[str, Path] = {}
    conflicts: list[tuple[str, Path, Path]] = []

    for py_file in sorted(src_root.rglob("*.py")):
        module_path = _module_path(py_file, src_root)
        if module_path in seen:
            conflicts.append((module_path, seen[module_path], py_file))
        else:
            seen[module_path] = py_file

    return conflicts


def main() -> int:
    src_root = _PROJECT_ROOT / "src"
    conflicts = find_layout_conflicts(src_root)
    if not conflicts:
        print(f"OK: no package layout conflicts in {src_root}")
        return 0

    print("ERROR: package layout conflict detected (flat module + package coexist):")
    for mod, first, second in conflicts:
        print(f"  - {mod!r}: {first} <-> {second}")
    print(
        "Either delete the flat module or rename the package to avoid "
        "implementation-defined import resolution."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

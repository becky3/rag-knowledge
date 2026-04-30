"""scripts/validate_package_layout の find_layout_conflicts のユニットテスト.

flat module（``foo.py``）と同名 package（``foo/__init__.py``）が共存する
ケースを synthetic ディレクトリで再現し、検出ロジックを検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from validate_package_layout import find_layout_conflicts  # noqa: E402


class TestFindLayoutConflicts:
    def test_empty_dir_returns_no_conflicts(self, tmp_path: Path) -> None:
        assert find_layout_conflicts(tmp_path) == []

    def test_nonexistent_dir_returns_empty(self, tmp_path: Path) -> None:
        assert find_layout_conflicts(tmp_path / "nonexistent") == []

    def test_no_conflict(self, tmp_path: Path) -> None:
        (tmp_path / "alpha.py").write_text("")
        (tmp_path / "beta").mkdir()
        (tmp_path / "beta" / "__init__.py").write_text("")
        assert find_layout_conflicts(tmp_path) == []

    def test_flat_and_package_conflict_detected(self, tmp_path: Path) -> None:
        (tmp_path / "foo.py").write_text("")
        (tmp_path / "foo").mkdir()
        (tmp_path / "foo" / "__init__.py").write_text("")
        conflicts = find_layout_conflicts(tmp_path)
        assert len(conflicts) == 1
        module_path, _first, _second = conflicts[0]
        assert module_path == "foo"

    def test_nested_conflict_detected(self, tmp_path: Path) -> None:
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "__init__.py").write_text("")
        (tmp_path / "pkg" / "sub.py").write_text("")
        (tmp_path / "pkg" / "sub").mkdir()
        (tmp_path / "pkg" / "sub" / "__init__.py").write_text("")
        conflicts = find_layout_conflicts(tmp_path)
        assert len(conflicts) == 1
        module_path, _first, _second = conflicts[0]
        assert module_path == "pkg.sub"

    def test_multiple_conflicts_collected(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("")
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "__init__.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / "__init__.py").write_text("")
        conflicts = find_layout_conflicts(tmp_path)
        assert {c[0] for c in conflicts} == {"a", "b"}

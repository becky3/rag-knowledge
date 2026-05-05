"""src/ 配下で ConstrainedClient を経由しない直接 HTTP クライアント利用を検出する.

Python AST でソースをパースし、Import / ImportFrom / Attribute ノードのみを
検査することで docstring / コメント / 文字列リテラル内の literal を構造的に除外する。

仕様: docs/specs/workflows/check-raw-http.md
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# `# safety:allowed` 同一行コメントを厳密一致で識別する。
# `# not safety:allowed` や `# safety:allowed-but` のような部分一致を許容しない:
# `^#\s*safety:allowed` は `#` 直後（空白許容）の出現に限定し、
# `(?=\s|$)` look-ahead で直後がホワイトスペースまたは行末であることを要求する。
_ALLOWED_PATTERN = re.compile(r"^#\s*safety:allowed(?=\s|$)")

_BANNED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "aiohttp": frozenset({"ClientSession"}),
    "httpx": frozenset({"Client", "AsyncClient"}),
    "requests": frozenset(
        {"get", "post", "put", "delete", "patch", "head", "options", "session", "Session"}
    ),
    "urllib": frozenset({"request"}),
}

_BANNED_FROM_IMPORT: dict[str, frozenset[str] | None] = {
    "aiohttp": frozenset({"ClientSession"}),
    "httpx": frozenset({"Client", "AsyncClient"}),
    "urllib.request": None,
    "requests": None,
}

_BANNED_DIRECT_IMPORT: frozenset[str] = frozenset({"urllib.request"})


@dataclass(frozen=True)
class Violation:
    file: Path
    lineno: int
    message: str

    def format(self, project_root: Path) -> str:
        try:
            rel = self.file.relative_to(project_root)
        except ValueError:
            rel = self.file
        return f"{rel}:{self.lineno}: {self.message}"


def _build_allowed_lines(source: str) -> set[int]:
    """`# safety:allowed` 同一行コメントを持つ行番号集合を返す."""
    allowed: set[int] = set()
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT and _ALLOWED_PATTERN.match(tok.string):
                allowed.add(tok.start[0])
    except tokenize.TokenError:
        pass
    return allowed


class _Visitor(ast.NodeVisitor):
    def __init__(self, file_path: Path, allowed_lines: set[int]) -> None:
        self.file_path = file_path
        self.allowed_lines = allowed_lines
        self.violations: list[Violation] = []

    def _record(self, lineno: int, message: str) -> None:
        if lineno in self.allowed_lines:
            return
        self.violations.append(Violation(self.file_path, lineno, message))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in _BANNED_DIRECT_IMPORT:
                self._record(node.lineno, f"`import {alias.name}` is a raw HTTP client")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        banned_names = _BANNED_FROM_IMPORT.get(module)
        if module in _BANNED_FROM_IMPORT and banned_names is None:
            self._record(node.lineno, f"`from {module} import ...` is a raw HTTP client")
        elif banned_names is not None:
            for alias in node.names:
                if alias.name in banned_names:
                    self._record(
                        node.lineno,
                        f"`from {module} import {alias.name}` is a raw HTTP client",
                    )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        root_name = _root_attribute_name(node)
        if root_name is not None:
            banned = _BANNED_ATTRIBUTES.get(root_name)
            if banned is not None and node.attr in banned:
                self._record(
                    node.lineno,
                    f"`{root_name}.{node.attr}` is a raw HTTP client usage",
                )
        self.generic_visit(node)


def _root_attribute_name(node: ast.Attribute) -> str | None:
    """`a.b.c` のような Attribute ノードの最も内側にある Name の id を返す."""
    current: ast.expr = node.value
    while isinstance(current, ast.Attribute):
        current = current.value
    if isinstance(current, ast.Name):
        return current.id
    return None


def check_source(source: str, file_path: Path) -> list[Violation]:
    """ソース文字列を AST 解析して違反リストを返す."""
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as exc:
        return [
            Violation(
                file_path,
                exc.lineno or 1,
                f"SyntaxError while parsing: {exc.msg}",
            )
        ]
    allowed = _build_allowed_lines(source)
    visitor = _Visitor(file_path, allowed)
    visitor.visit(tree)
    return visitor.violations


def iter_python_files(src_dir: Path) -> Iterable[Path]:
    yield from sorted(p for p in src_dir.rglob("*.py") if p.is_file())


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    src_dir = project_root / "src"
    if not src_dir.is_dir():
        print(f"ERROR: src/ directory not found at {src_dir}", file=sys.stderr)
        return 1

    all_violations: list[Violation] = []
    for py_file in iter_python_files(src_dir):
        source = py_file.read_text(encoding="utf-8")
        all_violations.extend(check_source(source, py_file))

    if all_violations:
        print(
            "ERROR: Direct HTTP client usage detected (bypass ConstrainedClient)",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        print(
            "The following lines use HTTP clients directly instead of ConstrainedClient:",
            file=sys.stderr,
        )
        for v in all_violations:
            print(v.format(project_root), file=sys.stderr)
        print("", file=sys.stderr)
        print("To fix:", file=sys.stderr)
        print("  1. Use ConstrainedClient from py-common-lib (py_common_lib.httpx)", file=sys.stderr)
        print("  2. Or add '# safety:allowed' comment if explicitly permitted", file=sys.stderr)
        return 1

    print("OK: No raw HTTP client usage detected outside allowlist")
    return 0


if __name__ == "__main__":
    sys.exit(main())

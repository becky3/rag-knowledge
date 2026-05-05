"""FastMCP インスタンスの単一定義モジュール.

`__init__.py` から分離した目的:
- `__init__.py` が tool サブモジュールを import する一方、tool サブモジュールは
  `mcp` を参照する循環構造になる。runtime では Python の partial module state で
  動作するが、mypy の static 解析が「Cannot determine type of mcp」となる。
- 本モジュールに `mcp` を切り出すことで、tool サブモジュールは
  `from .._mcp import mcp` で循環なしに参照できる。

公開 API としては `from rag.server import mcp` が SSoT（`__init__.py` で re-export）。
本モジュールは内部詳細であり外部から直接参照しない。
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("rag")

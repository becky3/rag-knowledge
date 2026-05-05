"""MCP tool 定義サブパッケージ.

各ファイルはツール本体（`@mcp.tool()` で装飾された async 関数）+ 専用フォーマッター +
専用 validation を同居させる凝集度優先の設計（仕様: docs/specs/rag-knowledge.md「server 構造」）。

複数ツール共有のフォーマッターは `..cli_subprocess` に集約する。
"""

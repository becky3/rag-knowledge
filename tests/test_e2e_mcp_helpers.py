"""tests/e2e/_mcp_helpers の純粋ヘルパーのユニットテスト.

assert_no_mojibake の純粋関数部分のみ検証する
（call_mcp_tool 自体の subprocess 越境動作は L2 e2e で検証）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
from _mcp_helpers import assert_no_mojibake  # noqa: E402


class TestAssertNoMojibake:
    def test_clean_text_passes(self) -> None:
        assert_no_mojibake("正常なテキスト abc 123")

    def test_replacement_char_fails(self) -> None:
        with pytest.raises(AssertionError, match="mojibake"):
            assert_no_mojibake("壊れた文字: �")

    def test_context_appears_in_message(self) -> None:
        with pytest.raises(AssertionError, match="rag_search response"):
            assert_no_mojibake("�", context="rag_search response")

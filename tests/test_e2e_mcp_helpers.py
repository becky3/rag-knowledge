"""tests/e2e/_mcp_helpers の純粋ヘルパーのユニットテスト.

assert_no_mojibake / assert_no_stderr_warnings の純粋関数部分のみ検証する
（call_mcp_tool 自体の subprocess 越境動作は L2 e2e で検証）。
"""

from __future__ import annotations

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
from _mcp_helpers import (  # noqa: E402
    assert_no_mojibake,
    assert_no_stderr_warnings,
)


class TestAssertNoMojibake:
    def test_clean_text_passes(self) -> None:
        assert_no_mojibake("正常なテキスト abc 123")

    def test_replacement_char_fails(self) -> None:
        with pytest.raises(AssertionError, match="mojibake"):
            assert_no_mojibake("壊れた文字: �")

    def test_context_appears_in_message(self) -> None:
        with pytest.raises(AssertionError, match="rag_search response"):
            assert_no_mojibake("�", context="rag_search response")


class TestAssertNoStderrWarnings:
    def test_empty_passes(self) -> None:
        assert_no_stderr_warnings([])

    def test_info_log_passes(self) -> None:
        assert_no_stderr_warnings(
            ["INFO - Server started\n", "DEBUG - tool called\n"]
        )

    def test_warning_fails(self) -> None:
        with pytest.raises(AssertionError, match="WARNING"):
            assert_no_stderr_warnings(["WARNING - deprecated API used\n"])

    def test_error_fails(self) -> None:
        with pytest.raises(AssertionError, match="ERROR"):
            assert_no_stderr_warnings(["ERROR - subprocess failed\n"])

    def test_critical_fails(self) -> None:
        with pytest.raises(AssertionError, match="CRITICAL"):
            assert_no_stderr_warnings(["CRITICAL - fatal failure\n"])

    def test_only_violations_included_in_message(self) -> None:
        with pytest.raises(AssertionError) as exc_info:
            assert_no_stderr_warnings(
                [
                    "INFO - normal log\n",
                    "WARNING - bad thing\n",
                    "DEBUG - other log\n",
                ]
            )
        msg = str(exc_info.value)
        assert "WARNING" in msg
        assert "INFO" not in msg

    def test_fake_mode_warning_is_allowlisted(self) -> None:
        assert_no_stderr_warnings(
            [
                "WARNING - [FAKE MODE: youtube] YouTube は FAKE モードで起動中\n",
                "WARNING - [FAKE MODE: bluesky] BlueSky は FAKE モードで起動中\n",
            ]
        )

    def test_non_allowlisted_warning_still_fails(self) -> None:
        with pytest.raises(AssertionError, match="WARNING"):
            assert_no_stderr_warnings(
                [
                    "WARNING - [FAKE MODE: youtube] OK\n",
                    "WARNING - some other unexpected warning\n",
                ]
            )

    def test_info_log_with_warning_in_body_does_not_match(self) -> None:
        """INFO ログ本文に "WARNING" が含まれていても誤検出しない."""
        assert_no_stderr_warnings(
            [
                "INFO - tool returned: please pay attention to WARNING markers\n",
                "INFO - status: ERROR field not present\n",
            ]
        )

"""scripts/check_raw_http.py のテスト.

仕様: docs/specs/workflows/check-raw-http.md
計画: aidlc-docs/plan-work/issue-735.md (Issue #735)

テスト方針:
- 違反検出: コード本体での raw HTTP クライアント直接利用を検出する
- 許可リスト: # safety:allowed 同一行コメント付き行は検出しない
- 誤検出ゼロ: docstring / コメント / 文字列リテラル内の literal は検出しない
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_checker_module() -> ModuleType:
    """scripts/check_raw_http.py を動的ロードする (scripts/ は通常パッケージではないため)."""
    project_root = Path(__file__).resolve().parents[1]
    script_path = project_root / "scripts" / "check_raw_http.py"
    spec = importlib.util.spec_from_file_location("_check_raw_http_under_test", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_checker = _load_checker_module()
check_source = _checker.check_source
Violation = _checker.Violation


def _check(source: str) -> list[Violation]:
    return check_source(source, Path("dummy.py"))


class TestViolationDetection:
    """コード本体での raw HTTP クライアント利用を検出すること."""

    def test_httpx_client_call(self) -> None:
        violations = _check("import httpx\nclient = httpx.Client()\n")
        assert len(violations) == 1
        assert violations[0].lineno == 2
        assert "httpx.Client" in violations[0].message

    def test_httpx_async_client_call(self) -> None:
        violations = _check("import httpx\nclient = httpx.AsyncClient()\n")
        assert len(violations) == 1
        assert violations[0].lineno == 2

    def test_httpx_from_import(self) -> None:
        violations = _check("from httpx import Client\nc = Client()\n")
        assert len(violations) == 1
        assert violations[0].lineno == 1

    def test_httpx_from_import_async(self) -> None:
        violations = _check("from httpx import AsyncClient\n")
        assert len(violations) == 1
        assert violations[0].lineno == 1

    def test_aiohttp_client_session(self) -> None:
        violations = _check("import aiohttp\ns = aiohttp.ClientSession()\n")
        assert len(violations) == 1
        assert violations[0].lineno == 2

    def test_aiohttp_from_import(self) -> None:
        violations = _check("from aiohttp import ClientSession\n")
        assert len(violations) == 1

    def test_requests_get(self) -> None:
        violations = _check("import requests\nrequests.get('http://example.com')\n")
        assert len(violations) == 1
        assert violations[0].lineno == 2

    def test_requests_post_session(self) -> None:
        violations = _check("import requests\nrequests.Session()\n")
        assert len(violations) == 1

    def test_requests_from_import(self) -> None:
        violations = _check("from requests import get\n")
        assert len(violations) == 1

    def test_urllib_request_import(self) -> None:
        violations = _check("import urllib.request\n")
        assert len(violations) == 1
        assert "urllib.request" in violations[0].message

    def test_urllib_request_from_import(self) -> None:
        violations = _check("from urllib.request import urlopen\n")
        assert len(violations) == 1


class TestAllowlist:
    """# safety:allowed 同一行コメントが付いた行は検出しないこと."""

    def test_attribute_call_with_safety_allowed(self) -> None:
        source = "import httpx\nclient = httpx.Client()  # safety:allowed\n"
        violations = _check(source)
        assert violations == []

    def test_async_client_with_safety_allowed(self) -> None:
        source = (
            "import httpx\n"
            "async def f():\n"
            "    async with httpx.AsyncClient() as session:  # safety:allowed\n"
            "        pass\n"
        )
        violations = _check(source)
        assert violations == []

    def test_import_with_safety_allowed(self) -> None:
        source = "import urllib.request  # safety:allowed\n"
        violations = _check(source)
        assert violations == []

    def test_from_import_with_safety_allowed(self) -> None:
        source = "from httpx import Client  # safety:allowed\n"
        violations = _check(source)
        assert violations == []

    def test_safety_allowed_with_trailing_context(self) -> None:
        """`# safety:allowed` の後にホワイトスペース＋追加コメントが続く形は許可."""
        source = "import httpx\nclient = httpx.Client()  # safety:allowed - reason here\n"
        violations = _check(source)
        assert violations == []


class TestAllowlistStrictMatching:
    """許可リストは厳密一致のみ。部分一致による誤許可を防ぐ."""

    def test_negated_safety_allowed_is_not_allowed(self) -> None:
        """`# not safety:allowed` は許可扱いにならず、違反として検出される."""
        source = "import httpx\nclient = httpx.Client()  # not safety:allowed\n"
        violations = _check(source)
        assert len(violations) == 1
        assert violations[0].lineno == 2

    def test_safety_allowed_suffix_is_not_allowed(self) -> None:
        """`# safety:allowed-but` のようなサフィックス付きは許可扱いにならない."""
        source = "import httpx\nclient = httpx.Client()  # safety:allowed-but\n"
        violations = _check(source)
        assert len(violations) == 1
        assert violations[0].lineno == 2

    def test_safety_allowed_with_underscore_is_not_allowed(self) -> None:
        """`# safety:allowed_var` のような単語接続も許可扱いにならない."""
        source = "import httpx\nclient = httpx.Client()  # safety:allowed_var\n"
        violations = _check(source)
        assert len(violations) == 1
        assert violations[0].lineno == 2


class TestNoFalsePositives:
    """docstring / コメント / 文字列リテラル内の literal を誤検出しないこと."""

    def test_module_docstring_mentioning_httpx(self) -> None:
        source = '"""このモジュールは httpx.AsyncClient を内部で使う説明."""\n'
        violations = _check(source)
        assert violations == []

    def test_function_docstring_mentioning_httpx(self) -> None:
        source = (
            "def f():\n"
            '    """ConstrainedClient 経路、未設定時は raw httpx.AsyncClient 経路を取る."""\n'
            "    return None\n"
        )
        violations = _check(source)
        assert violations == []

    def test_comment_mentioning_httpx(self) -> None:
        source = "# httpx.Client は使用禁止 (ConstrainedClient を使う)\nx = 1\n"
        violations = _check(source)
        assert violations == []

    def test_string_literal_mentioning_httpx(self) -> None:
        source = 'msg = "Use httpx.AsyncClient via ConstrainedClient"\n'
        violations = _check(source)
        assert violations == []

    def test_multiline_docstring_with_httpx_async_client(self) -> None:
        """PR #734 で workaround を要した実ケースの再現."""
        source = (
            "class C:\n"
            '    """SafeBrowsing client.\n'
            "\n"
            "    ``_cc_kwargs`` 設定時は ConstrainedClient 経路、未設定時は raw\n"
            "    ``httpx.AsyncClient`` 経路を取るが、いずれも 1 リクエストごとに\n"
            "    生成・破棄する。\n"
            '    """\n'
            "    pass\n"
        )
        violations = _check(source)
        assert violations == []

    def test_string_literal_mentioning_requests_get(self) -> None:
        source = 'msg = "requests.get is forbidden"\n'
        violations = _check(source)
        assert violations == []

    def test_unrelated_attribute_access(self) -> None:
        source = (
            "class Foo:\n"
            "    Client = None\n"
            "x = Foo.Client\n"
        )
        violations = _check(source)
        assert violations == []

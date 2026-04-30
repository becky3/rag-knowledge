"""validate_utf8_environment / check_utf8_environment のテスト.

Issue: #709（fail-fast 化）

テスト方針:
- check_utf8_environment は純粋関数として env / stdout_encoding を受け取り
  違反項目のリストを返す。各 env 単独違反・複数違反・正常系を検証
- validate_utf8_environment は違反時に sys.exit(1) するエントリ関数。
  subprocess で起動して終了コードと stderr メッセージを検証する
- エンコーディング名の表記揺れ（"utf-8" / "UTF8" / "utf_8" 等）の
  正規化を境界値として検証
"""

from __future__ import annotations

import subprocess
import sys

from rag.config import check_utf8_environment


class TestCheckUtf8EnvironmentNoViolation:
    """check_utf8_environment の正常系."""

    def test_canonical_values_pass(self) -> None:
        env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        assert check_utf8_environment(env, "utf-8") == []

    def test_uppercase_utf8_alias_passes(self) -> None:
        env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "UTF-8"}
        assert check_utf8_environment(env, "UTF-8") == []

    def test_no_dash_alias_passes(self) -> None:
        env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf8"}
        assert check_utf8_environment(env, "utf8") == []

    def test_underscore_alias_passes(self) -> None:
        env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf_8"}
        assert check_utf8_environment(env, "utf_8") == []


class TestCheckUtf8EnvironmentViolations:
    """check_utf8_environment の異常系."""

    def test_pythonutf8_unset(self) -> None:
        env = {"PYTHONIOENCODING": "utf-8"}
        errors = check_utf8_environment(env, "utf-8")
        assert len(errors) == 1
        assert "PYTHONUTF8" in errors[0]

    def test_pythonutf8_zero(self) -> None:
        env = {"PYTHONUTF8": "0", "PYTHONIOENCODING": "utf-8"}
        errors = check_utf8_environment(env, "utf-8")
        assert len(errors) == 1
        assert "PYTHONUTF8" in errors[0]

    def test_pythonioencoding_unset(self) -> None:
        env = {"PYTHONUTF8": "1"}
        errors = check_utf8_environment(env, "utf-8")
        assert len(errors) == 1
        assert "PYTHONIOENCODING" in errors[0]

    def test_pythonioencoding_cp932(self) -> None:
        env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "cp932"}
        errors = check_utf8_environment(env, "utf-8")
        assert len(errors) == 1
        assert "PYTHONIOENCODING" in errors[0]
        assert "cp932" in errors[0]

    def test_stdout_encoding_cp932(self) -> None:
        env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        errors = check_utf8_environment(env, "cp932")
        assert len(errors) == 1
        assert "sys.stdout.encoding" in errors[0]
        assert "cp932" in errors[0]

    def test_all_violations_collected(self) -> None:
        env = {"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp932"}
        errors = check_utf8_environment(env, "cp932")
        assert len(errors) == 3


class TestValidateUtf8EnvironmentSubprocess:
    """validate_utf8_environment の fail-fast 動作検証（subprocess 経由）."""

    def _run(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        """validate_utf8_environment を呼び出すワンライナーを subprocess で実行する."""
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from rag.config import validate_utf8_environment; "
                "validate_utf8_environment(); "
                "print('OK')",
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def _run_inherit(
        self, overrides: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        """現環境を継承して env のみ上書きするパターン（Windows でも DLL 解決可能）."""
        import os

        env = dict(os.environ)
        env.update(overrides)
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from rag.config import validate_utf8_environment; "
                "validate_utf8_environment(); "
                "print('OK')",
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_violation_exits_nonzero(self) -> None:
        result = self._run_inherit(
            {"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp932"}
        )
        assert result.returncode != 0
        assert "UTF-8 environment is not enforced" in result.stderr

    def test_canonical_passes(self) -> None:
        result = self._run_inherit(
            {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        )
        assert result.returncode == 0
        assert "OK" in result.stdout

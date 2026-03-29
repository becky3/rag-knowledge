"""Upload HTTP API 認証のテスト.

仕様: docs/specs/infrastructure/upload-auth.md

テスト方針:
- API キー検証（ヘッダー未指定、値不正、一致、keyring エラー）
- バインドアドレス検証（0.0.0.0 拒否、localhost 許可、プライベート許可、パブリック拒否）
- API キー未登録時の起動拒否
- generate-api-key CLI サブコマンド（表示のみ、--save、--force、既存キーの上書き確認）
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from rag.server import (
    _check_api_key_registered,
    _reset_pipeline_controller,
    _reset_rag_service,
    _validate_bind_address,
    mcp,
)


def _default_mock_settings(**overrides: object) -> MagicMock:
    """テスト用のデフォルト設定モックを生成する."""
    defaults: dict[str, object] = {
        "rag_upload_max_file_size_mb": 50,
        "rag_document_supported_extensions": ".md,.txt,.pdf,.adoc",
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


@pytest.fixture(autouse=True)
def _reset_global_state() -> None:
    """各テスト前にグローバル状態をリセットする."""
    _reset_rag_service()
    _reset_pipeline_controller()


@pytest.fixture(autouse=True)
def _mock_settings():
    """全テストで get_settings をモックする."""
    with patch("rag.server.get_settings", return_value=_default_mock_settings()):
        yield


@pytest.fixture
def app():
    """Starlette ASGI アプリを取得する."""
    return mcp.streamable_http_app()


@pytest.fixture
async def client(app):
    """httpx AsyncClient を作成する."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --- API キー検証テスト ---


class TestCheckApiKey:
    """_check_api_key のテスト."""

    @pytest.mark.asyncio
    async def test_missing_api_key_header_returns_401(
        self, client: httpx.AsyncClient,
    ) -> None:
        """X-API-Key ヘッダー未指定で 401 を返す."""
        with patch(
            "py_common_lib.secrets.get_secret", return_value="valid-key",
        ):
            resp = await client.post(
                "/upload/document",
                files={"file": ("test.md", b"content", "text/plain")},
            )
        assert resp.status_code == 401
        body = resp.json()
        assert body["status"] == "error"
        assert body["message"] == "Authentication required"

    @pytest.mark.asyncio
    async def test_invalid_api_key_returns_401(
        self, client: httpx.AsyncClient,
    ) -> None:
        """不正な API キーで 401 を返す."""
        with patch(
            "py_common_lib.secrets.get_secret", return_value="valid-key",
        ):
            resp = await client.post(
                "/upload/document",
                files={"file": ("test.md", b"content", "text/plain")},
                headers={"X-API-Key": "wrong-key"},
            )
        assert resp.status_code == 401
        body = resp.json()
        assert body["message"] == "Authentication required"

    @pytest.mark.asyncio
    async def test_valid_api_key_passes_auth(
        self, client: httpx.AsyncClient,
    ) -> None:
        """正しい API キーで認証を通過する（後続バリデーションで 400 になる）."""
        with patch(
            "py_common_lib.secrets.get_secret", return_value="valid-key",
        ):
            # ファイル未指定で送信 → auth は通過、バリデーションで 400
            resp = await client.post(
                "/upload/document",
                headers={"X-API-Key": "valid-key"},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_keyring_error_returns_500(
        self, client: httpx.AsyncClient,
    ) -> None:
        """keyring アクセス失敗で 500 を返す."""
        from py_common_lib.secrets import SecretStoreError

        with patch(
            "py_common_lib.secrets.get_secret",
            side_effect=SecretStoreError("keyring unavailable"),
        ):
            resp = await client.post(
                "/upload/document",
                files={"file": ("test.md", b"content", "text/plain")},
                headers={"X-API-Key": "some-key"},
            )
        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_journal_endpoint_also_requires_auth(
        self, client: httpx.AsyncClient,
    ) -> None:
        """journal エンドポイントでも認証が必要."""
        with patch(
            "py_common_lib.secrets.get_secret", return_value="valid-key",
        ):
            resp = await client.post(
                "/upload/journal",
                files={"file": ("journal.md", b"content", "text/plain")},
            )
        assert resp.status_code == 401


# --- バインドアドレス検証テスト ---


class TestValidateBindAddress:
    """_validate_bind_address のテスト."""

    def test_0000_is_rejected(self) -> None:
        """0.0.0.0 は拒否される."""
        result = _validate_bind_address("0.0.0.0")
        assert result is not None
        assert "0.0.0.0" in result

    def test_localhost_is_allowed(self) -> None:
        """localhost は許可される."""
        assert _validate_bind_address("localhost") is None

    def test_127001_is_allowed(self) -> None:
        """127.0.0.1 は許可される."""
        assert _validate_bind_address("127.0.0.1") is None

    def test_private_rfc1918_10_is_allowed(self) -> None:
        """10.x.x.x プライベートアドレスは許可される."""
        assert _validate_bind_address("10.0.0.1") is None

    def test_private_rfc1918_172_is_allowed(self) -> None:
        """172.16.x.x プライベートアドレスは許可される."""
        assert _validate_bind_address("172.16.0.1") is None

    def test_private_rfc1918_192_is_allowed(self) -> None:
        """192.168.x.x プライベートアドレスは許可される."""
        assert _validate_bind_address("192.168.1.100") is None

    def test_public_address_is_rejected_without_https(self) -> None:
        """パブリックアドレスは HTTPS なしで拒否される."""
        result = _validate_bind_address("8.8.8.8")
        assert result is not None
        assert "HTTPS" in result

    def test_hostname_is_allowed(self) -> None:
        """ホスト名（IP でない文字列）は許可される."""
        assert _validate_bind_address("myhost.local") is None


# --- API キー登録チェックテスト ---


class TestCheckApiKeyRegistered:
    """_check_api_key_registered のテスト."""

    def test_key_registered_returns_none(self) -> None:
        """キー登録済みなら None を返す."""
        with patch(
            "py_common_lib.secrets.get_secret", return_value="some-key",
        ):
            assert _check_api_key_registered() is None

    def test_key_not_found_returns_error(self) -> None:
        """キー未登録ならエラーメッセージを返す."""
        from py_common_lib.secrets import SecretNotFoundError

        with patch(
            "py_common_lib.secrets.get_secret",
            side_effect=SecretNotFoundError("not found"),
        ):
            result = _check_api_key_registered()
            assert result is not None
            assert "generate-api-key" in result

    def test_keyring_error_returns_error(self) -> None:
        """keyring エラーならエラーメッセージを返す."""
        from py_common_lib.secrets import SecretStoreError

        with patch(
            "py_common_lib.secrets.get_secret",
            side_effect=SecretStoreError("keyring error"),
        ):
            result = _check_api_key_registered()
            assert result is not None
            assert "keyring" in result


# --- generate-api-key CLI テスト ---


class TestGenerateApiKeyCli:
    """generate-api-key CLI サブコマンドのテスト."""

    def test_generate_display_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--save なしでキーを表示のみする."""
        import argparse

        from rag.cli import run_generate_api_key

        args = argparse.Namespace(save=False, force=False)
        run_generate_api_key(args)

        captured = capsys.readouterr()
        assert "Generated API key:" in captured.out
        assert "X-API-Key" in captured.out

    def test_generate_and_save(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--save でキーを keyring に保存する."""
        import argparse

        from rag.cli import run_generate_api_key

        args = argparse.Namespace(save=True, force=False)
        with (
            patch("keyring.get_password", return_value=None),
            patch("keyring.set_password") as mock_set,
        ):
            run_generate_api_key(args)

        captured = capsys.readouterr()
        assert "Generated API key:" in captured.out
        assert "keyring" in captured.out
        mock_set.assert_called_once()

    def test_save_existing_key_with_force(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--save --force で既存キーを確認なしで上書きする."""
        import argparse

        from rag.cli import run_generate_api_key

        args = argparse.Namespace(save=True, force=True)
        with (
            patch("keyring.get_password", return_value="old-key"),
            patch("keyring.set_password") as mock_set,
        ):
            run_generate_api_key(args)

        captured = capsys.readouterr()
        assert "Generated API key:" in captured.out
        mock_set.assert_called_once()

    def test_save_existing_key_without_force_declines(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--save で既存キーあり、上書き拒否でキー表示のみ."""
        import argparse

        from rag.cli import run_generate_api_key

        args = argparse.Namespace(save=True, force=False)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with (
            patch("keyring.get_password", return_value="old-key"),
            patch("keyring.set_password") as mock_set,
        ):
            run_generate_api_key(args)

        captured = capsys.readouterr()
        assert "Generated API key:" in captured.out
        assert "キャンセル" in captured.out
        mock_set.assert_not_called()

    def test_keyring_access_error_exits_1(self) -> None:
        """keyring アクセスエラーで exit code 1."""
        import argparse

        from rag.cli import run_generate_api_key

        args = argparse.Namespace(save=True, force=False)
        with (
            patch("keyring.get_password", side_effect=Exception("keyring error")),
            pytest.raises(SystemExit, match="1"),
        ):
            run_generate_api_key(args)

    def test_keyring_save_error_exits_1(self) -> None:
        """keyring 保存エラーで exit code 1."""
        import argparse

        from rag.cli import run_generate_api_key

        args = argparse.Namespace(save=True, force=False)
        with (
            patch("keyring.get_password", return_value=None),
            patch("keyring.set_password", side_effect=Exception("save error")),
            pytest.raises(SystemExit, match="1"),
        ):
            run_generate_api_key(args)

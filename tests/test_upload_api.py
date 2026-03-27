"""Upload HTTP API のテスト.

仕様: docs/specs/infrastructure/content-upload.md

テスト方針:
- Upload API のバリデーション（ファイル未指定、未対応拡張子、サイズ超過、upload_mode 不正）
- レスポンス形式（成功: 200 + JSON、エラー: 4xx/5xx + JSON）
- インジェストロックのノンブロッキング動作（同時インジェスト時の 409 レスポンス）
- multipart/form-data でのファイルアップロード → インジェスト完了の結合フロー
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from rag.server import (
    CLISubprocessError,
    _reset_pipeline_controller,
    _reset_rag_service,
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


# --- /upload/document バリデーションテスト ---


class TestUploadDocumentValidation:
    """POST /upload/document のバリデーションテスト."""

    @pytest.mark.asyncio
    async def test_file_missing_returns_400(self, client: httpx.AsyncClient) -> None:
        """file フィールド未指定で 400 を返す."""
        resp = await client.post("/upload/document")
        assert resp.status_code == 400
        body = resp.json()
        assert body["status"] == "error"
        assert "file" in body["message"]

    @pytest.mark.asyncio
    async def test_empty_file_returns_400(self, client: httpx.AsyncClient) -> None:
        """空ファイルで 400 を返す."""
        resp = await client.post(
            "/upload/document",
            files={"file": ("test.md", b"", "text/plain")},
        )
        assert resp.status_code == 400
        assert "空" in resp.json()["message"] or "0" in resp.json()["message"]

    @pytest.mark.asyncio
    async def test_unsupported_extension_returns_400(self, client: httpx.AsyncClient) -> None:
        """未対応拡張子で 400 を返す."""
        resp = await client.post(
            "/upload/document",
            files={"file": ("test.exe", b"content", "application/octet-stream")},
        )
        assert resp.status_code == 400
        assert "対応していない" in resp.json()["message"]

    @pytest.mark.asyncio
    async def test_invalid_upload_mode_returns_400(self, client: httpx.AsyncClient) -> None:
        """不正な upload_mode で 400 を返す."""
        resp = await client.post(
            "/upload/document",
            files={"file": ("test.md", b"content", "text/plain")},
            data={"upload_mode": "invalid"},
        )
        assert resp.status_code == 400
        assert "upload_mode" in resp.json()["message"]

    @pytest.mark.asyncio
    async def test_file_size_exceeds_limit_returns_413(self, client: httpx.AsyncClient) -> None:
        """ファイルサイズ上限超過で 413 を返す."""
        with patch(
            "rag.server.get_settings",
            return_value=_default_mock_settings(rag_upload_max_file_size_mb=1),
        ):
            large_data = b"x" * (1 * 1024 * 1024 + 1)
            resp = await client.post(
                "/upload/document",
                files={"file": ("test.md", large_data, "text/plain")},
            )
        assert resp.status_code == 413
        assert "上限" in resp.json()["message"]


# --- /upload/journal バリデーションテスト ---


class TestUploadJournalValidation:
    """POST /upload/journal のバリデーションテスト."""

    @pytest.mark.asyncio
    async def test_file_missing_returns_400(self, client: httpx.AsyncClient) -> None:
        """file フィールド未指定で 400 を返す."""
        resp = await client.post(
            "/upload/journal",
            data={"title": "Test", "repository": "test-repo"},
        )
        assert resp.status_code == 400
        assert "file" in resp.json()["message"]

    @pytest.mark.asyncio
    async def test_non_md_extension_returns_400(self, client: httpx.AsyncClient) -> None:
        """拡張子が .md 以外で 400 を返す."""
        resp = await client.post(
            "/upload/journal",
            files={"file": ("test.txt", b"content", "text/plain")},
            data={"title": "Test", "repository": "test-repo"},
        )
        assert resp.status_code == 400
        assert ".md" in resp.json()["message"]

    @pytest.mark.asyncio
    async def test_title_missing_returns_400(self, client: httpx.AsyncClient) -> None:
        """title 未指定で 400 を返す."""
        resp = await client.post(
            "/upload/journal",
            files={"file": ("test.md", b"content", "text/plain")},
            data={"repository": "test-repo"},
        )
        assert resp.status_code == 400
        assert "title" in resp.json()["message"]

    @pytest.mark.asyncio
    async def test_repository_missing_returns_400(self, client: httpx.AsyncClient) -> None:
        """repository 未指定で 400 を返す."""
        resp = await client.post(
            "/upload/journal",
            files={"file": ("test.md", b"content", "text/plain")},
            data={"title": "Test"},
        )
        assert resp.status_code == 400
        assert "repository" in resp.json()["message"]


# --- /upload/document 結合テスト ---


class TestUploadDocumentIntegration:
    """POST /upload/document の結合テスト."""

    @pytest.mark.asyncio
    async def test_successful_upload(self, client: httpx.AsyncClient) -> None:
        """正常なファイルアップロードが 200 を返す."""
        with patch("rag.server._run_cli_subprocess", new_callable=AsyncMock, return_value={}):
            resp = await client.post(
                "/upload/document",
                files={"file": ("notes.md", b"# Test content", "text/plain")},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "source_id" in body

    @pytest.mark.asyncio
    async def test_duplicate_file_returns_409(self, client: httpx.AsyncClient) -> None:
        """upload_mode=fail で同名ファイルが存在する場合 409 を返す."""
        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError(
                "同名ファイルが既に存在します: local/.upload/2026/01/01/test.md",
            ),
        ):
            resp = await client.post(
                "/upload/document",
                files={"file": ("test.md", b"content", "text/plain")},
                data={"upload_mode": "fail"},
            )

        assert resp.status_code == 409
        assert "同名ファイル" in resp.json()["message"]


# --- /upload/journal 結合テスト ---


class TestUploadJournalIntegration:
    """POST /upload/journal の結合テスト."""

    @pytest.mark.asyncio
    async def test_successful_upload(self, client: httpx.AsyncClient) -> None:
        """正常なジャーナルアップロードが 200 を返す."""
        mock_cli_result = {"entry_id": "20260326-120000-test"}

        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            return_value=mock_cli_result,
        ):
            resp = await client.post(
                "/upload/journal",
                files={"file": ("session.md", b"# Session log", "text/plain")},
                data={
                    "title": "Session log",
                    "repository": "rag-knowledge",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "source_id" in body


# --- インジェスト排他制御テスト ---


class TestIngestLockConflict:
    """CLI ファイルロック競合時の 409 レスポンステスト."""

    @pytest.mark.asyncio
    async def test_document_lock_conflict_returns_409(self, client: httpx.AsyncClient) -> None:
        """ドキュメントアップロード時にロック競合で 409 を返す."""
        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError("ロック競合", lock_conflict=True),
        ):
            resp = await client.post(
                "/upload/document",
                files={"file": ("test.md", b"content", "text/plain")},
            )

        assert resp.status_code == 409
        assert "インジェスト" in resp.json()["message"]

    @pytest.mark.asyncio
    async def test_journal_lock_conflict_returns_409(self, client: httpx.AsyncClient) -> None:
        """ジャーナルアップロード時にロック競合で 409 を返す."""
        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError("ロック競合", lock_conflict=True),
        ):
            resp = await client.post(
                "/upload/journal",
                files={"file": ("test.md", b"content", "text/plain")},
                data={"title": "Test", "repository": "test-repo"},
            )

        assert resp.status_code == 409
        assert "インジェスト" in resp.json()["message"]

    @pytest.mark.asyncio
    async def test_document_cli_error_returns_500(self, client: httpx.AsyncClient) -> None:
        """ドキュメントアップロード時に CLI エラーで 500 を返す."""
        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError("unexpected error"),
        ):
            resp = await client.post(
                "/upload/document",
                files={"file": ("test.md", b"content", "text/plain")},
            )

        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_journal_cli_error_returns_500(self, client: httpx.AsyncClient) -> None:
        """ジャーナルアップロード時に CLI エラーで 500 を返す."""
        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError("unexpected error"),
        ):
            resp = await client.post(
                "/upload/journal",
                files={"file": ("test.md", b"content", "text/plain")},
                data={"title": "Test", "repository": "test-repo"},
            )

        assert resp.status_code == 500


# --- MCP ツールのインジェストロックテスト ---


class TestMCPToolIngestLock:
    """既存 MCP ツール（rag_add_document / rag_add_journal）のロック競合テスト."""

    @pytest.mark.asyncio
    async def test_rag_add_document_lock_conflict(self) -> None:
        """rag_add_document がロック競合時にエラーメッセージを返す."""
        from rag.server import rag_add_document

        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError("ロック競合", lock_conflict=True),
        ):
            result = await rag_add_document(
                content="test",
                filename="test.md",
                encoding="text",
                upload_mode="fail",
            )

        assert "別のインジェスト" in result

    @pytest.mark.asyncio
    async def test_rag_add_journal_lock_conflict(self) -> None:
        """rag_add_journal がロック競合時にエラーメッセージを返す."""
        from rag.server import rag_add_journal

        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError("ロック競合", lock_conflict=True),
        ):
            result = await rag_add_journal(
                title="Test",
                content="body",
                filename="test.md",
                repository="test-repo",
            )

        assert "別のインジェスト" in result


# --- レスポンス形式テスト ---


class TestResponseFormat:
    """Upload API のレスポンス形式テスト."""

    @pytest.mark.asyncio
    async def test_error_response_has_status_and_message(self, client: httpx.AsyncClient) -> None:
        """エラーレスポンスが status と message を含む."""
        resp = await client.post("/upload/document")
        body = resp.json()
        assert "status" in body
        assert "message" in body
        assert body["status"] == "error"

    @pytest.mark.asyncio
    async def test_error_response_no_internal_info(self, client: httpx.AsyncClient) -> None:
        """エラーレスポンスにスタックトレースや内部パスが含まれない."""
        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=RuntimeError("internal error detail"),
        ):
            resp = await client.post(
                "/upload/document",
                files={"file": ("test.md", b"content", "text/plain")},
            )

        body = resp.json()
        assert "Traceback" not in body["message"]
        assert "internal error detail" not in body["message"]


# --- 設定項目テスト ---


class TestUploadMaxFileSizeConfig:
    """rag_upload_max_file_size_mb 設定のテスト."""

    def test_valid_value(self) -> None:
        """有効な値が受け入れられる."""
        from rag.config import RAGSettings
        from settings_defaults import TEST_SETTINGS_DEFAULTS

        settings = RAGSettings(**{**TEST_SETTINGS_DEFAULTS, "rag_upload_max_file_size_mb": 100})
        assert settings.rag_upload_max_file_size_mb == 100

    def test_min_boundary(self) -> None:
        """下限値 1 が受け入れられる."""
        from rag.config import RAGSettings
        from settings_defaults import TEST_SETTINGS_DEFAULTS

        settings = RAGSettings(**{**TEST_SETTINGS_DEFAULTS, "rag_upload_max_file_size_mb": 1})
        assert settings.rag_upload_max_file_size_mb == 1

    def test_max_boundary(self) -> None:
        """上限値 500 が受け入れられる."""
        from rag.config import RAGSettings
        from settings_defaults import TEST_SETTINGS_DEFAULTS

        settings = RAGSettings(**{**TEST_SETTINGS_DEFAULTS, "rag_upload_max_file_size_mb": 500})
        assert settings.rag_upload_max_file_size_mb == 500

    def test_below_min_raises_error(self) -> None:
        """下限未満の値が拒否される."""
        from pydantic import ValidationError
        from rag.config import RAGSettings
        from settings_defaults import TEST_SETTINGS_DEFAULTS

        with pytest.raises(ValidationError):
            RAGSettings(**{**TEST_SETTINGS_DEFAULTS, "rag_upload_max_file_size_mb": 0})

    def test_above_max_raises_error(self) -> None:
        """上限超過の値が拒否される."""
        from pydantic import ValidationError
        from rag.config import RAGSettings
        from settings_defaults import TEST_SETTINGS_DEFAULTS

        with pytest.raises(ValidationError):
            RAGSettings(**{**TEST_SETTINGS_DEFAULTS, "rag_upload_max_file_size_mb": 501})

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
    _decode_form_value,
    mcp,
)


def _default_mock_settings(**overrides: object) -> MagicMock:
    """テスト用のデフォルト設定モックを生成する."""
    defaults: dict[str, object] = {
        "rag_upload_max_file_size_mb": 50,
        "rag_upload_retry_after_write_sec": 30,
        "rag_upload_retry_after_rebuild_sec": 300,
        "rag_document_supported_extensions": ".md,.txt,.pdf,.adoc",
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


@pytest.fixture(autouse=True)
def _mock_settings():
    """全テストで get_settings をモックする."""
    with patch("rag.server.get_settings", return_value=_default_mock_settings()):
        yield


@pytest.fixture(autouse=True)
def _mock_auth():
    """全テストで API キー認証をバイパスする."""
    with patch("rag.server._check_api_key", return_value=None):
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

    @pytest.mark.asyncio
    async def test_japanese_title_passed_to_cli(self, client: httpx.AsyncClient) -> None:
        """日本語タイトルが CLI サブプロセスに正しく渡される."""
        mock_cli_result = {"entry_id": "20260327-test"}
        captured_args: list[list[str]] = []

        async def capture_cli(cmd: str, args: list[str], **kwargs: object) -> dict:
            captured_args.append(args)
            return mock_cli_result

        japanese_title = "QA \u30c6\u30b9\u30c8\u30bb\u30c3\u30b7\u30e7\u30f3\u8a18\u9332"  # "QA テストセッション記録"

        with patch("rag.server._run_cli_subprocess", side_effect=capture_cli):
            resp = await client.post(
                "/upload/journal",
                files={"file": ("session.md", b"# Test", "text/plain")},
                data={
                    "title": japanese_title,
                    "repository": "rag-knowledge",
                },
            )

        assert resp.status_code == 200
        assert len(captured_args) == 1
        args = captured_args[0]
        title_idx = args.index("--title")
        assert args[title_idx + 1] == japanese_title

    @pytest.mark.asyncio
    async def test_cp932_encoded_japanese_title_restored(
        self, app: object
    ) -> None:
        """Windows curl の cp932 エンコードで送信された日本語タイトルが復元される."""
        mock_cli_result = {"entry_id": "20260327-cp932"}
        captured_args: list[list[str]] = []

        async def capture_cli(cmd: str, args: list[str], **kwargs: object) -> dict:
            captured_args.append(args)
            return mock_cli_result

        japanese_title = "QA \u30c6\u30b9\u30c8\u30bb\u30c3\u30b7\u30e7\u30f3\u8a18\u9332"
        # cp932 エンコードされたバイト列で raw multipart body を構築
        title_cp932 = japanese_title.encode("cp932")
        boundary = "----Cp932TestBoundary"
        file_content = b"# Test journal"
        raw_body = (
            b"--" + boundary.encode() + b"\r\n"
            b'Content-Disposition: form-data; name="title"\r\n\r\n'
            + title_cp932 + b"\r\n"
            b"--" + boundary.encode() + b"\r\n"
            b'Content-Disposition: form-data; name="repository"\r\n\r\n'
            b"test-repo\r\n"
            b"--" + boundary.encode() + b"\r\n"
            b'Content-Disposition: form-data; name="file"; filename="test.md"\r\n'
            b"Content-Type: text/markdown\r\n\r\n"
            + file_content + b"\r\n"
            b"--" + boundary.encode() + b"--\r\n"
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as raw_client:
            with patch("rag.server._run_cli_subprocess", side_effect=capture_cli):
                resp = await raw_client.post(
                    "/upload/journal",
                    content=raw_body,
                    headers={
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                    },
                )

        assert resp.status_code == 200
        assert len(captured_args) == 1
        args = captured_args[0]
        title_idx = args.index("--title")
        assert args[title_idx + 1] == japanese_title


# --- インジェスト排他制御テスト ---


class TestIngestLockConflict:
    """CLI ファイルロック競合時の HTTP 応答テスト（修正版案I: 429 / 503）."""

    @pytest.mark.asyncio
    async def test_document_ingest_lock_returns_429_with_retry_after(
        self, client: httpx.AsyncClient,
    ) -> None:
        """ingest ロック競合時に 429 + Retry-After=30 + 取り込み中メッセージ."""
        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError(
                "ロック競合", code="LOCK_CONFLICT", lock_type="write",
            ),
        ):
            resp = await client.post(
                "/upload/document",
                files={"file": ("test.md", b"content", "text/plain")},
            )

        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "30"
        message = resp.json()["message"]
        assert "別の取り込みが実行中" in message
        assert "再試行" not in message

    @pytest.mark.asyncio
    async def test_document_rebuild_lock_returns_503_with_retry_after(
        self, client: httpx.AsyncClient,
    ) -> None:
        """rebuild ロック競合時に 503 + Retry-After=300 + 再構築中メッセージ."""
        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError(
                "ロック競合", code="LOCK_CONFLICT", lock_type="rebuild",
            ),
        ):
            resp = await client.post(
                "/upload/document",
                files={"file": ("test.md", b"content", "text/plain")},
            )

        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "300"
        message = resp.json()["message"]
        assert "再構築処理中" in message
        assert "再試行" not in message

    @pytest.mark.asyncio
    async def test_document_lock_conflict_without_lock_type_defaults_to_ingest(
        self, client: httpx.AsyncClient,
    ) -> None:
        """lock_type 欠落時は安全側（ingest 相当の 429 + 短め Retry-After）."""
        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError("ロック競合", code="LOCK_CONFLICT"),
        ):
            resp = await client.post(
                "/upload/document",
                files={"file": ("test.md", b"content", "text/plain")},
            )

        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "30"

    @pytest.mark.asyncio
    async def test_journal_ingest_lock_returns_429_with_retry_after(
        self, client: httpx.AsyncClient,
    ) -> None:
        """ジャーナル: ingest ロック競合時に 429 + Retry-After=30."""
        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError(
                "ロック競合", code="LOCK_CONFLICT", lock_type="write",
            ),
        ):
            resp = await client.post(
                "/upload/journal",
                files={"file": ("test.md", b"content", "text/plain")},
                data={"title": "Test", "repository": "test-repo"},
            )

        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "30"
        assert "別の取り込みが実行中" in resp.json()["message"]

    @pytest.mark.asyncio
    async def test_journal_rebuild_lock_returns_503_with_retry_after(
        self, client: httpx.AsyncClient,
    ) -> None:
        """ジャーナル: rebuild ロック競合時に 503 + Retry-After=300."""
        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError(
                "ロック競合", code="LOCK_CONFLICT", lock_type="rebuild",
            ),
        ):
            resp = await client.post(
                "/upload/journal",
                files={"file": ("test.md", b"content", "text/plain")},
                data={"title": "Test", "repository": "test-repo"},
            )

        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "300"
        assert "再構築処理中" in resp.json()["message"]

    @pytest.mark.asyncio
    async def test_retry_after_reflects_settings(
        self, client: httpx.AsyncClient,
    ) -> None:
        """Retry-After 値は設定（rag_upload_retry_after_*）を反映する."""
        custom_settings = _default_mock_settings(
            rag_upload_retry_after_write_sec=45,
            rag_upload_retry_after_rebuild_sec=600,
        )
        with patch("rag.server.get_settings", return_value=custom_settings):
            with patch(
                "rag.server._run_cli_subprocess",
                new_callable=AsyncMock,
                side_effect=CLISubprocessError(
                    "ロック競合", code="LOCK_CONFLICT", lock_type="write",
                ),
            ):
                resp = await client.post(
                    "/upload/document",
                    files={"file": ("test.md", b"c", "text/plain")},
                )
            assert resp.status_code == 429
            assert resp.headers["Retry-After"] == "45"

            with patch(
                "rag.server._run_cli_subprocess",
                new_callable=AsyncMock,
                side_effect=CLISubprocessError(
                    "ロック競合", code="LOCK_CONFLICT", lock_type="rebuild",
                ),
            ):
                resp = await client.post(
                    "/upload/document",
                    files={"file": ("test.md", b"c", "text/plain")},
                )
            assert resp.status_code == 503
            assert resp.headers["Retry-After"] == "600"

    @pytest.mark.asyncio
    async def test_document_cli_error_returns_500(self, client: httpx.AsyncClient) -> None:
        """ドキュメントアップロード時に CLI エラー（非ロック）で 500 を返す."""
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
        """ジャーナルアップロード時に CLI エラー（非ロック）で 500 を返す."""
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
    async def test_rag_add_document_ingest_lock_returns_ingest_message(self) -> None:
        """rag_add_document: ingest ロック競合時に取り込み中メッセージ."""
        from rag.server import rag_add_document

        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError(
                "ロック競合", code="LOCK_CONFLICT", lock_type="write",
            ),
        ):
            result = await rag_add_document(
                content="test",
                filename="test.md",
                encoding="text",
                upload_mode="fail",
            )

        assert "別の取り込みが実行中" in result
        assert "再試行" not in result

    @pytest.mark.asyncio
    async def test_rag_add_document_rebuild_lock_returns_rebuild_message(self) -> None:
        """rag_add_document: rebuild ロック競合時に再構築中メッセージ."""
        from rag.server import rag_add_document

        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError(
                "ロック競合", code="LOCK_CONFLICT", lock_type="rebuild",
            ),
        ):
            result = await rag_add_document(
                content="test",
                filename="test.md",
                encoding="text",
                upload_mode="fail",
            )

        assert "再構築処理中" in result
        assert "再試行" not in result

    @pytest.mark.asyncio
    async def test_rag_add_journal_ingest_lock_returns_ingest_message(self) -> None:
        """rag_add_journal: ingest ロック競合時に取り込み中メッセージ."""
        from rag.server import rag_add_journal

        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError(
                "ロック競合", code="LOCK_CONFLICT", lock_type="write",
            ),
        ):
            result = await rag_add_journal(
                title="Test",
                content="body",
                filename="test.md",
                repository="test-repo",
            )

        assert "別の取り込みが実行中" in result
        assert "再試行" not in result

    @pytest.mark.asyncio
    async def test_rag_add_journal_rebuild_lock_returns_rebuild_message(self) -> None:
        """rag_add_journal: rebuild ロック競合時に再構築中メッセージ."""
        from rag.server import rag_add_journal

        with patch(
            "rag.server._run_cli_subprocess",
            new_callable=AsyncMock,
            side_effect=CLISubprocessError(
                "ロック競合", code="LOCK_CONFLICT", lock_type="rebuild",
            ),
        ):
            result = await rag_add_journal(
                title="Test",
                content="body",
                filename="test.md",
                repository="test-repo",
            )

        assert "再構築処理中" in result
        assert "再試行" not in result


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


# --- _decode_form_value テスト ---


class TestDecodeFormValue:
    """Starlette Latin-1 フォールバック文字列の復元テスト."""

    def test_ascii_string_unchanged(self) -> None:
        """ASCII 文字列はそのまま返される."""
        assert _decode_form_value("hello") == "hello"

    def test_utf8_string_unchanged(self) -> None:
        """正しい UTF-8 文字列はそのまま返される."""
        assert _decode_form_value("日本語テキスト") == "日本語テキスト"

    def test_cp932_latin1_fallback_restored(self) -> None:
        """cp932 → Latin-1 フォールバック文字列が cp932 として復元される."""
        # Starlette が cp932 バイト列を Latin-1 にフォールバックした文字列を模擬
        original = "QA スキルの仕様書・スキル定義作成"
        cp932_bytes = original.encode("cp932")
        latin1_garbled = cp932_bytes.decode("latin-1")
        assert _decode_form_value(latin1_garbled) == original

    def test_utf8_latin1_fallback_restored(self) -> None:
        """UTF-8 バイト列が Latin-1 フォールバックされた場合に正しく復元される."""
        original = "日本語テキスト"
        utf8_bytes = original.encode("utf-8")
        latin1_garbled = utf8_bytes.decode("latin-1")
        assert _decode_form_value(latin1_garbled) == original

    def test_empty_string(self) -> None:
        """空文字列はそのまま返される."""
        assert _decode_form_value("") == ""

    def test_mixed_ascii_and_non_ascii(self) -> None:
        """ASCII と非 ASCII が混在する UTF-8 Latin-1 フォールバック文字列が復元される."""
        original = "Title: テスト"
        utf8_bytes = original.encode("utf-8")
        latin1_garbled = utf8_bytes.decode("latin-1")
        assert _decode_form_value(latin1_garbled) == original

    def test_cp932_mixed_ascii_restored(self) -> None:
        """ASCII と日本語が混在する cp932 Latin-1 フォールバックが復元される."""
        original = "QA テストセッション記録"
        cp932_bytes = original.encode("cp932")
        latin1_garbled = cp932_bytes.decode("latin-1")
        assert _decode_form_value(latin1_garbled) == original

    def test_latin1_text_not_misidentified_as_cp932(self) -> None:
        """正当な Latin-1 テキストが cp932 に誤変換されない."""
        # \xa1 は Latin-1 では ¡ だが cp932 では ｡（半角句点）
        # 日本語文字を含まないため cp932 デコード結果は採用されない
        latin1_text = "\u00a1Hola!"  # "¡Hola!"
        assert _decode_form_value(latin1_text) == latin1_text

    def test_latin1_accented_text_unchanged(self) -> None:
        """アクセント付き Latin-1 テキストがそのまま返される."""
        latin1_text = "caf\u00e9 r\u00e9sum\u00e9"  # "café résumé"
        assert _decode_form_value(latin1_text) == latin1_text

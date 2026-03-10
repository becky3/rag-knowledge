"""SafeBrowsingClient テスト

仕様: docs/specs/rag-knowledge.md
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from rag.safe_browsing import (
    CacheEntry,
    SafeBrowsingClient,
    SafeBrowsingResult,
    SafetyCheckError,
    ThreatType,
    create_safe_browsing_client,
)


class MockResponse:
    """モックHTTPレスポンス."""

    def __init__(self, status: int = 200, json_data: dict[str, object] | None = None) -> None:
        self.status = status
        self._json_data: dict[str, object] = json_data or {}

    async def json(self) -> dict[str, object]:
        return self._json_data

    async def text(self) -> str:
        return str(self._json_data)


class MockClientSession:
    """モックaiohttpクライアントセッション."""

    def __init__(self, response: MockResponse) -> None:
        self._response = response

    async def __aenter__(self) -> "MockClientSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def post(self, url: str, **kwargs: object) -> "MockContextManager":
        return MockContextManager(self._response)


class MockContextManager:
    """モックコンテキストマネージャ."""

    def __init__(self, response: MockResponse) -> None:
        self._response = response

    async def __aenter__(self) -> MockResponse:
        return self._response

    async def __aexit__(self, *args: object) -> None:
        pass


class TestSafeBrowsingClient:
    """SafeBrowsingClient のテスト."""

    def test_init(self) -> None:
        """クライアントが正しく初期化されること."""
        client = SafeBrowsingClient(api_key="test-api-key")
        assert client._api_key == "test-api-key"
        assert client._client_id == "rag-knowledge"
        assert client._client_version == "1.0.0"

    def test_init_with_custom_params(self) -> None:
        """カスタムパラメータで初期化できること."""
        client = SafeBrowsingClient(
            api_key="test-key",
            timeout=30.0,
            cache_ttl=600,
            client_id="custom-client",
            client_version="2.0.0",
        )
        assert client._cache_ttl == 600
        assert client._client_id == "custom-client"
        assert client._client_version == "2.0.0"

    def test_build_request_body(self) -> None:
        """リクエストボディが正しく構築されること."""
        client = SafeBrowsingClient(api_key="test-key")
        urls = ["https://example.com", "https://test.com"]
        body = client._build_request_body(urls)

        assert body["client"]["clientId"] == "rag-knowledge"
        assert body["client"]["clientVersion"] == "1.0.0"
        assert "MALWARE" in body["threatInfo"]["threatTypes"]
        assert "SOCIAL_ENGINEERING" in body["threatInfo"]["threatTypes"]
        assert "UNWANTED_SOFTWARE" in body["threatInfo"]["threatTypes"]
        assert "POTENTIALLY_HARMFUL_APPLICATION" in body["threatInfo"]["threatTypes"]
        assert body["threatInfo"]["platformTypes"] == ["ANY_PLATFORM"]
        assert body["threatInfo"]["threatEntryTypes"] == ["URL"]
        assert len(body["threatInfo"]["threatEntries"]) == 2

    def test_parse_response_no_matches(self) -> None:
        """脅威がない場合、全URLが安全と判定されること."""
        client = SafeBrowsingClient(api_key="test-key")
        urls = ["https://safe1.com", "https://safe2.com"]
        response_data: dict[str, object] = {}  # 空のレスポンス = マッチなし

        results = client._parse_response(response_data, urls)

        assert len(results) == 2
        assert results["https://safe1.com"].is_safe is True
        assert results["https://safe2.com"].is_safe is True

    def test_parse_response_with_threats(self) -> None:
        """脅威が検出された場合、該当URLが危険と判定されること."""
        client = SafeBrowsingClient(api_key="test-key")
        urls = ["https://safe.com", "https://malware.com"]
        response_data = {
            "matches": [
                {
                    "threatType": "MALWARE",
                    "platformType": "ANY_PLATFORM",
                    "threat": {"url": "https://malware.com"},
                    "cacheDuration": "300s",
                }
            ]
        }

        results = client._parse_response(response_data, urls)

        assert results["https://safe.com"].is_safe is True
        assert results["https://malware.com"].is_safe is False
        assert len(results["https://malware.com"].threats) == 1
        assert results["https://malware.com"].threats[0].threat_type == ThreatType.MALWARE


class TestSafeBrowsingClientAsync:
    """SafeBrowsingClient の非同期テスト."""

    @pytest.mark.asyncio
    async def test_check_url_safe(self) -> None:
        """API呼び出しと安全なURLのチェック."""
        client = SafeBrowsingClient(api_key="test-key")
        mock_response = MockResponse(200, {})  # 空 = 脅威なし

        with patch(
            "rag.safe_browsing.aiohttp.ClientSession",
            return_value=MockClientSession(mock_response),
        ):
            result = await client.check_url("https://safe-site.com")

        assert result.is_safe is True
        assert result.url == "https://safe-site.com"
        assert len(result.threats) == 0

    @pytest.mark.asyncio
    async def test_check_url_unsafe(self) -> None:
        """AC3: 危険なURLのチェックでis_safe=Falseが返ること."""
        client = SafeBrowsingClient(api_key="test-key")
        mock_response = MockResponse(
            200,
            {
                "matches": [
                    {
                        "threatType": "SOCIAL_ENGINEERING",
                        "platformType": "ANY_PLATFORM",
                        "threat": {"url": "https://phishing-site.com"},
                    }
                ]
            },
        )

        with patch(
            "rag.safe_browsing.aiohttp.ClientSession",
            return_value=MockClientSession(mock_response),
        ):
            result = await client.check_url("https://phishing-site.com")

        assert result.is_safe is False
        assert len(result.threats) == 1
        assert result.threats[0].threat_type == ThreatType.SOCIAL_ENGINEERING

    @pytest.mark.asyncio
    async def test_check_urls_batch(self) -> None:
        """複数URLの一括チェック."""
        client = SafeBrowsingClient(api_key="test-key")
        mock_response = MockResponse(
            200,
            {
                "matches": [
                    {
                        "threatType": "MALWARE",
                        "platformType": "ANY_PLATFORM",
                        "threat": {"url": "https://malware.com"},
                    }
                ]
            },
        )

        with patch(
            "rag.safe_browsing.aiohttp.ClientSession",
            return_value=MockClientSession(mock_response),
        ):
            urls = ["https://safe.com", "https://malware.com", "https://another-safe.com"]
            results = await client.check_urls(urls)

        assert len(results) == 3
        assert results["https://safe.com"].is_safe is True
        assert results["https://malware.com"].is_safe is False
        assert results["https://another-safe.com"].is_safe is True

    @pytest.mark.asyncio
    async def test_check_urls_empty_list(self) -> None:
        """空のURLリストに対して空の結果を返すこと."""
        client = SafeBrowsingClient(api_key="test-key")
        results = await client.check_urls([])
        assert results == {}

    @pytest.mark.asyncio
    async def test_is_url_safe_simple_interface(self) -> None:
        """シンプルなインターフェースで安全性を判定できること."""
        client = SafeBrowsingClient(api_key="test-key")
        mock_response = MockResponse(200, {})

        with patch(
            "rag.safe_browsing.aiohttp.ClientSession",
            return_value=MockClientSession(mock_response),
        ):
            is_safe = await client.is_url_safe("https://safe-site.com")

        assert is_safe is True


class TestSafeBrowsingCache:
    """SafeBrowsingClient のキャッシュテスト."""

    @pytest.mark.asyncio
    async def test_cache_hit(self) -> None:
        """AC8: キャッシュヒット時にAPIが呼ばれないこと."""
        client = SafeBrowsingClient(api_key="test-key", cache_ttl=300)

        # 最初のリクエスト
        mock_response = MockResponse(200, {})
        with patch(
            "rag.safe_browsing.aiohttp.ClientSession",
            return_value=MockClientSession(mock_response),
        ) as mock_session:
            await client.check_url("https://example.com")
            first_call_count = mock_session.call_count

        # 2回目のリクエスト（キャッシュヒット）
        with patch(
            "rag.safe_browsing.aiohttp.ClientSession",
            return_value=MockClientSession(mock_response),
        ) as mock_session:
            result = await client.check_url("https://example.com")
            second_call_count = mock_session.call_count

        assert first_call_count == 1
        assert second_call_count == 0  # キャッシュヒットでAPI呼び出しなし
        assert result.cached is True

    @pytest.mark.asyncio
    async def test_cache_expiry(self) -> None:
        """キャッシュ期限切れ時にAPIが再度呼ばれること."""
        client = SafeBrowsingClient(api_key="test-key", cache_ttl=0.1)  # 100ms TTL

        mock_response = MockResponse(200, {})

        # 最初のリクエスト
        with patch(
            "rag.safe_browsing.aiohttp.ClientSession",
            return_value=MockClientSession(mock_response),
        ):
            await client.check_url("https://example.com")

        # キャッシュ期限切れを待つ
        await asyncio.sleep(0.15)

        # 2回目のリクエスト（キャッシュミス）
        with patch(
            "rag.safe_browsing.aiohttp.ClientSession",
            return_value=MockClientSession(mock_response),
        ) as mock_session:
            result = await client.check_url("https://example.com")

        assert mock_session.call_count == 1  # キャッシュミスでAPI呼び出しあり
        assert result.cached is False

    @pytest.mark.asyncio
    async def test_cleanup_expired_cache(self) -> None:
        """期限切れキャッシュのクリーンアップ."""
        client = SafeBrowsingClient(api_key="test-key", cache_ttl=0.1)

        mock_response = MockResponse(200, {})

        # キャッシュにエントリを追加
        with patch(
            "rag.safe_browsing.aiohttp.ClientSession",
            return_value=MockClientSession(mock_response),
        ):
            await client.check_url("https://example1.com")
            await client.check_url("https://example2.com")

        assert len(client._cache) == 2

        # キャッシュ期限切れを待つ
        await asyncio.sleep(0.15)

        # クリーンアップ
        cleaned = await client.cleanup_expired_cache()

        assert cleaned == 2
        assert len(client._cache) == 0

    @pytest.mark.asyncio
    async def test_clear_cache(self) -> None:
        """キャッシュのクリア."""
        client = SafeBrowsingClient(api_key="test-key")
        # 直接キャッシュに追加
        client._cache["test-key"] = CacheEntry(
            result=SafeBrowsingResult(url="https://test.com", is_safe=True),
            expires_at=time.time() + 300,
        )

        assert len(client._cache) == 1
        await client.clear_cache()
        assert len(client._cache) == 0


class TestSafeBrowsingErrorHandling:
    """SafeBrowsingClient のエラーハンドリングテスト."""

    @pytest.mark.asyncio
    async def test_api_error_fail_open(self) -> None:
        """AC6: API障害時にfail-open（URLを許可）すること."""
        client = SafeBrowsingClient(api_key="test-key")
        mock_response = MockResponse(500, {"error": "Internal Server Error"})

        with patch(
            "rag.safe_browsing.aiohttp.ClientSession",
            return_value=MockClientSession(mock_response),
        ):
            result = await client.check_url("https://unknown-site.com")

        # fail-open: エラー時も安全と判定
        assert result.is_safe is True
        assert result.error is not None
        assert "500" in result.error

    @pytest.mark.asyncio
    async def test_network_error_fail_open(self) -> None:
        """AC6: ネットワークエラー時にfail-open（URLを許可）すること."""
        client = SafeBrowsingClient(api_key="test-key", fail_open=True)

        with patch(
            "rag.safe_browsing.aiohttp.ClientSession",
            side_effect=Exception("Network error"),
        ):
            result = await client.check_url("https://unknown-site.com")

        assert result.is_safe is True
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_api_error_fail_close(self) -> None:
        """AC7: fail_close設定時にAPI障害で例外が送出されること."""
        client = SafeBrowsingClient(api_key="test-key", fail_open=False)
        mock_response = MockResponse(500, {"error": "Internal Server Error"})

        with patch(
            "rag.safe_browsing.aiohttp.ClientSession",
            return_value=MockClientSession(mock_response),
        ):
            with pytest.raises(SafetyCheckError, match="API障害"):
                await client.check_url("https://unknown-site.com")

    @pytest.mark.asyncio
    async def test_network_error_fail_close(self) -> None:
        """AC7: fail_close設定時にネットワークエラーで例外が送出されること."""
        client = SafeBrowsingClient(api_key="test-key", fail_open=False)

        with patch(
            "rag.safe_browsing.aiohttp.ClientSession",
            side_effect=Exception("Network error"),
        ):
            with pytest.raises(SafetyCheckError, match="API障害"):
                await client.check_url("https://unknown-site.com")


class TestSafetyCheckError:
    """SafetyCheckError 例外のテスト."""

    def test_safety_check_error_attributes(self) -> None:
        """SafetyCheckErrorの属性が正しく設定されること."""
        error = SafetyCheckError(
            url="https://malware.com",
            message="危険なURLが検出されました",
            threats=["MALWARE", "SOCIAL_ENGINEERING"],
        )
        assert error.url == "https://malware.com"
        assert error.threats == ["MALWARE", "SOCIAL_ENGINEERING"]
        assert str(error) == "危険なURLが検出されました"

    def test_safety_check_error_without_threats(self) -> None:
        """threats未指定時に空リストになること."""
        error = SafetyCheckError(url="https://test.com", message="エラー")
        assert error.threats == []


class TestThreatTypes:
    """脅威タイプのテスト."""

    @pytest.mark.asyncio
    async def test_multiple_threat_types(self) -> None:
        """複数の脅威タイプが検出されること."""
        client = SafeBrowsingClient(api_key="test-key")
        mock_response = MockResponse(
            200,
            {
                "matches": [
                    {
                        "threatType": "MALWARE",
                        "platformType": "ANY_PLATFORM",
                        "threat": {"url": "https://bad-site.com"},
                    },
                    {
                        "threatType": "SOCIAL_ENGINEERING",
                        "platformType": "ANY_PLATFORM",
                        "threat": {"url": "https://bad-site.com"},
                    },
                ]
            },
        )

        with patch(
            "rag.safe_browsing.aiohttp.ClientSession",
            return_value=MockClientSession(mock_response),
        ):
            result = await client.check_url("https://bad-site.com")

        assert result.is_safe is False
        assert len(result.threats) == 2
        threat_types = {t.threat_type for t in result.threats}
        assert ThreatType.MALWARE in threat_types
        assert ThreatType.SOCIAL_ENGINEERING in threat_types


class TestCreateSafeBrowsingClient:
    """ファクトリ関数のテスト."""

    def test_create_client_disabled(self) -> None:
        """AC4: RAG_URL_SAFETY_CHECK=false の場合、チェックがスキップされること."""
        mock_settings = MagicMock()
        mock_settings.rag_url_safety_check = False

        client = create_safe_browsing_client(mock_settings)
        assert client is None

    def test_create_client_no_api_key(self) -> None:
        """AC5: GOOGLE_SAFE_BROWSING_API_KEY 未設定の場合、スキップ."""
        mock_settings = MagicMock()
        mock_settings.rag_url_safety_check = True
        mock_settings.google_safe_browsing_api_key = ""

        client = create_safe_browsing_client(mock_settings)
        assert client is None

    def test_create_client_enabled(self) -> None:
        """有効な設定でクライアントが作成されること."""
        mock_settings = MagicMock()
        mock_settings.rag_url_safety_check = True
        mock_settings.google_safe_browsing_api_key = "test-api-key"
        mock_settings.rag_url_safety_cache_ttl = 300
        mock_settings.rag_url_safety_fail_open = True
        mock_settings.rag_url_safety_timeout = 5.0

        client = create_safe_browsing_client(mock_settings)
        assert client is not None
        assert isinstance(client, SafeBrowsingClient)

    def test_create_client_with_custom_cache_ttl(self) -> None:
        """カスタムキャッシュTTLで作成されること."""
        mock_settings = MagicMock()
        mock_settings.rag_url_safety_check = True
        mock_settings.google_safe_browsing_api_key = "test-api-key"
        mock_settings.rag_url_safety_cache_ttl = 600
        mock_settings.rag_url_safety_fail_open = True
        mock_settings.rag_url_safety_timeout = 5.0

        client = create_safe_browsing_client(mock_settings)
        assert client is not None
        assert client._cache_ttl == 600.0

    def test_create_client_with_zero_cache_ttl(self) -> None:
        """キャッシュTTL=0の場合、APIレスポンスに従う設定になること."""
        mock_settings = MagicMock()
        mock_settings.rag_url_safety_check = True
        mock_settings.google_safe_browsing_api_key = "test-api-key"
        mock_settings.rag_url_safety_cache_ttl = 0
        mock_settings.rag_url_safety_fail_open = True
        mock_settings.rag_url_safety_timeout = 5.0

        client = create_safe_browsing_client(mock_settings)
        assert client is not None
        assert client._cache_ttl is None  # APIレスポンスに従う

    def test_create_client_with_fail_open_false(self) -> None:
        """fail_open=Falseで作成されること."""
        mock_settings = MagicMock()
        mock_settings.rag_url_safety_check = True
        mock_settings.google_safe_browsing_api_key = "test-api-key"
        mock_settings.rag_url_safety_cache_ttl = 300
        mock_settings.rag_url_safety_fail_open = False
        mock_settings.rag_url_safety_timeout = 5.0

        client = create_safe_browsing_client(mock_settings)
        assert client is not None
        assert client._fail_open is False

"""Scrapy SSRF Middleware のユニットテスト.

仕様: docs/specs/site-ingest.md

テスト方針:
- パブリック IP に解決される URL は通過すること
- プライベート IP に解決される URL は IgnoreRequest で拒否すること
- localhost は IgnoreRequest で拒否すること
- DNS 解決に失敗した場合は IgnoreRequest で拒否すること
- ホスト名なしの URL は IgnoreRequest で拒否すること
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Request

from rag.scrapy.middleware import SsrfMiddleware


class TestSsrfMiddleware:
    """SsrfMiddleware のテスト."""

    def setup_method(self) -> None:
        """各テスト前に Middleware インスタンスを生成する."""
        self.middleware = SsrfMiddleware()
        self.spider = MagicMock()

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_public_ip_passes(self, mock_getaddrinfo: MagicMock) -> None:
        """パブリック IP に解決される URL は通過する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]
        request = Request(url="https://example.com/page")
        result = self.middleware.process_request(request, self.spider)
        assert result is None

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_private_ip_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        """プライベート IP（10.0.0.0/8）に解決される URL は拒否する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("10.0.0.1", 0)),
        ]
        request = Request(url="https://evil.example.com/page")
        with pytest.raises(IgnoreRequest, match="SSRF check failed"):
            self.middleware.process_request(request, self.spider)

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_loopback_ip_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        """ループバック IP（127.0.0.0/8）に解決される URL は拒否する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]
        request = Request(url="https://rebind.example.com/page")
        with pytest.raises(IgnoreRequest, match="SSRF check failed"):
            self.middleware.process_request(request, self.spider)

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_link_local_ip_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        """リンクローカル IP（169.254.0.0/16）に解決される URL は拒否する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("169.254.1.1", 0)),
        ]
        request = Request(url="https://link-local.example.com/page")
        with pytest.raises(IgnoreRequest, match="SSRF check failed"):
            self.middleware.process_request(request, self.spider)

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_rfc1918_172_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        """RFC 1918 プライベート IP（172.16.0.0/12）は拒否する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("172.16.0.1", 0)),
        ]
        request = Request(url="https://internal.example.com/page")
        with pytest.raises(IgnoreRequest, match="SSRF check failed"):
            self.middleware.process_request(request, self.spider)

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_rfc1918_192_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        """RFC 1918 プライベート IP（192.168.0.0/16）は拒否する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("192.168.1.1", 0)),
        ]
        request = Request(url="https://home.example.com/page")
        with pytest.raises(IgnoreRequest, match="SSRF check failed"):
            self.middleware.process_request(request, self.spider)

    def test_localhost_hostname_rejected(self) -> None:
        """localhost ホスト名は DNS 解決前にホスト名マッチで拒否する."""
        request = Request(url="https://localhost/page")
        with pytest.raises(IgnoreRequest, match="SSRF check failed"):
            self.middleware.process_request(request, self.spider)

    def test_localhost_localdomain_rejected(self) -> None:
        """localhost.localdomain ホスト名は拒否する."""
        request = Request(url="https://localhost.localdomain/page")
        with pytest.raises(IgnoreRequest, match="SSRF check failed"):
            self.middleware.process_request(request, self.spider)

    @patch(
        "rag.utils.url.socket.getaddrinfo",
        side_effect=socket.gaierror("DNS resolution failed"),
    )
    def test_dns_failure_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        """DNS 解決に失敗した場合は拒否する."""
        request = Request(url="https://nonexistent.example.com/page")
        with pytest.raises(IgnoreRequest, match="SSRF check failed"):
            self.middleware.process_request(request, self.spider)

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_ipv6_loopback_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        """IPv6 ループバック（::1）に解決される URL は拒否する."""
        mock_getaddrinfo.return_value = [
            (10, 1, 6, "", ("::1", 0, 0, 0)),
        ]
        request = Request(url="https://ipv6-loopback.example.com/page")
        with pytest.raises(IgnoreRequest, match="SSRF check failed"):
            self.middleware.process_request(request, self.spider)

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_ipv6_unique_local_rejected(
        self, mock_getaddrinfo: MagicMock
    ) -> None:
        """IPv6 ユニークローカル（fc00::/7）に解決される URL は拒否する."""
        mock_getaddrinfo.return_value = [
            (10, 1, 6, "", ("fd00::1", 0, 0, 0)),
        ]
        request = Request(url="https://ipv6-ula.example.com/page")
        with pytest.raises(IgnoreRequest, match="SSRF check failed"):
            self.middleware.process_request(request, self.spider)

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_mixed_ips_with_private_rejected(
        self, mock_getaddrinfo: MagicMock
    ) -> None:
        """パブリック IP とプライベート IP が混在する場合は拒否する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("10.0.0.1", 0)),
        ]
        request = Request(url="https://dual.example.com/page")
        with pytest.raises(IgnoreRequest, match="SSRF check failed"):
            self.middleware.process_request(request, self.spider)

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_multiple_public_ips_pass(
        self, mock_getaddrinfo: MagicMock
    ) -> None:
        """複数のパブリック IP は全て通過する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("93.184.216.35", 0)),
        ]
        request = Request(url="https://cdn.example.com/page")
        result = self.middleware.process_request(request, self.spider)
        assert result is None

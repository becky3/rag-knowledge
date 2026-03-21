"""Scrapy SSRF Middleware のユニットテスト.

仕様: docs/specs/site-ingest.md

テスト方針:
- パブリック IP に解決される URL は通過すること
- プライベート IP に解決される URL は IgnoreRequest で拒否すること
- localhost は IgnoreRequest で拒否すること
- DNS 解決に失敗した場合は IgnoreRequest で拒否すること
- ホスト名なしの URL は IgnoreRequest で拒否すること
- IPv4-mapped IPv6 アドレスは対応する IPv4 として評価すること
- パース不能な IP アドレスは fail-closed で拒否すること
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Request

from rag.scrapy.middleware import SsrfMiddleware
from rag.utils.url import check_ssrf


class TestCheckSsrf:
    """check_ssrf ユーティリティのテスト."""

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_public_ip_passes(self, mock_getaddrinfo: MagicMock) -> None:
        """パブリック IP に解決される URL は通過する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]
        check_ssrf("https://example.com/page")

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_private_ip_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        """プライベート IP（10.0.0.0/8）に解決される URL は拒否する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("10.0.0.1", 0)),
        ]
        with pytest.raises(ValueError, match="プライベート IP"):
            check_ssrf("https://evil.example.com/page")

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_loopback_ip_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        """ループバック IP（127.0.0.0/8）に解決される URL は拒否する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]
        with pytest.raises(ValueError, match="プライベート IP"):
            check_ssrf("https://rebind.example.com/page")

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_link_local_ip_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        """リンクローカル IP（169.254.0.0/16）に解決される URL は拒否する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("169.254.1.1", 0)),
        ]
        with pytest.raises(ValueError, match="プライベート IP"):
            check_ssrf("https://link-local.example.com/page")

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_rfc1918_172_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        """RFC 1918 プライベート IP（172.16.0.0/12）は拒否する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("172.16.0.1", 0)),
        ]
        with pytest.raises(ValueError, match="プライベート IP"):
            check_ssrf("https://internal.example.com/page")

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_rfc1918_192_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        """RFC 1918 プライベート IP（192.168.0.0/16）は拒否する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("192.168.1.1", 0)),
        ]
        with pytest.raises(ValueError, match="プライベート IP"):
            check_ssrf("https://home.example.com/page")

    def test_localhost_hostname_rejected(self) -> None:
        """localhost ホスト名は DNS 解決前にホスト名マッチで拒否する."""
        with pytest.raises(ValueError, match="プライベートホスト"):
            check_ssrf("https://localhost/page")

    def test_localhost_localdomain_rejected(self) -> None:
        """localhost.localdomain ホスト名は拒否する."""
        with pytest.raises(ValueError, match="プライベートホスト"):
            check_ssrf("https://localhost.localdomain/page")

    @patch(
        "rag.utils.url.socket.getaddrinfo",
        side_effect=socket.gaierror("DNS resolution failed"),
    )
    def test_dns_failure_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        """DNS 解決に失敗した場合は拒否する."""
        with pytest.raises(ValueError, match="DNS 解決に失敗"):
            check_ssrf("https://nonexistent.example.com/page")

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_ipv6_loopback_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        """IPv6 ループバック（::1）に解決される URL は拒否する."""
        mock_getaddrinfo.return_value = [
            (10, 1, 6, "", ("::1", 0, 0, 0)),
        ]
        with pytest.raises(ValueError, match="プライベート IP"):
            check_ssrf("https://ipv6-loopback.example.com/page")

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_ipv6_unique_local_rejected(
        self, mock_getaddrinfo: MagicMock
    ) -> None:
        """IPv6 ユニークローカル（fc00::/7）に解決される URL は拒否する."""
        mock_getaddrinfo.return_value = [
            (10, 1, 6, "", ("fd00::1", 0, 0, 0)),
        ]
        with pytest.raises(ValueError, match="プライベート IP"):
            check_ssrf("https://ipv6-ula.example.com/page")

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_mixed_ips_with_private_rejected(
        self, mock_getaddrinfo: MagicMock
    ) -> None:
        """パブリック IP とプライベート IP が混在する場合は拒否する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("10.0.0.1", 0)),
        ]
        with pytest.raises(ValueError, match="プライベート IP"):
            check_ssrf("https://dual.example.com/page")

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_multiple_public_ips_pass(
        self, mock_getaddrinfo: MagicMock
    ) -> None:
        """複数のパブリック IP は全て通過する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("93.184.216.35", 0)),
        ]
        check_ssrf("https://cdn.example.com/page")

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_ipv4_mapped_ipv6_loopback_rejected(
        self, mock_getaddrinfo: MagicMock
    ) -> None:
        """IPv4-mapped IPv6（::ffff:127.0.0.1）はループバックとして拒否する."""
        mock_getaddrinfo.return_value = [
            (10, 1, 6, "", ("::ffff:127.0.0.1", 0, 0, 0)),
        ]
        with pytest.raises(ValueError, match="プライベート IP"):
            check_ssrf("https://mapped-loopback.example.com/page")

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_ipv4_mapped_ipv6_private_rejected(
        self, mock_getaddrinfo: MagicMock
    ) -> None:
        """IPv4-mapped IPv6（::ffff:10.0.0.1）はプライベート IP として拒否する."""
        mock_getaddrinfo.return_value = [
            (10, 1, 6, "", ("::ffff:10.0.0.1", 0, 0, 0)),
        ]
        with pytest.raises(ValueError, match="プライベート IP"):
            check_ssrf("https://mapped-private.example.com/page")

    @patch("rag.utils.url.socket.getaddrinfo")
    def test_unparseable_ip_rejected(
        self, mock_getaddrinfo: MagicMock
    ) -> None:
        """パース不能な IP アドレスは fail-closed で拒否する."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("not-an-ip", 0)),
        ]
        with pytest.raises(ValueError, match="無効な IP アドレス"):
            check_ssrf("https://bad-dns.example.com/page")

    def test_no_hostname_rejected(self) -> None:
        """ホスト名なしの URL は拒否する."""
        with pytest.raises(ValueError, match="ホスト名がありません"):
            check_ssrf("https:///path/only")


class TestSsrfMiddleware:
    """SsrfMiddleware のテスト（同期パス）."""

    def setup_method(self) -> None:
        """各テスト前に Middleware インスタンスを生成する."""
        self.middleware = SsrfMiddleware()
        self.spider = MagicMock()

    def test_no_hostname_rejected(self) -> None:
        """ホスト名なしの URL は同期パスで即座に拒否する."""
        request = Request(url="https:///path/only")
        with pytest.raises(IgnoreRequest, match="SSRF check failed"):
            self.middleware.process_request(request, self.spider)

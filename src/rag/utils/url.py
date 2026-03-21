"""URL バリデーション・SSRF 対策ユーティリティ.

Web インジェスター・Scrapy サイト取り込み等で共通利用する
URL 検証・SSRF チェック関数を提供する。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# SSRF 対策: ブロック対象ホスト名
_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})

# SSRF 対策: ブロック対象ネットワーク
_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
]


def validate_url(url: str) -> str:
    """URL のバリデーションを行う.

    Returns:
        フラグメントを除去した URL

    Raises:
        ValueError: 不正な URL の場合
    """
    if not url or not url.strip():
        raise ValueError("URL が空です")

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"無効な URL スキームです: {parsed.scheme}")

    if not parsed.hostname:
        raise ValueError(f"URL にホスト名がありません: {url}")

    # フラグメント除去
    if parsed.fragment:
        url = url.split("#")[0]

    return url


def check_ssrf(url: str) -> None:
    """SSRF 対策: プライベート IP・ローカルホストへのリクエストを拒否する.

    Raises:
        ValueError: SSRF の疑いがある場合
    """
    parsed = urlparse(url)
    hostname = parsed.hostname

    if not hostname:
        raise ValueError(f"URL にホスト名がありません: {url}")

    # ホスト名文字列マッチ
    if hostname.lower() in _BLOCKED_HOSTNAMES:
        raise ValueError(
            f"プライベートホストへのアクセスは拒否されています: {hostname}"
        )

    # DNS 解決 + IP 検証
    try:
        addrinfos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"DNS 解決に失敗しました: {hostname}") from e

    for addrinfo in addrinfos:
        addr = addrinfo[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as e:
            # パース不能なアドレス表現は fail-closed として拒否する
            raise ValueError(
                f"DNS 解決結果に無効な IP アドレスが含まれています: {hostname} ({addr})"
            ) from e

        # IPv4-mapped IPv6 (::ffff:127.0.0.1 など) は対応する IPv4 として評価する
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped

        for network in _BLOCKED_NETWORKS:
            if ip in network:
                raise ValueError(
                    f"プライベート IP へのアクセスは拒否されています: "
                    f"{hostname} ({addr})"
                )

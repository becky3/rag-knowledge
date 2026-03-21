"""Scrapy Downloader Middleware for SSRF protection.

仕様: docs/specs/site-ingest.md

各リクエストの送信前に DNS 解決 → IP 検証を実行し、
プライベート IP へのアクセスを拒否する。
DNS リバインディング攻撃に対応する。
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from scrapy.exceptions import IgnoreRequest
from scrapy.http import Request, Response
from twisted.internet import defer, threads
from twisted.python.failure import Failure

from rag.utils.url import check_ssrf

logger = logging.getLogger(__name__)


class SsrfMiddleware:
    """per-request SSRF チェック Downloader Middleware.

    各リクエストの送信前にホスト名を DNS 解決し、
    解決結果がプライベート IP に該当する場合は IgnoreRequest で拒否する。

    DNS 解決は同期 I/O のため、deferToThread でスレッドプールに
    オフロードして Twisted reactor のブロッキングを回避する。
    """

    def process_request(
        self, request: Request, spider: object
    ) -> defer.Deferred[Response | None]:
        """リクエスト送信前に SSRF チェックを実行する.

        DNS 解決を含む check_ssrf をスレッドプールで実行し、
        Deferred を返すことで reactor をブロックしない。

        Returns:
            Deferred[None]: 検証通過。次の Middleware / Downloader に処理を渡す。

        Raises:
            IgnoreRequest: プライベート IP / ローカルホスト / DNS 解決失敗の場合
        """
        url = request.url
        parsed = urlparse(url)
        hostname = parsed.hostname

        if not hostname:
            logger.warning("SSRF Middleware: ホスト名なし、リクエストを拒否: %s", url)
            raise IgnoreRequest(f"SSRF check failed: no hostname in {url}")

        d: defer.Deferred[None] = threads.deferToThread(check_ssrf, url)  # type: ignore[no-untyped-call]
        d.addErrback(self._on_ssrf_error, url)
        return d

    @staticmethod
    def _on_ssrf_error(
        failure: Failure,
        url: str,
    ) -> None:
        """check_ssrf が ValueError を送出した場合に IgnoreRequest に変換する."""
        failure.trap(ValueError)  # type: ignore[no-untyped-call]
        logger.warning("SSRF Middleware: リクエストを拒否: %s (%s)", url, failure.value)
        raise IgnoreRequest(f"SSRF check failed: {failure.value}")

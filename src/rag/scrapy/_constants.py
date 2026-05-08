"""rag.scrapy パッケージ内で共有する Scrapy 非依存の定数モジュール.

spider.py（subprocess 内で実行される）から runner.py / bridge.py（main プロセスで実行される）へ
直接 import すると、main プロセスにも scrapy / Twisted がロードされる。これは subprocess 分離の
設計意図に反するため、Scrapy 非依存で共有可能な定数は本モジュールに集約する。
"""

from __future__ import annotations

# Web 系拡張子（HTML 系コンテンツとみなす拡張子のセット）
# spider.py の `.html` 付加判定（`SiteSpider._url_to_filename`）と
# runner.py の url_pattern 自動生成判定で共有する
WEB_EXTENSIONS: frozenset[str] = frozenset(
    {".html", ".htm", ".xhtml", ".shtml", ".php", ".asp", ".aspx", ".jsp"},
)

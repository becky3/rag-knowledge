"""Scrapy ベースのサイト一括取り込みパッケージ.

仕様: docs/specs/site-ingest.md

Scrapy を subprocess 方式で起動し、大規模サイトをクロールする。
Spider（汎用クローラー）と Runner（subprocess ラッパー）を提供する。
"""

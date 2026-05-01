"""Fake Scrapy Runner.

仕様: docs/specs/infrastructure/fake-mode.md
仕様: docs/specs/infrastructure/fake-adapters/scrapy.md

ScrapyRunner Protocol の Fake 実装。subprocess を起動せず、
fixture から JSONL + HTML 相当のファイル群を tmp に展開して CrawlResult を返す。

シナリオは ``SCENARIOS`` で定義された値のみ受け付ける。fixture URL には IANA 予約 TLD
``test.invalid`` を使用し、実 URL との混同を防ぐ。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from rag.scrapy.runner import CrawlResult

# 受付可能な fake シナリオ（網羅性管理用）
SCENARIOS: tuple[str, ...] = ("happy", "empty", "partial", "failure")


class FakeScrapyRunner:
    """ScrapyRunner Protocol の Fake 実装.

    subprocess を起動せず、fixture から事前生成済みの JSONL と HTML ファイル群を
    tmp ディレクトリに展開して ``CrawlResult`` を返す。

    Args:
        temp_dir: 展開先の一時ディレクトリ。Real と同じパス計算を使う
        fixture_dir: シナリオ別 fixture を含むルートディレクトリ。
            ``<fixture_dir>/<scenario>/{metadata.jsonl, html/...}`` の構造を期待する
        scenario: 採用するシナリオ名（``SCENARIOS`` のいずれか）
    """

    def __init__(
        self,
        *,
        temp_dir: str | Path,
        fixture_dir: Path,
        scenario: str = "happy",
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(
                f"未対応のシナリオ: {scenario!r}。"
                f"許容値: {SCENARIOS}"
            )
        self._temp_dir = Path(temp_dir)
        self._fixture_dir = fixture_dir
        self._scenario = scenario

    async def run(
        self,
        *,
        start_url: str = "",
        start_urls: list[str] | None = None,
        allowed_domains: str = "",
        url_pattern: str = "",
        max_pages: int | None = None,
        force: bool = False,
    ) -> CrawlResult:
        """Fake クロールを実行する.

        Real と同じディレクトリ構造（``<domain>/<crawl_key>/{html, metadata.jsonl}``）
        を tmp_dir に作成し、fixture の内容を展開する。
        """
        del allowed_domains, url_pattern, force  # Fake では使わない（呼び出し契約のため受け付け）

        urls = list(start_urls or ([start_url] if start_url else []))
        if not urls:
            raise ValueError("start_url または start_urls は必須です")

        # Real と整合する一時ディレクトリ構造を生成
        multi_url_mode = len(urls) >= 2
        if multi_url_mode:
            domain = "_multi_"
            key = _multi_url_key(urls)
        else:
            from urllib.parse import urlparse
            domain = urlparse(urls[0]).hostname or "unknown"
            key = _crawl_key(urls[0], "")

        crawl_dir = self._temp_dir / domain / key
        html_dir = crawl_dir / "html"
        jsonl_path = crawl_dir / "metadata.jsonl"
        html_dir.mkdir(parents=True, exist_ok=True)

        # failure シナリオは exit_code=1 で返却（jsonl は空のまま）
        if self._scenario == "failure":
            jsonl_path.write_text("", encoding="utf-8")
            return CrawlResult(
                exit_code=1,
                output_dir=html_dir,
                jsonl_path=jsonl_path,
                success=False,
                crawl_dir=crawl_dir,
                stderr_tail="[FAKE] failure scenario",
            )

        # happy / empty / partial: fixture からコピー
        scenario_dir = self._fixture_dir / self._scenario
        if not scenario_dir.exists():
            raise FileNotFoundError(
                f"シナリオ fixture が見つかりません: {scenario_dir}"
            )

        fixture_jsonl = scenario_dir / "metadata.jsonl"
        if fixture_jsonl.exists():
            jsonl_path.write_bytes(fixture_jsonl.read_bytes())
        else:
            jsonl_path.write_text("", encoding="utf-8")

        fixture_html_dir = scenario_dir / "html"
        if fixture_html_dir.exists():
            for src in fixture_html_dir.rglob("*"):
                if src.is_file():
                    rel = src.relative_to(fixture_html_dir)
                    dst = html_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

        return CrawlResult(
            exit_code=0,
            output_dir=html_dir,
            jsonl_path=jsonl_path,
            success=True,
            crawl_dir=crawl_dir,
            stderr_tail="",
        )


def _crawl_key(start_url: str, url_pattern: str) -> str:
    """Real と同じキー算出ロジック.

    runner.py の ``_crawl_key`` と一致させる。
    """
    raw = f"{start_url}\n{url_pattern}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _multi_url_key(urls: list[str]) -> str:
    """Real と同じ複数 URL キー算出ロジック."""
    raw = "\n".join(sorted(urls))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


__all__ = ["FakeScrapyRunner", "SCENARIOS"]

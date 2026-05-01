"""FakeScrapyRunner の単体テスト.

仕様: docs/specs/infrastructure/fake-adapters/scrapy.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.scrapy._fake import SCENARIOS, FakeScrapyRunner


def _fixture_dir() -> Path:
    project_root = Path(__file__).resolve().parent.parent
    return project_root / "src" / "rag" / "scrapy" / "_fake" / "data"


class TestFakeScrapyRunnerScenarios:
    def test_invalid_scenario_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="未対応のシナリオ"):
            FakeScrapyRunner(
                temp_dir=tmp_path,
                fixture_dir=_fixture_dir(),
                scenario="bogus",
            )

    def test_scenarios_tuple_is_complete(self) -> None:
        assert "happy" in SCENARIOS
        assert "empty" in SCENARIOS
        assert "partial" in SCENARIOS
        assert "failure" in SCENARIOS


@pytest.mark.asyncio
class TestFakeScrapyRunnerRun:
    async def test_happy_scenario_returns_success(self, tmp_path: Path) -> None:
        runner = FakeScrapyRunner(
            temp_dir=tmp_path, fixture_dir=_fixture_dir(), scenario="happy",
        )
        result = await runner.run(start_url="https://test.invalid/page1")
        assert result.success is True
        assert result.exit_code == 0
        assert result.jsonl_path.exists()
        assert result.jsonl_path.read_text(encoding="utf-8").strip() != ""
        assert any(result.output_dir.glob("*.html"))

    async def test_empty_scenario_returns_empty_jsonl(self, tmp_path: Path) -> None:
        runner = FakeScrapyRunner(
            temp_dir=tmp_path, fixture_dir=_fixture_dir(), scenario="empty",
        )
        result = await runner.run(start_url="https://test.invalid/page1")
        assert result.success is True
        assert result.jsonl_path.exists()
        assert result.jsonl_path.read_text(encoding="utf-8").strip() == ""

    async def test_failure_scenario_returns_error(self, tmp_path: Path) -> None:
        runner = FakeScrapyRunner(
            temp_dir=tmp_path, fixture_dir=_fixture_dir(), scenario="failure",
        )
        result = await runner.run(start_url="https://test.invalid/page1")
        assert result.success is False
        assert result.exit_code == 1
        assert "failure scenario" in result.stderr_tail

    async def test_multi_url_mode(self, tmp_path: Path) -> None:
        runner = FakeScrapyRunner(
            temp_dir=tmp_path, fixture_dir=_fixture_dir(), scenario="happy",
        )
        result = await runner.run(
            start_urls=[
                "https://test.invalid/a", "https://test.invalid/b",
            ],
        )
        assert result.success is True
        assert result.crawl_dir is not None
        assert "_multi_" in str(result.crawl_dir)

    async def test_no_urls_raises(self, tmp_path: Path) -> None:
        runner = FakeScrapyRunner(
            temp_dir=tmp_path, fixture_dir=_fixture_dir(), scenario="happy",
        )
        with pytest.raises(ValueError, match="start_url"):
            await runner.run()

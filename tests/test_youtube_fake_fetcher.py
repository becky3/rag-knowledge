"""YouTube Fake Fetcher 自体の動作検証.

仕様: docs/specs/infrastructure/fake-adapters/youtube.md

FakeYoutubeFetcher の各シナリオ・カスタムデータ注入が仕様通りに動作することと、
Fake fetcher の戻り値構造が実 YouTube レスポンスの参照サンプルと等価であることを検証する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rag.pipeline.ingesters._fake.youtube import SCENARIOS, FakeYoutubeFetcher


_DATA_DIR = (
    Path(__file__).parent.parent
    / "src" / "rag" / "pipeline" / "ingesters" / "_fake" / "youtube" / "data"
)
_REFERENCE_DIR = _DATA_DIR / "reference"


def _make_fake(**kwargs: Any) -> FakeYoutubeFetcher:
    return FakeYoutubeFetcher(fixture_dir=_DATA_DIR, **kwargs)


class TestScenarioRegistry:
    def test_all_scenarios_are_listed(self) -> None:
        """SCENARIOS タプルに仕様書記載のシナリオが含まれる."""
        expected = {
            "happy",
            "no_subtitle",
            "transcripts_disabled",
            "ip_blocked",
            "metadata_error",
            "duration_exceeded",
            "missing_channel_id",
            "empty_playlist",
            "playlist_expand_error",
        }
        assert set(SCENARIOS) == expected

    def test_unknown_scenario_raises(self) -> None:
        """未知のシナリオはコンストラクタで拒否される."""
        with pytest.raises(ValueError, match="未知のシナリオ"):
            FakeYoutubeFetcher(fixture_dir=_DATA_DIR, scenario="unknown")


class TestFetchMetadata:
    @pytest.mark.asyncio()
    async def test_happy_returns_valid_metadata(self) -> None:
        fetcher = _make_fake(scenario="happy")
        meta = await fetcher.fetch_metadata("TestVideo01", 30)
        assert meta["id"] == "TestVideo01"
        assert meta["channel_id"] == "UCtest123456789012345"
        assert meta["duration"] > 0

    @pytest.mark.asyncio()
    async def test_metadata_error_raises(self) -> None:
        fetcher = _make_fake(scenario="metadata_error")
        with pytest.raises(RuntimeError, match="Fake metadata error"):
            await fetcher.fetch_metadata("TestVideo01", 30)

    @pytest.mark.asyncio()
    async def test_duration_exceeded_returns_long_duration(self) -> None:
        fetcher = _make_fake(scenario="duration_exceeded")
        meta = await fetcher.fetch_metadata("TestVideo02", 30)
        assert meta["duration"] > 86400  # 24h 超

    @pytest.mark.asyncio()
    async def test_missing_channel_id_returns_null_channel(self) -> None:
        fetcher = _make_fake(scenario="missing_channel_id")
        meta = await fetcher.fetch_metadata("TestVideo03", 30)
        assert meta["channel_id"] is None

    @pytest.mark.asyncio()
    async def test_custom_metadata_overrides_scenario(self) -> None:
        custom = {"id": "X", "title": "Y", "channel_id": "UCtest", "duration": 10}
        fetcher = _make_fake(scenario="duration_exceeded", metadata=custom)
        meta = await fetcher.fetch_metadata("TestVideo01", 30)
        assert meta == custom


class TestFetchSubtitle:
    @pytest.mark.asyncio()
    async def test_happy_returns_snippets(self) -> None:
        fetcher = _make_fake(scenario="happy")
        snippets, language = await fetcher.fetch_subtitle("TestVideo01", ["ja", "en"])
        assert language == "ja"
        assert len(snippets) > 0
        for s in snippets:
            assert "start" in s
            assert "end" in s
            assert "text" in s

    @pytest.mark.asyncio()
    async def test_transcripts_disabled_raises(self) -> None:
        from youtube_transcript_api import TranscriptsDisabled  # safety:allowed

        fetcher = _make_fake(scenario="transcripts_disabled")
        with pytest.raises(TranscriptsDisabled):
            await fetcher.fetch_subtitle("TestVideo01", ["ja"])

    @pytest.mark.asyncio()
    async def test_no_subtitle_raises(self) -> None:
        from youtube_transcript_api import NoTranscriptFound  # safety:allowed

        fetcher = _make_fake(scenario="no_subtitle")
        with pytest.raises(NoTranscriptFound):
            await fetcher.fetch_subtitle("TestVideo01", ["ja"])

    @pytest.mark.asyncio()
    async def test_ip_blocked_raises(self) -> None:
        from youtube_transcript_api import RequestBlocked  # safety:allowed

        fetcher = _make_fake(scenario="ip_blocked")
        with pytest.raises(RequestBlocked):
            await fetcher.fetch_subtitle("TestVideo01", ["ja"])

    @pytest.mark.asyncio()
    async def test_custom_snippets_overrides_default(self) -> None:
        custom = [{"start": 0.0, "end": 1.0, "text": "custom"}]
        fetcher = _make_fake(snippets=custom, language="en")
        snippets, language = await fetcher.fetch_subtitle("TestVideo01", ["ja"])
        assert snippets == custom
        assert language == "en"


class TestTranscribeAudio:
    @pytest.mark.asyncio()
    async def test_happy_returns_whisper_snippets(self) -> None:
        fetcher = _make_fake(scenario="happy")
        snippets, language = await fetcher.transcribe_audio(
            "TestVideo01", ["ja"], "base", "cuda", 30,
        )
        assert language == "ja"
        assert len(snippets) > 0


class TestExpandPlaylist:
    @pytest.mark.asyncio()
    async def test_happy_returns_entries(self) -> None:
        fetcher = _make_fake(scenario="happy")
        entries = await fetcher.expand_playlist("https://example/playlist?list=PLtest", 100, 30)
        assert len(entries) > 0
        for e in entries:
            assert "id" in e
            assert "url" in e

    @pytest.mark.asyncio()
    async def test_empty_playlist(self) -> None:
        fetcher = _make_fake(scenario="empty_playlist")
        entries = await fetcher.expand_playlist("https://example/playlist?list=PLtest", 100, 30)
        assert entries == []

    @pytest.mark.asyncio()
    async def test_playlist_expand_error_raises(self) -> None:
        fetcher = _make_fake(scenario="playlist_expand_error")
        with pytest.raises(RuntimeError, match="Fake playlist expand error"):
            await fetcher.expand_playlist("https://example/playlist?list=PLtest", 100, 30)

    @pytest.mark.asyncio()
    async def test_max_videos_truncates(self) -> None:
        fetcher = _make_fake(scenario="happy")
        entries = await fetcher.expand_playlist("https://example/playlist?list=PLtest", 2, 30)
        assert len(entries) == 2

    @pytest.mark.asyncio()
    async def test_custom_playlist_entries(self) -> None:
        custom = [{"id": "A", "url": "A"}, {"id": "B", "url": "B"}]
        fetcher = _make_fake(playlist_entries=custom)
        entries = await fetcher.expand_playlist("https://example/playlist?list=PLtest", 100, 30)
        assert entries == custom


class TestFakeFixtureDirRequired:
    @pytest.mark.asyncio()
    async def test_missing_fixture_file_raises(self, tmp_path: Path) -> None:
        """fixture ディレクトリに必要な JSON が無いと FileNotFoundError."""
        fetcher = FakeYoutubeFetcher(fixture_dir=tmp_path, scenario="happy")
        with pytest.raises(FileNotFoundError, match="Fake fixture が見つかりません"):
            await fetcher.fetch_metadata("TestVideo01", 30)

    def test_missing_fixture_dir_raises_at_init(self, tmp_path: Path) -> None:
        """fixture ディレクトリ自体が存在しない場合、__init__ で fail-fast."""
        non_existent = tmp_path / "does_not_exist"
        with pytest.raises(
            FileNotFoundError, match="Fake fixture ディレクトリが見つかりません"
        ):
            FakeYoutubeFetcher(fixture_dir=non_existent)


class TestContractWithReference:
    """実 YouTube レスポンス参照サンプルとの構造的等価性検証.

    キーの存在と型を比較対象とする（fake-adapters/youtube.md の契約検証スコープに準拠）。
    値の実態（具体的な ID 文字列等）は検証対象外。
    """

    @pytest.mark.asyncio()
    async def test_metadata_keys_subset_of_reference(self) -> None:
        """fake metadata のキーが参照サンプルに含まれる（fake は実より少ない情報量で OK）."""
        ref = json.loads((_REFERENCE_DIR / "metadata_real_sample.json").read_text(encoding="utf-8"))
        ref_keys = set(ref.keys()) - {"_comment"}

        fetcher = _make_fake(scenario="happy")
        meta = await fetcher.fetch_metadata("TestVideo01", 30)
        fake_keys = set(meta.keys())

        # fake_keys が ref_keys のサブセットになっていることを確認（fake が実存在しないキーを返さない）
        unexpected = fake_keys - ref_keys
        assert not unexpected, (
            f"Fake fetcher が参照サンプルにないキーを返しています: {unexpected}。"
            f"参照サンプルを更新するか fake データから該当キーを削除してください"
        )

    @pytest.mark.asyncio()
    async def test_metadata_value_types_match_reference(self) -> None:
        """fake metadata の各キーの値の型が参照サンプルと一致する（具体値は検証しない）."""
        ref = json.loads((_REFERENCE_DIR / "metadata_real_sample.json").read_text(encoding="utf-8"))
        ref_filtered = {k: v for k, v in ref.items() if k != "_comment"}

        fetcher = _make_fake(scenario="happy")
        meta = await fetcher.fetch_metadata("TestVideo01", 30)

        # fake が返すキーごとに参照サンプルの型と一致することを確認
        type_mismatches: list[str] = []
        for key, fake_value in meta.items():
            if key not in ref_filtered:
                continue
            ref_value = ref_filtered[key]
            # None は許容（参照サンプルが None 値を持ち得るが fake は具体値を返すケースなど）
            if fake_value is None or ref_value is None:
                continue
            if type(fake_value) is not type(ref_value):
                type_mismatches.append(
                    f"{key}: fake={type(fake_value).__name__} ref={type(ref_value).__name__}"
                )
        assert not type_mismatches, (
            "Fake fetcher の値の型が参照サンプルと不一致: " + ", ".join(type_mismatches)
        )

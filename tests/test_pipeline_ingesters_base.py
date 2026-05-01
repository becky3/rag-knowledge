"""BaseIngester 抽象基底のテスト.

仕様: docs/specs/architecture.md §3.1, §3.4
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rag.pipeline.ingesters.aozora import AozoraIngester
from rag.pipeline.ingesters.base import BaseIngester
from rag.pipeline.ingesters.bluesky import BlueskyIngester
from rag.pipeline.ingesters.journal import JournalIngester
from rag.pipeline.ingesters.local import LocalIngester
from rag.pipeline.ingesters.web import WebIngester
from rag.pipeline.ingesters.youtube import YoutubeIngester
from rag.pipeline.ingesters.zenn import ZennIngester
from rag.store.source_store import SourceStore


@pytest.fixture()
def source_store(tmp_path: Path) -> SourceStore:
    """テスト用 SourceStore."""
    store = SourceStore(tmp_path / "source_store")
    store.initialize()
    return store


class TestBaseIngester:
    """BaseIngester 抽象基底の構造確認."""

    def test_base_ingester_can_be_instantiated_directly(self) -> None:
        """BaseIngester は @abstractmethod を持たないため直接インスタンス化可能.

        ABC として宣言しているのは将来の共通エントリポイント追加余地のためで、
        現時点では具象クラスを介さず instance 化できる現状を契約として固定する
        ものではない。派生クラスからの利用を強制する方針は docstring + spec
        （architecture.md §3.4）で表現する。
        """
        instance = BaseIngester(MagicMock())  # type: ignore[abstract]
        assert instance.source_store is not None

    def test_source_store_is_accessible(self) -> None:
        """source_store プロパティ経由で配置先が取得できる."""

        class _DummyIngester(BaseIngester):
            pass

        store = MagicMock()
        ingester = _DummyIngester(store)
        assert ingester.source_store is store


class TestIngesterFamilyMembership:
    """7 Ingester すべてが BaseIngester のサブクラスであること."""

    @pytest.mark.parametrize("cls", [
        AozoraIngester,
        BlueskyIngester,
        JournalIngester,
        LocalIngester,
        WebIngester,
        YoutubeIngester,
        ZennIngester,
    ])
    def test_class_inherits_base_ingester(self, cls: type) -> None:
        assert issubclass(cls, BaseIngester)

    def test_journal_ingester_instance_is_base_ingester(
        self, source_store: SourceStore,
    ) -> None:
        """インスタンスが isinstance で BaseIngester と判定されること.

        コンストラクタが最も単純な JournalIngester で代表確認する
        (他 Ingester は固有の依存を多く受け取るため省略)。
        """
        ingester = JournalIngester(source_store)
        assert isinstance(ingester, BaseIngester)
        assert ingester.source_store is source_store

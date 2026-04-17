"""IngestResult の拡張フィールド・summary() のテスト.

仕様: docs/specs/ingesters/common.md

テスト方針:
- 新規フィールド（partial_failures / partial_failure_details / aborted / abort_reason）の
  デフォルト値・代入が正しく機能すること
- error_details / partial_failure_details は dict のリストであること
- summary() が新フィールドを適切に反映すること
  - partial_failures > 0 で「部分失敗: N件」を表示
  - aborted=True で冒頭に「【処理中断: {reason}】」を表示
  - error_details / partial_failure_details の各 dict は
    `{target} [{category}]` 固定フォーマットで出力される
    （status / url / message 等の詳細は dict 本体に残す方針）
"""

from __future__ import annotations

from rag.pipeline.ingesters._common import IngestResult


def test_default_values() -> None:
    """拡張フィールドのデフォルト値が仕様通りであること."""
    result = IngestResult()
    assert result.placed == 0
    assert result.skipped == 0
    assert result.overwritten == 0
    assert result.errors == 0
    assert result.error_details == []
    assert result.partial_failures == 0
    assert result.partial_failure_details == []
    assert result.aborted is False
    assert result.abort_reason is None


def test_error_details_accepts_dict() -> None:
    """error_details に dict を追加できること."""
    result = IngestResult()
    result.error_details.append(
        {
            "category": "placement",
            "target": "bluesky/did/2025/01/abc.json",
            "message": "disk full",
        },
    )
    assert result.error_details[0]["category"] == "placement"
    assert result.error_details[0]["target"] == "bluesky/did/2025/01/abc.json"


def test_partial_failure_details_accepts_dict() -> None:
    """partial_failure_details に dict を追加できること."""
    result = IngestResult()
    result.partial_failure_details.append(
        {
            "category": "media_download",
            "target": "https://cdn.example.com/image.webp",
            "status": 404,
            "url": "https://cdn.example.com/image.webp",
        },
    )
    assert result.partial_failure_details[0]["status"] == 404


def test_summary_without_extensions() -> None:
    """拡張フィールドを使わない場合、従来通りの summary が得られること."""
    result = IngestResult(placed=3, skipped=1)
    text = result.summary()
    assert "完了: 3件配置" in text
    assert "スキップ: 1件" in text
    assert "部分失敗" not in text
    assert "処理中断" not in text


def test_summary_with_partial_failures() -> None:
    """partial_failures > 0 のとき「部分失敗: N件」と target [category] が含まれること."""
    result = IngestResult(placed=5, partial_failures=2)
    result.partial_failure_details.append(
        {
            "category": "media_download",
            "target": "https://cdn.example.com/a.webp",
            "status": 503,
        },
    )
    result.partial_failure_details.append(
        {
            "category": "media_download",
            "target": "https://cdn.example.com/b.m3u8",
            "message": "variant not found",
        },
    )
    text = result.summary()
    assert "部分失敗: 2件" in text
    assert "https://cdn.example.com/a.webp [media_download]" in text
    assert "https://cdn.example.com/b.m3u8 [media_download]" in text
    # status / message 等の詳細は summary には出さず dict 本体に残す方針
    assert "status=503" not in text
    assert "variant not found" not in text


def test_summary_with_aborted() -> None:
    """aborted=True のとき冒頭に中断メッセージが含まれること."""
    result = IngestResult(
        placed=2,
        errors=5,
        aborted=True,
        abort_reason="consecutive failures",
    )
    text = result.summary()
    assert text.startswith("【処理中断: consecutive failures】")
    assert "完了: 2件配置" in text
    assert "エラー: 5件" in text


def test_summary_with_error_details_dict() -> None:
    """error_details の dict が `{target} [{category}]` 形式で展開されること."""
    result = IngestResult(placed=0, errors=1)
    result.error_details.append(
        {
            "category": "metadata_fetch",
            "target": "slug-xxx",
            "status": 500,
            "message": "internal server error",
        },
    )
    text = result.summary()
    assert "slug-xxx [metadata_fetch]" in text
    # 運用者が一覧するための簡易表示のため詳細は含めない
    assert "status=500" not in text
    assert "internal server error" not in text


def test_summary_context_still_works() -> None:
    """context 引数が従来通り機能すること."""
    result = IngestResult(placed=1)
    text = result.summary(context="bluesky:alice.bsky.social")
    assert "（bluesky:alice.bsky.social）" in text

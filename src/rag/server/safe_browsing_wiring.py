"""SafeBrowsingClient のプロセス内シングルトン管理.

仕様: docs/specs/rag-knowledge.md「server 構造」
"""

from __future__ import annotations

from ..config import get_settings
from ..safe_browsing import SafeBrowsingClient, create_safe_browsing_client

_safe_browsing_client_cache: SafeBrowsingClient | None = None
_safe_browsing_client_initialized = False


def _reset_safe_browsing_client() -> None:
    """SafeBrowsingClient のキャッシュをリセットする（テスト用）."""
    global _safe_browsing_client_cache, _safe_browsing_client_initialized
    _safe_browsing_client_cache = None
    _safe_browsing_client_initialized = False


def _get_safe_browsing_client() -> SafeBrowsingClient | None:
    """SafeBrowsingClient を取得する（プロセス内キャッシュ）.

    Returns:
        SafeBrowsingClient または None（無効時）

    Raises:
        SafeBrowsingConfigError: API キー未登録・空・keyring アクセス失敗時
    """
    global _safe_browsing_client_cache, _safe_browsing_client_initialized
    if _safe_browsing_client_initialized:
        return _safe_browsing_client_cache
    # SafeBrowsingConfigError 時は initialized を True にしない
    # （設定修正まで毎回エラー送出）

    settings = get_settings()
    _safe_browsing_client_cache = create_safe_browsing_client(settings)
    _safe_browsing_client_initialized = True
    return _safe_browsing_client_cache

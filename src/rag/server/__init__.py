"""RAG MCP サーバー パッケージ.

仕様: docs/specs/rag-knowledge.md「server 構造」「MCP 薄層アダプターパターン」

10 種類の architectural concern をディレクトリ構造に分割した薄層アダプター実装。
各 concern の責務はパス構造として SSoT 化されている:

- bootstrap.py            : プロセス起動シーケンス・ログファイル handler の attach
- transport.py            : HTTP モードのバインドアドレス検証・API キー登録確認
- http_auth.py            : HTTP モードの API キー認証 middleware
- logging_setup.py        : ログ値サニタイズ・CLI 子プロセス出力をログ handler に流す処理
- cli_subprocess.py       : CLI サブプロセス起動・JSON Lines パース・共有フォーマッター
- fake_labels.py          : Fake モード状態の表示ラベル生成・source ↔ Fake source マッピング定数
- safe_browsing_wiring.py : SafeBrowsingClient のプロセス内シングルトン管理
- tools/                  : MCP tool 定義（@mcp.tool() で装飾。各ファイルはツール本体 + 専用フォーマッター + 専用 validation を同居）
- upload/                 : HTTP custom_route によるアップロード API
"""

from __future__ import annotations

import logging
import os

# ChromaDB テレメトリを無効化（import 前に設定する必要がある）
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

logger = logging.getLogger("rag.server")

# mcp インスタンスは _mcp.py に切り出している（循環 import 回避のため）。
# 詳細は _mcp.py の docstring を参照。
from ._mcp import mcp  # noqa: E402, F401

# tool / upload サブモジュールの import により @mcp.tool() / @mcp.custom_route() が
# 発火し、mcp インスタンスに各エンドポイントが登録される。
# 順序は依存関係上の制約なし（各 tool は独立）。
from .tools import (  # noqa: E402, F401
    delete,
    ingest_aozora,
    ingest_bluesky,
    ingest_local,
    ingest_site,
    ingest_youtube,
    ingest_zenn,
    listing,
    rebuild,
    search,
)
from .upload import document, journal  # noqa: E402, F401

# --- 互換 re-export（読み取り専用） -----------------------------------------
# テスト側が `import_module("rag.server").X` 経由で参照する名前を re-export する。
# patch するには各 SSoT モジュール（cli_subprocess / tools.* / upload.* 等）の
# パスを使うこと。本層の re-export を patch しても呼び出し側には反映されない
# （呼び出し側は `from .. import cli_subprocess` の dotted access で参照するため）。

# cli_subprocess
from .cli_subprocess import (  # noqa: E402, F401
    CLISubprocessError,
    _format_cli_ingest_result,
    _format_full_rebuild_summary,
    _format_rebuild_summary,
    _run_cli_subprocess,
    _SEGFAULT_EXIT_CODES,
)
# safe_browsing_wiring
from .safe_browsing_wiring import _get_safe_browsing_client  # noqa: E402, F401
# upload helpers
from .upload._helpers import _decode_form_value  # noqa: E402, F401
# transport（HTTP モード起動時の検証）
from .transport import _check_api_key_registered, _validate_bind_address  # noqa: E402, F401
# config（テスト patch 対象）
from ..config import get_settings  # noqa: E402, F401

# MCP tool 関数の re-export
from .tools.search import rag_get_document, rag_search  # noqa: E402, F401
from .tools.ingest_zenn import rag_add_zenn, rag_crawl_zenn  # noqa: E402, F401
from .tools.ingest_bluesky import rag_add_bluesky, rag_crawl_bluesky  # noqa: E402, F401
from .tools.ingest_youtube import rag_add_youtube, rag_crawl_youtube  # noqa: E402, F401
from .tools.ingest_local import (  # noqa: E402, F401
    rag_add_document,
    rag_add_journal,
    rag_crawl_documents,
)
from .tools.ingest_aozora import (  # noqa: E402, F401
    rag_add_aozora,
    rag_crawl_aozora,
    rag_search_aozora,
    rag_update_aozora_catalog,
)
from .tools.ingest_site import rag_site_ingest  # noqa: E402, F401
from .tools.delete import rag_delete  # noqa: E402, F401
from .tools.rebuild import rag_rebuild  # noqa: E402, F401
from .tools.listing import (  # noqa: E402, F401
    rag_list_by_date_range,
    rag_list_recent,
    rag_stats,
)


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    """`from rag.server import _configure_and_run` 等の遅延 import を許可する.

    bootstrap.py を package import 時に読み込むと chromadb や atexit を含む
    重い依存が早期にロードされてしまうため、`_configure_and_run` および
    `_attach_log_file_handler` は遅延 import とする。
    """
    if name in {"_configure_and_run", "_attach_log_file_handler"}:
        from . import bootstrap

        return getattr(bootstrap, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

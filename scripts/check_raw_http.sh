#!/usr/bin/env bash
# scripts/check_raw_http.sh
#
# src/ 配下で ConstrainedClient を経由しない直接 HTTP クライアント利用を検出する。
# 許可リスト:
#   - src/rag/safety/constrained_client.py (中間ライブラリ自身)
#   - "# safety:allowed" コメントが付与された行
#
# 終了コード: 0 = 違反なし, 1 = 違反あり

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="$PROJECT_ROOT/src"

if [ ! -d "$SRC_DIR" ]; then
  echo "ERROR: src/ directory not found at $SRC_DIR" >&2
  exit 1
fi

# 検出パターン (拡張正規表現)
# モジュール修飾形と from-import 形の両方を検出する
PATTERNS=(
  'aiohttp\.ClientSession'
  'from\s+aiohttp\s+import\s+.*ClientSession'
  'httpx\.(Client|AsyncClient)'
  'from\s+httpx\s+import\s+.*(Client|AsyncClient)'
  'requests\.(get|post|put|delete|patch|head|options|session|Session)'
  'from\s+requests\s+import'
  'urllib\.request'
  'from\s+urllib\.request\s+import'
)

# パターンを | で結合
COMBINED_PATTERN=""
for p in "${PATTERNS[@]}"; do
  if [ -z "$COMBINED_PATTERN" ]; then
    COMBINED_PATTERN="$p"
  else
    COMBINED_PATTERN="$COMBINED_PATTERN|$p"
  fi
done

# 検出実行: 許可リスト外のマッチを抽出
violations=$(
  grep -rn -E "$COMBINED_PATTERN" "$SRC_DIR" \
    --include="*.py" \
    | grep -v "src/rag/safety/constrained_client\.py" \
    | grep -v "#\s*safety:allowed\b" \
  || true
)

if [ -n "$violations" ]; then
  echo "ERROR: Direct HTTP client usage detected (bypass ConstrainedClient)" >&2
  echo "" >&2
  echo "The following lines use HTTP clients directly instead of ConstrainedClient:" >&2
  echo "$violations" >&2
  echo "" >&2
  echo "To fix:" >&2
  echo "  1. Use ConstrainedClient from src/rag/safety/constrained_client.py" >&2
  echo "  2. Or add '# safety:allowed' comment if explicitly permitted" >&2
  exit 1
fi

echo "OK: No raw HTTP client usage detected outside allowlist"
exit 0

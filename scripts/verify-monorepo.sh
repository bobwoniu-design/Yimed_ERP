#!/usr/bin/env bash
set -Eeuo pipefail

YIMED_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
YIMED_REPO_ROOT="$(cd -- "$YIMED_SCRIPT_DIR/.." && pwd)"
YIMED_REQUIRED_APPS=(frappe erpnext channel_erp yimed_ecommerce)
YIMED_FAILED=0

for YIMED_APP in "${YIMED_REQUIRED_APPS[@]}"; do
  if [[ ! -f "$YIMED_REPO_ROOT/apps/$YIMED_APP/pyproject.toml" ]]; then
    printf '缺少应用：%s\n' "$YIMED_APP" >&2
    YIMED_FAILED=1
  fi
done

while IFS= read -r YIMED_FORBIDDEN; do
  printf '发现不应提交的目录：%s\n' "$YIMED_FORBIDDEN" >&2
  YIMED_FAILED=1
done < <(find "$YIMED_REPO_ROOT/apps" -type d \( -name .git -o -name node_modules \) -print)

while IFS= read -r YIMED_LARGE_FILE; do
  printf '发现超过 95MB 的文件：%s\n' "$YIMED_LARGE_FILE" >&2
  YIMED_FAILED=1
done < <(find "$YIMED_REPO_ROOT/apps" -type f -size +95M -print)

if (( YIMED_FAILED != 0 )); then
  exit 1
fi

printf 'Monorepo 结构检查通过。\n'

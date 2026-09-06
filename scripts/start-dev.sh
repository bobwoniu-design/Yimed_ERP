#!/usr/bin/env bash
set -Eeuo pipefail

YIMED_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
YIMED_REPO_ROOT="$(cd -- "$YIMED_SCRIPT_DIR/.." && pwd)"
YIMED_BENCH_DIR="${1:-$HOME/frappe-bench}"
[[ -d "$YIMED_BENCH_DIR/sites" ]] || {
  printf '错误：不是有效的 Bench 目录：%s\n' "$YIMED_BENCH_DIR" >&2
  exit 1
}

YIMED_BENCH_BIN="$(command -v bench || true)"
if [[ -z "$YIMED_BENCH_BIN" && -x "$HOME/.local/bin/bench" ]]; then
  YIMED_BENCH_BIN="$HOME/.local/bin/bench"
fi
[[ -x "$YIMED_BENCH_BIN" ]] || {
  printf '错误：找不到 bench 命令。\n' >&2
  exit 1
}

cd "$YIMED_BENCH_DIR"
if [[ -f "$YIMED_REPO_ROOT/sites/assets/assets.json" ]]; then
  mkdir -p "$YIMED_BENCH_DIR/sites/assets"
  ln -sf "$YIMED_REPO_ROOT/sites/assets/assets.json" \
    "$YIMED_BENCH_DIR/sites/assets/assets.json"
fi
if [[ -f "$YIMED_REPO_ROOT/sites/assets/assets-rtl.json" ]]; then
  mkdir -p "$YIMED_BENCH_DIR/sites/assets"
  ln -sf "$YIMED_REPO_ROOT/sites/assets/assets-rtl.json" \
    "$YIMED_BENCH_DIR/sites/assets/assets-rtl.json"
fi
if [[ -d "$YIMED_REPO_ROOT/sites/assets/frappe/dist" ]]; then
  ln -sfn "$YIMED_REPO_ROOT/sites/assets/frappe/dist" \
    "$YIMED_REPO_ROOT/apps/frappe/frappe/public/dist"
fi
if [[ -d "$YIMED_REPO_ROOT/sites/assets/erpnext/dist" ]]; then
  ln -sfn "$YIMED_REPO_ROOT/sites/assets/erpnext/dist" \
    "$YIMED_REPO_ROOT/apps/erpnext/erpnext/public/dist"
fi
export PATH="$(dirname "$YIMED_BENCH_BIN"):$YIMED_BENCH_DIR/env/bin:$PATH"
exec "$YIMED_BENCH_BIN" start --man "$YIMED_BENCH_DIR/env/bin/honcho"

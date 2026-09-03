#!/usr/bin/env bash
set -Eeuo pipefail

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
exec "$YIMED_BENCH_BIN" start

#!/usr/bin/env bash
set -Eeuo pipefail

YIMED_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
YIMED_REPO_ROOT="$(cd -- "$YIMED_SCRIPT_DIR/.." && pwd)"
YIMED_BENCH_DIR="${1:-$HOME/frappe-bench}"
YIMED_BENCH_VERSION="5.31.0"
YIMED_PYTHON_VERSION="3.14"
YIMED_REQUIRED_APPS=(frappe erpnext channel_erp yimed_ecommerce)

YIMED_TEMP_DIR=""
cleanup() {
  if [[ -n "$YIMED_TEMP_DIR" && -d "$YIMED_TEMP_DIR" ]]; then
    rm -rf -- "$YIMED_TEMP_DIR"
  fi
}
trap cleanup EXIT

fail() {
  printf '错误：%s\n' "$1" >&2
  exit 1
}

if [[ "$(uname -s)" != "Linux" ]]; then
  fail "此脚本应在 Windows 的 WSL2 Ubuntu 中运行。"
fi

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  fail "请使用普通 WSL 用户运行脚本，脚本会在需要时调用 sudo。"
fi

for YIMED_APP in "${YIMED_REQUIRED_APPS[@]}"; do
  [[ -f "$YIMED_REPO_ROOT/apps/$YIMED_APP/pyproject.toml" ]] \
    || fail "缺少 apps/$YIMED_APP/pyproject.toml，请确认仓库克隆完整。"
done

if [[ -e "$YIMED_BENCH_DIR" ]]; then
  fail "目标目录已存在：$YIMED_BENCH_DIR。请传入一个不存在的新目录，避免覆盖已有环境。"
fi

if ! command -v apt-get >/dev/null 2>&1; then
  fail "当前系统不是受支持的 Ubuntu/Debian 环境。"
fi

printf '安装 WSL2 系统依赖……\n'
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  build-essential ca-certificates curl git jq pkg-config rsync \
  libffi-dev libssl-dev libmariadb-dev libmariadb-dev-compat \
  libjpeg-dev zlib1g-dev liblcms2-dev libwebp-dev \
  mariadb-client mariadb-server redis-server \
  xvfb libfontconfig1

if [[ "$(apt-cache policy wkhtmltopdf | awk '/Candidate:/ {print $2}')" != "(none)" ]]; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y wkhtmltopdf
else
  printf '提示：当前 Ubuntu 软件源没有 wkhtmltopdf，已跳过。需要旧式 PDF 打印时可后续单独安装 wkhtmltox。\n'
fi

YIMED_MARIADB_CONFIG="$(mktemp)"
cat > "$YIMED_MARIADB_CONFIG" <<'EOF'
[server]
character-set-server = utf8mb4
collation-server = utf8mb4_unicode_ci
skip-character-set-client-handshake

[mysql]
default-character-set = utf8mb4
EOF
sudo install -m 0644 "$YIMED_MARIADB_CONFIG" /etc/mysql/mariadb.conf.d/99-yimed-frappe.cnf
rm -f -- "$YIMED_MARIADB_CONFIG"

if command -v systemctl >/dev/null 2>&1 && systemctl is-system-running >/dev/null 2>&1; then
  sudo systemctl enable --now mariadb redis-server
  sudo systemctl restart mariadb redis-server
else
  sudo service mariadb restart
  sudo service redis-server restart
fi

YIMED_UV_BIN="$(command -v uv || true)"
if [[ -z "$YIMED_UV_BIN" && -x "$HOME/.local/bin/uv" ]]; then
  YIMED_UV_BIN="$HOME/.local/bin/uv"
fi

if [[ -z "$YIMED_UV_BIN" ]]; then
  printf '安装 uv 和 Python %s……\n' "$YIMED_PYTHON_VERSION"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
YIMED_UV_BIN="$(command -v uv || true)"
if [[ -z "$YIMED_UV_BIN" && -x "$HOME/.local/bin/uv" ]]; then
  YIMED_UV_BIN="$HOME/.local/bin/uv"
fi
[[ -x "$YIMED_UV_BIN" ]] || fail "uv 安装失败。"
export PATH="$(dirname "$YIMED_UV_BIN"):$PATH"
"$YIMED_UV_BIN" python install "$YIMED_PYTHON_VERSION"
YIMED_PYTHON_BIN="$("$YIMED_UV_BIN" python find "$YIMED_PYTHON_VERSION")"

YIMED_NODE_MAJOR=0
if command -v node >/dev/null 2>&1; then
  YIMED_NODE_MAJOR="$(node -p "Number(process.versions.node.split('.')[0])")"
fi
if (( YIMED_NODE_MAJOR < 24 )); then
  printf '安装 Node.js 24……\n'
  curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi
if ! command -v yarn >/dev/null 2>&1; then
  sudo npm install --global yarn@1.22.22
fi

printf '安装 Bench %s……\n' "$YIMED_BENCH_VERSION"
"$YIMED_UV_BIN" tool install --force "frappe-bench==$YIMED_BENCH_VERSION" \
  --python "$YIMED_PYTHON_BIN"
YIMED_BENCH_BIN="$(command -v bench || true)"
if [[ -z "$YIMED_BENCH_BIN" && -x "$HOME/.local/bin/bench" ]]; then
  YIMED_BENCH_BIN="$HOME/.local/bin/bench"
fi
[[ -x "$YIMED_BENCH_BIN" ]] || fail "Bench 安装失败。"

# Bench expects every app to be an individual Git repository. Build temporary
# local snapshot repositories, install from them, then link the runtime back to
# this Monorepo so Windows-side edits remain tracked by the parent repository.
YIMED_TEMP_DIR="$(mktemp -d)"
for YIMED_APP in "${YIMED_REQUIRED_APPS[@]}"; do
  mkdir -p "$YIMED_TEMP_DIR/$YIMED_APP"
  rsync -a \
    --exclude='.git/' \
    --exclude='node_modules/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "$YIMED_REPO_ROOT/apps/$YIMED_APP/" "$YIMED_TEMP_DIR/$YIMED_APP/"
  git -C "$YIMED_TEMP_DIR/$YIMED_APP" init --initial-branch snapshot --quiet
  git -C "$YIMED_TEMP_DIR/$YIMED_APP" add --all
  git -C "$YIMED_TEMP_DIR/$YIMED_APP" \
    -c user.name='Yimed Migration' \
    -c user.email='migration@local.invalid' \
    commit --message 'Monorepo source snapshot' --quiet
done

printf '创建 Bench：%s\n' "$YIMED_BENCH_DIR"
"$YIMED_BENCH_BIN" init \
  --frappe-path "$YIMED_TEMP_DIR/frappe" \
  --clone-without-update \
  --python "$YIMED_PYTHON_BIN" \
  --skip-assets \
  --dev \
  "$YIMED_BENCH_DIR"

cd "$YIMED_BENCH_DIR"
for YIMED_APP in erpnext channel_erp yimed_ecommerce; do
  "$YIMED_BENCH_BIN" get-app --branch snapshot --skip-assets \
    "$YIMED_TEMP_DIR/$YIMED_APP"
done

[[ -d "$YIMED_BENCH_DIR/apps" && -d "$YIMED_BENCH_DIR/sites" ]] \
  || fail "Bench 初始化结果不完整，已停止以避免误操作。"

for YIMED_APP in "${YIMED_REQUIRED_APPS[@]}"; do
  rm -rf -- "$YIMED_BENCH_DIR/apps/$YIMED_APP"
  ln -s "$YIMED_REPO_ROOT/apps/$YIMED_APP" "$YIMED_BENCH_DIR/apps/$YIMED_APP"
done

cd "$YIMED_BENCH_DIR"
"$YIMED_BENCH_BIN" setup requirements --dev
mkdir -p "$YIMED_REPO_ROOT/sites"
ln -sf "$YIMED_BENCH_DIR/sites/common_site_config.json" \
  "$YIMED_REPO_ROOT/sites/common_site_config.json"
(
  cd "$YIMED_REPO_ROOT/apps/frappe"
  yarn install --frozen-lockfile
)
(
  cd "$YIMED_REPO_ROOT/apps/erpnext"
  yarn install --frozen-lockfile
)
(
  cd "$YIMED_REPO_ROOT/apps/erpnext/banking"
  yarn install --frozen-lockfile
)
"$YIMED_BENCH_BIN" build
"$YIMED_BENCH_BIN" set-config -g webserver_port 8000
"$YIMED_BENCH_BIN" set-config -g socketio_port 9000
"$YIMED_BENCH_BIN" set-config -g serve_default_site true

printf '\n环境安装完成。\n'
printf 'Bench 目录：%s\n' "$YIMED_BENCH_DIR"
printf '下一步恢复数据：%s/scripts/restore-site.sh /备份目录 %s\n' \
  "$YIMED_REPO_ROOT" "$YIMED_BENCH_DIR"

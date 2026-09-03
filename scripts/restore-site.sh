#!/usr/bin/env bash
set -Eeuo pipefail

YIMED_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
YIMED_REPO_ROOT="$(cd -- "$YIMED_SCRIPT_DIR/.." && pwd)"
YIMED_BACKUP_DIR="${1:-}"
YIMED_BENCH_DIR="${2:-$HOME/frappe-bench}"
YIMED_SITE_NAME="${3:-yimed.local}"

fail() {
  printf '错误：%s\n' "$1" >&2
  exit 1
}

if [[ -z "$YIMED_BACKUP_DIR" ]]; then
  printf '用法：%s /备份目录 [Bench目录] [站点名]\n' "$0" >&2
  exit 2
fi

[[ -d "$YIMED_BACKUP_DIR" ]] || fail "备份目录不存在：$YIMED_BACKUP_DIR"
[[ -d "$YIMED_BENCH_DIR/apps" && -d "$YIMED_BENCH_DIR/sites" ]] \
  || fail "不是有效的 Bench 目录：$YIMED_BENCH_DIR"
[[ ! -e "$YIMED_BENCH_DIR/sites/$YIMED_SITE_NAME" ]] \
  || fail "站点已存在：$YIMED_SITE_NAME。脚本不会覆盖已有站点。"

YIMED_BENCH_BIN="$(command -v bench || true)"
if [[ -z "$YIMED_BENCH_BIN" && -x "$HOME/.local/bin/bench" ]]; then
  YIMED_BENCH_BIN="$HOME/.local/bin/bench"
fi
[[ -x "$YIMED_BENCH_BIN" ]] || fail "找不到 bench 命令。"

mapfile -t YIMED_BACKUP_FILES < <(
  "$YIMED_BENCH_DIR/env/bin/python" - "$YIMED_BACKUP_DIR" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1]).expanduser().resolve()
files = [path for path in root.iterdir() if path.is_file()]

def newest(matches):
    return str(max(matches, key=lambda path: path.stat().st_mtime)) if matches else ""

database = newest([p for p in files if p.name.endswith((".sql", ".sql.gz"))])
private_files = newest([p for p in files if "private-files.tar" in p.name])
public_files = newest([
    p for p in files
    if "files.tar" in p.name and "private-files.tar" not in p.name
])
site_config = newest([p for p in files if "site_config_backup.json" in p.name])

print(database)
print(public_files)
print(private_files)
print(site_config)
PY
)

YIMED_DATABASE_FILE="${YIMED_BACKUP_FILES[0]:-}"
YIMED_PUBLIC_FILES="${YIMED_BACKUP_FILES[1]:-}"
YIMED_PRIVATE_FILES="${YIMED_BACKUP_FILES[2]:-}"
YIMED_CONFIG_BACKUP="${YIMED_BACKUP_FILES[3]:-}"

[[ -f "$YIMED_DATABASE_FILE" ]] || fail "没有找到 .sql 或 .sql.gz 数据库备份。"
[[ -f "$YIMED_PUBLIC_FILES" ]] || fail "没有找到公共附件备份。"
[[ -f "$YIMED_PRIVATE_FILES" ]] || fail "没有找到私有附件备份。"
[[ -f "$YIMED_CONFIG_BACKUP" ]] || fail "没有找到 site_config_backup.json。"

printf '将恢复以下文件：\n'
printf '数据库：%s\n' "$YIMED_DATABASE_FILE"
printf '公共附件：%s\n' "$YIMED_PUBLIC_FILES"
printf '私有附件：%s\n' "$YIMED_PRIVATE_FILES"
printf '加密配置：%s\n' "$YIMED_CONFIG_BACKUP"
printf '\n创建站点时 Bench 会安全询问 MariaDB root 密码和管理员密码。\n'

cd "$YIMED_BENCH_DIR"
"$YIMED_BENCH_BIN" new-site "$YIMED_SITE_NAME" --set-default

# Keep the newly-created Windows database connection settings. Only merge the
# portable settings required to decrypt existing Password fields and load the
# four apps restored from the database snapshot.
"$YIMED_BENCH_DIR/env/bin/python" - \
  "$YIMED_CONFIG_BACKUP" \
  "$YIMED_BENCH_DIR/sites/$YIMED_SITE_NAME/site_config.json" <<'PY'
import json
from pathlib import Path
import sys

backup_path = Path(sys.argv[1])
target_path = Path(sys.argv[2])
backup = json.loads(backup_path.read_text(encoding="utf-8"))
target = json.loads(target_path.read_text(encoding="utf-8"))

for key in ("encryption_key", "installed_apps", "allow_tests"):
    if key in backup:
        target[key] = backup[key]

target["developer_mode"] = 1
target["pause_scheduler"] = 1
target_path.write_text(
    json.dumps(target, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY
chmod 600 "$YIMED_BENCH_DIR/sites/$YIMED_SITE_NAME/site_config.json"

"$YIMED_BENCH_BIN" --site "$YIMED_SITE_NAME" restore "$YIMED_DATABASE_FILE" \
  --with-public-files "$YIMED_PUBLIC_FILES" \
  --with-private-files "$YIMED_PRIVATE_FILES"
"$YIMED_BENCH_BIN" --site "$YIMED_SITE_NAME" disable-scheduler
"$YIMED_BENCH_BIN" --site "$YIMED_SITE_NAME" migrate
"$YIMED_BENCH_BIN" --site "$YIMED_SITE_NAME" clear-cache
"$YIMED_BENCH_BIN" use "$YIMED_SITE_NAME"

printf '\n恢复完成，调度器仍处于关闭状态，避免新旧电脑同时执行平台同步。\n'
printf '检查应用：cd %s && bench --site %s list-apps\n' \
  "$YIMED_BENCH_DIR" "$YIMED_SITE_NAME"
printf '启动服务：%s/scripts/start-dev.sh %s\n' \
  "$YIMED_REPO_ROOT" "$YIMED_BENCH_DIR"
printf '验证完成后开启调度器：bench --site %s enable-scheduler\n' "$YIMED_SITE_NAME"

#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
	echo "Usage: $0 <site>" >&2
	exit 2
fi

site_name="$1"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_root="$(cd "${script_dir}/.." && pwd)"
bench_root="$(cd "${app_root}/../.." && pwd)"

python3 "${script_dir}/check_core_immutability.py"
node --check "${app_root}/channel_erp/channel_erp/page/content_center/content_center.js"
git -C "${app_root}" diff --check
cd "${bench_root}"
bench --site "${site_name}" execute channel_erp.upgrade_checks.run_upgrade_checks --kwargs '{"fail_on_error": 1}'

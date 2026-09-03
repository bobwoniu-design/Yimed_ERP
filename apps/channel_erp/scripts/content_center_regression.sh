#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
	echo "Usage: $0 <test-site>" >&2
	exit 2
fi

site_name="$1"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_root="$(cd "${script_dir}/.." && pwd)"
bench_root="$(cd "${app_root}/../.." && pwd)"

echo "Running content-center integration tests on ${site_name}."
echo "The target must be a disposable test/staging site with allow_tests enabled."
cd "${bench_root}"
bench --site "${site_name}" run-tests \
	--doctype "Channel Content Asset" \
	--test-category integration

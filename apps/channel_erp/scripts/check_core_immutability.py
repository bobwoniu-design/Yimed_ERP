#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
BENCH_ROOT = APP_ROOT.parent.parent
CORE_APPS = ("frappe", "erpnext")


def git(repo: Path, *args: str) -> str:
	return subprocess.check_output(
		["git", "-C", str(repo), *args],
		text=True,
		stderr=subprocess.STDOUT,
	).strip()


def inspect_repo(app: str) -> dict:
	repo = BENCH_ROOT / "apps" / app
	result = {"app": app, "path": str(repo), "exists": repo.is_dir(), "dirty": [], "error": None}
	if not result["exists"]:
		result["error"] = "repository is missing"
		return result
	try:
		result["revision"] = git(repo, "describe", "--tags", "--always")
		result["branch"] = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
		result["dirty"] = [
			line for line in git(repo, "status", "--porcelain", "--untracked-files=no").splitlines() if line
		]
	except subprocess.CalledProcessError as exc:
		result["error"] = exc.output.strip() or str(exc)
	return result


def main() -> int:
	parser = argparse.ArgumentParser(
		description="Fail when tracked files in Frappe or ERPNext have local modifications."
	)
	parser.add_argument("--json", action="store_true", help="print the report as JSON")
	parser.add_argument(
		"--report-only",
		action="store_true",
		help="report modifications without returning a failing exit status",
	)
	args = parser.parse_args()
	report = {
		"bench": str(BENCH_ROOT),
		"custom_app": str(APP_ROOT),
		"repositories": [inspect_repo(app) for app in CORE_APPS],
	}
	report["status"] = (
		"failed"
		if any(repo["error"] or repo["dirty"] for repo in report["repositories"])
		else "passed"
	)

	if args.json:
		print(json.dumps(report, ensure_ascii=False, indent=2))
	else:
		print(f"Bench: {BENCH_ROOT}")
		for repo in report["repositories"]:
			print(f"{repo['app']}: {repo.get('revision', 'unknown')} ({repo.get('branch', 'unknown')})")
			if repo["error"]:
				print(f"  ERROR: {repo['error']}")
			for change in repo["dirty"]:
				print(f"  MODIFIED: {change}")
		print(f"Result: {report['status'].upper()}")

	return 0 if args.report_only or report["status"] == "passed" else 1


if __name__ == "__main__":
	sys.exit(main())

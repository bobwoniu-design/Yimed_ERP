import hashlib
import hmac
import json
from collections import Counter

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_datetime, getdate, now_datetime, nowdate


ISSUE_DEFINITIONS = {
	"unassigned": ("High", _("未设置责任人"), "assign_responsible_user", 15),
	"no_department": ("Medium", _("未设置归属部门"), "set_department", 10),
	"no_expiry": ("Low", _("未设置到期日"), "set_expiry", 5),
	"no_tags": ("Medium", _("未设置标签"), "add_tags", 10),
	"no_relation": ("Medium", _("未关联业务记录"), "link_business_record", 10),
	"index_missing": ("High", _("全文索引缺失"), "rebuild_index", 15),
	"index_failed": ("High", _("全文索引失败"), "rebuild_index", 15),
	"index_unsupported": ("Medium", _("文件格式不支持全文索引"), "review_file_format", 8),
	"insight_missing": ("Low", _("当前版本缺少本地内容洞察"), "generate_insights", 5),
	"expired": ("High", _("文件已经过期"), "review_expiry", 20),
	"stale": ("Medium", _("文件长期未使用"), "review_stale_content", 10),
	"duplicate": ("High", _("存在内容完全相同的文件"), "review_duplicates", 15),
}


def _settings():
	settings = frappe.get_single("Channel Content Settings")
	return {
		"store_query": bool(cint(settings.store_search_query_text)),
		"retention_days": min(max(cint(settings.search_event_retention_days or 90), 1), 3650),
		"stale_days": min(max(cint(settings.content_stale_days or 180), 1), 3650),
		"responsible_governance": bool(cint(getattr(settings, "enable_responsible_governance", 0))),
	}


def _query_hash(query):
	secret = str(frappe.local.conf.get("encryption_key") or frappe.local.site or "channel-content-search").encode()
	return hmac.new(secret, query.casefold().encode("utf-8"), hashlib.sha256).hexdigest()


def record_search_event(company, query, result_count, duration_ms, filters=None):
	"""Best-effort analytics recording; failures must never break search responses."""
	try:
		settings = _settings()
		query = str(query or "")[:200]
		safe_filters = {
			key: value for key, value in (filters or {}).items()
			if key in {"folder", "view", "file_type", "department", "responsible_user", "modified_from", "modified_to"}
			and value not in (None, "")
		}
		frappe.get_doc({
			"doctype": "Channel Content Search Event", "company": company, "user": frappe.session.user,
			"query_hash": _query_hash(query), "query_text": query if settings["store_query"] else None,
			"result_count": max(cint(result_count), 0), "duration_ms": max(cint(duration_ms), 0),
			"filters_summary": json.dumps(safe_filters, ensure_ascii=False)[:1000],
		}).insert(ignore_permissions=True)
	except Exception:
		try:
			frappe.log_error(frappe.get_traceback(), "Content search event recording failed")
		except Exception:
			pass


def _issue(issue_type, **details):
	severity, reason, action, penalty = ISSUE_DEFINITIONS[issue_type]
	return {"issue_type": issue_type, "severity": severity, "reason": reason, "action": action, "details": details, "penalty": penalty}


def _duplicate_hashes(company):
	rows = frappe.db.sql(
		"""
		SELECT version.content_hash, COUNT(*) AS asset_count
		FROM `tabChannel Content Asset` asset
		JOIN `tabChannel Content Asset Version` version ON version.name = asset.current_version
		WHERE asset.company = %s AND asset.status = 'Active' AND COALESCE(version.content_hash, '') != ''
		GROUP BY version.content_hash HAVING COUNT(*) > 1
		""", company, as_dict=True,
	)
	return {row.content_hash: cint(row.asset_count) for row in rows}


def _asset_issues(asset, duplicate_hashes, stale_before, responsible_governance=False):
	issues = []
	if responsible_governance and not asset.responsible_user:
		issues.append(_issue("unassigned"))
	if not asset.department:
		issues.append(_issue("no_department"))
	if not asset.expires_on:
		issues.append(_issue("no_expiry"))
	if not (asset.tags or "").strip():
		issues.append(_issue("no_tags"))
	if not frappe.db.exists("Channel Content Relation", {"asset": asset.name}):
		issues.append(_issue("no_relation"))
	index = frappe.db.get_value("Channel Content Search Index", {"asset": asset.name}, ["version", "status"], as_dict=True)
	if not index or index.version != asset.current_version:
		issues.append(_issue("index_missing"))
	elif index.status == "Failed":
		issues.append(_issue("index_failed"))
	elif index.status == "Unsupported":
		issues.append(_issue("index_unsupported"))
	insight = frappe.db.get_value("Channel Content Insight", {"asset": asset.name}, ["version", "status"], as_dict=True)
	if not insight or insight.version != asset.current_version or insight.status != "Ready":
		issues.append(_issue("insight_missing"))
	if asset.expires_on and getdate(asset.expires_on) < getdate(nowdate()):
		issues.append(_issue("expired", expires_on=str(asset.expires_on)))
	last_accessed = frappe.db.get_value("Channel Content Access Log", {"asset": asset.name}, "last_accessed")
	last_activity = max(filter(None, [get_datetime(asset.creation), get_datetime(last_accessed) if last_accessed else None]))
	if last_activity < stale_before:
		issues.append(_issue("stale", last_activity=str(last_activity)))
	content_hash = frappe.db.get_value("Channel Content Asset Version", asset.current_version, "content_hash") if asset.current_version else None
	if content_hash and content_hash in duplicate_hashes:
		issues.append(_issue("duplicate", duplicate_count=duplicate_hashes[content_hash]))
	return issues


def rebuild_company_metrics(company):
	settings = _settings()
	duplicate_hashes = _duplicate_hashes(company)
	stale_before = get_datetime(add_days(nowdate(), -settings["stale_days"]))
	active_names = []
	for asset in frappe.get_all(
		"Channel Content Asset", filters={"company": company, "status": "Active"}, fields=["*"], limit=0
	):
		active_names.append(asset.name)
		issues = _asset_issues(asset, duplicate_hashes, stale_before, settings["responsible_governance"])
		public_issues = [{key: value for key, value in issue.items() if key != "penalty"} for issue in issues]
		values = {
			"company": company, "folder": asset.folder, "version": asset.current_version,
			"quality_score": max(0, 100 - sum(issue["penalty"] for issue in issues)),
			"issue_count": len(issues), "issues": json.dumps(public_issues, ensure_ascii=False),
			"evaluated_at": now_datetime(),
		}
		name = frappe.db.get_value("Channel Content Quality Metric", {"asset": asset.name}, "name")
		if name:
			frappe.db.set_value("Channel Content Quality Metric", name, values, update_modified=False)
		else:
			frappe.get_doc({"doctype": "Channel Content Quality Metric", "asset": asset.name, **values}).insert(ignore_permissions=True)
	for name in frappe.get_all(
		"Channel Content Quality Metric", filters={"company": company, "asset": ["not in", active_names or [""]]}, pluck="name"
	):
		doc = frappe.get_doc("Channel Content Quality Metric", name)
		doc.flags.allow_metric_delete = True
		doc.delete(ignore_permissions=True)
	return len(active_names)


def visible_metric_rows(company):
	from channel_erp.content_center import can_access_folder

	rows = frappe.get_all("Channel Content Quality Metric", filters={"company": company}, fields=["*"], limit=0)
	return [row for row in rows if can_access_folder(row.folder, "can_view", raise_exception=False)]


def parse_issues(row):
	try:
		return frappe.parse_json(row.issues) if row.issues else []
	except (TypeError, ValueError):
		return []


def purge_search_events():
	cutoff = add_days(nowdate(), -_settings()["retention_days"])
	names = frappe.get_all("Channel Content Search Event", filters={"creation": ["<", cutoff]}, pluck="name", limit=10000)
	for name in names:
		doc = frappe.get_doc("Channel Content Search Event", name)
		doc.flags.allow_retention_delete = True
		doc.delete(ignore_permissions=True)
	return len(names)


def run_knowledge_quality_maintenance():
	from channel_erp.content_center import append_content_audit

	purged = purge_search_events()
	rebuilt = 0
	for company in frappe.get_all("Company", pluck="name", limit=0):
		company_rebuilt = rebuild_company_metrics(company)
		rebuilt += company_rebuilt
		append_content_audit(
			"quality-maintenance", company=company,
			details={"purged_search_events": purged, "rebuilt_assets": company_rebuilt},
		)
	return {"purged_search_events": purged, "rebuilt_assets": rebuilt}

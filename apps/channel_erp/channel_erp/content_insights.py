import json
import re
from collections import Counter

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime


STOP_WORDS = {
	"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "in", "is",
	"it", "of", "on", "or", "that", "the", "this", "to", "was", "were", "will", "with",
	"you", "your", "we", "our", "can", "not", "all", "new", "file", "document",
	"的", "了", "和", "与", "及", "或", "在", "是", "为", "对", "由", "于", "将", "把",
	"本", "该", "此", "一个", "一种", "可以", "进行", "以及", "内容", "文件", "文档",
}
BUSINESS_TYPES = (
	("Item", "item_name"),
	("Customer", "customer_name"),
	("Supplier", "supplier_name"),
	("Project", "project_name"),
)
MAX_ANALYSIS_CHARS = 500_000
MAX_RELATION_SCAN_PER_TYPE = 100


def _settings():
	settings = frappe.get_single("Channel Content Settings")
	return {
		"enabled": bool(cint(settings.enable_content_insights)),
		"min_confidence": min(max(flt(settings.insight_min_confidence or 0.8), 0.5), 1),
		"keyword_limit": min(max(cint(settings.insight_keyword_limit or 12), 1), 50),
		"summary_sentences": min(max(cint(settings.insight_summary_sentences or 3), 1), 10),
		"suggestion_limit": min(max(cint(settings.insight_suggestion_limit or 10), 1), 50),
	}


def _sentences(text):
	return [part.strip() for part in re.split(r"(?<=[。！？!?；;\.])\s*|[\r\n]+", text) if part.strip()][:5000]


def _tokens(text):
	result = []
	for token in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{1,63}|[0-9][A-Za-z0-9_.-]{1,63}|[\u3400-\u9fff]{2,16}", text):
		normalized = token.casefold().strip("._-")
		if normalized and normalized not in STOP_WORDS and len(normalized) <= 64:
			result.append(normalized)
	return result


def _analyse(text, summary_limit, keyword_limit):
	sentences = _sentences(text)
	all_tokens = _tokens(text)
	counts = Counter(all_tokens)
	first = {token: all_tokens.index(token) for token in counts}
	keywords = [
		{"value": token, "count": count}
		for token, count in sorted(counts.items(), key=lambda item: (-item[1], first[item[0]], item[0]))[:keyword_limit]
	]
	if not sentences:
		return "", keywords
	scores = []
	for position, sentence in enumerate(sentences):
		words = _tokens(sentence)
		score = sum(counts[word] for word in words) / max(len(words), 1)
		scores.append((score, position, sentence))
	selected = sorted(sorted(scores, key=lambda row: (-row[0], row[1]))[:summary_limit], key=lambda row: row[1])
	return " ".join(row[2] for row in selected), keywords


def _insight_doc(asset_doc, version, status, **values):
	name = frappe.db.get_value("Channel Content Insight", {"asset": asset_doc.name}, "name")
	data = {
		"company": asset_doc.company,
		"folder": asset_doc.folder,
		"version": version,
		"status": status,
		**values,
	}
	if name:
		frappe.db.set_value("Channel Content Insight", name, data, update_modified=False)
		return frappe.get_doc("Channel Content Insight", name)
	return frappe.get_doc({"doctype": "Channel Content Insight", "asset": asset_doc.name, **data}).insert(
		ignore_permissions=True
	)


def _delete_pending(asset):
	for name in frappe.get_all(
		"Channel Content Suggestion", filters={"asset": asset, "status": "Pending"}, pluck="name"
	):
		doc = frappe.get_doc("Channel Content Suggestion", name)
		doc.flags.allow_suggestion_delete = True
		doc.delete(ignore_permissions=True)


def _existing_tags(asset_doc):
	return {tag.strip().casefold() for tag in re.split(r"[,，\n]", asset_doc.tags or "") if tag.strip()}


def _tag_suggestions(asset_doc, version, keywords, settings):
	if not keywords:
		return []
	maximum = max(keyword["count"] for keyword in keywords) or 1
	existing = _existing_tags(asset_doc)
	result = []
	for keyword in keywords:
		value = keyword["value"]
		confidence = min(0.95, 0.72 + 0.23 * keyword["count"] / maximum)
		if confidence < settings["min_confidence"] or value.casefold() in existing or len(value) > 50:
			continue
		result.append({
			"asset": asset_doc.name, "company": asset_doc.company, "folder": asset_doc.folder,
			"version": version, "suggestion_type": "Tag", "value": value,
			"confidence": confidence, "reason": _("正文高频关键词（出现 {0} 次）").format(keyword["count"]),
		})
	return result


def _relation_suggestions(asset_doc, version, searchable_text, settings):
	result = []
	for doctype, title_field in BUSINESS_TYPES:
		if not frappe.db.exists("DocType", doctype) or not frappe.has_permission(doctype, "read"):
			continue
		meta = frappe.get_meta(doctype)
		fields = ["name"]
		if meta.has_field(title_field):
			fields.append(title_field)
		if meta.has_field("company"):
			fields.append("company")
		filters = {"company": asset_doc.company} if meta.has_field("company") else {}
		for row in frappe.get_all(
			doctype, filters=filters, fields=fields, order_by="modified desc", limit=MAX_RELATION_SCAN_PER_TYPE
		):
			if not frappe.has_permission(doctype, "read", doc=row.name):
				continue
			name = (row.name or "").strip()
			title = ((row.get(title_field) if len(fields) > 1 else None) or "").strip()
			matched = None
			confidence = 0
			if len(name) >= 2 and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", searchable_text, re.IGNORECASE):
				matched, confidence = name, 0.99
			elif len(title) >= 3 and title.casefold() in searchable_text.casefold():
				matched, confidence = title, 0.95
			if not matched or confidence < settings["min_confidence"]:
				continue
			if frappe.db.exists("Channel Content Relation", {
				"asset": asset_doc.name, "reference_doctype": doctype, "reference_name": name,
			}):
				continue
			result.append({
				"asset": asset_doc.name, "company": asset_doc.company, "folder": asset_doc.folder,
				"version": version, "suggestion_type": "Relation", "value": f"{doctype}: {name}",
				"reference_doctype": doctype, "reference_name": name, "confidence": confidence,
				"reason": _("正文精确出现可读业务记录的编号或名称：{0}").format(matched),
			})
	return sorted(result, key=lambda row: (-row["confidence"], BUSINESS_TYPES.index((row["reference_doctype"], dict(BUSINESS_TYPES)[row["reference_doctype"]])), row["reference_name"]))


def _insert_suggestions(rows, limit):
	created = []
	for row in rows[:limit]:
		duplicate = {
			"asset": row["asset"], "version": row["version"], "suggestion_type": row["suggestion_type"],
			"value": row["value"], "status": ["in", ["Pending", "Accepted"]],
		}
		if frappe.db.exists("Channel Content Suggestion", duplicate):
			continue
		doc = frappe.get_doc({
			"doctype": "Channel Content Suggestion", **row, "status": "Pending", "generated_at": now_datetime(),
		}).insert(ignore_permissions=True)
		created.append(doc.name)
	return created


def queue_content_insights(asset, force=False, now=False, requested_by=None):
	from channel_erp.content_center import append_content_audit

	settings = _settings()
	asset_doc = frappe.get_doc("Channel Content Asset", asset)
	version = asset_doc.current_version
	if not settings["enabled"]:
		_insight_doc(asset_doc, version or "", "Disabled", summary=None, keywords=None,
			error_summary=_("本地内容洞察已在设置中关闭"), generated_at=None)
		return {"asset": asset, "version": version, "status": "Disabled", "queued": False}
	index = frappe.db.get_value(
		"Channel Content Search Index", {"asset": asset}, ["version", "status"], as_dict=True
	)
	if asset_doc.status != "Active" or not version or not index or index.version != version or index.status != "Ready":
		_insight_doc(asset_doc, version or "", "Waiting", summary=None, keywords=None,
			error_summary=_("等待当前版本全文索引就绪"), generated_at=None)
		return {"asset": asset, "version": version, "status": "Waiting", "queued": False}
	existing = frappe.db.get_value("Channel Content Insight", {"asset": asset}, ["version", "status"], as_dict=True)
	if existing and existing.version == version and existing.status in {"Pending", "Processing", "Ready"} and not force:
		return {"asset": asset, "version": version, "status": existing.status, "queued": False}
	_insight_doc(asset_doc, version, "Pending", summary=None, keywords=None, error_summary=None,
		queued_at=now_datetime(), generated_at=None)
	append_content_audit("insight-enqueue", asset_doc, details={"version": version, "force": bool(force)})
	requested_by = requested_by or frappe.session.user
	if now:
		process_content_insights(asset, version, requested_by)
	else:
		frappe.enqueue(
			"channel_erp.content_insights.process_content_insights", queue="short", timeout=180,
			asset=asset, version=version, requested_by=requested_by, enqueue_after_commit=True,
			job_id=f"content-insight-{asset}-{version}", deduplicate=True,
		)
	return {"asset": asset, "version": version, "status": "Pending", "queued": True}


def process_content_insights(asset, version, requested_by=None):
	from channel_erp.content_center import append_content_audit

	if requested_by and frappe.db.exists("User", requested_by):
		frappe.set_user(requested_by)
	if not frappe.db.exists("Channel Content Asset", asset):
		return
	asset_doc = frappe.get_doc("Channel Content Asset", asset)
	if asset_doc.status != "Active" or asset_doc.current_version != version:
		return
	index = frappe.db.get_value(
		"Channel Content Search Index", {"asset": asset, "version": version, "status": "Ready"},
		["extracted_text", "language"], as_dict=True,
	)
	if not index:
		_insight_doc(asset_doc, version, "Waiting", error_summary=_("等待当前版本全文索引就绪"))
		return
	frappe.db.set_value("Channel Content Insight", {"asset": asset}, "status", "Processing", update_modified=False)
	try:
		settings = _settings()
		text = (index.extracted_text or "")[:MAX_ANALYSIS_CHARS]
		summary, keywords = _analyse(text, settings["summary_sentences"], settings["keyword_limit"])
		_delete_pending(asset)
		tag_rows = _tag_suggestions(asset_doc, version, keywords, settings)
		relation_rows = _relation_suggestions(asset_doc, version, f"{asset_doc.title}\n{text}", settings)
		created = _insert_suggestions(relation_rows + tag_rows, settings["suggestion_limit"])
		if asset_doc.current_version != version or asset_doc.status != "Active":
			return
		_insight_doc(asset_doc, version, "Ready", summary=summary, keywords=json.dumps(keywords, ensure_ascii=False),
			language=index.language, error_summary=None, generated_at=now_datetime())
		append_content_audit("insight-ready", asset_doc, details={
			"version": version, "keyword_count": len(keywords), "suggestion_count": len(created),
		})
	except Exception:
		_insight_doc(asset_doc, version, "Failed", summary=None, keywords=None,
			error_summary=_("本地内容洞察生成失败"), generated_at=now_datetime())
		append_content_audit("insight-failed", asset_doc, details={"version": version, "reason": "analysis-failed"})
		frappe.log_error(frappe.get_traceback(), "Content insight generation failed")


def invalidate_content_insights(asset, delete=False):
	name = frappe.db.get_value("Channel Content Insight", {"asset": asset}, "name")
	if name:
		if delete:
			doc = frappe.get_doc("Channel Content Insight", name)
			doc.flags.allow_insight_delete = True
			doc.delete(ignore_permissions=True)
		else:
			frappe.db.set_value("Channel Content Insight", name, {
				"status": "Waiting", "summary": None, "keywords": None,
				"error_summary": _("等待当前版本全文索引就绪"), "generated_at": None,
			}, update_modified=False)
	for suggestion_name in frappe.get_all(
		"Channel Content Suggestion", filters={"asset": asset, "status": "Pending"}, pluck="name"
	):
		doc = frappe.get_doc("Channel Content Suggestion", suggestion_name)
		doc.flags.allow_suggestion_delete = True
		doc.delete(ignore_permissions=True)


def run_content_insight_maintenance(limit=100):
	if not _settings()["enabled"]:
		return {"queued": 0, "disabled": True}
	queued = 0
	for row in frappe.get_all("Channel Content Asset", filters={"status": "Active"}, fields=["name", "current_version"], limit=cint(limit) or 100):
		index = frappe.db.get_value("Channel Content Search Index", {"asset": row.name}, ["version", "status"], as_dict=True)
		insight = frappe.db.get_value("Channel Content Insight", {"asset": row.name}, ["version", "status"], as_dict=True)
		if index and index.version == row.current_version and index.status == "Ready" and (
			not insight or insight.version != row.current_version or insight.status in {"Waiting", "Failed"}
		):
			if queue_content_insights(row.name).get("queued"):
				queued += 1
	return {"queued": queued}

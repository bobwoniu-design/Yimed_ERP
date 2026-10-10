"""批次库存查询与批次检测报告维护（京东自营）。

批次库存口径（2026-10-10 起）：读取「吉客云批次库存快照」
（Jackyun Batch Inventory，白天每 10 分钟增量刷新、凌晨全量兜底），
直接镜像吉客云仓库的批次库存，不经 ERPNext 出入库单据与库存账。
在此之上叠加京东备货池软占用，得到「可用数量」，
并标注每个批次检测报告的维护状态。
"""
from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import flt, format_datetime, getdate, now_datetime, today

from yimed_ecommerce.jd.workflow import _remaining_shelf_life

MAX_ROWS = 500


@frappe.whitelist()
def get_batch_stock(
	company: str | None = None,
	warehouse: str | None = None,
	item_code: str | None = None,
	batch_no: str | None = None,
	keyword: str | None = None,
	only_in_stock: int = 0,
	only_reported: int = 0,
	only_unreported: int = 0,
) -> dict:
	"""按 物料×仓库×批号 查询批次库存，附带软占用与检测报告状态。"""
	# 与模块惯例一致（get_batch_candidates 校验 Item/Warehouse）：
	# 不直接校验 Batch 权限（ERPNext 只给 Item Manager），让库存角色用户也能使用本页面
	frappe.has_permission("JD Batch Inspection Report", "read", throw=True)

	company = (company or "").strip()
	warehouse = (warehouse or "").strip()
	item_code = (item_code or "").strip()
	batch_no = (batch_no or "").strip()
	keyword = (keyword or "").strip()

	conditions = ["ifnull(t.item_code, '') != ''"]
	values_company: dict = {}
	if company:
		conditions.append("w.company = %(company)s")
		values_company["company"] = company
	if warehouse:
		conditions.append("t.warehouse = %(warehouse)s")
		values_company["warehouse"] = warehouse
	if item_code:
		conditions.append("t.item_code = %(item_code)s")
		values_company.update({"item_code": item_code})
	if batch_no:
		conditions.append("t.batch_no like %(batch_no)s")
		values_company.update({"batch_no": f"%{batch_no}%"})
	if keyword:
		conditions.append("(t.batch_no like %(kw)s or t.item_code like %(kw)s or ifnull(i.item_name,'') like %(kw)s)")
		values_company.update({"kw": f"%{keyword}%"})
	if flt(only_in_stock):
		conditions.append("t.quantity > 0.000001")

	stock_sql = """
		select t.item_code as item_code, i.item_name as item_name, i.stock_uom as uom,
		       t.batch_no as batch_no, t.production_date as manufacturing_date,
		       t.expiry_date as expiry_date,
		       t.warehouse as warehouse, t.quantity as actual_qty,
		       t.snapshot_at as snapshot_at, t.locked_quantity as jackyun_locked_qty
		from `tabJackyun Batch Inventory` t
		inner join `tabItem` i on i.name = t.item_code
		inner join `tabWarehouse` w on w.name = t.warehouse
		where {conditions}
		order by t.item_code, t.batch_no, t.warehouse
		limit {limit}
	""".format(conditions=" and ".join(conditions), limit=MAX_ROWS + 1)

	rows = frappe.db.sql(stock_sql, values_company, as_dict=True)
	truncated = len(rows) > MAX_ROWS
	rows = rows[:MAX_ROWS]

	if not rows:
		return {"rows": [], "truncated": False, "limit": MAX_ROWS}

	batch_nos = list({row.batch_no for row in rows})
	reserved_map = _soft_reserved_map(batch_nos)
	report_map = _inspection_report_map(batch_nos)

	result = []
	snapshots = []
	for row in rows:
		reserved = reserved_map.get((row.item_code, row.warehouse, row.batch_no), 0)
		percent = _remaining_shelf_life(row.manufacturing_date, row.expiry_date, today())
		expired = bool(row.expiry_date and getdate(row.expiry_date) < getdate(today()))
		report = report_map.get(row.batch_no)
		# 检测报告以附件为准：只有上传了附件才算“已上传”
		if flt(only_reported) and not (report and report.get("report_attachment")):
			continue
		if flt(only_unreported) and (report and report.get("report_attachment")):
			continue
		result.append({
			"item_code": row.item_code,
			"item_name": row.item_name,
			"uom": row.uom,
			"batch_no": row.batch_no,
			"warehouse": row.warehouse,
			"actual_qty": flt(row.actual_qty, 6),
			"soft_reserved_qty": flt(reserved, 6),
			"available_qty": max(flt(row.actual_qty - reserved, 6), 0),
			"manufacturing_date": row.manufacturing_date,
			"expiry_date": row.expiry_date,
			"remaining_shelf_life_percent": percent,
			"expired": expired,
			"shelf_life_eligible": None if percent is None else percent >= 66.6667,
			"inspection_report": report,
			"snapshot_at": format_datetime(row.snapshot_at) if row.snapshot_at else None,
		})
		snapshots.append(row.snapshot_at)
	snapshot_latest = max(snapshots) if snapshots else None
	return {"rows": result, "truncated": truncated, "limit": MAX_ROWS, "snapshot_at": format_datetime(snapshot_latest) if snapshot_latest else None}


def _soft_reserved_map(batch_nos: list[str]) -> dict[tuple, float]:
	"""京东备货池软占用：同一批次的库存可能被多个导入批次备货占用。"""
	if not batch_nos:
		return {}
	rows = frappe.get_all(
		"JD Stocking Pool Item",
		filters={"batch_no": ["in", batch_nos], "stocked_qty": [">", 0]},
		fields=["stock_item", "warehouse", "batch_no", "stocked_qty"],
	)
	out: dict[tuple, float] = {}
	for row in rows:
		key = (row.stock_item, row.warehouse, row.batch_no)
		out[key] = flt(out.get(key, 0) + row.stocked_qty, 6)
	return out


def _inspection_report_map(batch_nos: list[str]) -> dict[str, dict]:
	if not batch_nos:
		return {}
	rows = frappe.get_all(
		"JD Batch Inspection Report",
		filters={"batch_no": ["in", batch_nos]},
		fields=["name", "batch_no", "item_code", "report_no", "inspection_date",
			"inspection_agency", "inspection_result", "inspection_items", "remarks", "report_attachment"],
	)
	return {row.batch_no: row for row in rows}


@frappe.whitelist()
def get_inspection_report(batch_no: str):
	"""读取某个批次的检测报告；未维护时返回 batch 的基础信息便于新建。"""
	batch_no = (batch_no or "").strip()
	if not batch_no:
		frappe.throw(_("批号不能为空。"))
	frappe.has_permission("JD Batch Inspection Report", "read", throw=True)
	report = frappe.db.get_value(
		"JD Batch Inspection Report",
		{"batch_no": batch_no},
		["name", "batch_no", "item_code", "item_name", "report_no", "inspection_date",
			"inspection_agency", "inspection_result", "inspection_items", "remarks", "report_attachment"],
		as_dict=True,
	)
	return {"exists": bool(report), "report": report}


@frappe.whitelist()
def save_inspection_report(values: str | dict):
	"""创建或更新批次的检测报告（每个批次一份）。"""
	if isinstance(values, str):
		try:
			values = json.loads(values)
		except (TypeError, ValueError):
			frappe.throw(_("检测报告数据格式无效。"))
	if not isinstance(values, dict):
		frappe.throw(_("检测报告数据格式无效。"))
	values = {key: value for key, value in values.items() if key in {
		"batch_no", "report_no", "inspection_date", "inspection_agency",
		"inspection_result", "inspection_items", "remarks", "report_attachment",
	}}
	batch_no = (values.get("batch_no") or "").strip()
	if not batch_no:
		frappe.throw(_("批号不能为空。"))
	frappe.has_permission("Batch", "read", batch_no, throw=True)

	existing = frappe.db.get_value("JD Batch Inspection Report", {"batch_no": batch_no}, "name")
	if existing:
		doc = frappe.get_doc("JD Batch Inspection Report", existing)
		doc.update(values)
		doc.save()
	else:
		doc = frappe.get_doc({"doctype": "JD Batch Inspection Report", **values}).insert()
	return {"name": doc.name, "batch_no": doc.batch_no, "item_code": doc.item_code}


@frappe.whitelist()
def set_inspection_attachment(batch_no: str, file_url: str):
	"""检测报告仅维护附件：上传即维护，清空附件且无其他信息时删除报告记录。"""
	batch_no = (batch_no or "").strip()
	if not batch_no:
		frappe.throw(_("批号不能为空。"))
	# 写权限由后续 insert/save 按角色校验；这里先确保可读
	frappe.has_permission("JD Batch Inspection Report", "read", throw=True)
	file_url = (file_url or "").strip()

	existing = frappe.db.get_value("JD Batch Inspection Report", {"batch_no": batch_no}, "name")
	if not file_url:
		if not existing:
			return {"name": None, "deleted": False, "has_attachment": False}
		doc = frappe.get_doc("JD Batch Inspection Report", existing)
		has_details = bool(doc.report_no or doc.inspection_date or doc.inspection_agency
			or doc.inspection_items or doc.remarks)
		if not has_details:
			frappe.delete_doc("JD Batch Inspection Report", existing)
			return {"name": None, "deleted": True, "has_attachment": False}
		doc.report_attachment = None
		doc.save()
		return {"name": doc.name, "deleted": False, "has_attachment": False}

	if existing:
		doc = frappe.get_doc("JD Batch Inspection Report", existing)
		doc.report_attachment = file_url
		doc.save()
	else:
		doc = frappe.get_doc({
			"doctype": "JD Batch Inspection Report",
			"batch_no": batch_no,
			"report_attachment": file_url,
		}).insert()
	return {"name": doc.name, "deleted": False, "has_attachment": True}


@frappe.whitelist()
def download_inspection_reports(batch_nos: str | list):
	"""批量打印批次检测报告 PDF：每个批次一页（A4 纵向）。"""
	if isinstance(batch_nos, str):
		try:
			batch_nos = json.loads(batch_nos)
		except (TypeError, ValueError):
			batch_nos = [batch_nos]
	if not isinstance(batch_nos, list) or not batch_nos:
		frappe.throw(_("请选择要打印检测报告的批次。"))
	if len(batch_nos) > 200:
		frappe.throw(_("单次最多打印 200 个批次的检测报告。"))
	batch_nos = [str(value).strip() for value in batch_nos if str(value).strip()]
	if not batch_nos:
		frappe.throw(_("请选择要打印检测报告的批次。"))

	frappe.has_permission("JD Batch Inspection Report", "print", throw=True)

	reports = frappe.get_all(
		"JD Batch Inspection Report",
		filters={"batch_no": ["in", batch_nos]},
		fields=["name", "batch_no", "item_code", "item_name", "report_no", "inspection_date",
			"inspection_agency", "inspection_result", "inspection_items", "remarks", "report_attachment"],
		order_by="batch_no",
	)
	missing = sorted(set(batch_nos) - {row.batch_no for row in reports if row.report_attachment})
	if missing:
		frappe.throw(_("以下批次未上传检测报告附件，请先上传后再打印：{0}").format("、".join(missing[:20]) + ("…" if len(missing) > 20 else "")))

	batch_fields = {row.name: row for row in frappe.get_all(
		"Batch", filters={"name": ["in", batch_nos]},
		fields=["name", "item", "manufacturing_date", "expiry_date"],
	)}
	company_by_batch = _batch_companies(batch_nos)
	stock_by_batch = _batch_stock_summary(batch_nos)

	pages = []
	for report in reports:
		batch = batch_fields.get(report.batch_no, frappe._dict())
		stock_rows = stock_by_batch.get(report.batch_no) or []
		total_qty = sum(row.get("qty", 0) for row in stock_rows)
		pages.append({
			"report": report,
			"batch": batch,
			"company": company_by_batch.get(report.batch_no) or "",
			"stock_rows": stock_rows,
			"total_qty": total_qty,
			"has_expiry": bool(batch.expiry_date),
		})

	html = frappe.render_template(
		"yimed_ecommerce/templates/jd_batch_inspection_pdf.html",
		{"pages": pages, "printed_at": format_datetime(now_datetime())},
	)

	pdf = _render_chrome_pdf_portrait(html)
	frappe.local.response.filename = "批次检测报告.pdf"
	frappe.local.response.filecontent = pdf
	frappe.local.response.type = "pdf"


def _render_chrome_pdf_portrait(html: str) -> bytes:
	from frappe.utils.pdf import get_chrome_pdf

	options = {
		"page-width": "210mm",
		"page-height": "297mm",
		"orientation": "Portrait",
		"margin-top": "10mm",
		"margin-bottom": "10mm",
		"margin-left": "10mm",
		"margin-right": "10mm",
	}
	return get_chrome_pdf(None, html, options, None, pdf_generator="chrome")


def _batch_companies(batch_nos: list[str]) -> dict[str, str]:
	rows = frappe.db.sql(
		"""
		select sle.batch_no, w.company
		from `tabStock Ledger Entry` sle
		inner join `tabWarehouse` w on w.name = sle.warehouse
		where sle.batch_no in %(batch_nos)s and ifnull(sle.company, '') != ''
		group by sle.batch_no, w.company
		order by max(sle.posting_date) desc
		""",
		{"batch_nos": batch_nos},
		as_dict=True,
	)
	out: dict[str, str] = {}
	for row in rows:
		out.setdefault(row.batch_no, row.company)
	return out


def _batch_stock_summary(batch_nos: list[str]) -> dict[str, list]:
	"""打印时点每个批次的仓库库存分布（含零库存仓库不显示）。"""
	rows = frappe.db.sql(
		"""
		select t.batch_no, t.warehouse, sum(t.qty) as qty
		from (
			select sbe.batch_no as batch_no, sbe.warehouse as warehouse, sum(sbe.qty) as qty
			from `tabSerial and Batch Entry` sbe
			inner join `tabStock Ledger Entry` sle on sle.serial_and_batch_bundle = sbe.parent
			where sle.is_cancelled = 0 and sbe.batch_no in %(batch_nos)s
			group by sbe.batch_no, sbe.warehouse
			union all
			select sle.batch_no as batch_no, sle.warehouse as warehouse, sum(sle.actual_qty) as qty
			from `tabStock Ledger Entry` sle
			where sle.is_cancelled = 0
			  and ifnull(sle.batch_no, '') != ''
			  and ifnull(sle.serial_and_batch_bundle, '') = ''
			  and sle.batch_no in %(batch_nos)s
			group by sle.batch_no, sle.warehouse
		) t
		group by t.batch_no, t.warehouse
		having sum(t.qty) > 0.000001
		order by t.batch_no, t.warehouse
		""",
		{"batch_nos": batch_nos},
		as_dict=True,
	)
	out: dict[str, list] = {}
	for row in rows:
		out.setdefault(row.batch_no, []).append({"warehouse": row.warehouse, "qty": flt(row.qty, 6)})
	return out

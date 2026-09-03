from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate


@frappe.whitelist()
def get_filter_options():
	_check_permission()
	rows = frappe.get_all(
		"Ecommerce Promotion Performance",
		fields=["company", "channel_name", "store_name", "promotion_scene"],
		order_by="company, channel_name, store_name",
	)
	return {
		"companies": sorted({row.company for row in rows if row.company}),
		"channels": sorted({row.channel_name for row in rows if row.channel_name}),
		"stores": sorted({row.store_name for row in rows if row.store_name}),
		"scenes": sorted({row.promotion_scene for row in rows if row.promotion_scene}),
		"attribution_windows": ["1天转化", "15天转化"],
	}


@frappe.whitelist()
def get_promotion_dashboard(
	company=None, from_date=None, to_date=None, channel_name=None, store_name=None,
	promotion_scene=None, attribution_window="1天转化",
):
	"""返回独立推广分析口径；成交额均为平台归因值，不是ERP经营收入。"""
	_check_permission()
	if not from_date or not to_date:
		frappe.throw(_("From Date and To Date are required."))
	start, end = getdate(from_date), getdate(to_date)
	if start > end:
		frappe.throw(_("From Date cannot be later than To Date."))
	if end >= getdate(frappe.utils.nowdate()):
		frappe.throw(_("To Date must be earlier than today; today's real-time data belongs to the realtime sales dashboard."))
	if attribution_window not in {"1天转化", "15天转化"}:
		frappe.throw(_("Attribution window must be 1-day or 15-day."))

	conditions = ["posting_date between %s and %s", "attribution_window=%s"]
	params = [start, end, attribution_window]
	for fieldname, value in (
		("company", company), ("channel_name", channel_name), ("store_name", store_name),
		("promotion_scene", promotion_scene),
	):
		if value:
			conditions.append(f"{fieldname}=%s")
			params.append(value)
	where = " and ".join(conditions)
	base_fields = "sum(impressions) impressions, sum(clicks) clicks, sum(amount) amount, sum(attributed_orders) attributed_orders, sum(attributed_sales) attributed_sales"

	summary_row = frappe.db.sql(
		f"select {base_fields}, sum(case when match_status='待匹配' then amount else 0 end) unmatched_amount from `tabEcommerce Promotion Performance` where {where}",
		params, as_dict=True,
	)[0]
	summary = _metrics(summary_row)
	summary["unmatched_amount"] = flt(summary_row.unmatched_amount)

	trend = frappe.db.sql(
		f"select posting_date date, {base_fields} from `tabEcommerce Promotion Performance` where {where} group by posting_date order by posting_date",
		params, as_dict=True,
	)
	for row in trend:
		row.update(_metrics(row))

	details = frappe.db.sql(
		f"""select company, channel_name channel, store_name store, promotion_scene scene,
			campaign_id, campaign_name, subject_id, max(subject_name) subject_name,
			max(listing) listing, max(match_status) match_status, {base_fields}
		from `tabEcommerce Promotion Performance` where {where}
		group by company, channel_name, store_name, promotion_scene, campaign_id, campaign_name, subject_id
		order by amount desc, attributed_sales desc""",
		params, as_dict=True,
	)
	for row in details:
		row.update(_metrics(row))

	return {
		"filters": {"company": company or "", "from_date": start, "to_date": end,
			"channel_name": channel_name or "", "store_name": store_name or "",
			"promotion_scene": promotion_scene or "", "attribution_window": attribution_window},
		"summary": summary, "trend": trend, "details": details,
		"source_note": "平台归因成交口径；经营收入与利润请查看利润预估仪表盘。",
	}


def _metrics(row):
	impressions, clicks = flt(row.get("impressions")), flt(row.get("clicks"))
	amount, orders, sales = flt(row.get("amount")), flt(row.get("attributed_orders")), flt(row.get("attributed_sales"))
	return {
		"impressions": impressions, "clicks": clicks, "amount": amount,
		"attributed_orders": orders, "attributed_sales": sales,
		"ctr_pct": clicks / impressions * 100 if impressions else 0,
		"cpc": amount / clicks if clicks else 0,
		"conversion_rate_pct": orders / clicks * 100 if clicks else 0,
		"roi": sales / amount if amount else 0,
	}


def _check_permission():
	frappe.only_for(("Sales Manager", "Accounts User", "Accounts Manager", "System Manager"))

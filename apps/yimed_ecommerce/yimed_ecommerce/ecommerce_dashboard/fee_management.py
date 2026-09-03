from __future__ import annotations

import hashlib
from collections.abc import Iterable

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, nowdate


ALLOCATION_DRIVER_FIELDS = {
	"销售额": "sales_amount",
	"订单数": "order_count",
	"件数": "quantity",
}


@frappe.whitelist()
def generate_daily_accruals(from_date=None, to_date=None, rule_name=None):
	"""按日生成计提事实；来源唯一键保证任务重跑不会重复记费。"""
	frappe.only_for(("Accounts Manager", "System Manager"))
	from_date = getdate(from_date or nowdate())
	to_date = getdate(to_date or from_date)
	if from_date > to_date:
		frappe.throw(_("From Date cannot be later than To Date."))

	filters = {"enabled": 1, "auto_accrual": 1}
	if rule_name:
		filters["name"] = rule_name
	rules = frappe.get_all(
		"Ecommerce Fee Rule",
		filters=filters,
		fields=[
			"name", "company", "fee_item", "valid_from", "valid_to", "daily_accrual_amount",
			"default_allocation_method",
		],
	)
	created, existing = [], []
	for rule in rules:
		if not rule.fee_item:
			continue
		for posting_date in _date_range(max(from_date, getdate(rule.valid_from)), min(to_date, getdate(rule.valid_to or to_date))):
			for channel_name, store_name in _rule_scopes(rule):
				key = _source_key("accrual", rule.name, posting_date, channel_name, store_name)
				name = frappe.db.get_value("Ecommerce Fee Detail", {"source_unique_key": key}, "name")
				if name:
					existing.append(name)
					continue
				detail = frappe.get_doc({
					"doctype": "Ecommerce Fee Detail",
					"posting_date": posting_date,
					"company": rule.company,
					"fee_item": rule.fee_item,
					"amount": flt(rule.daily_accrual_amount),
					"currency": frappe.db.get_value("Company", rule.company, "default_currency"),
					"channel_name": channel_name,
					"store_name": store_name,
					"allocation_method": rule.default_allocation_method or "不分摊",
					"entry_type": "计提",
					"source_type": "费用规则自动计提",
					"source_reference_doctype": "Ecommerce Fee Rule",
					"source_reference_name": rule.name,
					"source_unique_key": key,
					"fee_rule": rule.name,
				})
				detail.flags.ignore_permissions = True
				detail.insert()
				created.append(detail.name)
	return {"created": created, "existing": existing}


@frappe.whitelist()
def upsert_fee_detail(values):
	"""按来源唯一键写入实际或调整费用，适合导入器安全重试。"""
	frappe.only_for(("Accounts Manager", "System Manager"))
	values = frappe.parse_json(values) if isinstance(values, str) else dict(values or {})
	if values.get("entry_type") not in {"实际", "调整"}:
		frappe.throw(_("Only Actual or Adjustment fee details can be written through this API."))
	if not values.get("source_unique_key"):
		frappe.throw(_("Source Unique Key is required."))

	name = frappe.db.get_value("Ecommerce Fee Detail", {"source_unique_key": values["source_unique_key"]}, "name")
	if name:
		doc = frappe.get_doc("Ecommerce Fee Detail", name)
		doc.update({key: value for key, value in values.items() if key not in {"doctype", "name"}})
		doc.save()
	else:
		doc = frappe.get_doc({"doctype": "Ecommerce Fee Detail", **values}).insert()
	return doc.as_dict()


def _weights_for(method, targets):
	if method == "平均":
		return [1.0] * len(targets)
	if method == "固定比例":
		return [row["fixed_ratio"] for row in targets]
	fieldname = ALLOCATION_DRIVER_FIELDS.get(method)
	return [row[fieldname] for row in targets] if fieldname else []


def _allocate_amount(total, weights):
	"""按货币最小两位分配，并将舍入尾差放入最后一行。"""
	denominator = sum(weights)
	amounts, allocated = [], 0.0
	for index, weight in enumerate(weights):
		amount = round(total - allocated, 2) if index == len(weights) - 1 else round(total * weight / denominator, 2)
		amounts.append(amount)
		allocated += amount
	return amounts


def _rule_scopes(rule):
	rows = frappe.get_all(
		"Ecommerce Fee Rule Scope",
		filters={"parent": rule.name, "parenttype": "Ecommerce Fee Rule"},
		fields=["channel_name", "store_name"],
		order_by="idx",
	)
	return [(row.channel_name or "", row.store_name or "") for row in rows] or [("", "")]


def _date_range(start, end) -> Iterable:
	current = start
	while current <= end:
		yield current
		current = add_days(current, 1)


def _source_key(*parts):
	raw = "|".join(str(part or "-") for part in parts)
	return f"{raw[:90]}|{hashlib.sha256(raw.encode()).hexdigest()[:32]}"

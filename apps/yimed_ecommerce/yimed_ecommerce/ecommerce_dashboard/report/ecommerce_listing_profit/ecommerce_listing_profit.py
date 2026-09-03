from __future__ import annotations

import frappe
from frappe import _

from yimed_ecommerce.ecommerce_dashboard.dashboard import get_dashboard


def execute(filters=None):
	filters = frappe._dict(filters or {})
	dimension = filters.listing_dimension or "main"
	data = get_dashboard(
		filters.company,
		filters.from_date,
		filters.to_date,
		filters.channel_name,
		filters.store_name,
		dimension,
	)
	rows = data["listings"]
	fee_types = _fee_types(rows)
	columns = _base_columns(dimension) + [
		{"fieldname": f"fee_{idx}", "label": _fee_label(fee), "fieldtype": "Float", "precision": 2, "width": 110}
		for idx, fee in enumerate(fee_types, start=1)
	] + _profit_columns()
	for row in rows:
		for idx, fee in enumerate(fee_types, start=1):
			row[f"fee_{idx}"] = (row.get("fee_breakdown") or {}).get(fee, 0)
	return columns, rows


def _base_columns(dimension):
	return [
		{"fieldname":"channel", "label":_("Channel"), "fieldtype":"Data", "width":120},
		{"fieldname":"store", "label":_("Store"), "fieldtype":"Data", "width":180},
		{"fieldname":"listing_id", "label":_("Child Listing ID" if dimension == "child" else "Main Listing ID"), "fieldtype":"Data", "width":150},
		{"fieldname":"listing_name", "label":_("Product"), "fieldtype":"Data", "width":220},
		{"fieldname":"orders", "label":_("Order Count"), "fieldtype":"Int", "width":80},
		{"fieldname":"net_sales", "label":_("Net Sales"), "fieldtype":"Float", "precision":2, "width":120},
		{"fieldname":"product_cost", "label":_("Product Cost"), "fieldtype":"Float", "precision":2, "width":120},
		{"fieldname":"gross_profit", "label":_("Gross Profit"), "fieldtype":"Float", "precision":2, "width":120},
		{"fieldname":"promotion_expense", "label":_("Promotion Expense"), "fieldtype":"Float", "precision":2, "width":120},
	]


def _profit_columns():
	return [
		{"fieldname":"rule_fees", "label":_("Allocated Fees Total"), "fieldtype":"Float", "precision":2, "width":130},
		{"fieldname":"contribution_profit", "label":_("Contribution Profit"), "fieldtype":"Float", "precision":2, "width":130},
		{"fieldname":"contribution_margin_pct", "label":_("Contribution Margin"), "fieldtype":"Percent", "width":110},
	]


def _fee_types(rows):
	primary = ["快递费", "税费", "管理分摊", "平台佣金"]
	actual = {fee for row in rows for fee in (row.get("fee_breakdown") or {})}
	return primary + sorted(actual - set(primary))


def _fee_label(fee):
	return _("Management Fee") if fee == "管理分摊" else fee

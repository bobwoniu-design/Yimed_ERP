from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt, getdate

from yimed_ecommerce.jd.packing import validate_packing_quantities
from yimed_ecommerce.yimed_ecommerce.doctype.jd_self_operated_settings.jd_self_operated_settings import (
	validate_warehouse_company,
)


TRANSFER_MODE_DIRECT = "一步调拨"
TRANSFER_MODE_TRANSIT = "两步调拨"


@frappe.whitelist()
def create_transfer(
	purchase_order: str,
	source_warehouse: str,
	target_warehouse: str,
	transfer_mode: str = TRANSFER_MODE_DIRECT,
	transit_warehouse: str | None = None,
	posting_date: str | None = None,
):
	"""Create the single draft Stock Entry allowed for a JD purchase order."""
	po = frappe.get_doc("JD Purchase Order", purchase_order)
	po.check_permission("write")
	po.require_complete_mapping()
	validate_no_existing_transfer(po)
	validate_packing_complete(po)
	validate_transfer_settings(
		po,
		source_warehouse=source_warehouse,
		target_warehouse=target_warehouse,
		transfer_mode=transfer_mode,
		transit_warehouse=transit_warehouse,
	)

	if transfer_mode not in (TRANSFER_MODE_DIRECT, TRANSFER_MODE_TRANSIT):
		frappe.throw(_("Unsupported transfer mode: {0}").format(transfer_mode))
	if transfer_mode == TRANSFER_MODE_TRANSIT and not transit_warehouse:
		frappe.throw(_("Transit Warehouse is required for a two-step transfer."))

	actual_target = transit_warehouse if transfer_mode == TRANSFER_MODE_TRANSIT else target_warehouse
	stock_entry = frappe.new_doc("Stock Entry")
	stock_entry.company = po.company
	stock_entry.stock_entry_type = "Material Transfer"
	stock_entry.purpose = "Material Transfer"
	stock_entry.posting_date = getdate(posting_date)
	stock_entry.from_warehouse = source_warehouse
	stock_entry.to_warehouse = actual_target
	stock_entry.add_to_transit = 1 if transfer_mode == TRANSFER_MODE_TRANSIT else 0
	stock_entry.custom_jd_purchase_order = po.name
	stock_entry.custom_jd_transfer_mode = transfer_mode
	stock_entry.custom_jd_final_warehouse = target_warehouse

	for row in get_packed_stock_items(po.name):
		stock_entry.append(
			"items",
			{
				"item_code": row["item_code"],
				"qty": row["qty"],
				"uom": row["uom"],
				"stock_uom": row["uom"],
				"conversion_factor": 1,
				"s_warehouse": source_warehouse,
				"t_warehouse": actual_target,
				"batch_no": row["batch_no"],
			},
		)

	stock_entry.set_missing_values()
	stock_entry.insert()
	po.db_set("stock_entry", stock_entry.name)
	po.db_set("transfer_status", "调拨草稿")
	return stock_entry.name


@frappe.whitelist()
def get_transfer_defaults(purchase_order: str) -> dict:
	po = frappe.get_doc("JD Purchase Order", purchase_order)
	po.check_permission("read")
	settings = get_company_settings(po.company)
	if not settings:
		return {"configured": False, "company": po.company, "allow_warehouse_override": True}
	return {
		"configured": True,
		"company": po.company,
		"default_source_warehouse": settings.default_source_warehouse,
		"default_target_warehouse": settings.default_target_warehouse,
		"default_transfer_mode": settings.default_transfer_mode,
		"default_transit_warehouse": settings.default_transit_warehouse,
		"allow_warehouse_override": bool(settings.allow_warehouse_override),
	}


def get_company_settings(company: str):
	name = frappe.db.get_value("JD Self Operated Settings", {"company": company}, "name")
	return frappe.get_doc("JD Self Operated Settings", name) if name else None


def validate_transfer_settings(
	po,
	*,
	source_warehouse: str,
	target_warehouse: str,
	transfer_mode: str,
	transit_warehouse: str | None,
) -> None:
	if transfer_mode not in (TRANSFER_MODE_DIRECT, TRANSFER_MODE_TRANSIT):
		frappe.throw(_("Unsupported transfer mode: {0}").format(transfer_mode))
	if transfer_mode == TRANSFER_MODE_TRANSIT and not transit_warehouse:
		frappe.throw(_("Transit Warehouse is required for a two-step transfer."))

	for warehouse in (source_warehouse, target_warehouse):
		validate_warehouse_company(warehouse, po.company)
	if transit_warehouse:
		validate_warehouse_company(transit_warehouse, po.company)

	settings = get_company_settings(po.company)
	if not settings or settings.allow_warehouse_override:
		return

	expected = {
		"transfer_mode": settings.default_transfer_mode,
		"source_warehouse": settings.default_source_warehouse,
		"target_warehouse": settings.default_target_warehouse,
		"transit_warehouse": (
			settings.default_transit_warehouse if settings.default_transfer_mode == TRANSFER_MODE_TRANSIT else None
		),
	}
	actual = {
		"transfer_mode": transfer_mode,
		"source_warehouse": source_warehouse,
		"target_warehouse": target_warehouse,
		"transit_warehouse": transit_warehouse if transfer_mode == TRANSFER_MODE_TRANSIT else None,
	}
	if actual != expected:
		frappe.throw(_("The transfer route must use the locked defaults in JD Self Operated Settings."))


def validate_no_existing_transfer(po) -> None:
	existing = frappe.db.get_value(
		"Stock Entry",
		{"custom_jd_purchase_order": po.name, "docstatus": ["<", 2]},
		"name",
	)
	if existing:
		frappe.throw(
			_("JD Purchase Order {0} already has Stock Entry {1}.").format(
				frappe.bold(po.name), frappe.bold(existing)
			)
		)


def validate_packing_complete(po) -> None:
	from yimed_ecommerce.jd.workflow import validate_order_ready_for_packing

	validate_order_ready_for_packing(po.name)
	if po.packing_status != "已装箱":
		frappe.throw(_("Complete and verify all cartons before creating the Stock Entry."))
	if not frappe.db.exists("JD Carton", {"purchase_order": po.name}):
		frappe.throw(_("No cartons exist for JD Purchase Order {0}.").format(frappe.bold(po.name)))
	if frappe.db.exists("JD Carton", {"purchase_order": po.name, "verified": 0}):
		frappe.throw(_("Every carton must be verified before creating the Stock Entry."))
	validate_packing_quantities(po.name)


def get_packed_stock_items(purchase_order: str) -> list[dict]:
	rows = frappe.db.sql(
		"""
		select ci.stock_item, ci.uom, ci.batch_no, sum(ci.qty) as qty
		from `tabJD Carton Item` ci
		inner join `tabJD Carton` c on c.name = ci.parent
		where c.purchase_order = %(purchase_order)s
		  and ci.parenttype = 'JD Carton'
		  and ci.parentfield = 'components'
		group by ci.stock_item, ci.uom, ci.batch_no
		order by ci.stock_item, ci.batch_no
		""",
		{"purchase_order": purchase_order},
		as_dict=True,
	)
	if not rows:
		frappe.throw(_("No packed stock Items were found."))
	return [
		{
			"item_code": row.stock_item,
			"uom": row.uom,
			"batch_no": row.batch_no,
			"qty": flt(row.qty),
		}
		for row in rows
	]

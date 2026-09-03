from __future__ import annotations

from dataclasses import asdict, dataclass

import frappe
from frappe import _
from frappe.utils import flt


@dataclass(frozen=True)
class ExpandedItem:
	platform_item: str
	stock_item: str
	stock_uom: str
	qty: float
	is_product_bundle: bool


def is_product_bundle(item_code: str) -> bool:
	return bool(frappe.db.exists("Product Bundle", {"new_item_code": item_code, "disabled": 0}))


def is_supported_platform_item(item_code: str) -> bool:
	if not item_code:
		return False
	if is_product_bundle(item_code):
		return True
	return bool(frappe.db.get_value("Item", item_code, "is_stock_item"))


def expand_platform_item(item_code: str, qty: float) -> list[ExpandedItem]:
	"""Expand a business Item into the physical stock Items that must move.

	ERPNext Product Bundle remains the single source of truth for combination
	contents. The bundle parent is a non-stock business Item; only its components
	are returned for packing and Stock Entry creation.
	"""
	qty = flt(qty)
	if qty <= 0:
		frappe.throw(_("Quantity must be greater than zero."))

	if is_product_bundle(item_code):
		bundle = frappe.get_doc("Product Bundle", item_code)
		return [
			ExpandedItem(
				platform_item=item_code,
				stock_item=row.item_code,
				stock_uom=row.uom or frappe.get_cached_value("Item", row.item_code, "stock_uom"),
				qty=flt(row.qty) * qty,
				is_product_bundle=True,
			)
			for row in bundle.items
		]

	is_stock_item, stock_uom = frappe.get_cached_value(
		"Item", item_code, ["is_stock_item", "stock_uom"]
	) or (0, None)
	if not is_stock_item:
		frappe.throw(
			_("Platform Item {0} is neither a stock Item nor an enabled Product Bundle.").format(
				frappe.bold(item_code)
			)
		)

	return [
		ExpandedItem(
			platform_item=item_code,
			stock_item=item_code,
			stock_uom=stock_uom,
			qty=qty,
			is_product_bundle=False,
		)
	]


@frappe.whitelist()
def preview_expansion(item_code: str, qty: float = 1) -> list[dict]:
	return [asdict(row) for row in expand_platform_item(item_code, qty)]

from __future__ import annotations

from html import escape

import frappe
from frappe import _

from yimed_ecommerce.jd.packing import packing_quantities_match, update_carton_totals


@frappe.whitelist()
def clear_unverified_cartons(purchase_order: str, reason: str):
	"""Delete all unverified cartons before transfer preparation starts."""
	po = _get_locked_purchase_order(purchase_order)
	reason = _require_reason(reason)
	_reject_active_transfer(po)

	if frappe.db.exists("JD Carton", {"purchase_order": po.name, "verified": 1}):
		frappe.throw(_("Verified cartons exist. Undo carton verification before clearing cartons."))

	cartons = frappe.get_all(
		"JD Carton",
		filters={"purchase_order": po.name, "verified": 0},
		pluck="name",
		order_by="carton_sequence desc",
	)
	if not cartons:
		frappe.throw(_("No unverified cartons were found for JD Purchase Order {0}.").format(po.name))

	# PO write permission is the authorization boundary for this controlled cleanup.
	# Stock Users intentionally do not receive unrestricted delete permission on cartons.
	for carton in cartons:
		frappe.delete_doc("JD Carton", carton, ignore_permissions=True)

	update_carton_totals(po.name)
	frappe.db.set_value(
		"JD Purchase Order",
		po.name,
		{"packing_status": "待装箱", "status": "待备货"},
		update_modified=False,
	)
	_add_audit_comment(po, _("Clear Unverified Cartons"), reason, _("Deleted {0} carton(s).").format(len(cartons)))
	return {"purchase_order": po.name, "deleted_cartons": len(cartons), "packing_status": "待装箱"}


@frappe.whitelist()
def undo_carton_verification(purchase_order: str, reason: str):
	"""Return verified cartons to editable packing state when no transfer exists."""
	po = _get_locked_purchase_order(purchase_order)
	reason = _require_reason(reason)
	_reject_active_transfer(po)

	verified_cartons = frappe.get_all(
		"JD Carton",
		filters={"purchase_order": po.name, "verified": 1},
		pluck="name",
		order_by="carton_sequence",
	)
	if not verified_cartons:
		frappe.throw(_("No verified cartons were found for JD Purchase Order {0}.").format(po.name))

	for carton_name in verified_cartons:
		carton = frappe.get_doc("JD Carton", carton_name)
		carton.verified = 0
		carton.flags.jd_allow_rework = True
		carton.save()
	frappe.db.set_value(
		"JD Purchase Order",
		po.name,
		{"packing_status": "装箱中", "status": "装箱中"},
		update_modified=False,
	)
	_add_audit_comment(
		po,
		_("Undo Carton Verification"),
		reason,
		_("Returned {0} carton(s) to unverified state.").format(len(verified_cartons)),
	)
	return {"purchase_order": po.name, "unverified_cartons": len(verified_cartons), "packing_status": "装箱中"}


@frappe.whitelist()
def delete_draft_transfer_and_rework(purchase_order: str, reason: str, stock_entry: str | None = None):
	"""Delete only this PO's draft Stock Entry and restore the verified packing state."""
	po = _get_locked_purchase_order(purchase_order)
	reason = _require_reason(reason)
	entry = _get_linked_transfer_for_rework(po, stock_entry)

	if entry.docstatus != 0:
		frappe.throw(
			_("Stock Entry {0} is submitted. Cancel it through ERPNext before starting rework.").format(
				frappe.bold(entry.name)
			)
		)
	entry.check_permission("delete")

	entry_name = entry.name
	frappe.delete_doc("Stock Entry", entry_name)

	packing_status = "已装箱" if _packing_is_verified_and_complete(po.name) else "装箱中"
	order_status = "已装箱" if packing_status == "已装箱" else "装箱中"
	frappe.db.set_value(
		"JD Purchase Order",
		po.name,
		{
			"stock_entry": None,
			"transfer_status": "待调拨",
			"packing_status": packing_status,
			"status": order_status,
		},
		update_modified=False,
	)
	_add_audit_comment(
		po,
		_("Delete Draft Transfer and Rework"),
		reason,
		_("Deleted draft Stock Entry {0}.").format(entry_name),
	)
	return {
		"purchase_order": po.name,
		"deleted_stock_entry": entry_name,
		"transfer_status": "待调拨",
		"packing_status": packing_status,
	}


def _get_locked_purchase_order(purchase_order: str):
	if not purchase_order:
		frappe.throw(_("JD Purchase Order is required."))
	if not frappe.db.exists("JD Purchase Order", purchase_order):
		frappe.throw(_("JD Purchase Order {0} does not exist.").format(purchase_order))
	frappe.db.sql("select name from `tabJD Purchase Order` where name = %s for update", purchase_order)
	po = frappe.get_doc("JD Purchase Order", purchase_order)
	po.check_permission("write")
	return po


def _require_reason(reason: str | None) -> str:
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("A rework reason is required."))
	if len(reason) > 500:
		frappe.throw(_("The rework reason cannot exceed 500 characters."))
	return reason


def _get_active_transfers(purchase_order: str) -> list[frappe._dict]:
	return frappe.get_all(
		"Stock Entry",
		filters={"custom_jd_purchase_order": purchase_order, "docstatus": ["<", 2]},
		fields=["name", "docstatus"],
		order_by="creation",
	)


def _reject_active_transfer(po) -> None:
	active = _get_active_transfers(po.name)
	if not active:
		return
	entry = active[0]
	if entry.docstatus == 1:
		frappe.throw(
			_("Stock Entry {0} is submitted. Cancel it through ERPNext before starting rework.").format(
				frappe.bold(entry.name)
			)
		)
	frappe.throw(
		_("Draft Stock Entry {0} exists. Delete the draft transfer before changing packing.").format(
			frappe.bold(entry.name)
		)
	)


def _get_linked_transfer_for_rework(po, requested_stock_entry: str | None):
	active = _get_active_transfers(po.name)
	if len(active) > 1:
		frappe.throw(_("Multiple active Stock Entries are linked to this purchase order. Resolve them manually."))

	entry_name = requested_stock_entry or po.stock_entry
	if not entry_name:
		frappe.throw(_("No linked draft Stock Entry was found for JD Purchase Order {0}.").format(po.name))
	if po.stock_entry != entry_name:
		frappe.throw(_("Stock Entry {0} is not the Stock Entry recorded on this purchase order.").format(entry_name))
	if not frappe.db.exists("Stock Entry", entry_name):
		frappe.throw(_("Stock Entry {0} does not exist.").format(entry_name))

	entry = frappe.get_doc("Stock Entry", entry_name)
	if entry.custom_jd_purchase_order != po.name:
		frappe.throw(_("Stock Entry {0} does not belong to JD Purchase Order {1}.").format(entry.name, po.name))
	if active and active[0].name != entry.name:
		frappe.throw(_("The active Stock Entry linked to this purchase order is {0}.").format(active[0].name))
	return entry


def _packing_is_verified_and_complete(purchase_order: str) -> bool:
	return bool(frappe.db.exists("JD Carton", {"purchase_order": purchase_order})) and not frappe.db.exists(
		"JD Carton", {"purchase_order": purchase_order, "verified": 0}
	) and packing_quantities_match(purchase_order)


def _add_audit_comment(po, action: str, reason: str, detail: str) -> None:
	content = _("<b>{0}</b><br>Reason: {1}<br>{2}").format(
		escape(action), escape(reason), escape(detail)
	)
	po.add_comment("Info", content)

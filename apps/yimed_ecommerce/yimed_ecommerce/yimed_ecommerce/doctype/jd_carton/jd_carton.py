import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

from yimed_ecommerce.jd.product_bundle import expand_platform_item
from yimed_ecommerce.jd.packing import packing_quantities_match
from yimed_ecommerce.jd.shelf_life import apply_batch_shelf_life


class JDCarton(Document):
	def validate(self):
		self.validate_saved_state_invariants()
		self.validate_sequence()
		self.validate_allocations()
		self.validate_order_quantities()
		self.expand_components()
		self.validate_components()
		self.sku_count = len({row.jd_sku for row in self.allocations})

	def on_update(self):
		self.update_purchase_order_totals()

	def on_trash(self):
		self.validate_delete_invariants()
		self.update_purchase_order_totals(exclude_current=True)

	def validate_saved_state_invariants(self):
		"""Protect verified/transferred cartons even when saved outside custom APIs."""
		if self.is_new():
			from yimed_ecommerce.jd.workflow import validate_order_ready_for_packing

			validate_order_ready_for_packing(self.purchase_order)
			return
		previous = frappe.get_doc("JD Carton", self.name)
		if previous.purchase_order != self.purchase_order:
			frappe.throw(_("A carton's Purchase Order cannot be changed after creation."))

		verified_changed = int(previous.verified or 0) != int(self.verified or 0)
		allocations_changed = _child_signature(previous.allocations, "allocation") != _child_signature(
			self.allocations, "allocation"
		)
		components_changed = _child_signature(previous.components, "component") != _child_signature(
			self.components, "component"
		)
		header_changed = (
			int(previous.carton_sequence or 0) != int(self.carton_sequence or 0)
			or str(previous.packing_date or "") != str(self.packing_date or "")
		)
		protected_changed = verified_changed or allocations_changed or components_changed or header_changed
		if not protected_changed:
			return
		if not self.flags.get("jd_allow_rework"):
			from yimed_ecommerce.jd.workflow import validate_order_ready_for_packing

			validate_order_ready_for_packing(previous.purchase_order)

		active_transfer = get_active_transfer(previous.purchase_order)
		if active_transfer:
			frappe.throw(
				_("Stock Entry {0} is active; this carton's packing data cannot be changed.").format(
					frappe.bold(active_transfer.name)
				)
			)

		if int(previous.verified or 0):
			controlled_undo = (
				verified_changed
				and not int(self.verified or 0)
				and self.flags.get("jd_allow_rework")
				and not allocations_changed
				and not components_changed
				and not header_changed
			)
			if not controlled_undo:
				frappe.throw(_("Verified carton {0} cannot be changed directly.").format(frappe.bold(self.name)))

		if verified_changed and int(self.verified or 0):
			controlled_verify = (
				self.flags.get("jd_allow_verify")
				and not allocations_changed
				and not components_changed
				and not header_changed
			)
			if not controlled_verify:
				frappe.throw(_("Verify cartons through the controlled verification action."))

	def validate_delete_invariants(self):
		if self.is_new():
			return
		previous = frappe.db.get_value(
			"JD Carton", self.name, ["purchase_order", "verified"], as_dict=True
		)
		if not previous:
			return
		active_transfer = get_active_transfer(previous.purchase_order)
		if active_transfer:
			frappe.throw(
				_("Stock Entry {0} is active; carton {1} cannot be deleted.").format(
					frappe.bold(active_transfer.name), frappe.bold(self.name)
				)
			)
		if previous.verified:
			frappe.throw(_("Verified carton {0} cannot be deleted.").format(frappe.bold(self.name)))

	def validate_sequence(self):
		if self.carton_sequence <= 0:
			frappe.throw(_("Carton Sequence must be greater than zero."))
		duplicate = frappe.db.exists(
			"JD Carton",
			{
				"purchase_order": self.purchase_order,
				"carton_sequence": self.carton_sequence,
				"name": ["!=", self.name],
			},
		)
		if duplicate:
			frappe.throw(
				_("Carton Sequence {0} already exists for Purchase Order {1}.").format(
					self.carton_sequence, frappe.bold(self.purchase_order)
				)
			)

	def validate_allocations(self):
		if not self.allocations:
			frappe.throw(_("Every carton must contain at least one item."))

		order_items = {
			row.jd_sku: row
			for row in frappe.get_all(
				"JD Purchase Order Item",
				filters={"parent": self.purchase_order, "parenttype": "JD Purchase Order"},
				fields=["jd_sku", "platform_item", "purchase_uom"],
			)
		}
		for row in self.allocations:
			order_item = order_items.get(row.jd_sku)
			if not order_item:
				frappe.throw(
					_("JD SKU {0} does not belong to Purchase Order {1}.").format(
						frappe.bold(row.jd_sku), frappe.bold(self.purchase_order)
					)
				)
			if flt(row.qty) <= 0:
				frappe.throw(_("Row {0}: Quantity must be greater than zero.").format(row.idx))
			row.platform_item = order_item.platform_item
			row.uom = order_item.purchase_uom

	def expand_components(self):
		batch_by_key = {
			(row.jd_sku, row.stock_item): row.batch_no
			for row in self.components
			if row.jd_sku and row.stock_item and row.batch_no
		}
		self.set("components", [])
		for allocation in self.allocations:
			for component in expand_platform_item(allocation.platform_item, allocation.qty):
				self.append(
					"components",
					{
						"jd_sku": allocation.jd_sku,
						"platform_item": allocation.platform_item,
						"stock_item": component.stock_item,
						"qty": component.qty,
						"uom": component.stock_uom,
						"batch_no": batch_by_key.get((allocation.jd_sku, component.stock_item)),
					},
				)

	def validate_order_quantities(self):
		qty_by_sku = {}
		for row in self.allocations:
			qty_by_sku[row.jd_sku] = flt(qty_by_sku.get(row.jd_sku, 0) + row.qty, 6)
		for jd_sku, carton_qty in qty_by_sku.items():
			ordered = frappe.db.sql(
				"""select coalesce(sum(purchase_qty), 0)
				from `tabJD Purchase Order Item`
				where parent = %s and parenttype = 'JD Purchase Order' and jd_sku = %s""",
				(self.purchase_order, jd_sku),
			)[0][0]
			packed_elsewhere = frappe.db.sql(
				"""select coalesce(sum(a.qty), 0)
				from `tabJD Carton Allocation` a
				inner join `tabJD Carton` c on c.name = a.parent
				where c.purchase_order = %s and c.name != %s
				  and a.parenttype = 'JD Carton' and a.parentfield = 'allocations'
				  and a.jd_sku = %s""",
				(self.purchase_order, self.name or "", jd_sku),
			)[0][0]
			if flt(packed_elsewhere, 6) + carton_qty > flt(ordered, 6):
				frappe.throw(
					_(
						"Carton quantity would exceed ordered quantity for JD SKU {0}: "
						"ordered {1}, packed elsewhere {2}, this carton {3}."
					).format(
						frappe.bold(jd_sku),
						flt(ordered, 6),
						flt(packed_elsewhere, 6),
						carton_qty,
					)
				)

	def validate_components(self):
		for row in self.components:
			has_batch_no = frappe.get_cached_value("Item", row.stock_item, "has_batch_no")
			if has_batch_no and not row.batch_no:
				if self.verified:
					frappe.throw(
						_("Row {0}: Batch is required for stock Item {1} before verification.").format(
							row.idx, frappe.bold(row.stock_item)
						)
					)
				continue
			apply_batch_shelf_life(row, self.packing_date)

	def update_purchase_order_totals(self, exclude_current=False):
		if not self.purchase_order or not frappe.db.exists("JD Purchase Order", self.purchase_order):
			return
		filters = {"purchase_order": self.purchase_order}
		if exclude_current and self.name:
			filters["name"] = ["!=", self.name]
		total = frappe.db.count("JD Carton", filters=filters)
		frappe.db.set_value("JD Purchase Order", self.purchase_order, "total_cartons", total)
		frappe.db.set_value(
			"JD Purchase Order",
			self.purchase_order,
			"packing_status",
			"已装箱"
			if total
			and all_cartons_verified(self.purchase_order, self.name if exclude_current else None)
			and packing_quantities_match(self.purchase_order)
			else "装箱中",
		)

	@frappe.whitelist()
	def mark_printed(self):
		self.db_set("print_count", (self.print_count or 0) + 1)
		self.db_set("last_printed_by", frappe.session.user)
		self.db_set("last_printed_at", now_datetime())


def all_cartons_verified(purchase_order: str, exclude: str | None = None) -> bool:
	filters = {"purchase_order": purchase_order, "verified": 0}
	if exclude:
		filters["name"] = ["!=", exclude]
	return not frappe.db.exists("JD Carton", filters)


def get_active_transfer(purchase_order: str):
	return frappe.db.get_value(
		"Stock Entry",
		{"custom_jd_purchase_order": purchase_order, "docstatus": ["<", 2]},
		["name", "docstatus"],
		as_dict=True,
	)


def _child_signature(rows, kind: str) -> list[tuple]:
	if kind == "allocation":
		return [(row.jd_sku, row.platform_item, flt(row.qty, 6), row.uom) for row in rows]
	return [
		(
			row.jd_sku,
			row.platform_item,
			row.stock_item,
			flt(row.qty, 6),
			row.uom,
			row.batch_no,
		)
		for row in rows
	]

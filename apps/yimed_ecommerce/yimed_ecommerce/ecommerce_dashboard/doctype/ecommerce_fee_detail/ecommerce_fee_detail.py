from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class EcommerceFeeDetail(Document):
	def before_validate(self):
		if not self.source_unique_key and self.source_type:
			self.source_unique_key = f"manual|{frappe.generate_hash(length=32)}"

	def validate(self):
		if not self.currency and self.company:
			self.currency = frappe.db.get_value("Company", self.company, "default_currency")
		if flt(self.amount) < 0 and self.entry_type != "调整":
			frappe.throw(_("Only an Adjustment fee detail can contain a negative amount."))
		if bool(self.source_reference_doctype) != bool(self.source_reference_name):
			frappe.throw(_("Source document type and source document must be provided together."))
		if self.product_listing and self.allocation_method != "不分摊":
			frappe.throw(_("A directly attributed product fee cannot also be allocated."))
		if self.allocation_method == "固定比例":
			self._validate_fixed_ratios()
		elif self.allocation_ratios:
			frappe.throw(_("Fixed ratios can only be entered for the Fixed Ratio allocation method."))

	def _validate_fixed_ratios(self):
		seen = set()
		for row in self.allocation_ratios:
			if row.product_listing in seen:
				frappe.throw(_("Product listing {0} is repeated in fixed ratios.").format(row.product_listing))
			if flt(row.ratio) <= 0:
				frappe.throw(_("Fixed ratio in row {0} must be greater than zero.").format(row.idx))
			seen.add(row.product_listing)
		if not seen:
			frappe.throw(_("At least one fixed ratio is required."))

	def on_update(self):
		if self.replaces_fee_detail and self.status == "有效":
			frappe.db.set_value("Ecommerce Fee Detail", self.replaces_fee_detail, "status", "已替换")

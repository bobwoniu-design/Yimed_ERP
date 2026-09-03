from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class EcommerceFeeEstimateRule(Document):
	def validate(self):
		if flt(self.rate) < 0:
			frappe.throw(_("Rate cannot be negative."))
		if self.calculation_method == "按销售额比例" and flt(self.rate) > 100:
			frappe.throw(_("A percentage rate cannot exceed 100."))

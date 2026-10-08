import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class JDPurchaseOrder(Document):
	def validate(self):
		self.apply_sku_mappings()
		self.calculate_totals()

	def apply_sku_mappings(self):
		for row in self.items:
			mapping = frappe.db.get_value(
				"JD SKU Mapping",
				{"jd_sku": row.jd_sku, "disabled": 0},
				["platform_item"],
				as_dict=True,
			)
			row.platform_item = mapping.platform_item if mapping else None
			row.mapping_status = "已映射" if mapping else "未映射"

		self.mapping_status = "已映射" if all(row.mapping_status == "已映射" for row in self.items) else "待映射"

	def calculate_totals(self):
		self.total_purchase_qty = sum(flt(row.purchase_qty) for row in self.items)
		for row in self.items:
			row.purchase_amount = flt(row.purchase_qty) * flt(row.purchase_price)

	def require_complete_mapping(self):
		unmapped = sorted({row.jd_sku for row in self.items if row.mapping_status != "已映射"})
		if unmapped:
			frappe.throw(_("请先完成京东SKU映射，再继续操作：{0}").format(", ".join(unmapped)))


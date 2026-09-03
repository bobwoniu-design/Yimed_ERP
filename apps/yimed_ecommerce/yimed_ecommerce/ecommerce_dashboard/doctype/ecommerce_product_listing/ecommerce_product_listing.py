import frappe
from frappe import _
from frappe.model.document import Document


class EcommerceProductListing(Document):
	def validate(self):
		for field in ("channel_name", "platform_code", "store_id", "store_name", "platform_item_id"):
			self.set(field, frappe.utils.cstr(self.get(field)).strip())

		duplicate = frappe.db.exists(
			"Ecommerce Product Listing",
			{
				"company": self.company,
				"platform_code": self.platform_code,
				"store_id": self.store_id,
				"platform_item_id": self.platform_item_id,
				"name": ["!=", self.name or ""],
			},
		)
		if duplicate:
			frappe.throw(
				_("A product listing already exists for company {0}, platform {1}, store {2}, and listing ID {3}.").format(
					frappe.bold(self.company),
					frappe.bold(self.platform_code),
					frappe.bold(self.store_name or self.store_id),
					frappe.bold(self.platform_item_id),
				)
			)

		seen = set()
		for row in self.skus:
			row.platform_sku_id = frappe.utils.cstr(row.platform_sku_id).strip()
			row.outer_sku_id = frappe.utils.cstr(row.outer_sku_id).strip()
			key = (row.platform_sku_id, row.item_code)
			if key in seen:
				frappe.throw(_("Duplicate platform SKU and ERP item mapping in row {0}.").format(row.idx))
			seen.add(key)


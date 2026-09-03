import frappe
from frappe import _
from frappe.model.document import Document


class JDSelfOperatedSettings(Document):
	def validate(self):
		if self.default_transfer_mode == "两步调拨" and not self.default_transit_warehouse:
			frappe.throw(_("Default Transit Warehouse is required for a two-step transfer."))

		for fieldname in (
			"default_source_warehouse",
			"default_target_warehouse",
			"default_transit_warehouse",
		):
			warehouse = self.get(fieldname)
			if warehouse:
				validate_warehouse_company(warehouse, self.company)


def validate_warehouse_company(warehouse: str, company: str) -> None:
	warehouse_company, is_group, disabled = frappe.db.get_value(
		"Warehouse", warehouse, ["company", "is_group", "disabled"]
	) or (None, None, None)
	if warehouse_company != company:
		frappe.throw(
			_("Warehouse {0} does not belong to Company {1}.").format(
				frappe.bold(warehouse), frappe.bold(company)
			)
		)
	if is_group:
		frappe.throw(_("Warehouse {0} is a group warehouse.").format(frappe.bold(warehouse)))
	if disabled:
		frappe.throw(_("Warehouse {0} is disabled.").format(frappe.bold(warehouse)))

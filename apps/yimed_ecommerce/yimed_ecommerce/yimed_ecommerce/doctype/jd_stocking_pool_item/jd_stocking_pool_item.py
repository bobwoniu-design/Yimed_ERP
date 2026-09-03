import frappe
from frappe import _
from frappe.model.document import Document


class JDStockingPoolItem(Document):
	def validate(self):
		if not self.flags.get("jd_workflow_controlled"):
			frappe.throw(_("Stocking pool rows must be changed through the procurement workflow."))

	def on_trash(self):
		if not self.flags.get("jd_workflow_controlled"):
			frappe.throw(_("Stocking pool rows must be deleted through the procurement workflow."))

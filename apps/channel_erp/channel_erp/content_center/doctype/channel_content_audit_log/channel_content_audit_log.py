import frappe
from frappe import _
from frappe.model.document import Document


class ChannelContentAuditLog(Document):
	def before_save(self):
		if not self.is_new():
			frappe.throw(_("内容中心审计日志不可修改"))

	def on_trash(self):
		frappe.throw(_("内容中心审计日志不可删除"))

import frappe
from frappe import _
from frappe.model.document import Document


class ChannelContentSearchEvent(Document):
	def before_save(self):
		if not self.is_new():
			frappe.throw(_("搜索事件不可修改"))

	def on_trash(self):
		if not self.flags.get("allow_retention_delete"):
			frappe.throw(_("搜索事件仅可由保留期任务清理"))

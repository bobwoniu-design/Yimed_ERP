import frappe
from frappe import _
from frappe.model.document import Document


class ChannelContentSearchIndex(Document):
	def before_save(self):
		if not self.is_new():
			frappe.throw(_("全文索引不能直接修改，请使用索引服务重建"))

	def on_trash(self):
		if self.flags.get("allow_index_delete"):
			return
		frappe.throw(_("全文索引不能手工删除"))

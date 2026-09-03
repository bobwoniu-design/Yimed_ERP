import frappe
from frappe import _
from frappe.model.document import Document


class ChannelContentAccessGrant(Document):
	def before_save(self):
		if not self.is_new():
			frappe.throw(_("内容中心授权记录不能直接修改，请使用借阅管理接口"))

	def on_trash(self):
		frappe.throw(_("内容中心授权记录不可删除"))

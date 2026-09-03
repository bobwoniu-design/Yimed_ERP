import frappe
from frappe import _
from frappe.model.document import Document


class ChannelContentShareLink(Document):
	def before_save(self):
		if not self.is_new():
			frappe.throw(_("外部分享记录不能直接修改，请使用分享管理接口"))

	def on_trash(self):
		frappe.throw(_("外部分享记录不可删除"))

import frappe
from frappe import _
from frappe.model.document import Document


class ChannelContentSuggestion(Document):
	def before_save(self):
		if not self.is_new() and not self.flags.get("allow_suggestion_update"):
			frappe.throw(_("内容建议不能直接修改，请使用建议服务处理"))

	def on_trash(self):
		if not self.flags.get("allow_suggestion_delete"):
			frappe.throw(_("内容建议不能手工删除"))

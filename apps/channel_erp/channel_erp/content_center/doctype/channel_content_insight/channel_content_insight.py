import frappe
from frappe import _
from frappe.model.document import Document


class ChannelContentInsight(Document):
	def before_save(self):
		if not self.is_new() and not self.flags.get("allow_insight_update"):
			frappe.throw(_("内容洞察不能直接修改，请使用洞察服务重新生成"))

	def on_trash(self):
		if not self.flags.get("allow_insight_delete"):
			frappe.throw(_("内容洞察不能手工删除"))

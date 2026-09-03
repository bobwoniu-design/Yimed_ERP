import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class ChannelContentAccessLog(Document):
	def before_insert(self):
		self.last_accessed = self.last_accessed or now_datetime()
		self.access_count = self.access_count or 1

	def validate(self):
		self.company = frappe.db.get_value("Channel Content Asset", self.asset, "company")
		if not self.company:
			frappe.throw(_("访问的内容资产不存在"))
		if frappe.db.exists(
			"Channel Content Access Log",
			{"user": self.user, "asset": self.asset, "name": ["!=", self.name or ""]},
		):
			frappe.throw(_("同一用户和文件只能有一条访问记录"))


def on_doctype_update():
	frappe.db.add_unique("Channel Content Access Log", ["user", "asset"])
	frappe.db.add_index("Channel Content Access Log", ["user", "company", "last_accessed"])

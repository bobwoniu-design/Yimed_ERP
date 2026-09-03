import frappe
from frappe import _
from frappe.model.document import Document


class ChannelContentFavorite(Document):
	def validate(self):
		self.company = frappe.db.get_value("Channel Content Asset", self.asset, "company")
		if not self.company:
			frappe.throw(_("收藏的内容资产不存在"))
		if frappe.db.exists(
			"Channel Content Favorite",
			{"user": self.user, "asset": self.asset, "name": ["!=", self.name or ""]},
		):
			frappe.throw(_("该文件已经收藏"))


def on_doctype_update():
	frappe.db.add_unique("Channel Content Favorite", ["user", "asset"])
	frappe.db.add_index("Channel Content Favorite", ["user", "company"])

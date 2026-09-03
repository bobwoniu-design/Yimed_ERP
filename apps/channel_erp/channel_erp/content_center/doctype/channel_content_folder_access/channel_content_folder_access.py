import frappe
from frappe import _
from frappe.model.document import Document


class ChannelContentFolderAccess(Document):
	def validate(self):
		if self.subject_type == "Role":
			if not self.role:
				frappe.throw(_("请选择角色"))
			self.user = None
			subject = self.role
		else:
			if not self.user:
				frappe.throw(_("请选择用户"))
			self.role = None
			subject = self.user

		filters = {
			"folder": self.folder,
			"subject_type": self.subject_type,
			self.subject_type.lower(): subject,
			"name": ["!=", self.name or ""],
		}
		if frappe.db.exists("Channel Content Folder Access", filters):
			frappe.throw(_("该文件夹已经存在相同授权对象"))

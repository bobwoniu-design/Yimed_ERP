import frappe
from frappe import _
from frappe.utils.nestedset import NestedSet


class ChannelContentFolder(NestedSet):
	nsm_parent_field = "parent_channel_content_folder"

	def validate(self):
		self.folder_name = (self.folder_name or "").strip()
		if not self.folder_name:
			frappe.throw(_("文件夹名称不能为空"))

		filters = {
			"folder_name": self.folder_name,
			"parent_channel_content_folder": self.parent_channel_content_folder or ["is", "not set"],
			"name": ["!=", self.name or ""],
		}
		if frappe.db.exists("Channel Content Folder", filters):
			frappe.throw(_("同一层级已存在同名文件夹"))

	def on_update(self):
		super().on_update()

	def on_trash(self):
		if self.is_protected or not self.parent_channel_content_folder:
			frappe.throw(_("根目录或系统保护文件夹不能删除"))
		if frappe.db.exists("Channel Content Folder", {"parent_channel_content_folder": self.name}):
			frappe.throw(_("文件夹仍有下级目录，不能删除"))
		if frappe.db.exists("Channel Content Asset", {"folder": self.name}):
			frappe.throw(_("文件夹中仍有文件，不能删除"))
		super().on_trash()


def on_doctype_update():
	frappe.db.add_index("Channel Content Folder", ["lft", "rgt"])

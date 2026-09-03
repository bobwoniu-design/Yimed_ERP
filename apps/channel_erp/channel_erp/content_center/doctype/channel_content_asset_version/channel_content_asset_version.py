import frappe
from frappe import _
from frappe.model.document import Document


class ChannelContentAssetVersion(Document):
	def before_insert(self):
		if not self.version_number:
			latest_versions = frappe.get_all(
				"Channel Content Asset Version",
				filters={"asset": self.asset},
				pluck="version_number",
				order_by="version_number desc",
				limit=1,
			)
			latest = latest_versions[0] if latest_versions else 0
			self.version_number = latest + 1

		if frappe.db.exists("Channel Content Asset Version", {"file": self.file}):
			frappe.throw(_("该 Frappe 文件已经登记为内容版本"))

	def validate(self):
		if not self.is_new():
			before = self.get_doc_before_save()
			immutable = ("asset", "version_number", "file", "file_url", "original_filename", "content_hash")
			if before and any(before.get(field) != self.get(field) for field in immutable):
				frappe.throw(_("历史文件版本不可修改"))

	def on_trash(self):
		if self.flags.get("allow_content_center_delete"):
			return
		frappe.throw(_("历史文件版本不可删除"))

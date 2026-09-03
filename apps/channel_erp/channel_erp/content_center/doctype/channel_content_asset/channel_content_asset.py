import frappe
from frappe import _
from frappe.model.document import Document


class ChannelContentAsset(Document):
	def validate(self):
		stored_state = None if self.is_new() else frappe.db.get_value(
			self.doctype, self.name, ["content_locked", "publication_status"], as_dict=True
		)
		if stored_state and (
			stored_state.content_locked or (stored_state.publication_status or "Draft") == "Pending"
		):
			protected_fields = {
				"title",
				"folder",
				"status",
				"current_version",
				"file_name",
				"file_type",
				"mime_type",
				"file_size",
				"rating",
				"tags",
				"notes",
				"responsible_user",
				"department",
				"expires_on",
				"content_locked",
				"locked_by",
				"locked_at",
				"publication_status",
				"submitted_by",
				"submitted_at",
				"reviewed_by",
				"reviewed_at",
				"review_note",
			}
			if any(self.has_value_changed(fieldname) for fieldname in protected_fields):
				frappe.throw(_("文件已锁定或正在审批中，请先解除限制后再修改"))

		if not 0 <= (self.rating or 0) <= 5:
			frappe.throw(_("星级必须在 0 到 5 之间"))

		if self.responsible_user:
			if not frappe.db.get_value("User", self.responsible_user, "enabled"):
				frappe.throw(_("请选择有效且启用的责任人"))

		if self.department and not frappe.db.exists("Department", self.department):
			frappe.throw(_("请选择有效部门"))

	def after_insert(self):
		from channel_erp.tag_index import sync_asset_tags
		sync_asset_tags(self.name)

	def on_update(self):
		from channel_erp.tag_index import sync_asset_tags
		sync_asset_tags(self.name)

	def on_trash(self):
		if self.flags.get("allow_content_center_delete"):
			return
		frappe.throw(_("内容资产不能直接删除，请先移入回收站"))


def has_permission(doc, ptype=None, user=None):
	from channel_erp.content_center import can_access_folder

	action = "can_download" if ptype in {"read", "print"} else "can_manage"
	return can_access_folder(doc.folder, action, user=user, raise_exception=False)

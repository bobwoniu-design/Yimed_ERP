import frappe
from frappe import _


def protect_content_asset_file(doc, method=None):
	if doc.flags.get("allow_content_center_delete"):
		return
	if doc.attached_to_doctype == "Channel Content Asset":
		frappe.throw(_("内容中心文件不能从附件侧直接删除，请在内容中心中管理版本或文件"))


def handle_attachment_deleted(doc, method=None):
	# Historical mapping identifiers are retained as read-only audit context.
	# Deleting an ERP File must never delete the content relation or other user data.
	return

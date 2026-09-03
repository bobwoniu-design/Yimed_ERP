"""Upgrade-safe integrity checks for Channel ERP extensions."""

import frappe
from frappe import _
from frappe.utils import cint


CORE_DOCTYPES = {
	"Channel Content Folder": ("folder_name", "parent_channel_content_folder", "is_protected"),
	"Channel Content Folder Access": ("folder", "subject_type", "can_view", "can_download"),
	"Channel Content Asset": ("title", "folder", "status", "current_version"),
	"Channel Content Asset Version": ("asset", "file", "version_number"),
}

RETIRED_DOCTYPES = (
	"Channel Content Asset Company Access",
	"Channel Content Batch Company Membership",
	"Channel Content Batch Report",
	"Channel Content Batch Report Requirement",
	"Channel Content Archive Source",
	"Channel Content Archive Template",
	"Channel Content Document Folder Setting",
	"Channel Content Relation",
)

PUBLIC_METHODS = (
	"channel_erp.content_center.get_workspace",
	"channel_erp.content_center.create_folder",
	"channel_erp.content_center.register_uploaded_file",
	"channel_erp.content_center.get_asset_detail",
	"channel_erp.content_center.get_batch_inspection_report",
	"channel_erp.content_center.set_batch_inspection_report",
	"channel_erp.content_center.delete_batch_inspection_report",
	"channel_erp.content_center.export_delivery_note_inspection_reports_zip",
)


@frappe.whitelist()
def run_upgrade_checks(company=None, fail_on_error=0):
	errors = []
	warnings = []
	for doctype, fields in CORE_DOCTYPES.items():
		if not frappe.db.exists("DocType", doctype):
			errors.append(_("缺少DocType：{0}").format(doctype))
			continue
		meta = frappe.get_meta(doctype)
		for fieldname in fields:
			if not meta.has_field(fieldname):
				errors.append(_("{0} 缺少字段 {1}").format(doctype, fieldname))
		if meta.has_field("company") or frappe.db.has_column(doctype, "company"):
			errors.append(_("{0} 仍残留公司字段").format(doctype))
	for doctype in RETIRED_DOCTYPES:
		if frappe.db.exists("DocType", doctype):
			errors.append(_("仍残留已停用DocType：{0}").format(doctype))
	for method in PUBLIC_METHODS:
		try:
			frappe.get_attr(method)
		except Exception:
			errors.append(_("接口不可用：{0}").format(method))
	field = frappe.db.get_value("Custom Field", "Batch-custom_inspection_report_attachment", ["label", "fieldtype"], as_dict=True)
	if not field or field.label != "检测报告附件" or field.fieldtype != "Attach":
		errors.append(_("Batch检测报告附件字段未正确安装"))
	try:
		from channel_erp.content_center import get_content_health
		health = get_content_health()
		if health.get("broken_versions"):
			warnings.append(_("内容中心存在异常版本"))
	except Exception:
		errors.append(_("内容中心健康检查失败"))
	result = {"status": "passed" if not errors else "failed", "errors": errors, "warnings": warnings}
	if errors and cint(fail_on_error):
		frappe.throw("\n".join(errors))
	return result

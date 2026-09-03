from __future__ import annotations

import frappe
from frappe.model.document import Document


class EcommercePromotionImportBatch(Document):
	def on_trash(self):
		frappe.db.delete("Ecommerce Promotion Performance", {"import_batch": self.name})
		frappe.db.delete("Ecommerce Promotion Expense", {"import_batch": self.name})


@frappe.whitelist()
def import_expenses(name: str):
	batch = frappe.get_doc("Ecommerce Promotion Import Batch", name)
	return batch_import(batch)


@frappe.whitelist()
def create_and_import_batch(file_url: str, sales_channel: str):
	"""Create a traceable batch and import it from the expense list dialog.

	公司由所选销售渠道带出，不再单独传。
	"""
	frappe.has_permission("Ecommerce Promotion Import Batch", "create", throw=True)
	file_url = frappe.utils.cstr(file_url).strip()
	if not file_url:
		frappe.throw("请上传推广费文件。")
	sales_channel = frappe.utils.cstr(sales_channel).strip()
	channel = frappe.db.get_value(
		"Jackyun Sales Channel", sales_channel,
		["channel_name", "company_name", "disabled", "deleted"], as_dict=True,
	) if sales_channel else None
	if not channel or channel.disabled or channel.deleted:
		frappe.throw("请选择有效的销售渠道。")
	if not channel.company_name or not frappe.db.exists("Company", channel.company_name):
		frappe.throw("所选销售渠道未关联有效公司，请先在渠道档案中维护公司。")
	batch = frappe.get_doc(
		{
			"doctype": "Ecommerce Promotion Import Batch",
			"company": channel.company_name,
			"import_file": file_url,
			"sales_channel": sales_channel,
		}
	).insert()
	try:
		batch_import(batch)
	except Exception:
		batch.db_set("status", "导入失败")
		raise
	batch.reload()
	return {
		"name": batch.name,
		"status": batch.status,
		"total_rows": batch.total_rows,
		"success_count": batch.success_count,
		"failed_count": batch.failed_count,
		"total_amount": batch.total_amount,
		"error_log": batch.error_log,
	}


def batch_import(batch):
	from yimed_ecommerce.ecommerce_dashboard.promotion_import import import_promotion_expenses

	return import_promotion_expenses(batch)

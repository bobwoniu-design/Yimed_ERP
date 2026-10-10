import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import make_autoname
from frappe.utils import flt

from yimed_ecommerce.jd.import_purchase_orders import import_purchase_orders


class JDPurchaseImportBatch(Document):
	def autoname(self):
		self.batch_code = _normalise_batch_code(self.batch_code or get_default_batch_code())
		self.name = self.batch_code

	def validate(self):
		if not self.batch_code:
			self.batch_code = self.name
		self.batch_code = _normalise_batch_code(self.batch_code)
		if not self.is_new() and self.name != self.batch_code:
			frappe.throw(_("批次号创建后不能修改，以免破坏采购单、装箱及交接数据的关联。"))

	@frappe.whitelist()
	def run_import(self):
		return import_purchase_orders(self)

	def on_trash(self):
		"""Delete import-only purchase orders, but never erase downstream operations."""
		purchase_orders = frappe.get_all(
			"JD Purchase Order", filters={"import_batch": self.name}, pluck="name"
		)
		blockers = _get_delete_blockers(self.name, purchase_orders)
		if blockers:
			frappe.throw(
				_("该批次已经产生后续作业，不能删除：{0}。请先通过返工流程清理这些记录。")
				.format("、".join(blockers)),
				title=_("不能删除导入批次"),
			)

		for purchase_order in purchase_orders:
			frappe.delete_doc(
				"JD Purchase Order",
				purchase_order,
				ignore_permissions=True,
			)


def update_report_shortage_count(import_batch: str) -> int:
	"""统计批次内回告数量低于采购数量的采购单数，写入批次列表汇总字段。

	触发时机：确认/取消批次备货（workflow）与采购单保存（JD Purchase Order.on_update）。
	"""
	count = frappe.db.sql(
		"select count(name) from `tabJD Purchase Order` "
		"where import_batch=%s and report_qty < total_purchase_qty - 0.000001",
		import_batch,
	)[0][0]
	if frappe.db.exists("JD Purchase Import Batch", import_batch):
		frappe.db.set_value(
			"JD Purchase Import Batch", import_batch, "report_shortage_orders", flt(count), update_modified=False
		)
	return flt(count)


@frappe.whitelist()
def get_default_batch_code():
	frappe.has_permission("JD Purchase Import Batch", "create", throw=True)
	return make_autoname("JD-IMP-.YYYY..MM..DD.-.###")


@frappe.whitelist()
def create_and_import_batch(company: str, file_url: str, batch_code: str | None = None):
	"""Create one traceable import batch and immediately process its workbook."""
	batch = frappe.get_doc(
		{
			"doctype": "JD Purchase Import Batch",
			"batch_code": batch_code,
			"company": company,
			"import_file": file_url,
		}
	).insert()
	result = import_purchase_orders(batch)
	return {"batch_name": batch.name, **result}


def _normalise_batch_code(value: str) -> str:
	value = (value or "").strip()
	if not value:
		frappe.throw(_("批次号不能为空。"))
	if len(value) > 140:
		frappe.throw(_("批次号不能超过 140 个字符。"))
	if any(character in value for character in ("/", "\\", "#", "?", "%")):
		frappe.throw(_("批次号不能包含 /、\\、#、? 或 %。"))
	return value


def _get_delete_blockers(import_batch: str, purchase_orders: list[str]) -> list[str]:
	checks = [
		("JD Stocking Pool Item", {"import_batch": import_batch}, _("备货记录")),
		("JD Sorting Allocation", {"import_batch": import_batch}, _("分拣记录")),
		("JD Handover", {"import_batch": import_batch}, _("上门交接单")),
	]
	if purchase_orders:
		checks.extend(
			[
				("JD Carton", {"purchase_order": ["in", purchase_orders]}, _("装箱记录")),
				(
					"Stock Entry",
					{"custom_jd_purchase_order": ["in", purchase_orders], "docstatus": ["<", 2]},
					_("调拨单"),
				),
			]
		)
	return [label for doctype, filters, label in checks if frappe.db.exists(doctype, filters)]

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class JDHandover(Document):
	def validate(self):
		self.validate_purchase_orders()
		self.total_cartons = sum(row.carton_count or 0 for row in self.purchase_orders)
		self.actual_received_cartons = sum(row.actual_received_cartons or 0 for row in self.purchase_orders)

	def validate_purchase_orders(self):
		seen = set()
		for row in self.purchase_orders:
			if row.purchase_order in seen:
				frappe.throw(_("Purchase Order {0} is listed more than once.").format(row.purchase_order))
			seen.add(row.purchase_order)
			po = frappe.db.get_value(
				"JD Purchase Order",
				row.purchase_order,
				["name", "import_batch", "packing_status", "total_cartons", "destination_city"],
				as_dict=True,
			)
			if not po or po.import_batch != self.import_batch:
				frappe.throw(_("Purchase Order {0} does not belong to Import Batch {1}.").format(row.purchase_order, self.import_batch))
			issues = _handover_issues(po)
			if issues:
				frappe.throw(
					_("京东采购单 {0} 尚不能交接：{1}").format(
						frappe.bold(row.purchase_order), "；".join(issues)
					)
				)


@frappe.whitelist()
def create_from_import_batch(import_batch: str):
	batch = frappe.get_doc("JD Purchase Import Batch", import_batch)
	batch.check_permission("read")
	handover = frappe.new_doc("JD Handover")
	handover.import_batch = batch.name
	handover.company = batch.company
	orders = frappe.get_all(
		"JD Purchase Order",
		filters={"import_batch": batch.name},
		fields=["name", "mapping_status", "packing_status", "total_cartons", "total_purchase_qty", "destination_city"],
		order_by="creation asc",
	)
	shippable_orders = [po for po in orders if flt(po.total_purchase_qty) > 0]
	if not shippable_orders:
		frappe.throw(_("导入批次 {0} 中没有采购数量大于0的采购单，无需创建上门交接单。").format(batch.name))

	invalid_orders = [(po, _handover_issues(po, include_mapping=True)) for po in shippable_orders]
	invalid_orders = [(po, issues) for po, issues in invalid_orders if issues]
	ready_orders = [po for po in shippable_orders if not _handover_issues(po, include_mapping=True)]
	if not ready_orders:
		details = "<br>".join(
			_("• {0}：{1}").format(frappe.bold(po.name), "；".join(issues))
			for po, issues in invalid_orders
		)
		frappe.throw(
			_("以下京东采购单尚未满足交接条件：<br>{0}").format(details),
			title=_("无法创建上门交接单"),
		)

	for po in ready_orders:
		handover.append("purchase_orders", {"purchase_order": po.name})
	handover.insert()
	for po in ready_orders:
		frappe.db.set_value("JD Purchase Order", po.name, "workflow_stage", "交接发运", update_modified=False)
	return handover.name


def _handover_issues(po, include_mapping=False):
	from yimed_ecommerce.jd.workflow import get_order_packing_gate

	issues = []
	import_batch = po.get("import_batch") or frappe.db.get_value("JD Purchase Order", po.get("name"), "import_batch")
	if import_batch and frappe.db.exists("JD Stocking Pool Item", {"import_batch": import_batch}):
		issues.extend(get_order_packing_gate(po.get("name"))["issues"])
	if include_mapping and po.get("mapping_status") != "已映射":
		issues.append(_("SKU尚未全部映射"))
	if po.get("packing_status") != "已装箱":
		issues.append(_("装箱状态为“{0}”").format(po.get("packing_status") or _("未设置")))
	if not po.get("total_cartons"):
		issues.append(_("总箱数为0"))
	if not po.get("destination_city"):
		issues.append(_("目的城市为空"))
	return issues

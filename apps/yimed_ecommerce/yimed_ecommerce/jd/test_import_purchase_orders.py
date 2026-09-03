import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.file_manager import save_file
from frappe.utils.xlsxutils import make_xlsx

from yimed_ecommerce.jd.import_purchase_orders import import_purchase_orders, validate_purchase_orders_file
from yimed_ecommerce.jd.test_product_bundle import make_item
from yimed_ecommerce.yimed_ecommerce.doctype.jd_purchase_import_batch.jd_purchase_import_batch import (
	create_and_import_batch,
)


class TestJDPurchaseOrderImport(IntegrationTestCase):
	def test_preflight_returns_counts_and_each_failure_reason(self):
		content = make_xlsx(
			[
				["采购单号", "商品编号", "商品名称", "采购数量", "采购单位"],
				["_TEST-JD-PREFLIGHT-1", "_SKU-1", "测试商品", 0, "Nos"],
				["_TEST-JD-PREFLIGHT-2", "", "缺少SKU", 2, "Nos"],
			],
			"JD Purchase Orders",
		).getvalue()
		file_doc = save_file("test_jd_preflight.xlsx", content, None, None, is_private=1)

		result = validate_purchase_orders_file(file_doc.file_url)

		self.assertEqual(result["total"], 2)
		self.assertEqual(result["item_row_count"], 2)
		self.assertEqual(result["success_count"], 0)
		self.assertEqual(result["failed_count"], 2)
		self.assertEqual(len(result["errors"]), 2)

	def test_import_only_batch_can_be_deleted_with_its_purchase_orders(self):
		company = frappe.db.get_value("Company", {}, "name")
		batch = frappe.get_doc(
			{
				"doctype": "JD Purchase Import Batch",
				"batch_code": "_TEST-JD-IMPORT-BATCH-DELETE",
				"company": company,
				"import_file": "/private/files/test-jd-delete.xlsx",
			}
		).insert()
		purchase_order = frappe.get_doc(
			{
				"doctype": "JD Purchase Order",
				"jd_purchase_order_no": "_TEST-JD-PO-BATCH-DELETE",
				"import_batch": batch.name,
				"company": company,
				"destination_city": "上海",
				"items": [{"jd_sku": "_TEST-DELETE", "purchase_qty": 1, "purchase_uom": "Nos"}],
			}
		).insert()

		frappe.delete_doc("JD Purchase Import Batch", batch.name, delete_permanently=True)

		self.assertFalse(frappe.db.exists("JD Purchase Import Batch", batch.name))
		self.assertFalse(frappe.db.exists("JD Purchase Order", purchase_order.name))

	def test_batch_with_stocking_records_cannot_be_deleted(self):
		company = frappe.db.get_value("Company", {}, "name")
		item = make_item("_Test JD Protected Batch Item", is_stock_item=1)
		warehouse = frappe.db.get_value(
			"Warehouse", {"company": company, "is_group": 0, "disabled": 0}, "name"
		)
		batch = frappe.get_doc(
			{
				"doctype": "JD Purchase Import Batch",
				"batch_code": "_TEST-JD-IMPORT-BATCH-PROTECTED",
				"company": company,
				"import_file": "/private/files/test-jd-protected.xlsx",
			}
		).insert()
		stocking = frappe.get_doc(
			{
				"doctype": "JD Stocking Pool Item",
				"import_batch": batch.name,
				"company": company,
				"stock_item": item,
				"warehouse": warehouse,
				"stocked_qty": 1,
			}
		)
		stocking.flags.jd_workflow_controlled = True
		stocking.insert()

		with self.assertRaisesRegex(frappe.ValidationError, "备货记录"):
			frappe.delete_doc("JD Purchase Import Batch", batch.name, delete_permanently=True)

		self.assertTrue(frappe.db.exists("JD Purchase Import Batch", batch.name))

		stocking.flags.jd_workflow_controlled = True
		stocking.delete(ignore_permissions=True, delete_permanently=True)

	def test_create_and_import_batch_from_list_action(self):
		item = make_item("_Test JD Batch List Item", is_stock_item=1)
		jd_sku = "_TEST-JD-SKU-BATCH-LIST"
		if not frappe.db.exists("JD SKU Mapping", jd_sku):
			frappe.get_doc(
				{"doctype": "JD SKU Mapping", "jd_sku": jd_sku, "platform_item": item}
			).insert()

		content = make_xlsx(
			[
				["采购单号", "商品编号", "商品名称", "采购数量", "采购单位", "配送中心"],
				["_TEST-JD-PO-BATCH-LIST", jd_sku, "批次列表测试商品", 2, "Nos", "上海"],
			],
			"JD Purchase Orders",
		).getvalue()
		file_doc = save_file("test_jd_batch_list.xlsx", content, None, None, is_private=1)
		result = create_and_import_batch(
			frappe.db.get_value("Company", {}, "name"), file_doc.file_url
		)

		self.assertEqual(result["success_count"], 1)
		self.assertEqual(result["failed_count"], 0)
		batch = frappe.get_doc("JD Purchase Import Batch", result["batch_name"])
		self.assertEqual(batch.status, "导入成功")
		self.assertEqual(batch.purchase_order_count, 1)
		self.assertEqual(
			frappe.db.get_value("JD Purchase Order", "_TEST-JD-PO-BATCH-LIST", "import_batch"),
			batch.name,
		)

	def test_import_groups_rows_and_applies_sku_mapping(self):
		item = make_item("_Test JD Import Item", is_stock_item=1)
		jd_sku = "_TEST-JD-SKU-IMPORT"
		if not frappe.db.exists("JD SKU Mapping", jd_sku):
			frappe.get_doc(
				{"doctype": "JD SKU Mapping", "jd_sku": jd_sku, "platform_item": item}
			).insert()

		content = make_xlsx(
			[
				["采购单号", "商品编号", "商品名称", "采购数量", "采购单位", "配送中心"],
				["_TEST-JD-PO-IMPORT-1", jd_sku, "测试商品", 3, "Nos", "上海"],
				["_TEST-JD-PO-IMPORT-1", jd_sku, "测试商品", 2, "Nos", "上海"],
				["_TEST-JD-PO-IMPORT-2", jd_sku, "测试商品", 4, "Nos", "成都"],
			],
			"JD Purchase Orders",
		).getvalue()
		file_doc = save_file("test_jd_purchase_orders.xlsx", content, None, None, is_private=1)
		batch = frappe.get_doc(
			{
				"doctype": "JD Purchase Import Batch",
				"company": frappe.db.get_value("Company", {}, "name"),
				"import_file": file_doc.file_url,
			}
		).insert()

		result = import_purchase_orders(batch)

		self.assertEqual(result["purchase_order_count"], 2)
		self.assertEqual(result["item_row_count"], 3)
		self.assertEqual(result["success_count"], 2)
		po = frappe.get_doc("JD Purchase Order", "_TEST-JD-PO-IMPORT-1")
		self.assertEqual(len(po.items), 2)
		self.assertEqual(po.mapping_status, "已映射")
		self.assertEqual(po.items[0].platform_item, item)
		self.assertEqual(po.destination_city, "上海")

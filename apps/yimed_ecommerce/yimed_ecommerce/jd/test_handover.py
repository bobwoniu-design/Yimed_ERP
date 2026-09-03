import frappe
from frappe.tests import IntegrationTestCase

from yimed_ecommerce.jd.packing import create_equal_cartons, verify_cartons
from yimed_ecommerce.jd.test_product_bundle import make_item
from yimed_ecommerce.yimed_ecommerce.doctype.jd_handover.jd_handover import (
	create_from_import_batch,
)


class TestJDHandover(IntegrationTestCase):
	def setUp(self):
		self.company = frappe.db.get_value("Company", {}, "name")
		self.item = make_item("_Test JD Handover Item", is_stock_item=1)
		self.jd_sku = "_TEST-JD-HANDOVER-SKU"
		if not frappe.db.exists("JD SKU Mapping", self.jd_sku):
			frappe.get_doc(
				{"doctype": "JD SKU Mapping", "jd_sku": self.jd_sku, "platform_item": self.item}
			).insert()
		self.batch = frappe.get_doc(
			{
				"doctype": "JD Purchase Import Batch",
				"company": self.company,
				"import_file": "/private/files/test-jd-handover.xlsx",
			}
		).insert()
		self.po = frappe.get_doc(
			{
				"doctype": "JD Purchase Order",
				"jd_purchase_order_no": f"_TEST-JD-HANDOVER-{self._testMethodName}",
				"import_batch": self.batch.name,
				"company": self.company,
				"destination_city": "上海",
				"items": [
					{
						"jd_sku": self.jd_sku,
						"jd_item_name": "交接测试商品",
						"purchase_qty": 2,
						"purchase_uom": "Nos",
					}
				],
			}
		).insert()

	def test_create_explains_each_unready_purchase_order(self):
		with self.assertRaises(frappe.ValidationError) as context:
			create_from_import_batch(self.batch.name)

		message = str(context.exception)
		self.assertIn(self.po.name, message)
		self.assertIn("装箱状态", message)
		self.assertIn("总箱数为0", message)

	def test_create_after_all_shippable_orders_are_packed(self):
		create_equal_cartons(self.po.name, self.jd_sku, total_qty=2, qty_per_carton=1)
		verify_cartons(self.po.name)

		handover_name = create_from_import_batch(self.batch.name)
		handover = frappe.get_doc("JD Handover", handover_name)

		self.assertEqual(len(handover.purchase_orders), 1)
		self.assertEqual(handover.purchase_orders[0].purchase_order, self.po.name)
		self.assertEqual(handover.total_cartons, 2)

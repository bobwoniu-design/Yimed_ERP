import frappe
from frappe.tests import IntegrationTestCase

from yimed_ecommerce.jd.packing import create_equal_cartons, verify_cartons
from yimed_ecommerce.jd.test_product_bundle import make_item


class TestJDPacking(IntegrationTestCase):
	def setUp(self):
		# 固定名测试数据：transfer/invariants 测试中的 Stock Entry submit 会 commit，
		# 破坏 IntegrationTestCase 的事务回滚，残留同名 PO 导致本类测试 Duplicate。
		super().setUp()
		for doctype, field in [
			("JD Purchase Order Item", "parent"),
			("JD Carton Allocation", "parent"),
			("JD Carton Item", "parent"),
			("JD Carton", "purchase_order"),
			("JD Purchase Order", "name"),
			("JD SKU Mapping", "jd_sku"),
		]:
			frappe.db.delete(doctype, {field: ["like", "_TEST-JD-PO-%"]})
			frappe.db.delete(doctype, {field: ["like", "_TEST-JD-EXCESS%"]})
			frappe.db.delete(doctype, {field: ["like", "_Test JD Packing%"]})
			frappe.db.delete(doctype, {field: ["like", "_Test JD Excess%"]})
			frappe.db.delete(doctype, {field: ["like", "_Test JD Duplicate SKU%"]})
		frappe.db.commit()
	def test_verification_requires_exact_total_across_duplicate_sku_rows(self):
		company = frappe.db.get_value("Company", {}, "name")
		item = make_item("_Test JD Complete Packing Item", is_stock_item=1)
		jd_sku = "_TEST-JD-COMPLETE-PACKING-SKU"
		if not frappe.db.exists("JD SKU Mapping", jd_sku):
			frappe.get_doc(
				{"doctype": "JD SKU Mapping", "jd_sku": jd_sku, "platform_item": item}
			).insert()

		batch = frappe.get_doc(
			{
				"doctype": "JD Purchase Import Batch",
				"company": company,
				"import_file": "/private/files/test-jd-complete-packing.xlsx",
			}
		).insert()
		po = frappe.get_doc(
			{
				"doctype": "JD Purchase Order",
				"jd_purchase_order_no": "_TEST-JD-PO-COMPLETE-PACKING",
				"import_batch": batch.name,
				"company": company,
				"items": [
					{"jd_sku": jd_sku, "purchase_qty": 2, "purchase_uom": "Nos"},
					{"jd_sku": jd_sku, "purchase_qty": 3, "purchase_uom": "Nos"},
				],
			}
		).insert()

		create_equal_cartons(po.name, jd_sku, total_qty=4, qty_per_carton=4)
		with self.assertRaisesRegex(frappe.ValidationError, "shortage 1"):
			verify_cartons(po.name)
		self.assertFalse(frappe.db.exists("JD Carton", {"purchase_order": po.name, "verified": 1}))
		po.reload()
		self.assertEqual(po.packing_status, "装箱中")

		create_equal_cartons(po.name, jd_sku, total_qty=1, qty_per_carton=1, start_sequence=2)
		result = verify_cartons(po.name)
		self.assertEqual(result["verified_count"], 2)
		po.reload()
		self.assertEqual(po.packing_status, "已装箱")
		self.assertEqual(po.status, "已装箱")

	def test_equal_packing_rejects_quantity_above_aggregated_order_total(self):
		company = frappe.db.get_value("Company", {}, "name")
		item = make_item("_Test JD Excess Packing Item", is_stock_item=1)
		jd_sku = "_TEST-JD-EXCESS-PACKING-SKU"
		if not frappe.db.exists("JD SKU Mapping", jd_sku):
			frappe.get_doc(
				{"doctype": "JD SKU Mapping", "jd_sku": jd_sku, "platform_item": item}
			).insert()
		batch = frappe.get_doc(
			{
				"doctype": "JD Purchase Import Batch",
				"company": company,
				"import_file": "/private/files/test-jd-excess-packing.xlsx",
			}
		).insert()
		po = frappe.get_doc(
			{
				"doctype": "JD Purchase Order",
				"jd_purchase_order_no": "_TEST-JD-PO-EXCESS-PACKING",
				"import_batch": batch.name,
				"company": company,
				"items": [
					{"jd_sku": jd_sku, "purchase_qty": 2, "purchase_uom": "Nos"},
					{"jd_sku": jd_sku, "purchase_qty": 3, "purchase_uom": "Nos"},
				],
			}
		).insert()

		with self.assertRaisesRegex(frappe.ValidationError, "exceed ordered quantity"):
			create_equal_cartons(po.name, jd_sku, total_qty=6, qty_per_carton=3)

	def test_equal_cartons_generate_remainder_and_bundle_components(self):
		company = frappe.db.get_value("Company", {}, "name")
		self.assertTrue(company)
		component = make_item("_Test JD Packing Component", is_stock_item=1)
		parent = make_item("_Test JD Packing Parent", is_stock_item=0)
		if not frappe.db.exists("Product Bundle", parent):
			frappe.get_doc(
				{
					"doctype": "Product Bundle",
					"new_item_code": parent,
					"items": [{"item_code": component, "qty": 2, "uom": "Nos"}],
				}
			).insert()

		jd_sku = "_TEST-JD-PACKING-SKU"
		if not frappe.db.exists("JD SKU Mapping", jd_sku):
			frappe.get_doc(
				{"doctype": "JD SKU Mapping", "jd_sku": jd_sku, "platform_item": parent}
			).insert()

		batch = frappe.get_doc(
			{
				"doctype": "JD Purchase Import Batch",
				"company": company,
				"import_file": "/private/files/test-jd-purchase.xlsx",
			}
		).insert()
		po = frappe.get_doc(
			{
				"doctype": "JD Purchase Order",
				"jd_purchase_order_no": "_TEST-JD-PO-PACKING",
				"import_batch": batch.name,
				"company": company,
				"destination_city": "上海",
				"items": [{"jd_sku": jd_sku, "purchase_qty": 5, "purchase_uom": "Nos"}],
			}
		).insert()

		result = create_equal_cartons(po.name, jd_sku, total_qty=5, qty_per_carton=2)
		self.assertEqual(result["carton_count"], 3)
		cartons = frappe.get_all(
			"JD Carton",
			filters={"purchase_order": po.name},
			fields=["name", "carton_sequence", "total_cartons"],
			order_by="carton_sequence",
		)
		self.assertEqual([row.carton_sequence for row in cartons], [1, 2, 3])
		self.assertEqual([row.total_cartons for row in cartons], [3, 3, 3])
		component_quantities = [
			frappe.db.get_value("JD Carton Item", {"parent": row.name, "stock_item": component}, "qty")
			for row in cartons
		]
		self.assertEqual(component_quantities, [4, 4, 2])

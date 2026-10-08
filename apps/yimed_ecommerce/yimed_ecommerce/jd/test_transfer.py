import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from erpnext.stock.doctype.stock_entry.stock_entry import make_stock_in_entry
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

from yimed_ecommerce.jd.packing import create_equal_cartons, verify_cartons
from yimed_ecommerce.jd.test_product_bundle import make_item
from yimed_ecommerce.jd.transfer import create_transfer, get_transfer_defaults


class TestJDTransfer(IntegrationTestCase):

	def setUp(self):
		super().setUp()
		company = frappe.db.get_value("Company", {}, "name")
		self._orig_settings = None
		if frappe.db.exists("JD Self Operated Settings", company):
			doc = frappe.get_doc("JD Self Operated Settings", company)
			self._orig_settings = {f: doc.get(f) for f in ("company", "default_source_warehouse", "default_target_warehouse", "default_transit_warehouse", "default_transfer_mode", "allow_warehouse_override")}
		for doctype, field in [
			("JD Purchase Order Item", "parent"),
			("JD Carton Allocation", "parent"),
			("JD Carton Item", "parent"),
			("JD Carton", "purchase_order"),
			("JD Purchase Order", "name"),
			("JD SKU Mapping", "jd_sku"),
			("JD Purchase Import Batch", "import_file"),
		]:
			frappe.db.delete(doctype, {field: ["like", "_TEST-JD%"]})
			frappe.db.delete(doctype, {field: ["like", "_Test JD%"]})
		frappe.db.delete("JD Purchase Import Batch", {"import_file": ["like", "/private/files/test-%"]})
		# 清理关联的测试库存单据（submit 会 commit 破坏回滚，导致草稿单残留阻塞后续用例）
		for se in frappe.get_all("Stock Entry", filters={"custom_jd_purchase_order": ["like", "_TEST-JD-PO-%"]}, pluck="name"):
			docstatus = frappe.db.get_value("Stock Entry", se, "docstatus")
			if docstatus == 1:
				frappe.get_doc("Stock Entry", se).cancel()
			frappe.db.delete("Stock Ledger Entry", {"voucher_no": se})
			frappe.db.delete("Stock Entry Detail", {"parent": se})
			frappe.db.delete("Stock Entry", {"name": se})
		frappe.db.commit()

	def _cleanup_leaked_test_data(self):
		for doctype, field in [
			("JD Purchase Order Item", "parent"),
			("JD Carton Allocation", "parent"),
			("JD Carton Item", "parent"),
			("JD Carton", "purchase_order"),
			("JD Purchase Order", "name"),
			("JD SKU Mapping", "jd_sku"),
			("JD Purchase Import Batch", "import_file"),
		]:
			frappe.db.delete(doctype, {field: ["like", "_TEST-JD%"]})
			frappe.db.delete(doctype, {field: ["like", "_Test JD%"]})
		frappe.db.delete("JD Purchase Import Batch", {"import_file": ["like", "/private/files/test-%"]})
		frappe.db.commit()

	def tearDown(self):
		# 1) 恢复/清理京东自营设置（测试会改写第一个公司的设置）
		if getattr(self, "_orig_settings", None):
			doc = frappe.get_doc("JD Self Operated Settings", self._orig_settings["company"])
			doc.update(self._orig_settings)
			doc.save()
		elif frappe.db.exists("JD Self Operated Settings", frappe.db.get_value("Company", {}, "name")):
			frappe.delete_doc("JD Self Operated Settings", frappe.db.get_value("Company", {}, "name"), ignore_permissions=True)
		# 2) 清理本用例泄漏的测试数据（Stock Entry submit 破坏事务回滚）
		try:
			self._cleanup_leaked_test_data()
		except Exception:
			pass
		frappe.db.commit()
		super().tearDown()
	def test_company_defaults_are_returned_and_locked_on_server(self):
		company = frappe.db.get_value("Company", {}, "name")
		source, target = _get_two_warehouses(company)
		if frappe.db.exists("JD Self Operated Settings", company):
			settings = frappe.get_doc("JD Self Operated Settings", company)
			settings.update({
				"default_source_warehouse": source,
				"default_target_warehouse": target,
				"default_transfer_mode": "一步调拨",
				"allow_warehouse_override": 0,
			})
			settings.save()
		else:
			frappe.get_doc(
				{
					"doctype": "JD Self Operated Settings",
					"company": company,
					"default_source_warehouse": source,
					"default_target_warehouse": target,
					"default_transfer_mode": "一步调拨",
					"allow_warehouse_override": 0,
				}
			).insert()

		item = make_item("_Test JD Locked Defaults Item", is_stock_item=1)
		jd_sku = "_TEST-JD-LOCKED-DEFAULTS-SKU"
		if not frappe.db.exists("JD SKU Mapping", jd_sku):
			frappe.get_doc(
				{"doctype": "JD SKU Mapping", "jd_sku": jd_sku, "platform_item": item}
			).insert()
		batch = frappe.get_doc(
			{
				"doctype": "JD Purchase Import Batch",
				"company": company,
				"import_file": "/private/files/test-jd-locked-defaults.xlsx",
			}
		).insert()
		po = frappe.get_doc(
			{
				"doctype": "JD Purchase Order",
				"jd_purchase_order_no": "_TEST-JD-PO-LOCKED-DEFAULTS",
				"import_batch": batch.name,
				"company": company,
				"items": [{"jd_sku": jd_sku, "purchase_qty": 1, "purchase_uom": "Nos"}],
			}
		).insert()
		create_equal_cartons(po.name, jd_sku, total_qty=1, qty_per_carton=1)
		verify_cartons(po.name)

		defaults = get_transfer_defaults(po.name)
		self.assertTrue(defaults["configured"])
		self.assertFalse(defaults["allow_warehouse_override"])
		self.assertEqual(defaults["default_source_warehouse"], source)
		self.assertEqual(defaults["default_target_warehouse"], target)

		with self.assertRaisesRegex(frappe.ValidationError, "locked defaults"):
			create_transfer(po.name, target, source, transfer_mode="一步调拨")

		stock_entry = frappe.get_doc(
			"Stock Entry", create_transfer(po.name, source, target, transfer_mode="一步调拨")
		)
		self.assertEqual(stock_entry.from_warehouse, source)
		self.assertEqual(stock_entry.to_warehouse, target)

	def test_two_step_company_settings_require_transit_warehouse(self):
		company = frappe.db.get_value("Company", {}, "name")
		source, target = _get_two_warehouses(company)
		if frappe.db.exists("JD Self Operated Settings", company):
			frappe.delete_doc("JD Self Operated Settings", company, ignore_permissions=True)
		settings = frappe.get_doc(
			{
				"doctype": "JD Self Operated Settings",
				"company": company,
				"default_source_warehouse": source,
				"default_target_warehouse": target,
				"default_transfer_mode": "两步调拨",
			}
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Transit Warehouse is required"):
			settings.insert()

	def test_transfer_revalidates_packed_quantities(self):
		company = frappe.db.get_value("Company", {}, "name")
		item = make_item("_Test JD Transfer Quantity Item", is_stock_item=1)
		jd_sku = "_TEST-JD-TRANSFER-QUANTITY-SKU"
		if not frappe.db.exists("JD SKU Mapping", jd_sku):
			frappe.get_doc(
				{"doctype": "JD SKU Mapping", "jd_sku": jd_sku, "platform_item": item}
			).insert()
		batch = frappe.get_doc(
			{
				"doctype": "JD Purchase Import Batch",
				"company": company,
				"import_file": "/private/files/test-jd-transfer-quantity.xlsx",
			}
		).insert()
		po = frappe.get_doc(
			{
				"doctype": "JD Purchase Order",
				"jd_purchase_order_no": "_TEST-JD-PO-TRANSFER-QUANTITY",
				"import_batch": batch.name,
				"company": company,
				"items": [{"jd_sku": jd_sku, "purchase_qty": 2, "purchase_uom": "Nos"}],
			}
		).insert()
		create_equal_cartons(po.name, jd_sku, total_qty=2, qty_per_carton=2)
		verify_cartons(po.name)

		allocation = frappe.db.get_value(
			"JD Carton Allocation", {"jd_sku": jd_sku, "parenttype": "JD Carton"}, "name"
		)
		frappe.db.set_value("JD Carton Allocation", allocation, "qty", 1, update_modified=False)
		with self.assertRaisesRegex(frappe.ValidationError, "shortage 1"):
			create_transfer(po.name, "Source Warehouse", "Target Warehouse")
		self.assertFalse(frappe.db.exists("Stock Entry", {"custom_jd_purchase_order": po.name}))

	def test_creates_one_step_material_transfer_from_verified_cartons(self):
		company = frappe.db.get_value("Company", {}, "name")
		item = make_item("_Test JD Transfer Item", is_stock_item=1)
		jd_sku = "_TEST-JD-TRANSFER-SKU"
		if not frappe.db.exists("JD SKU Mapping", jd_sku):
			frappe.get_doc(
				{"doctype": "JD SKU Mapping", "jd_sku": jd_sku, "platform_item": item}
			).insert()

		batch = frappe.get_doc(
			{
				"doctype": "JD Purchase Import Batch",
				"company": company,
				"import_file": "/private/files/test-jd-transfer.xlsx",
			}
		).insert()
		po = frappe.get_doc(
			{
				"doctype": "JD Purchase Order",
				"jd_purchase_order_no": "_TEST-JD-PO-TRANSFER",
				"import_batch": batch.name,
				"company": company,
				"items": [{"jd_sku": jd_sku, "purchase_qty": 2, "purchase_uom": "Nos"}],
			}
		).insert()
		create_equal_cartons(po.name, jd_sku, total_qty=2, qty_per_carton=2)
		verify_cartons(po.name)

		source, target = _get_two_warehouses(company)
		stock_entry_name = create_transfer(po.name, source, target, transfer_mode="一步调拨")
		stock_entry = frappe.get_doc("Stock Entry", stock_entry_name)

		self.assertEqual(stock_entry.docstatus, 0)
		self.assertEqual(stock_entry.purpose, "Material Transfer")
		self.assertEqual(stock_entry.custom_jd_purchase_order, po.name)
		self.assertEqual(len(stock_entry.items), 1)
		self.assertEqual(stock_entry.items[0].item_code, item)
		self.assertEqual(stock_entry.items[0].qty, 2)
		self.assertEqual(stock_entry.items[0].s_warehouse, source)
		self.assertEqual(stock_entry.items[0].t_warehouse, target)

	def test_two_step_transfer_reaches_transit_then_final_warehouse(self):
		company = "_Test Company"
		source = "Stores - _TC"
		transit = "Goods In Transit - _TC"
		final = "Finished Goods - _TC"
		item = make_item("_Test JD Two Step Transfer Item", is_stock_item=1)
		jd_sku = "_TEST-JD-TWO-STEP-SKU"
		if not frappe.db.exists("JD SKU Mapping", jd_sku):
			frappe.get_doc(
				{"doctype": "JD SKU Mapping", "jd_sku": jd_sku, "platform_item": item}
			).insert()

		batch = frappe.get_doc(
			{
				"doctype": "JD Purchase Import Batch",
				"company": company,
				"import_file": "/private/files/test-jd-two-step-transfer.xlsx",
			}
		).insert()
		po = frappe.get_doc(
			{
				"doctype": "JD Purchase Order",
				"jd_purchase_order_no": "_TEST-JD-PO-TWO-STEP",
				"import_batch": batch.name,
				"company": company,
				"items": [{"jd_sku": jd_sku, "purchase_qty": 3, "purchase_uom": "Nos"}],
			}
		).insert()
		create_equal_cartons(po.name, jd_sku, total_qty=3, qty_per_carton=3)
		verify_cartons(po.name)
		make_stock_entry(item_code=item, target=source, qty=3, basic_rate=10)

		outgoing_name = create_transfer(
			po.name,
			source,
			final,
			transfer_mode="两步调拨",
			transit_warehouse=transit,
		)
		outgoing = frappe.get_doc("Stock Entry", outgoing_name)
		self.assertEqual(outgoing.add_to_transit, 1)
		self.assertEqual(outgoing.items[0].t_warehouse, transit)
		outgoing.submit()
		po.reload()
		self.assertEqual(po.transfer_status, "在途")

		receipt = make_stock_in_entry(outgoing.name)
		receipt.to_warehouse = final
		for row in receipt.items:
			row.t_warehouse = final
		receipt.save().submit()

		po.reload()
		outgoing.reload()
		self.assertEqual(po.transfer_status, "已完成")
		self.assertEqual(po.status, "已完成")
		self.assertEqual(outgoing.per_transferred, 100)
		final_qty = frappe.db.get_value("Bin", {"item_code": item, "warehouse": final}, "actual_qty")
		self.assertEqual(flt(final_qty), 3)


def _get_two_warehouses(company):
	warehouses = frappe.get_all(
		"Warehouse",
		filters={"company": company, "is_group": 0, "disabled": 0},
		pluck="name",
		limit=2,
	)
	while len(warehouses) < 2:
		sequence = len(warehouses) + 1
		warehouse = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": f"_Test JD Transfer Warehouse {sequence}",
				"company": company,
			}
		).insert()
		warehouses.append(warehouse.name)
	return warehouses

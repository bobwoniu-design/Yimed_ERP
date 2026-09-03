import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, generate_hash, nowdate

from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

from yimed_ecommerce.jd.packing import create_equal_cartons, verify_cartons
from yimed_ecommerce.jd.rework import clear_unverified_cartons
from yimed_ecommerce.jd.test_product_bundle import make_item
from yimed_ecommerce.jd.test_transfer import _get_two_warehouses
from yimed_ecommerce.jd.transfer import validate_packing_complete
from yimed_ecommerce.jd.workflow import (
	auto_sort,
	confirm_stocking,
	get_batch_candidates,
	get_erp_stocking_summary,
	get_order_packing_gate,
	get_sorting_matrix,
	get_stocking_workbench,
	get_workflow_overview,
	get_workflow_purchase_order,
	update_sorting,
)
from yimed_ecommerce.yimed_ecommerce.doctype.jd_handover.jd_handover import create_from_import_batch


class TestJDWorkflow(IntegrationTestCase):
	def test_successfully_imported_batch_cannot_create_cartons_before_stocking_and_sorting(self):
		po, jd_sku, item, warehouse = _make_po("REAL-IMPORT-GATE", qty=2, stock_qty=2)
		frappe.db.set_value("JD Purchase Import Batch", po.import_batch, "status", "导入成功", update_modified=False)

		gate = get_order_packing_gate(po.name)
		self.assertFalse(gate["can_pack"])
		self.assertIn("Stocking has not been confirmed.", gate["issues"])
		self.assertIn("Sorting has not been completed.", gate["issues"])
		with self.assertRaisesRegex(frappe.ValidationError, "Stocking has not been confirmed"):
			create_equal_cartons(po.name, jd_sku, 2, 2)

		confirm_stocking(po.import_batch, [{"stock_item": item, "warehouse": warehouse, "stocked_qty": 2}])
		auto_sort(po.import_batch)
		self.assertTrue(get_order_packing_gate(po.name)["can_pack"])
		created = create_equal_cartons(po.name, jd_sku, 2, 2)
		self.assertEqual(created["carton_count"], 1)

	def test_unverified_carton_freezes_stocking_and_sorting_until_rework_clears_it(self):
		po, jd_sku, item, warehouse = _make_po("DOWNSTREAM-FREEZE", qty=2, stock_qty=2)
		stocking_rows = [{"stock_item": item, "warehouse": warehouse, "stocked_qty": 2}]
		confirm_stocking(po.import_batch, stocking_rows)
		auto_sort(po.import_batch)
		pool = frappe.db.get_value("JD Stocking Pool Item", {"import_batch": po.import_batch}, "name")
		sorting_rows = [{
			"purchase_order": po.name, "stocking_pool_item": pool, "jd_sku": jd_sku,
			"stock_item": item, "sorted_qty": 2,
		}]
		create_equal_cartons(po.name, jd_sku, 2, 2)

		for action in (
			lambda: confirm_stocking(po.import_batch, stocking_rows),
			lambda: auto_sort(po.import_batch),
			lambda: update_sorting(po.import_batch, sorting_rows),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "Clear downstream cartons through the rework flow"):
				action()

		clear_unverified_cartons(po.name, "重做批次备货和分拣")
		confirm_stocking(po.import_batch, stocking_rows)
		auto_sort(po.import_batch)
		pool = frappe.db.get_value("JD Stocking Pool Item", {"import_batch": po.import_batch}, "name")
		sorting_rows[0]["stocking_pool_item"] = pool
		result = update_sorting(po.import_batch, sorting_rows)
		self.assertEqual(result["sorting_status"], "分拣完成")

	def test_initial_stocking_workbench_exposes_expanded_requirements_without_pool_rows(self):
		po, _, item, _ = _make_po("INITIAL-WORKBENCH", qty=3)
		workbench = get_stocking_workbench(po.import_batch)
		self.assertEqual(workbench["pool_rows"], [])
		self.assertEqual(
			[(row["stock_item"], row["required_qty"], row["uom"]) for row in workbench["requirements"]],
			[(item, 3, frappe.get_cached_value("Item", item, "stock_uom"))],
		)
		placeholders = get_erp_stocking_summary(import_batch=po.import_batch)
		self.assertEqual(len(placeholders), 1)
		self.assertEqual(placeholders[0]["stock_item"], item)
		self.assertEqual(placeholders[0]["is_requirement_placeholder"], 1)

	def test_stocking_is_soft_reservation_then_sorting_opens_packing_gate(self):
		po, jd_sku, item, warehouse = _make_po("HAPPY", qty=5, stock_qty=10)
		before = frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty")

		stocking = confirm_stocking(po.import_batch, [{"stock_item": item, "warehouse": warehouse, "stocked_qty": 5}])
		self.assertEqual(stocking["stocking_status"], "备货完成")
		self.assertEqual(frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty"), before)
		self.assertFalse(frappe.db.exists("Stock Entry", {"remarks": ["like", f"%{po.name}%"]}))

		sorting = auto_sort(po.import_batch)
		self.assertEqual(sorting["sorting_status"], "分拣完成")
		self.assertEqual(sorting["shortage_qty"], 0)
		gate = get_order_packing_gate(po.name)
		self.assertTrue(gate["can_pack"])

		create_equal_cartons(po.name, jd_sku, 5, 5)
		verify_cartons(po.name)
		po.reload()
		self.assertEqual(po.workflow_stage, "装箱")

		workbench = get_workflow_purchase_order(po.name)
		self.assertEqual(workbench["purchase_order"]["actual_stocked_qty"], 5)
		self.assertEqual(workbench["purchase_order"]["actual_sorted_qty"], 5)
		self.assertEqual(len(workbench["stocking"]), 1)
		self.assertEqual(len(workbench["sorting"]), 1)

	def test_shortage_is_recorded_without_changing_original_order_quantity(self):
		po, _, item, warehouse = _make_po("SHORTAGE", qty=5, stock_qty=5)
		result = confirm_stocking(
			po.import_batch,
			[{"stock_item": item, "warehouse": warehouse, "stocked_qty": 3, "shortage_reason": "库位少货"}],
		)
		self.assertEqual(result["stocking_status"], "备货异常")
		self.assertEqual(result["shortage_qty"], 2)
		po.reload()
		self.assertEqual(po.total_purchase_qty, 5)
		# The stocking pool belongs to the import batch. Per-order actuals are only
		# known after sorting allocates that pool across purchase orders.
		self.assertEqual(po.actual_stocked_qty, 0)
		self.assertEqual(po.shortage_reason, "库位少货")

		sorting = auto_sort(po.import_batch)
		self.assertEqual(sorting["sorting_status"], "分拣异常")
		self.assertEqual(sorting["shortage_qty"], 2)
		po.reload()
		self.assertEqual(po.actual_stocked_qty, 3)
		self.assertEqual(po.shortage_qty, 2)
		self.assertFalse(get_order_packing_gate(po.name)["can_pack"])

	def test_soft_reservation_prevents_two_orders_from_claiming_same_stock(self):
		po_one, _, item, warehouse = _make_po("RESERVE-ONE", qty=6, stock_qty=10)
		po_two = _make_po("RESERVE-TWO", qty=6, item=item, warehouse=warehouse)[0]
		confirm_stocking(po_one.import_batch, [{"stock_item": item, "warehouse": warehouse, "stocked_qty": 6}])

		with self.assertRaisesRegex(frappe.ValidationError, "Insufficient available inventory"):
			confirm_stocking(po_two.import_batch, [{"stock_item": item, "warehouse": warehouse, "stocked_qty": 6}])
		self.assertFalse(frappe.db.exists("JD Stocking Pool Item", {"import_batch": po_two.import_batch}))

	def test_manual_sorting_and_batch_level_overview_keep_orders_independent(self):
		po, jd_sku, item, warehouse = _make_po("MANUAL", qty=4, stock_qty=8)
		other_po = _make_po("INDEPENDENT", qty=2, item=item, warehouse=warehouse, import_batch=po.import_batch)[0]
		confirm_stocking(po.import_batch, [{"stock_item": item, "warehouse": warehouse, "stocked_qty": 6}])
		pool = frappe.db.get_value("JD Stocking Pool Item", {"import_batch": po.import_batch}, "name")
		manual = update_sorting(po.import_batch, [
			{"purchase_order":po.name, "stocking_pool_item":pool, "jd_sku":jd_sku, "stock_item":item, "sorted_qty":4},
			{"purchase_order":other_po.name, "stocking_pool_item":pool, "jd_sku":other_po.items[0].jd_sku, "stock_item":item, "sorted_qty":2},
		])
		self.assertEqual(manual["sorting_status"], "分拣完成")

		overview = get_workflow_overview(po.import_batch)
		by_name = {row.name: row for row in overview["orders"]}
		self.assertEqual(by_name[po.name].workflow_stage, "分拣")
		self.assertEqual(by_name[other_po.name].workflow_stage, "分拣")

	def test_sorting_matrix_keeps_zero_allocated_order_in_requirements(self):
		po, jd_sku, item, warehouse = _make_po("MATRIX-A", qty=4, stock_qty=6)
		other_po = _make_po("MATRIX-B", qty=2, item=item, warehouse=warehouse, import_batch=po.import_batch)[0]
		confirm_stocking(po.import_batch, [{"stock_item": item, "warehouse": warehouse, "stocked_qty": 6}])
		pool = frappe.db.get_value("JD Stocking Pool Item", {"import_batch": po.import_batch}, "name")
		update_sorting(po.import_batch, [{
			"purchase_order": po.name, "stocking_pool_item": pool, "jd_sku": jd_sku,
			"stock_item": item, "sorted_qty": 4,
		}])

		matrix = get_sorting_matrix(po.import_batch)
		self.assertEqual({row["purchase_order"] for row in matrix["requirements"]}, {po.name, other_po.name})
		self.assertEqual({row["purchase_order"] for row in matrix["allocations"]}, {po.name})
		self.assertEqual(matrix["pools"][0]["stocked_qty"], 6)

	def test_auto_sort_allocates_one_batch_pool_across_two_purchase_orders(self):
		first, _, item, warehouse = _make_po("CROSS-AUTO-1", qty=2, stock_qty=5)
		second = _make_po("CROSS-AUTO-2", qty=3, item=item, warehouse=warehouse, import_batch=first.import_batch)[0]
		confirm_stocking(first.import_batch, [{"stock_item":item, "warehouse":warehouse, "stocked_qty":5}])
		result = auto_sort(first.import_batch)
		self.assertEqual(result["sorting_status"], "分拣完成")
		allocated = {
			name: sum(frappe.get_all("JD Sorting Allocation", filters={"purchase_order":name}, pluck="sorted_qty"))
			for name in (first.name, second.name)
		}
		self.assertEqual(allocated, {first.name:2, second.name:3})

	def test_workflow_write_apis_require_permission(self):
		po, _, item, warehouse = _make_po("PERMISSION", qty=2, stock_qty=2)
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				confirm_stocking(po.import_batch, [{"stock_item": item, "warehouse": warehouse, "stocked_qty": 2}])
			with self.assertRaises(frappe.PermissionError):
				get_stocking_workbench(po.import_batch)
			with self.assertRaises(frappe.PermissionError):
				get_sorting_matrix(po.import_batch)
		finally:
			frappe.set_user("Administrator")

	def test_batch_can_be_added_later_but_missing_or_low_shelf_life_blocks_packing(self):
		suffix = generate_hash(length=8)
		item = make_item(f"_Test JD Workflow Batch Item {suffix}", is_stock_item=1, has_batch_no=1)
		po, _, _, warehouse = _make_po("BATCH-GATE", qty=2, item=item)
		batch = frappe.get_doc({
			"doctype":"Batch", "batch_id":f"_TEST-JD-WF-BATCH-{suffix}", "item":item,
			"manufacturing_date":add_days(nowdate(), -100), "expiry_date":add_days(nowdate(), 10),
		}).insert()
		make_stock_entry(item_code=item, to_warehouse=warehouse, qty=2, rate=10, batch_no=batch.name)

		missing = confirm_stocking(po.import_batch, [{"stock_item":item, "warehouse":warehouse, "stocked_qty":2}])
		self.assertEqual(missing["stocking_status"], "备货中")
		auto_sort(po.import_batch)
		self.assertIn("Batch assignment is incomplete.", get_order_packing_gate(po.name)["issues"])

		confirmed = confirm_stocking(po.import_batch, [{"stock_item":item, "warehouse":warehouse, "batch_no":batch.name, "stocked_qty":2}])
		self.assertEqual(confirmed["stocking_status"], "备货完成")
		auto_sort(po.import_batch)
		self.assertFalse(get_order_packing_gate(po.name)["can_pack"])
		candidate = next(row for row in get_batch_candidates(item, warehouse) if row["batch_no"] == batch.name)
		self.assertFalse(candidate["eligible"])

	def test_shortage_gate_is_enforced_by_verification_handover_and_transfer(self):
		po, jd_sku, item, warehouse = _make_po("ALL-GATES", qty=2, stock_qty=2)
		confirm_stocking(po.import_batch, [{"stock_item":item, "warehouse":warehouse, "stocked_qty":1, "shortage_reason":"少货1件"}])
		auto_sort(po.import_batch)

		with self.assertRaisesRegex(frappe.ValidationError, "Stocking is not complete"):
			create_equal_cartons(po.name, jd_sku, 2, 2)
		with self.assertRaisesRegex(frappe.ValidationError, "Stocking is not complete"):
			verify_cartons(po.name)
		with self.assertRaisesRegex(frappe.ValidationError, "Stocking is not complete"):
			create_from_import_batch(po.import_batch)
		with self.assertRaisesRegex(frappe.ValidationError, "Stocking is not complete"):
			validate_packing_complete(po)


def _make_po(label, qty, stock_qty=0, item=None, warehouse=None, import_batch=None):
	suffix = generate_hash(length=8)
	company = frappe.db.get_value("Company", {}, "name")
	item = item or make_item(f"_Test JD Workflow Item {label} {suffix}", is_stock_item=1)
	if not warehouse:
		warehouse = _get_two_warehouses(company)[0]
	if stock_qty:
		make_stock_entry(item_code=item, target=warehouse, qty=stock_qty, basic_rate=10)
	jd_sku = f"_TEST-JD-WORKFLOW-{label}-{suffix}"
	frappe.get_doc({"doctype": "JD SKU Mapping", "jd_sku": jd_sku, "platform_item": item}).insert()
	if not import_batch:
		import_batch = frappe.get_doc({"doctype": "JD Purchase Import Batch", "company": company, "import_file": f"/private/files/test-jd-workflow-{suffix}.xlsx"}).insert().name
	po = frappe.get_doc({"doctype":"JD Purchase Order", "jd_purchase_order_no":f"_TEST-JD-PO-WORKFLOW-{label}-{suffix}", "import_batch":import_batch, "company":company, "destination_city":"上海", "items":[{"jd_sku":jd_sku, "purchase_qty":qty, "purchase_uom":"Nos"}]}).insert()
	return po, jd_sku, item, warehouse

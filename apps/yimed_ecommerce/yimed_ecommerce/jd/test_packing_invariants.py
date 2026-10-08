import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, generate_hash, nowdate

from yimed_ecommerce.jd.packing import (
	assign_batch_to_cartons,
	create_equal_cartons,
	create_mixed_cartons,
	generate_whole_carton_suggestions,
	verify_cartons,
)
from yimed_ecommerce.jd.rework import undo_carton_verification
from yimed_ecommerce.jd.test_product_bundle import make_item
from yimed_ecommerce.jd.test_transfer import _get_two_warehouses
from yimed_ecommerce.jd.transfer import create_transfer


class TestJDPackingInvariants(IntegrationTestCase):

	def setUp(self):
		super().setUp()
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
	def test_direct_save_and_delete_cannot_bypass_verified_state(self):
		po, jd_sku, _ = _make_po("DIRECT-VERIFIED", qty=2)
		carton_name = create_equal_cartons(po.name, jd_sku, 2, 2)["cartons"][0]

		# 装满即自动确认（待装=0）：直接 save 修改会先命中"已确认箱不可直改"保护
		carton = frappe.get_doc("JD Carton", carton_name)
		carton.allocations[0].qty = 3
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be changed directly|exceed ordered quantity"):
			carton.save()

		# 装满自动确认后 verified=1；绕过受控通道改数量/翻转/删除均被拦
		carton = frappe.get_doc("JD Carton", carton_name)
		carton.allocations[0].qty = 1
		with self.assertRaisesRegex(frappe.ValidationError, "Verified carton.*cannot be changed directly"):
			carton.save()

		carton = frappe.get_doc("JD Carton", carton_name)
		carton.verified = 0
		with self.assertRaisesRegex(frappe.ValidationError, "Verified carton.*cannot be changed directly"):
			carton.save()
		with self.assertRaisesRegex(frappe.ValidationError, "Verified carton.*cannot be deleted"):
			frappe.delete_doc("JD Carton", carton_name, ignore_permissions=True)

		undo_carton_verification(po.name, "Controlled rework remains supported")
		self.assertEqual(frappe.db.get_value("JD Carton", carton_name, "verified"), 0)

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
		# 测试中 Stock Entry submit 会 commit 破坏事务回滚；
		# setUp 只能清理上一轮残留，最后一个用例的数据靠 tearDown 兜底
		try:
			self._cleanup_leaked_test_data()
		except Exception:
			pass
		super().tearDown()

	def test_active_transfer_blocks_direct_changes_deletion_and_new_packing(self):
		po, jd_sku, _ = _make_po("ACTIVE-TRANSFER", qty=2, box_factor=1)
		carton_name = create_equal_cartons(po.name, jd_sku, 2, 2)["cartons"][0]
		verify_cartons(po.name)
		source, target = _get_two_warehouses(po.company)
		entry_name = create_transfer(po.name, source, target)

		carton = frappe.get_doc("JD Carton", carton_name)
		carton.allocations[0].qty = 1
		with self.assertRaisesRegex(frappe.ValidationError, f"Stock Entry.*{entry_name}.*active"):
			carton.save()
		with self.assertRaisesRegex(frappe.ValidationError, f"Stock Entry.*{entry_name}.*active"):
			frappe.delete_doc("JD Carton", carton_name, ignore_permissions=True)
		with self.assertRaisesRegex(frappe.ValidationError, "Draft Stock Entry.*Delete it"):
			create_equal_cartons(po.name, jd_sku, 1, 1, start_sequence=2)
		with self.assertRaisesRegex(frappe.ValidationError, "Draft Stock Entry.*Delete it"):
			generate_whole_carton_suggestions(po.name)

	def test_mixed_template_aggregates_duplicate_sku_before_quantity_check(self):
		po, jd_sku, _ = _make_po("DUPLICATE-SKU", qty=5)

		with self.assertRaisesRegex(frappe.ValidationError, "ordered 5.*new 6"):
			create_mixed_cartons(
				po.name,
				[{"jd_sku": jd_sku, "qty": 3}, {"jd_sku": jd_sku, "qty": 3}],
				repeat_count=1,
			)
		self.assertFalse(frappe.db.exists("JD Carton", {"purchase_order": po.name}))

	def test_batch_assignment_rejects_any_verified_carton_and_active_transfer(self):
		suffix = generate_hash(length=8)
		item = make_item(f"_Test JD Invariant Batch Item {suffix}", is_stock_item=1, has_batch_no=1)
		po, jd_sku, _ = _make_po("BATCH-LOCK", qty=2, item=item)
		create_equal_cartons(po.name, jd_sku, 2, 1)
		batch_one = _make_batch(item, f"_TEST-JD-INV-B1-{suffix}")
		batch_two = _make_batch(item, f"_TEST-JD-INV-B2-{suffix}")
		assign_batch_to_cartons(po.name, item, batch_one.name)
		verify_cartons(po.name, start_sequence=1, end_sequence=1)

		# 待装=0 即装箱完成（无独立确认环节）：未转移的已确认箱允许改批次，
		# 改动自动退回编辑态，装满后自动重新确认
		result = assign_batch_to_cartons(po.name, item, batch_two.name, only_empty=0)
		self.assertGreater(result["updated_rows"], 0)

		source, target = _get_two_warehouses(po.company)
		create_transfer(po.name, source, target)
		with self.assertRaisesRegex(frappe.ValidationError, "Draft Stock Entry.*Delete it"):
			assign_batch_to_cartons(po.name, item, batch_two.name, only_empty=0)


def _make_po(label: str, qty: int, item: str | None = None, box_factor: int | None = None):
	suffix = generate_hash(length=8)
	company = frappe.db.get_value("Company", {}, "name")
	item = item or make_item(f"_Test JD Invariant Item {label} {suffix}", is_stock_item=1)
	if box_factor:
		if not frappe.db.exists("UOM", "Box"):
			frappe.get_doc({"doctype": "UOM", "uom_name": "Box"}).insert()
		item_doc = frappe.get_doc("Item", item)
		item_doc.append("uoms", {"uom": "Box", "conversion_factor": box_factor})
		item_doc.save()
	jd_sku = f"_TEST-JD-INVARIANT-{label}-{suffix}"
	frappe.get_doc({"doctype": "JD SKU Mapping", "jd_sku": jd_sku, "platform_item": item}).insert()
	batch = frappe.get_doc(
		{
			"doctype": "JD Purchase Import Batch",
			"company": company,
			"import_file": f"/private/files/test-jd-invariant-{suffix}.xlsx",
		}
	).insert()
	po = frappe.get_doc(
		{
			"doctype": "JD Purchase Order",
			"jd_purchase_order_no": f"_TEST-JD-PO-INVARIANT-{label}-{suffix}",
			"import_batch": batch.name,
			"company": company,
			"items": [{"jd_sku": jd_sku, "purchase_qty": qty, "purchase_uom": "Nos"}],
		}
	).insert()
	return po, jd_sku, item


def _make_batch(item: str, batch_id: str):
	return frappe.get_doc(
		{
			"doctype": "Batch",
			"batch_id": batch_id,
			"item": item,
			"manufacturing_date": add_days(nowdate(), -10),
			"expiry_date": add_days(nowdate(), 100),
		}
	).insert()

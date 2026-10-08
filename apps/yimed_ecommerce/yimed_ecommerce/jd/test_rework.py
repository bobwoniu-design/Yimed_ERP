import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import generate_hash

from yimed_ecommerce.jd.packing import create_equal_cartons, verify_cartons
from yimed_ecommerce.jd.rework import (
	clear_unverified_cartons,
	delete_draft_transfer_and_rework,
	undo_carton_verification,
)
from yimed_ecommerce.jd.test_product_bundle import make_item
from yimed_ecommerce.jd.test_transfer import _get_two_warehouses
from yimed_ecommerce.jd.transfer import create_transfer


class TestJDRework(IntegrationTestCase):

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
	def test_clear_unverified_cartons_requires_reason_and_leaves_audit_comment(self):
		po, jd_sku = _make_purchase_order("CLEAR", qty=3)
		create_equal_cartons(po.name, jd_sku, total_qty=3, qty_per_carton=1)

		with self.assertRaisesRegex(frappe.ValidationError, "reason is required"):
			clear_unverified_cartons(po.name, "  ")

		# 待装=0 即装箱完成（装满自动确认）：clear 前需先受控撤销确认
		undo_carton_verification(po.name, "Packing plan changed")
		result = clear_unverified_cartons(po.name, "Packing plan changed")
		self.assertEqual(result["deleted_cartons"], 3)
		self.assertFalse(frappe.db.exists("JD Carton", {"purchase_order": po.name}))
		po.reload()
		self.assertEqual(po.packing_status, "待装箱")
		self.assertEqual(po.status, "待备货")
		self.assertTrue(_has_audit_comment(po.name, "Packing plan changed"))

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

	def test_clear_rejects_verified_cartons(self):
		po, jd_sku = _make_purchase_order("CLEAR-VERIFIED")
		create_equal_cartons(po.name, jd_sku, total_qty=2, qty_per_carton=2)
		verify_cartons(po.name)

		with self.assertRaisesRegex(frappe.ValidationError, "Verified cartons exist"):
			clear_unverified_cartons(po.name, "Need a fresh packing plan")

	def test_undo_verification_restores_editable_packing_and_audits(self):
		po, jd_sku = _make_purchase_order("UNDO")
		create_equal_cartons(po.name, jd_sku, total_qty=2, qty_per_carton=1)
		verify_cartons(po.name)

		result = undo_carton_verification(po.name, "Batch allocation needs correction")
		self.assertEqual(result["unverified_cartons"], 2)
		self.assertFalse(frappe.db.exists("JD Carton", {"purchase_order": po.name, "verified": 1}))
		po.reload()
		self.assertEqual(po.packing_status, "装箱中")
		self.assertEqual(po.status, "装箱中")
		self.assertTrue(_has_audit_comment(po.name, "Batch allocation needs correction"))

	def test_delete_only_own_draft_transfer_and_restore_ready_state(self):
		po, jd_sku = _make_purchase_order("DRAFT")
		create_equal_cartons(po.name, jd_sku, total_qty=2, qty_per_carton=2)
		verify_cartons(po.name)
		source, target = _get_two_warehouses(po.company)
		entry_name = create_transfer(po.name, source, target)

		result = delete_draft_transfer_and_rework(po.name, "Warehouse selection was wrong", entry_name)
		self.assertEqual(result["deleted_stock_entry"], entry_name)
		self.assertFalse(frappe.db.exists("Stock Entry", entry_name))
		po.reload()
		self.assertIsNone(po.stock_entry)
		self.assertEqual(po.transfer_status, "待调拨")
		self.assertEqual(po.packing_status, "已装箱")
		self.assertEqual(po.status, "已装箱")
		self.assertTrue(_has_audit_comment(po.name, "Warehouse selection was wrong"))

	def test_rejects_other_or_submitted_stock_entry(self):
		po, jd_sku = _make_purchase_order("BOUNDARY")
		other_po, _ = _make_purchase_order("OTHER")
		create_equal_cartons(po.name, jd_sku, total_qty=2, qty_per_carton=2)
		verify_cartons(po.name)
		source, target = _get_two_warehouses(po.company)
		entry_name = create_transfer(po.name, source, target)
		with self.assertRaisesRegex(frappe.ValidationError, "Draft Stock Entry.*Delete the draft"):
			undo_carton_verification(po.name, "Transfer draft must be handled first")

		with self.assertRaisesRegex(frappe.ValidationError, "not the Stock Entry recorded"):
			delete_draft_transfer_and_rework(po.name, "Wrong reference test", "NOT-A-REAL-ENTRY")
		with self.assertRaisesRegex(frappe.ValidationError, "not the Stock Entry recorded"):
			delete_draft_transfer_and_rework(other_po.name, "Wrong purchase order", entry_name)

		frappe.db.set_value("Stock Entry", entry_name, "docstatus", 1, update_modified=False)
		with self.assertRaisesRegex(frappe.ValidationError, "submitted.*Cancel it through ERPNext"):
			delete_draft_transfer_and_rework(po.name, "Need to revise submitted transfer", entry_name)
		with self.assertRaisesRegex(frappe.ValidationError, "submitted.*Cancel it through ERPNext"):
			undo_carton_verification(po.name, "Need to revise cartons after submission")

	def test_write_permission_is_required(self):
		po, jd_sku = _make_purchase_order("PERMISSION")
		create_equal_cartons(po.name, jd_sku, total_qty=2, qty_per_carton=2)
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				clear_unverified_cartons(po.name, "Guest must not rework")
		finally:
			frappe.set_user("Administrator")


def _make_purchase_order(label: str, qty: int = 2):
	suffix = generate_hash(length=8)
	company = frappe.db.get_value("Company", {}, "name")
	item = make_item(f"_Test JD Rework Item {label} {suffix}", is_stock_item=1)
	jd_sku = f"_TEST-JD-REWORK-{label}-{suffix}"
	frappe.get_doc({"doctype": "JD SKU Mapping", "jd_sku": jd_sku, "platform_item": item}).insert()
	batch = frappe.get_doc(
		{
			"doctype": "JD Purchase Import Batch",
			"company": company,
			"import_file": f"/private/files/test-jd-rework-{suffix}.xlsx",
		}
	).insert()
	po = frappe.get_doc(
		{
			"doctype": "JD Purchase Order",
			"jd_purchase_order_no": f"_TEST-JD-PO-REWORK-{label}-{suffix}",
			"import_batch": batch.name,
			"company": company,
			"items": [{"jd_sku": jd_sku, "purchase_qty": qty, "purchase_uom": "Nos"}],
		}
	).insert()
	return po, jd_sku


def _has_audit_comment(purchase_order: str, text: str) -> bool:
	return bool(
		frappe.db.exists(
			"Comment",
			{
				"reference_doctype": "JD Purchase Order",
				"reference_name": purchase_order,
				"comment_type": "Info",
				"content": ["like", f"%{text}%"],
			},
		)
	)

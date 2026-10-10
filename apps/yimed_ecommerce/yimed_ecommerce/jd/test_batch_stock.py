import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, generate_hash, nowdate

from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

from yimed_ecommerce.jd.batch_stock import (
	download_inspection_reports,
	get_batch_stock,
	get_inspection_report,
	save_inspection_report,
	set_inspection_attachment,
)
from yimed_ecommerce.jd.test_product_bundle import make_item
from yimed_ecommerce.jd.test_transfer import _get_two_warehouses


class TestJDBatchStock(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self._cleanup()

	def tearDown(self):
		# Stock Entry submit 会 commit 破坏事务回滚，只能显式清理
		try:
			self._cleanup()
		except Exception:
			pass
		super().tearDown()

	def _cleanup(self):
		frappe.db.delete("JD Batch Inspection Report", {"batch_no": ["like", "_TEST-JD-BS-BATCH%"]})
		frappe.db.delete("Jackyun Batch Inventory", {"item_code": ["like", "_Test JD Batch Stock%"]})
		frappe.db.delete("Batch", {"name": ["like", "_TEST-JD-BS-BATCH%"]})
		frappe.db.delete("Item", {"name": ["like", "_Test JD Batch Stock%"]})
		frappe.db.delete("Stock Ledger Entry", {"item_code": ["like", "_Test JD Batch Stock%"]})
		frappe.db.commit()

	def _make_batched_stock(self, batch_suffix="A", qty=10):
		"""批次库存走吉客云快照口径：直接插快照行，不再构造库存流水。"""
		suffix = generate_hash(length=8)
		company = frappe.db.get_value("Company", {}, "name")
		item = make_item(f"_Test JD Batch Stock {batch_suffix} {suffix}", is_stock_item=1, has_batch_no=1)
		warehouse = _get_two_warehouses(company)[0]
		batch_no = f"_TEST-JD-BS-BATCH-{batch_suffix}-{suffix}"
		# 检测报告以 Batch 主数据为 Link 目标，保留档案但不产生库存流水
		frappe.get_doc({
			"doctype": "Batch",
			"batch_id": batch_no,
			"item": item,
			"manufacturing_date": add_days(nowdate(), -100),
			"expiry_date": add_days(nowdate(), 400),
		}).insert()
		frappe.get_doc({
			"doctype": "Jackyun Batch Inventory",
			"item_code": item,
			"item_name": item,
			"warehouse_code": warehouse[:8],
			"warehouse": warehouse,
			"jackyun_warehouse_name": "TEST",
			"batch_no": batch_no,
			"quantity": qty,
			"locked_quantity": 0,
			"available_quantity": qty,
			"quantity_id": suffix,
			"snapshot_at": frappe.utils.now_datetime(),
			"production_date": add_days(nowdate(), -100),
			"expiry_date": add_days(nowdate(), 400),
		}).insert()
		return item, warehouse, batch_no

	def test_batch_stock_returns_rows_with_report_status(self):
		item, warehouse, batch_no = self._make_batched_stock("REPORT", qty=6)
		result = get_batch_stock(item_code=item, warehouse=warehouse)
		row = next((row for row in result["rows"] if row["batch_no"] == batch_no), None)
		self.assertIsNotNone(row)
		self.assertEqual(row["item_code"], item)
		self.assertEqual(row["actual_qty"], 6)
		self.assertEqual(row["soft_reserved_qty"], 0)
		self.assertEqual(row["available_qty"], 6)
		self.assertIsNone(row["inspection_report"])
		self.assertFalse(row["expired"])

	def test_attachment_upload_and_removal_manage_report_lifecycle(self):
		item, _, batch_no = self._make_batched_stock("ATTACH", qty=3)
		# 上传附件即维护检测报告，无需填写任何信息
		created = set_inspection_attachment(batch_no, "/private/files/report.pdf")
		self.assertTrue(created["has_attachment"])
		self.assertEqual(created["name"], batch_no)
		report = frappe.db.get_value("JD Batch Inspection Report", {"batch_no": batch_no},
			["report_attachment", "inspection_result", "item_code"], as_dict=True)
		self.assertEqual(report.report_attachment, "/private/files/report.pdf")
		self.assertEqual(report.item_code, item)
		self.assertFalse(report.inspection_result is None)

		# 已上传过滤生效（以附件为准）
		reported = get_batch_stock(item_code=item, only_reported=1)
		self.assertTrue(any(row["batch_no"] == batch_no for row in reported["rows"]))

		# 删除附件：无其他维护信息时报告记录一并删除，批次回到未上传状态
		removed = set_inspection_attachment(batch_no, "")
		self.assertTrue(removed["deleted"])
		self.assertFalse(frappe.db.exists("JD Batch Inspection Report", {"batch_no": batch_no}))
		unreported = get_batch_stock(item_code=item, only_unreported=1)
		self.assertTrue(any(row["batch_no"] == batch_no for row in unreported["rows"]))

	def test_attachment_removal_keeps_report_with_manual_details(self):
		_, _, batch_no = self._make_batched_stock("KEEP", qty=2)
		save_inspection_report({"batch_no": batch_no, "report_no": "R-KEEP", "inspection_result": "合格"})
		set_inspection_attachment(batch_no, "/private/files/report.pdf")
		removed = set_inspection_attachment(batch_no, "")
		self.assertFalse(removed["deleted"])
		report = get_inspection_report(batch_no)
		self.assertTrue(report["exists"])
		self.assertFalse(report["report"].report_attachment)

	def test_save_inspection_report_upserts_once_per_batch(self):
		item, _, batch_no = self._make_batched_stock("UPSERT", qty=3)
		created = save_inspection_report({
			"batch_no": batch_no, "report_no": "R-100", "inspection_result": "合格",
			"inspection_date": nowdate(),
		})
		self.assertEqual(created["name"], batch_no)
		self.assertEqual(created["item_code"], item)

		updated = save_inspection_report({
			"batch_no": batch_no, "report_no": "R-200", "inspection_result": "不合格",
			"inspection_date": nowdate(),
		})
		self.assertEqual(updated["name"], batch_no)
		data = get_inspection_report(batch_no)
		self.assertTrue(data["exists"])
		self.assertEqual(data["report"]["report_no"], "R-200")
		self.assertEqual(data["report"]["inspection_result"], "不合格")
		self.assertEqual(frappe.db.count("JD Batch Inspection Report", {"batch_no": batch_no}), 1)

	def test_save_inspection_report_rejects_unknown_batch(self):
		with self.assertRaises(frappe.ValidationError):
			save_inspection_report({"batch_no": "_TEST-JD-BS-NOT-EXIST", "inspection_result": "合格"})

	def test_download_inspection_reports_requires_uploaded_attachment(self):
		_, _, batch_no = self._make_batched_stock("PRINT", qty=2)
		# 未维护报告：拦截
		with self.assertRaisesRegex(frappe.ValidationError, "未上传检测报告附件"):
			download_inspection_reports([batch_no])
		# 有报告但无附件：同样拦截
		save_inspection_report({"batch_no": batch_no, "report_no": "R-NOFILE", "inspection_result": "合格"})
		with self.assertRaisesRegex(frappe.ValidationError, "未上传检测报告附件"):
			download_inspection_reports([batch_no])

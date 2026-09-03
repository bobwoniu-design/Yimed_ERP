import frappe
from frappe.tests import IntegrationTestCase

from yimed_ecommerce.jd.packing import create_equal_cartons
from yimed_ecommerce.jd.report_pdf import _validate_batch_access, build_summary_html
from yimed_ecommerce.jd.test_product_bundle import make_item


class TestJDSummaryPDF(IntegrationTestCase):
	def setUp(self):
		test_suffix = self._testMethodName.removeprefix("test_")
		self.company = frappe.db.get_value("Company", {}, "name")
		self.item = make_item("_Test JD Report PDF Item", is_stock_item=1)
		self.jd_sku = "_TEST-JD-REPORT-PDF-SKU"
		if not frappe.db.exists("JD SKU Mapping", self.jd_sku):
			frappe.get_doc(
				{"doctype": "JD SKU Mapping", "jd_sku": self.jd_sku, "platform_item": self.item}
			).insert()

		self.batch = frappe.get_doc(
			{
				"doctype": "JD Purchase Import Batch",
				"company": self.company,
				"import_file": "/private/files/test-jd-report.pdf.xlsx",
			}
		).insert()
		self.po = frappe.get_doc(
			{
				"doctype": "JD Purchase Order",
				"jd_purchase_order_no": f"_TEST-JD-PO-REPORT-PDF-{test_suffix}",
				"import_batch": self.batch.name,
				"company": self.company,
				"destination_city": "上海",
				"distribution_center": "上海配送中心",
				"items": [
					{
						"jd_sku": self.jd_sku,
						"jd_item_name": "测试商品<b>不可执行</b>",
						"purchase_qty": 4,
						"purchase_uom": "Nos",
					}
				],
			}
		).insert()

	def test_stocking_summary_html_uses_trusted_template_and_escapes_data(self):
		html = build_summary_html("stocking", {"import_batch": self.batch.name})

		self.assertIn("商品备货汇总表", html)
		self.assertIn(self.jd_sku, html)
		self.assertIn("A4", html)
		self.assertIn("page-width: 297mm", html)
		self.assertIn("table-header-group", html)
		self.assertIn("page-break-inside: avoid", html)
		self.assertIn("&lt;b&gt;不可执行&lt;/b&gt;", html)
		self.assertNotIn("<b>不可执行</b>", html)

	def test_stocking_summary_keeps_unmapped_purchase_rows_visible(self):
		unmapped_sku = f"_TEST-JD-UNMAPPED-{self.po.name}"
		self.po.append(
			"items",
			{
				"jd_sku": unmapped_sku,
				"jd_item_name": "待映射商品",
				"purchase_qty": 3,
				"purchase_uom": "Nos",
			},
		)
		self.po.save()

		html = build_summary_html("stocking", {"import_batch": self.batch.name})

		self.assertIn(unmapped_sku, html)
		self.assertIn("待映射商品", html)
		self.assertIn("未映射", html)

	def test_stocking_summary_ignores_zero_quantity_rows(self):
		self.po.append(
			"items",
			{
				"jd_sku": self.jd_sku,
				"jd_item_name": "零数量商品",
				"purchase_qty": 0,
				"purchase_uom": "Nos",
			},
		)
		self.po.save()

		html = build_summary_html("stocking", {"import_batch": self.batch.name})

		self.assertIn(self.jd_sku, html)
		self.assertNotIn("零数量商品", html)

	def test_warehouse_summary_html_contains_packing_rows(self):
		create_equal_cartons(self.po.name, self.jd_sku, total_qty=4, qty_per_carton=2)
		html = build_summary_html("warehouse", {"import_batch": self.batch.name})

		self.assertIn("分仓汇总表", html)
		self.assertIn(self.po.name, html)
		self.assertIn(self.item, html)
		self.assertIn("上海", html)

	def test_batch_access_rejects_unauthorized_user(self):
		original_user = frappe.session.user
		try:
			frappe.set_user("Guest")
			with self.assertRaises(frappe.PermissionError):
				_validate_batch_access(self.batch.name)
		finally:
			frappe.set_user(original_user)

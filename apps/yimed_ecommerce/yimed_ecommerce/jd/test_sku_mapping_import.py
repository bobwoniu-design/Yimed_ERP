import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.file_manager import save_file
from frappe.utils.xlsxutils import make_xlsx

from yimed_ecommerce.jd.test_product_bundle import make_item
from yimed_ecommerce.yimed_ecommerce.doctype.jd_sku_mapping.jd_sku_mapping import import_mapping_file


class TestJDSKUMappingImport(IntegrationTestCase):
	def test_imports_operational_price_fields_and_refreshes_purchase_order(self):
		item = make_item("_Test JD Full Mapping Item", is_stock_item=1)
		jd_sku = "_TEST-JD-FULL-MAPPING-SKU"
		company = frappe.db.get_value("Company", {}, "name")
		batch = frappe.get_doc(
			{
				"doctype": "JD Purchase Import Batch",
				"company": company,
				"import_file": "/private/files/test-full-mapping.xlsx",
			}
		).insert()
		po = frappe.get_doc(
			{
				"doctype": "JD Purchase Order",
				"jd_purchase_order_no": "_TEST-JD-PO-FULL-MAPPING",
				"import_batch": batch.name,
				"company": company,
				"items": [{"jd_sku": jd_sku, "jd_item_name": "待映射", "purchase_qty": 1}],
			}
		).insert()
		self.assertEqual(po.mapping_status, "待映射")

		content = make_xlsx(
			[
				["品牌", "三级类目", "SKU", "ERP编码", "ERP名称", "是否贴码", "采购价", "到手价", "成本价", "是否器械", "备注"],
				["医麦德", "家庭护理", jd_sku, item, "测试ERP名称", "是", 12.3, 15.8, 4.2, "否", "测试备注"],
			],
			"医麦德+爱科",
		).getvalue()
		file_doc = save_file("test_jd_full_mapping.xlsx", content, None, None, is_private=1)

		result = import_mapping_file(file_doc.file_url)

		mapping = frappe.get_doc("JD SKU Mapping", jd_sku)
		self.assertEqual(mapping.brand, "医麦德")
		self.assertEqual(mapping.third_level_category, "家庭护理")
		self.assertEqual(mapping.erp_name, "测试ERP名称")
		self.assertEqual(mapping.labeling_required, "是")
		self.assertEqual(mapping.purchase_price, 12.3)
		self.assertEqual(mapping.net_price, 15.8)
		self.assertEqual(mapping.cost_price, 4.2)
		self.assertEqual(mapping.is_medical_device, "否")
		self.assertEqual(mapping.remarks, "测试备注")
		self.assertEqual(result["purchase_orders_refreshed"], 1)
		po.reload()
		self.assertEqual(po.mapping_status, "已映射")
		self.assertEqual(po.items[0].platform_item, item)

	def test_imports_legacy_attachment_headers(self):
		item = make_item("_Test JD Legacy Mapping Item", is_stock_item=1)
		content = make_xlsx(
			[["名称", "链接主SKU", "SKU", "父记录"], ["测试", item, "_TEST-JD-LEGACY-SKU", None]],
			"SKU映射表",
		).getvalue()
		file_doc = save_file("test_jd_sku_mapping.xlsx", content, None, None, is_private=1)

		result = import_mapping_file(file_doc.file_url)

		self.assertEqual(result["created"], 1)
		self.assertEqual(result["failed"], 0)
		self.assertEqual(
			frappe.db.get_value("JD SKU Mapping", "_TEST-JD-LEGACY-SKU", "platform_item"), item
		)

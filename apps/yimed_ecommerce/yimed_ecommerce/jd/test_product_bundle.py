import frappe
from frappe.tests import IntegrationTestCase

from yimed_ecommerce.jd.product_bundle import expand_platform_item


class TestJDProductBundle(IntegrationTestCase):
	def test_expands_product_bundle_and_keeps_stock_item(self):
		component = make_item("_Test JD Bundle Component", is_stock_item=1)
		parent = make_item("_Test JD Bundle Parent", is_stock_item=0)
		if not frappe.db.exists("Product Bundle", parent):
			frappe.get_doc(
				{
					"doctype": "Product Bundle",
					"new_item_code": parent,
					"items": [{"item_code": component, "qty": 2, "uom": "Nos"}],
				}
			).insert()

		expanded = expand_platform_item(parent, 3)
		self.assertEqual(len(expanded), 1)
		self.assertEqual(expanded[0].stock_item, component)
		self.assertEqual(expanded[0].qty, 6)
		self.assertTrue(expanded[0].is_product_bundle)

		direct = expand_platform_item(component, 3)
		self.assertEqual(direct[0].stock_item, component)
		self.assertEqual(direct[0].qty, 3)
		self.assertFalse(direct[0].is_product_bundle)


def make_item(item_code, is_stock_item, has_batch_no=0):
	if frappe.db.exists("Item", item_code):
		frappe.db.set_value("Item", item_code, "has_batch_no", has_batch_no)
		frappe.clear_cache(doctype="Item")
		return item_code
	item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": item_group,
			"stock_uom": "Nos",
			"is_stock_item": is_stock_item,
			"has_batch_no": has_batch_no,
			"is_sales_item": 1,
		}
	).insert()
	return item_code

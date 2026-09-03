from datetime import date

import frappe
from frappe.tests import IntegrationTestCase

from yimed_ecommerce.jd.shelf_life import apply_batch_shelf_life
from yimed_ecommerce.jd.test_product_bundle import make_item


class TestJDShelfLife(IntegrationTestCase):
	def test_fetches_batch_dates_and_accepts_two_thirds_remaining(self):
		item = make_item("_Test JD Batch Item", is_stock_item=1, has_batch_no=1)
		batch_name = "_TEST-JD-BATCH-VALID"
		if not frappe.db.exists("Batch", batch_name):
			frappe.get_doc(
				{
					"doctype": "Batch",
					"batch_id": batch_name,
					"item": item,
					"manufacturing_date": "2026-01-01",
					"expiry_date": "2027-01-01",
				}
			).insert()

		row = frappe._dict(batch_no=batch_name, stock_item=item)
		apply_batch_shelf_life(row, date(2026, 4, 1))

		self.assertEqual(str(row.manufacturing_date), "2026-01-01")
		self.assertGreaterEqual(row.remaining_shelf_life_percent, 66.6667)
		self.assertEqual(row.shelf_life_status, "符合")

	def test_rejects_batch_below_two_thirds_remaining(self):
		item = make_item("_Test JD Batch Rejected Item", is_stock_item=1, has_batch_no=1)
		batch_name = "_TEST-JD-BATCH-REJECTED"
		if not frappe.db.exists("Batch", batch_name):
			frappe.get_doc(
				{
					"doctype": "Batch",
					"batch_id": batch_name,
					"item": item,
					"manufacturing_date": "2026-01-01",
					"expiry_date": "2027-01-01",
				}
			).insert()

		row = frappe._dict(batch_no=batch_name, stock_item=item)
		with self.assertRaises(frappe.ValidationError):
			apply_batch_shelf_life(row, date(2026, 6, 1))

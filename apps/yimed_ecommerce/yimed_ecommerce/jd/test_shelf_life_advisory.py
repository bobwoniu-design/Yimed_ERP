"""Shelf-life checks without creating or changing business records."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.utils import flt
from yimed_ecommerce.jd import shelf_life


class TestShelfLifeAdvisory(unittest.TestCase):
	def setUp(self):
		self.frappe = self.enterContext(patch.object(shelf_life, "frappe"))
		self.enterContext(patch.object(shelf_life, "_", lambda text: text))
		self.enterContext(patch.object(shelf_life, "flt", lambda value, precision: flt(value, precision, rounding_method="Banker's Rounding")))
		self.frappe.throw.side_effect = ValueError("Invalid batch")
		self.frappe.db.get_value.return_value = SimpleNamespace(item="ITEM", manufacturing_date="2026-01-01", expiry_date="2027-01-01")

	def test_ratio_is_advisory_for_all_packing_save_paths(self):
		for packing_date, status in [("2026-04-01", "符合"), ("2026-04-25", "临期预警"), ("2026-06-01", "不符合")]:
			with self.subTest(packing_date=packing_date):
				row = SimpleNamespace(batch_no="BATCH", stock_item="ITEM")
				shelf_life.apply_batch_shelf_life(row, packing_date)
				self.assertEqual(row.shelf_life_status, status)
				self.assertEqual(row.expiry_date, "2027-01-01")
		self.frappe.throw.assert_not_called()

	def test_wrong_item_batch_still_rejected(self):
		with self.assertRaises(ValueError):
			shelf_life.apply_batch_shelf_life(SimpleNamespace(batch_no="BATCH", stock_item="OTHER"), "2026-06-01")

	def test_missing_batch_still_rejected(self):
		self.frappe.db.get_value.return_value = None
		with self.assertRaises(ValueError):
			shelf_life.apply_batch_shelf_life(SimpleNamespace(batch_no="MISSING", stock_item="ITEM"), "2026-06-01")

	def test_no_batch_needs_no_ratio_check(self):
		row = SimpleNamespace(batch_no=None)
		shelf_life.apply_batch_shelf_life(row, "2026-06-01")
		self.assertEqual(row.shelf_life_status, "无需校验")
		self.frappe.db.get_value.assert_not_called()

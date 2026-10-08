import json
import unittest
from unittest.mock import patch

from yimed_ecommerce.jd import packing


class TestCartonPrintSelection(unittest.TestCase):
	def setUp(self):
		self.frappe_patch = patch.object(packing, "frappe")
		self.frappe = self.frappe_patch.start()
		self.addCleanup(self.frappe_patch.stop)
		self.translation_patch = patch.object(packing, "_", lambda value: value)
		self.translation_patch.start()
		self.addCleanup(self.translation_patch.stop)
		self.time_patch = patch.object(packing, "now_datetime", return_value="2026-09-24 12:00:00")
		self.time_patch.start()
		self.addCleanup(self.time_patch.stop)
		self.frappe.get_all.return_value = ["BOX-1", "BOX-2", "BOX-3"]
		self.frappe.db.get_value.return_value = 0
		self.frappe.parse_json.side_effect = json.loads
		self.frappe.throw.side_effect = ValueError("Invalid carton selection")

	def test_selected_only_in_sequence_order_and_without_duplicates(self):
		names = packing.get_carton_names("PO-1", '["BOX-3", "BOX-1", "BOX-1"]')
		self.assertEqual(names, ["BOX-1", "BOX-3"])
		self.assertEqual([call.args[1] for call in self.frappe.db.set_value.call_args_list], names)
		self.frappe.get_doc.return_value.check_permission.assert_called_once_with("read")

	def test_existing_print_all_behavior(self):
		self.assertEqual(packing.get_carton_names("PO-1"), ["BOX-1", "BOX-2", "BOX-3"])
		self.assertEqual(self.frappe.db.set_value.call_count, 3)

	def test_empty_selection_does_not_print_all(self):
		self.assertEqual(packing.get_carton_names("PO-1", []), [])
		self.frappe.db.set_value.assert_not_called()

	def test_foreign_cartons_rejected_before_any_print_counts_change(self):
		with self.assertRaises(ValueError):
			packing.get_carton_names("PO-1", ["BOX-1", "FOREIGN"])
		self.frappe.db.set_value.assert_not_called()

	def test_invalid_selection_types(self):
		for selection in ({}, [None], [1]):
			with self.subTest(selection=selection), self.assertRaises(ValueError):
				packing.get_carton_names("PO-1", selection)
		self.frappe.db.set_value.assert_not_called()

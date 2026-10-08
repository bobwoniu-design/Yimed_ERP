import unittest
from unittest.mock import patch
import frappe
from yimed_ecommerce.jd import assembly_labels as labels


class AssemblyLabelsTest(unittest.TestCase):
    def test_earliest_expiry_keeps_matching_manufacture(self):
        rows = [dict(batch_no="A", expiry_date="2029-01-01", manufacturing_date="2025-01-01"),
                dict(batch_no="B", expiry_date="2028-01-01", manufacturing_date="2026-01-01")]
        result = labels.earliest_batch(rows)
        self.assertEqual(result["batch_no"], "B")
        self.assertEqual(result["manufacturing_date"], "2026-01-01")
        self.assertIsNone(labels.earliest_batch([dict(batch_no="C")]))

    def fixture(self, missing=False):
        order = frappe._dict(items=None)
        class Order:
            items = [frappe._dict(platform_item="KIT", jd_sku="10001")]
            def check_permission(self, kind): pass
        pools = [frappe._dict(stock_item="KIT", stocked_qty=2),
                 frappe._dict(stock_item="C1", batch_no="B1", required_qty=2, shortage_qty=0, stocked_qty=0),
                 frappe._dict(stock_item="C1", batch_no="B2", required_qty=2, shortage_qty=0, stocked_qty=0),
                 frappe._dict(stock_item="C2", batch_no="B3", required_qty=2, shortage_qty=0, stocked_qty=0)]
        def doc(dt, name):
            if dt == "Item": return frappe._dict(item_name=name)
            return frappe._dict(item="C2" if name == "B3" else "C1", name=name, batch_id=name,
                expiry_date=None if missing and name == "B3" else ("2027-01-01" if name == "B2" else "2028-01-01"),
                manufacturing_date="2025-01-01" if name == "B2" else "2026-01-01")
        with patch.object(labels, "_validate_batch_access", return_value=frappe._dict(name="IMPORT")), \
             patch.object(labels.workflow, "_batch_orders", return_value=[Order()]), \
             patch.object(labels.workflow, "_bundle_requirements", return_value={"KIT": {"components": {"C1": 1, "C2": 1}, "jd_item_name": "套装"}}), \
             patch.object(frappe, "get_all", return_value=pools), patch.object(frappe, "get_doc", side_effect=doc):
            return labels.build_labels("IMPORT", "KIT")[0]

    def test_all_consumed_component_batches_survive_assembly(self):
        label = self.fixture()
        self.assertEqual([r["batch_no"] for r in label["batches"]], ["B1", "B2", "B3"])
        self.assertEqual(label["earliest"]["batch_no"], "B2")
        self.assertEqual(label["earliest"]["manufacturing_date"], "2025-01-01")
        self.assertEqual(label["sku"], "10001")
        self.assertTrue(label["barcode"].startswith("data:image/svg+xml;base64,"))

    def test_missing_expiry_does_not_invent_combined_expiry(self):
        label = self.fixture(missing=True)
        self.assertIsNone(label["earliest"])
        self.assertTrue(label["date_warning"])

    def test_shelf_life_uses_same_batch_dates(self):
        self.assertEqual(labels.shelf_life(dict(manufacturing_date="2026-01-15", expiry_date="2028-01-15")), "2年")
        self.assertEqual(labels.shelf_life(dict(manufacturing_date="2026-08-15", expiry_date="2029-08-14")), "3年")
        self.assertEqual(labels.shelf_life(dict(manufacturing_date="2026-01-01", expiry_date="2026-07-01")), "6个月")
        self.assertEqual(labels.shelf_life(None), "未维护")

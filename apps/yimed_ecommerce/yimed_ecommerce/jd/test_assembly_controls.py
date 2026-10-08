"""Isolated assembly regression tests; all database access is mocked."""
import unittest
from types import SimpleNamespace as Row
from unittest.mock import Mock, patch

from frappe.utils import flt
from yimed_ecommerce.jd import workflow


class TestAssemblyControls(unittest.TestCase):
	def setUp(self):
		self.enterContext(patch.object(workflow, "flt", lambda value, precision=None: flt(value, precision, rounding_method="Banker's Rounding")))
		self.frappe = self.enterContext(patch.object(workflow, "frappe"))
		self.enterContext(patch.object(workflow, "_", lambda text: text))
		self.frappe.throw.side_effect = lambda message, **kwargs: (_ for _ in ()).throw(ValueError(message))
		self.frappe.db.exists.return_value = False
		self.frappe.db.sql.return_value = []
		self.bundles = {"KIT": {"components": {"A": 2, "B": 1}}}

	def pool(self, name, item, original, current, shortage=0):
		return Row(name=name, stock_item=item, required_qty=original + shortage,
			stocked_qty=current, shortage_qty=shortage)

	def test_restore_original_rows_with_multiple_batches_and_shortage(self):
		pools = [self.pool("A-BATCH-1", "A", 8, 0), self.pool("A-BATCH-2", "A", 5, 3, 2),
			self.pool("B-BATCH", "B", 5, 0), self.pool("FINISHED", "KIT", 5, 5)]
		finished, updates = workflow._assembly_restore_plan(self.bundles, pools)
		self.assertEqual([row.name for row in finished], ["FINISHED"])
		self.assertEqual(updates, [("A-BATCH-1", 8), ("A-BATCH-2", 5), ("B-BATCH", 5)])

	def test_shared_components_are_restored_once(self):
		bundles = {"K1": {"components": {"A": 2}}, "K2": {"components": {"A": 3}}}
		pools = [self.pool("SOURCE", "A", 30, 20), self.pool("F1", "K1", 2, 2), self.pool("F2", "K2", 2, 2)]
		self.assertEqual(workflow._assembly_restore_plan(bundles, pools)[1], [("SOURCE", 30)])

	def test_inconsistent_deductions_are_rejected(self):
		with self.assertRaisesRegex(ValueError, "不一致"):
			workflow._assembly_restore_plan(self.bundles, [self.pool("SOURCE", "A", 10, 1), self.pool("F", "KIT", 5, 5)])

	def test_no_finished_rows_is_idempotent(self):
		self.assertEqual(workflow._assembly_restore_plan(self.bundles, [self.pool("SOURCE", "A", 10, 10)]), ([], []))

	def test_sorted_bundle_blocks_cancellation(self):
		self.frappe.db.exists.return_value = True
		self.assertIn("已分拣", workflow._assembly_downstream_reason("BATCH", ["KIT"]))

	def test_packed_bundle_blocks_cancellation_even_without_allocations(self):
		self.frappe.db.sql.return_value = [("CARTON-ITEM",)]
		self.assertIn("已装箱", workflow._assembly_downstream_reason("BATCH", ["KIT"]))

	def test_no_bundles_does_not_query_downstream(self):
		self.assertEqual(workflow._assembly_downstream_reason("BATCH", []), "")
		self.frappe.db.exists.assert_not_called()

	def test_cancel_rejection_precedes_any_pool_write(self):
		with patch.object(workflow, "_lock_batch", return_value=Row(name="BATCH")), patch.object(workflow, "_batch_orders", return_value=[]), patch.object(workflow, "_bundle_requirements", return_value=self.bundles):
			self.frappe.db.exists.return_value = True
			with self.assertRaisesRegex(ValueError, "已分拣"):
				workflow.cancel_assembly("BATCH")
		self.frappe.db.set_value.assert_not_called()
		self.frappe.db.delete.assert_not_called()

	def test_cancel_restores_sources_and_removes_only_finished_pool(self):
		self.frappe.get_all.return_value = [self.pool("A1", "A", 10, 0), self.pool("B1", "B", 5, 0), self.pool("F", "KIT", 5, 5)]
		with patch.object(workflow, "_lock_batch", return_value=Row(name="BATCH")), patch.object(workflow, "_batch_orders", return_value=[]), patch.object(workflow, "_bundle_requirements", return_value=self.bundles), patch.object(workflow, "_locked_pools"):
			self.assertEqual(workflow.cancel_assembly("BATCH")["cancelled_count"], 1)
		self.assertEqual(self.frappe.db.set_value.call_count, 2)
		self.frappe.db.delete.assert_called_once_with("JD Stocking Pool Item", {"name": "F"})

	def test_both_sorting_writes_block_pending_assembly_before_mutation(self):
		for name, args in [("auto_sort", ("BATCH",)), ("update_sorting", ("BATCH", []))]:
			with self.subTest(name=name), patch.object(workflow, "_lock_batch", return_value=Row(name="BATCH")), patch.object(workflow, "_batch_orders", return_value=[]), patch.object(workflow, "_assembly_pending", return_value=True):
				with self.assertRaisesRegex(ValueError, "组装确认"):
					getattr(workflow, name)(*args)
		self.frappe.db.delete.assert_not_called()

	def test_completed_or_non_bundle_batch_can_pass_assembly_gate(self):
		with patch.object(workflow, "_assembly_pending", return_value=False):
			workflow._validate_assembly_complete("BATCH")
		self.frappe.throw.assert_not_called()

	def test_packing_gate_blocks_pending_assembly(self):
		self.frappe.get_doc.return_value = Row(name="PO", import_batch="BATCH", check_permission=Mock(), workflow_stage="备货", stocking_status="备货完成", sorting_status="分拣完成")
		self.frappe.get_all.return_value = []
		self.frappe.db.get_value.return_value = "草稿"
		with patch.object(workflow, "_assembly_pending", return_value=True):
			gate = workflow.get_order_packing_gate("PO")
		self.assertFalse(gate["can_pack"])
		self.assertTrue(any("组装确认" in issue for issue in gate["issues"]))

	def test_confirm_only_requested_bundle_and_preserve_other_allocations(self):
		bundles = {"K1": {"total_qty": 2, "components": {"A": 2}}, "K2": {"total_qty": 2, "components": {"A": 3}}}
		self.frappe.get_all.return_value = [Row(name="SOURCE", stock_item="A", warehouse="WH", stocked_qty=10)]
		doc = Mock()
		doc.name = "FINISHED"
		self.frappe.get_doc.return_value = doc
		self.frappe._dict.side_effect = lambda **kwargs: Row(**kwargs)
		with patch.object(workflow, "_lock_batch", return_value=Row(name="BATCH", company="CO")), patch.object(workflow, "_batch_orders", return_value=[]), patch.object(workflow, "_bundle_requirements", return_value=bundles), patch.object(workflow, "_reject_downstream_rewrite"), patch.object(workflow, "now_datetime", return_value="2026-09-24 12:00:00"):
			result = workflow.confirm_assembly("BATCH", [{"platform_item": "K1", "assembled_qty": 2}])
		self.assertEqual(result["assembled_count"], 1)
		self.frappe.db.set_value.assert_called_once_with("JD Stocking Pool Item", "SOURCE", "stocked_qty", 6, update_modified=False)
		self.frappe.db.delete.assert_not_called()

	def test_repeated_confirmation_does_not_consume_components_twice(self):
		bundles = {"KIT": {"total_qty": 5, "components": {"A": 2}}}
		self.frappe.get_all.return_value = [Row(name="F", stock_item="KIT", warehouse="WH", stocked_qty=5)]
		with patch.object(workflow, "_lock_batch", return_value=Row(name="BATCH")), patch.object(workflow, "_batch_orders", return_value=[]), patch.object(workflow, "_bundle_requirements", return_value=bundles), patch.object(workflow, "_reject_downstream_rewrite"):
			result = workflow.confirm_assembly("BATCH", [{"platform_item": "KIT", "assembled_qty": 5}])
		self.assertEqual(result["assembled_count"], 0)
		self.frappe.db.set_value.assert_not_called()
		self.frappe.db.delete.assert_not_called()

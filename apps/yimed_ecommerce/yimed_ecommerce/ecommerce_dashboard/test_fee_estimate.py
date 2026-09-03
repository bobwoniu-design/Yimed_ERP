from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from yimed_ecommerce.ecommerce_dashboard.dashboard import _apply_fee_estimates


class TestFeeEstimate(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.company = frappe.db.get_value("Company", {}, "name")
		self.suffix = frappe.generate_hash(length=8)
		self.channel = f"京东-{self.suffix}"
		self.other_channel = f"天猫-{self.suffix}"
		self.store = f"旗舰店-{self.suffix}"
		self.fee_item = frappe.get_doc({
			"doctype": "Ecommerce Fee Item",
			"item_name": f"平台佣金-{self.suffix}",
			"dashboard_field": f"commission_{self.suffix}",
		}).insert()

	def test_estimate_by_sales_ratio(self):
		self._rule("按销售额比例", 5)
		rows = [self._row(net_sales=1000)]
		_apply_fee_estimates(rows, self._filters())
		self.assertEqual(round(rows[0]["fee_breakdown"][self.fee_item.item_name], 2), 50)

	def test_estimate_by_order_count(self):
		self._rule("按订单数", 2.5)
		rows = [self._row(net_sales=1000, orders=10)]
		_apply_fee_estimates(rows, self._filters())
		self.assertEqual(round(rows[0]["fee_breakdown"][self.fee_item.item_name], 2), 25)

	def test_estimate_by_quantity(self):
		self._rule("按件数", 1)
		rows = [self._row(net_sales=1000, net_qty=30)]
		_apply_fee_estimates(rows, self._filters())
		self.assertEqual(round(rows[0]["fee_breakdown"][self.fee_item.item_name], 2), 30)

	def test_estimate_fixed_amount_spread_by_sales(self):
		self._rule("固定金额", 100, scopes=[(self.channel, "")])
		rows = [self._row(net_sales=300, store=self.store), self._row(net_sales=100, store=f"分店-{self.suffix}")]
		_apply_fee_estimates(rows, self._filters())
		total = sum(row["fee_breakdown"][self.fee_item.item_name] for row in rows)
		self.assertEqual(round(total, 2), 100)
		self.assertEqual(round(rows[0]["fee_breakdown"][self.fee_item.item_name], 2), 75)
		self.assertEqual(round(rows[1]["fee_breakdown"][self.fee_item.item_name], 2), 25)

	def test_estimate_scope_mismatch_is_skipped(self):
		self._rule("按销售额比例", 5, scopes=[(self.other_channel, "")])
		rows = [self._row(net_sales=1000)]
		_apply_fee_estimates(rows, self._filters())
		self.assertNotIn(self.fee_item.item_name, rows[0]["fee_breakdown"])

	def test_estimate_multiple_scope_rows_match_any(self):
		"""范围多行=多选：任一行命中渠道即适用。"""
		self._rule("按销售额比例", 5, scopes=[(self.channel, ""), (self.other_channel, "")])
		rows = [self._row(net_sales=1000), self._row(net_sales=1000, channel=self.other_channel)]
		_apply_fee_estimates(rows, self._filters())
		self.assertEqual(round(rows[0]["fee_breakdown"][self.fee_item.item_name], 2), 50)
		self.assertEqual(round(rows[1]["fee_breakdown"][self.fee_item.item_name], 2), 50)

	def test_estimate_scope_store_refines_channel(self):
		"""店铺留空适用渠道下全部店铺；填了店铺则只匹配该店铺。"""
		self._rule("按销售额比例", 5, scopes=[(self.channel, self.store)])
		rows = [self._row(net_sales=1000, store=self.store), self._row(net_sales=1000, store=f"分店-{self.suffix}")]
		_apply_fee_estimates(rows, self._filters())
		self.assertEqual(round(rows[0]["fee_breakdown"][self.fee_item.item_name], 2), 50)
		self.assertNotIn(self.fee_item.item_name, rows[1]["fee_breakdown"])

	def test_estimate_empty_scopes_apply_to_all(self):
		"""范围表留空表示适用全部渠道和店铺。"""
		self._rule("按销售额比例", 5, scopes=[])
		rows = [self._row(net_sales=1000), self._row(net_sales=1000, channel=self.other_channel)]
		_apply_fee_estimates(rows, self._filters())
		self.assertEqual(round(rows[0]["fee_breakdown"][self.fee_item.item_name], 2), 50)
		self.assertEqual(round(rows[1]["fee_breakdown"][self.fee_item.item_name], 2), 50)

	def test_estimate_promotion_goes_to_promotion_expense(self):
		promotion_item = frappe.db.get_value("Ecommerce Fee Item", {"dashboard_field": "promotion_expense"}, "name")
		if not promotion_item:
			promotion_item = frappe.get_doc({
				"doctype": "Ecommerce Fee Item",
				"item_name": f"推广费-{self.suffix}",
				"dashboard_field": "promotion_expense",
			}).insert().name
		frappe.get_doc({
			"doctype": "Ecommerce Fee Estimate Rule",
			"rule_name": f"推广预估-{self.suffix}",
			"enabled": 1,
			"company": self.company,
			"fee_item": promotion_item,
			"calculation_method": "按销售额比例",
			"rate": 10,
			"scopes": [{"channel_name": self.channel, "store_name": self.store}],
		}).insert()

		rows = [self._row(net_sales=1000)]
		_apply_fee_estimates(rows, self._filters())
		# 推广费以导入的实际数据为准，预估规则一律跳过，防止与导入数据重复扣减。
		self.assertEqual(rows[0]["promotion_expense"], 0.0)
		self.assertNotIn(f"推广费-{self.suffix}", rows[0]["fee_breakdown"])

	def _rule(self, method, rate, scopes=None):
		doc = frappe.get_doc({
			"doctype": "Ecommerce Fee Estimate Rule",
			"rule_name": f"{method}-{self.suffix}",
			"enabled": 1,
			"company": self.company,
			"fee_item": self.fee_item.name,
			"calculation_method": method,
			"rate": rate,
		})
		if scopes is not None:
			for channel, store in scopes:
				doc.append("scopes", {"channel_name": channel, "store_name": store})
		doc.insert()

	def _row(self, net_sales, orders=1, net_qty=1, store=None, channel=None):
		return {
			"date": "2026-08-22",
			"channel": channel or self.channel,
			"store": store or self.store,
			"net_sales": net_sales,
			"orders": orders,
			"net_qty": net_qty,
			"fee_breakdown": {},
			"promotion_expense": 0.0,
		}

	def _filters(self):
		return {"company": self.company}

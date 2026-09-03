from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from yimed_ecommerce.ecommerce_dashboard.dashboard import (
	_apply_unified_listing_fees,
	_empty_listing_row,
)
from yimed_ecommerce.ecommerce_dashboard.fee_management import (
	_allocate_amount,
	_weights_for,
	generate_daily_accruals,
)


class TestFeeManagement(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.company = frappe.db.get_value("Company", {}, "name")
		self.currency = frappe.db.get_value("Company", self.company, "default_currency")
		self.suffix = frappe.generate_hash(length=8)
		self.channel = f"测试平台-{self.suffix}"
		self.store = f"测试店铺-{self.suffix}"
		self.fee_item = frappe.get_doc({
			"doctype": "Ecommerce Fee Item",
			"item_name": f"测试费用-{self.suffix}",
			"dashboard_field": f"test_fee_{self.suffix}",
			"default_allocation_method": "销售额",
		}).insert()

	def test_daily_accrual_is_idempotent(self):
		rule = frappe.get_doc({
			"doctype": "Ecommerce Fee Rule",
			"rule_name": f"每日计提-{self.suffix}",
			"enabled": 1,
			"company": self.company,
			"fee_item": self.fee_item.name,
			"valid_from": "2026-08-20",
			"valid_to": "2026-08-21",
			"auto_accrual": 1,
			"daily_accrual_amount": 10,
			"default_allocation_method": "不分摊",
		}).insert()

		first = generate_daily_accruals("2026-08-20", "2026-08-21", rule.name)
		second = generate_daily_accruals("2026-08-20", "2026-08-21", rule.name)
		self.assertEqual(len(first["created"]), 2)
		self.assertEqual(len(second["created"]), 0)
		self.assertEqual(set(second["existing"]), set(first["created"]))

	def test_all_supported_allocation_weights_and_rounding(self):
		targets = [
			{"sales_amount": 40, "order_count": 2, "quantity": 5, "fixed_ratio": 7},
			{"sales_amount": 60, "order_count": 3, "quantity": 15, "fixed_ratio": 3},
		]
		self.assertEqual(_weights_for("销售额", targets), [40, 60])
		self.assertEqual(_weights_for("订单数", targets), [2, 3])
		self.assertEqual(_weights_for("件数", targets), [5, 15])
		self.assertEqual(_weights_for("固定比例", targets), [7, 3])
		self.assertEqual(_weights_for("平均", targets), [1, 1])
		self.assertEqual(sum(_allocate_amount(100, [1, 1, 1])), 100)

	def test_realtime_sales_allocation_sums_to_detail_amount(self):
		rows = self._rows([("A", 300), ("B", 100)])
		self._fee_detail("销售额", 100)
		_apply_unified_listing_fees(rows, self._filters(), "main")
		self.assertEqual(sorted(round(row["rule_fees"], 2) for row in rows.values()), [25, 75])
		self.assertEqual(sum(row["rule_fees"] for row in rows.values()), 100)

	def test_realtime_unallocated_fee_never_enters_listings(self):
		rows = self._rows([("A", 300), ("B", 100)])
		self._fee_detail("不分摊", 50)
		_apply_unified_listing_fees(rows, self._filters(), "main")
		self.assertEqual(sum(row["rule_fees"] for row in rows.values()), 0)

	def test_realtime_fixed_ratio_uses_detail_ratios(self):
		listing_a = self._listing("A")
		listing_b = self._listing("B")
		rows = {}
		self._fee_detail(
			"固定比例", 100,
			ratios=[(listing_a.name, 7), (listing_b.name, 3)],
		)
		_apply_unified_listing_fees(rows, self._filters(), "main")
		self.assertEqual(sorted(round(row["rule_fees"], 2) for row in rows.values()), [30, 70])

	def test_realtime_direct_attribution_goes_to_single_listing(self):
		listing = self._listing("A")
		rows = {}
		self._fee_detail("不分摊", 100, product_listing=listing.name)
		_apply_unified_listing_fees(rows, self._filters(), "main")
		self.assertEqual(len(rows), 1)
		self.assertEqual(round(next(iter(rows.values()))["rule_fees"], 2), 100)

	def _filters(self):
		return {
			"company": self.company,
			"from_date": "2026-08-22",
			"to_date": "2026-08-22",
			"channel_name": self.channel,
			"store_name": self.store,
		}

	def _rows(self, entries):
		rows = {}
		for label, sales in entries:
			key = ("2026-08-22", self.channel, self.store, f"{self.suffix}-{label}")
			row = _empty_listing_row(key)
			row["sales_amount"] = sales
			row["orders"] = 1
			row["sales_qty"] = sales
			rows[key] = row
		return rows

	def _fee_detail(self, allocation_method, amount, product_listing=None, ratios=None):
		doc = frappe.get_doc({
			"doctype": "Ecommerce Fee Detail",
			"posting_date": "2026-08-22",
			"company": self.company,
			"fee_item": self.fee_item.name,
			"amount": amount,
			"currency": self.currency,
			"channel_name": self.channel,
			"store_name": self.store,
			"product_listing": product_listing,
			"allocation_method": allocation_method,
			"entry_type": "实际",
			"source_type": "集成测试",
			"status": "有效",
			"source_unique_key": f"test-detail-{self.suffix}-{allocation_method}-{len(ratios or [])}-{product_listing or 'none'}",
		})
		for listing_name, ratio in (ratios or []):
			doc.append("allocation_ratios", {"product_listing": listing_name, "ratio": ratio})
		return doc.insert()

	def _listing(self, label):
		item_code = frappe.db.get_value("Item", {"disabled": 0}, "name")
		doc = frappe.get_doc({
			"doctype": "Ecommerce Product Listing",
			"company": self.company,
			"channel_name": self.channel,
			"platform_code": "TEST",
			"store_id": f"STORE-{self.suffix}",
			"store_name": self.store,
			"platform_item_id": f"{self.suffix}-{label}",
			"platform_item_name": f"测试链接{label}",
		})
		doc.append("skus", {"platform_sku_id": f"{self.suffix}-{label}-SKU", "item_code": item_code})
		return doc.insert()

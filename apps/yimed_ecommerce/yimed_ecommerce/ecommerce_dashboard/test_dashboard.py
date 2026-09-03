from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from yimed_ecommerce.ecommerce_dashboard.dashboard import (
	_aggregate_listing_period,
	_aggregate_stores,
	_allocate_fee_breakdown,
	_channel_sql,
	_dashboard_fee_columns,
	_fee_breakdown,
	_listing_dimension_columns,
	get_dashboard,
	get_filter_options,
)
from yimed_ecommerce.setup import ensure_system_fee_items
from yimed_ecommerce.ecommerce_dashboard.promotion_import import (
	build_import_template,
	get_channel_store_options,
)


class TestEcommerceDashboard(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.company = frappe.db.get_value("Company", {}, "name")

	def test_dashboard_all_companies_does_not_require_company_filter(self):
		result = get_dashboard(from_date="2026-08-01", to_date="2026-08-02")
		self.assertEqual(result["filters"]["company"], "")

	def test_custom_fee_breakdown_is_allocated_to_listing_rows(self):
		rows = [
			{"net_sales": 75, "fee_breakdown": {}, "rule_fees": 0},
			{"net_sales": 25, "fee_breakdown": {}, "rule_fees": 0},
		]
		_allocate_fee_breakdown(rows, {"平台佣金": 10, "快递费": 20})
		self.assertEqual(rows[0]["fee_breakdown"], {"平台佣金": 7.5, "快递费": 15})
		self.assertEqual(rows[1]["fee_breakdown"], {"平台佣金": 2.5, "快递费": 5})
		self.assertEqual([row["rule_fees"] for row in rows], [22.5, 7.5])

	def test_promotion_template_is_xlsx(self):
		content = build_import_template()
		self.assertTrue(content.startswith(b"PK"))
		self.assertGreater(len(content), 1000)

	def test_promotion_fee_column_is_controlled_by_fee_item_but_profit_keeps_expense(self):
		fee_item = ensure_system_fee_items()
		frappe.db.set_value("Ecommerce Fee Item", fee_item, {"enabled": 1, "show_on_dashboard": 1})
		row = {
			**self._row("天猫", "旗舰店", 100), "sales_amount": 100, "return_amount": 0,
			"return_qty": 0, "net_qty": 1, "product_cost": 20, "promotion_expense": 10,
			"rule_fees": 0, "gross_profit": 80, "contribution_profit": 70,
			"covered_sales_qty": 0, "covered_return_qty": 0,
		}
		self.assertTrue(any(column["dashboard_field"] == "promotion_expense" for column in _dashboard_fee_columns()))
		self.assertEqual(next(item["amount"] for item in _fee_breakdown([row]) if item["fee_type"] == "推广费"), 10)

		frappe.db.set_value("Ecommerce Fee Item", fee_item, "show_on_dashboard", 0)
		self.assertFalse(any(column["dashboard_field"] == "promotion_expense" for column in _dashboard_fee_columns()))
		self.assertNotIn("推广费", {item["fee_type"] for item in _fee_breakdown([row])})
		# 显示开关不改变已计算的贡献利润。
		self.assertEqual(row["contribution_profit"], row["gross_profit"] - row["promotion_expense"] - row["rule_fees"])

		frappe.db.set_value("Ecommerce Fee Item", fee_item, {"enabled": 0, "show_on_dashboard": 1})
		self.assertFalse(any(column["dashboard_field"] == "promotion_expense" for column in _dashboard_fee_columns()))

	def test_visible_promotion_fee_item_returns_zero_amount_for_empty_period(self):
		fee_item = ensure_system_fee_items()
		frappe.db.set_value("Ecommerce Fee Item", fee_item, {"enabled": 1, "show_on_dashboard": 1})
		columns = _dashboard_fee_columns()
		self.assertTrue(any(column["dashboard_field"] == "promotion_expense" for column in columns))
		self.assertEqual(next(item["amount"] for item in _fee_breakdown([]) if item["fee_type"] == "推广费"), 0)

	def test_fee_and_promotion_options_use_channel_name_as_store(self):
		suffix = frappe.generate_hash(length=6)
		store = f"渠道名称-{suffix}"
		legacy_store = f"平台店铺名称-{suffix}"
		self._sales_channel("京东", store, legacy_store)
		options = get_channel_store_options(self.company)
		self.assertIn({"channel": "京东", "store": store}, options["stores"])
		self.assertNotIn(legacy_store, {row["store"] for row in options["stores"]})

	def test_listing_period_aggregates_dates_without_losing_profit(self):
		base = {
			"channel": "拼多多", "store": "拼多多医麦德旗舰店", "listing_id": "784314361164",
			"listing_name": "测试链接", "orders": 1, "sales_qty": 1,
			"sales_amount": 100, "sales_cost": 40, "covered_sales_qty": 1,
			"return_amount": 0, "return_qty": 0, "return_cost": 0,
			"covered_return_qty": 0, "promotion_expense": 10, "rule_fees": 5,
			"net_sales": 100, "net_qty": 1, "product_cost": 40,
			"gross_profit": 60, "contribution_profit": 45,
		}
		rows = _aggregate_listing_period([{**base, "date": "2026-08-20"}, {**base, "date": "2026-08-21"}])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["orders"], 2)
		self.assertEqual(rows[0]["contribution_profit"], 90)
		self.assertEqual(rows[0]["contribution_margin_pct"], 45)

	def test_main_and_child_listing_dimensions_use_distinct_identifiers(self):
		self.assertEqual(
			_listing_dimension_columns("main"),
			("custom_ecommerce_listing_id", "custom_ecommerce_platform_item_name", "platform_item_code"),
		)
		self.assertEqual(
			_listing_dimension_columns("child"),
			("custom_ecommerce_platform_sku_id", "item_name", "platform_sku"),
		)

	def test_main_and_child_listing_queries_reconcile_to_dashboard_totals(self):
		for dimension in ("main", "child"):
			data = get_dashboard(
				self.company,
				"2026-08-01",
				"2026-08-17",
				listing_dimension=dimension,
			)
			self.assertAlmostEqual(
				data["summary"]["net_sales"],
				sum(row["net_sales"] for row in data["listings"]),
				places=4,
			)
			self.assertAlmostEqual(
				data["summary"]["contribution_profit"],
				sum(row["contribution_profit"] for row in data["listings"]),
				places=4,
			)

	def test_pdd_filter_uses_channel_name_as_store(self):
		suffix = frappe.generate_hash(length=6)
		store = f"拼多多医麦德旗舰店-{suffix}"
		legacy_shop_name = f"旧平台店铺字段-{suffix}"
		frappe.get_doc({
			"doctype": "Jackyun Sales Channel",
			"channel_id": f"PDD-{suffix}",
			"channel_code": f"PDD-{suffix}",
			"channel_name": store,
			"channel_type": "1",
			"online_platform_name": "拼多多",
			"platform_shop_name": legacy_shop_name,
		}).insert()
		options = get_filter_options()
		self.assertIn("拼多多", options["channels"])
		self.assertIn({"channel": "拼多多", "store": store}, options["stores"])
		self.assertNotIn("未映射", options["channels"])
		self.assertNotIn(legacy_shop_name, {row["store"] for row in options["stores"]})

	def test_filter_options_exclude_non_online_and_disabled_channels(self):
		suffix = frappe.generate_hash(length=6)
		self._sales_channel(f"有效平台-{suffix}", f"有效店铺-{suffix}")
		self._sales_channel(f"线下平台-{suffix}", f"线下店铺-{suffix}", channel_type="0")
		self._sales_channel(f"停用平台-{suffix}", f"停用店铺-{suffix}", disabled=1)
		options = get_filter_options()
		self.assertIn(f"有效平台-{suffix}", options["channels"])
		self.assertNotIn(f"线下平台-{suffix}", options["channels"])
		self.assertNotIn(f"停用平台-{suffix}", options["channels"])

	def test_pdd_sales_returns_and_listing_sql_share_store_dimension(self):
		_join, channel, store = _channel_sql("doc")
		self.assertIn("doc.custom_jackyun_sales_channel", _join)
		self.assertIn("ch.channel_type", _join)
		self.assertIn("ch.disabled", _join)
		self.assertIn("ch.deleted", _join)
		self.assertIn("ch.online_platform_name", channel)
		self.assertIn("ch.channel_name", store)
		self.assertNotIn("platform_shop_name", store)

	def test_store_performance_keeps_platform_and_store_grouping(self):
		rows = [
			{**self._row("拼多多", "同名店铺", 120), "sales_amount": 120},
			{**self._row("抖音", "同名店铺", 80), "sales_amount": 80},
		]
		for row in rows:
			row.update({
				"sales_qty": 1, "return_amount": 0, "return_qty": 0,
				"product_cost": 0, "promotion_expense": 0, "rule_fees": 0,
				"gross_profit": row["net_sales"], "contribution_profit": row["net_sales"],
				"covered_sales_qty": 0, "covered_return_qty": 0,
			})
		stores = _aggregate_stores(rows)
		self.assertEqual([(row["channel"], row["store"]) for row in stores], [("拼多多", "同名店铺"), ("抖音", "同名店铺")])

	def _sales_channel(self, platform, store, platform_shop_name=None, channel_type="1", disabled=0):
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "Jackyun Sales Channel",
				"channel_id": f"TEST-{suffix}",
				"channel_code": f"TEST-{suffix}",
				"channel_name": store,
				"channel_type": channel_type,
				"disabled": disabled,
				"deleted": 0,
				"online_platform_name": platform,
				"platform_shop_name": platform_shop_name or store,
				"company_name": self.company,
			}
		).insert()

	def _row(self, channel, store, sales):
		return {
			"date": "2026-08-21", "channel": channel, "store": store,
			"net_sales": sales, "orders": 1, "net_qty": 1, "fee_breakdown": {},
		}

	def _filters(self):
		return {"company": self.company, "from_date": "2026-08-01", "to_date": "2026-08-31"}

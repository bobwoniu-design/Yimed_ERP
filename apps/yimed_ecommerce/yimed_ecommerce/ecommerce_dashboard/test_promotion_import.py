from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from yimed_ecommerce.ecommerce_dashboard.dashboard import get_dashboard
from yimed_ecommerce.ecommerce_dashboard.promotion_dashboard import _metrics
from yimed_ecommerce.ecommerce_dashboard.promotion_import import (
	_import_alihealth_product_report,
	_import_pdd_product_report,
	_import_tmall_product_report,
)


class TestTmallPromotionImport(IntegrationTestCase):
	def setUp(self):
		self.company = frappe.defaults.get_user_default("Company") or frappe.get_all("Company", pluck="name", limit=1)[0]
		self.suffix = frappe.generate_hash(length=8)
		self.store = f"天猫测试店-{self.suffix}"
		self.subject_id = f"SUBJECT-{self.suffix}"
		channel = frappe.get_doc({
			"doctype": "Jackyun Sales Channel", "channel_id": f"TM-{self.suffix}",
			"channel_code": f"TM-{self.suffix}", "channel_name": self.store,
			"channel_type": "1", "disabled": 0, "deleted": 0,
			"online_platform_name": "天猫", "platform_shop_name": self.store,
			"company_name": self.company,
		}).insert()
		item_code = frappe.get_all("Item", filters={"disabled": 0}, pluck="name", limit=1)[0]
		listing = frappe.get_doc({
			"doctype": "Ecommerce Product Listing", "company": self.company,
			"channel_name": "天猫", "platform_code": "TMALL", "store_id": channel.name,
			"store_name": self.store, "platform_item_id": self.subject_id,
			"platform_item_name": "测试推广商品",
		})
		listing.append("skus", {"platform_sku_id": f"SKU-{self.suffix}", "item_code": item_code})
		listing.insert()
		self.batch = frappe.get_doc({
			"doctype": "Ecommerce Promotion Import Batch", "company": self.company,
			"import_file": "/files/test-tmall-product-report.xlsx",
			"sales_channel": channel.name,
		}).insert()

	def test_two_attribution_windows_create_one_expense_and_import_is_idempotent(self):
		headers = [
			"统计日期", "店铺名称", "转化周期", "场景名字", "原二级场景名字",
			"计划ID", "计划名字", "主体ID", "主体类型", "主体名称", "展现量",
			"点击量", "花费", "总成交金额", "总成交笔数", "直接成交金额", "直接成交笔数",
		]
		base = {
			"统计日期": "2026-08-22", "店铺名称": self.store, "场景名字": "关键词推广",
			"原二级场景名字": "关键词推广", "计划ID": f"PLAN-{self.suffix}",
			"计划名字": "测试计划", "主体ID": self.subject_id, "主体类型": "商品",
			"主体名称": "测试推广商品", "展现量": "1,000", "点击量": "50", "花费": "10.00",
			"直接成交金额": "20.00", "直接成交笔数": "1",
		}
		rows = [
			{**base, "转化周期": "1天转化", "总成交金额": "30.00", "总成交笔数": "1"},
			{**base, "转化周期": "15天转化", "总成交金额": "50.00", "总成交笔数": "2"},
		]

		for _run in range(2):
			result = _import_tmall_product_report(self.batch, rows, headers)
			self.assertEqual(result["success_count"], 2)
			self.assertEqual(result["total_amount"], 10)

		filters = {"company": self.company, "store_name": self.store, "source_subject_id": self.subject_id}
		self.assertEqual(frappe.db.count("Ecommerce Promotion Expense", filters), 1)
		self.assertEqual(frappe.db.get_value("Ecommerce Promotion Expense", filters, "amount"), 10)
		self.assertEqual(frappe.db.get_value("Ecommerce Promotion Expense", filters, "match_status"), "已匹配")
		self.assertEqual(frappe.db.count("Ecommerce Fee Detail", {
			"company": self.company, "store_name": self.store, "source_type": "天猫商品推广报表",
		}), 1)
		fee_detail = frappe.db.get_value("Ecommerce Fee Detail", {
			"company": self.company, "store_name": self.store, "source_type": "天猫商品推广报表",
		}, ["amount", "product_listing", "entry_type"], as_dict=True)
		self.assertEqual(fee_detail.amount, 10)
		self.assertTrue(fee_detail.product_listing)
		self.assertEqual(fee_detail.entry_type, "实际")
		business = get_dashboard(
			self.company, "2026-08-22", "2026-08-22", channel_name="天猫", store_name=self.store, mode="settle",
		)
		self.assertEqual(business["summary"]["promotion_expense"], 10)
		self.assertEqual(business["summary"]["net_sales"], 0)
		self.assertEqual(sum(row["promotion_expense"] for row in business["listings"]), 10)
		self.assertEqual(frappe.db.count("Ecommerce Promotion Performance", {"import_batch": self.batch.name}), 2)
		windows = set(frappe.get_all("Ecommerce Promotion Performance", filters={"import_batch": self.batch.name}, pluck="attribution_window"))
		self.assertEqual(windows, {"1天转化", "15天转化"})

	def test_dashboard_metrics_use_ratio_of_aggregates(self):
		metrics = _metrics({"impressions": 1000, "clicks": 50, "amount": 25, "attributed_orders": 5, "attributed_sales": 100})
		self.assertEqual(metrics["ctr_pct"], 5)
		self.assertEqual(metrics["cpc"], 0.5)
		self.assertEqual(metrics["conversion_rate_pct"], 10)
		self.assertEqual(metrics["roi"], 4)


class TestPddPromotionImport(IntegrationTestCase):
	def setUp(self):
		self.company = frappe.defaults.get_user_default("Company") or frappe.get_all("Company", pluck="name", limit=1)[0]
		self.suffix = frappe.generate_hash(length=8)
		self.store = f"PDD测试店-{self.suffix}"
		self.goods_id = f"GOODS-{self.suffix}"
		item_code = frappe.get_all("Item", filters={"disabled": 0}, pluck="name", limit=1)[0]
		listing = frappe.get_doc({
			"doctype": "Ecommerce Product Listing", "company": self.company,
			"channel_name": "拼多多", "platform_code": "PDD", "store_id": f"PDD-{self.suffix}",
			"store_name": self.store, "platform_item_id": self.goods_id,
			"platform_item_name": "测试拼多多商品",
		})
		listing.append("skus", {"platform_sku_id": f"PDD-SKU-{self.suffix}", "item_code": item_code})
		listing.insert()
		self.pdd_channel = frappe.get_doc({
			"doctype": "Jackyun Sales Channel", "channel_id": f"PDD-{self.suffix}",
			"channel_code": f"PDD-{self.suffix}", "channel_name": self.store,
			"channel_type": "1", "disabled": 0, "deleted": 0,
			"online_platform_name": "拼多多", "platform_shop_name": self.store,
			"company_name": self.company,
		}).insert()
		self.batch = frappe.get_doc({
			"doctype": "Ecommerce Promotion Import Batch", "company": self.company,
			"import_file": "/files/test-pdd-product-report.xlsx",
			"sales_channel": self.pdd_channel.name,
		}).insert()

	def test_pdd_report_imports_and_infers_store_from_listing(self):
		headers = [
			"日期", "商品ID", "商品名称", "推广场景", "推广名称", "出价方式",
			"总花费(元)", "交易额(元)", "成交笔数", "直接交易额(元)", "直接成交笔数",
			"曝光量", "点击量",
		]
		rows = [
			{"日期": "2026-08-24", "商品ID": self.goods_id, "商品名称": "测试拼多多商品",
			 "推广场景": "稳定成本推广", "推广名称": "测试计划", "出价方式": "目标投产比：2.55",
			 "总花费(元)": "100.50", "交易额(元)": "300.00", "成交笔数": "5",
			 "直接交易额(元)": "200.00", "直接成交笔数": "3", "曝光量": "1,500", "点击量": "80"},
			{"日期": "2026-08-25", "商品ID": self.goods_id, "商品名称": "测试拼多多商品",
			 "推广场景": "稳定成本推广", "推广名称": "测试计划", "出价方式": "",
			 "总花费(元)": "50.25", "交易额(元)": "150.00", "成交笔数": "2",
			 "直接交易额(元)": "100.00", "直接成交笔数": "1", "曝光量": "800", "点击量": "40"},
			# 总计行必须跳过
			{"日期": "总计", "商品ID": "", "商品名称": "", "推广场景": "", "推广名称": "",
			 "出价方式": "", "总花费(元)": "150.75", "交易额(元)": "", "成交笔数": "",
			 "直接交易额(元)": "", "直接成交笔数": "", "曝光量": "", "点击量": ""},
		]

		for _run in range(2):  # 幂等
			result = _import_pdd_product_report(self.batch, rows, headers)
			self.assertEqual(result["success_count"], 2)
			self.assertEqual(result["total_amount"], 150.75)
			self.assertEqual(result["matched_count"], 2)

		expense = frappe.get_all(
			"Ecommerce Promotion Expense",
			filters={"import_batch": self.batch.name, "source_type": "拼多多商品推广报表"},
			fields=["posting_date", "store_name", "amount", "match_status"],
			order_by="posting_date",
		)
		self.assertEqual(len(expense), 2)
		self.assertEqual(expense[0]["store_name"], self.store)
		self.assertEqual(expense[0]["amount"], 100.50)
		self.assertEqual(expense[0]["match_status"], "已匹配")
		self.assertEqual(
			frappe.db.count("Ecommerce Fee Detail", {
				"company": self.company, "store_name": self.store,
				"source_type": "拼多多商品推广报表", "status": "有效",
			}),
			2,
		)
		performance = frappe.get_all(
			"Ecommerce Promotion Performance",
			filters={"import_batch": self.batch.name},
			pluck="attributed_sales",
		)
		self.assertEqual(sorted(performance), [150.0, 300.0])

	def test_pdd_report_store_override_beats_inference(self):
		"""批次指定店铺时，全部行入指定店铺，不再按商品ID推断。"""
		other_store = f"PDD指定店-{self.suffix}"
		other_channel = frappe.get_doc({
			"doctype": "Jackyun Sales Channel", "channel_id": f"PDD2-{self.suffix}",
			"channel_code": f"PDD2-{self.suffix}", "channel_name": other_store,
			"channel_type": "1", "disabled": 0, "deleted": 0,
			"online_platform_name": "拼多多", "platform_shop_name": other_store,
			"company_name": self.company,
		}).insert()
		batch = frappe.get_doc({
			"doctype": "Ecommerce Promotion Import Batch", "company": self.company,
			"import_file": "/files/test-pdd-override.xlsx",
			"sales_channel": other_channel.name,
		}).insert()
		headers = ["日期", "商品ID", "商品名称", "推广场景", "推广名称", "总花费(元)"]
		rows = [
			{"日期": "2026-08-25", "商品ID": self.goods_id, "商品名称": "x",
			 "推广场景": "稳定成本推广", "推广名称": "p", "总花费(元)": "88.00"},
			{"日期": "2026-08-25", "商品ID": "UNKNOWN-GOODS", "商品名称": "y",
			 "推广场景": "稳定成本推广", "推广名称": "p", "总花费(元)": "12.00"},
		]
		result = _import_pdd_product_report(batch, rows, headers)
		self.assertEqual(result["success_count"], 2)
		stores = set(frappe.get_all(
			"Ecommerce Promotion Expense",
			filters={"import_batch": batch.name}, pluck="store_name",
		))
		self.assertEqual(stores, {other_store})

		# 平台不符的指定店铺要报错
		tm_channel = frappe.get_doc({
			"doctype": "Jackyun Sales Channel", "channel_id": f"TMX-{self.suffix}",
			"channel_code": f"TMX-{self.suffix}", "channel_name": f"TM店-{self.suffix}",
			"channel_type": "1", "disabled": 0, "deleted": 0,
			"online_platform_name": "天猫", "platform_shop_name": f"TM店-{self.suffix}",
			"company_name": self.company,
		}).insert()
		batch2 = frappe.get_doc({
			"doctype": "Ecommerce Promotion Import Batch", "company": self.company,
			"import_file": "/files/test-pdd-override2.xlsx",
			"sales_channel": tm_channel.name,
		}).insert()
		with self.assertRaises(frappe.ValidationError):
			_import_pdd_product_report(batch2, rows, headers)


class TestAliHealthPromotionImport(IntegrationTestCase):
	def setUp(self):
		self.company = frappe.defaults.get_user_default("Company") or frappe.get_all("Company", pluck="name", limit=1)[0]
		self.suffix = frappe.generate_hash(length=8)
		self.store = f"AL测试店-{self.suffix}"
		self.al_channel = frappe.get_doc({
			"doctype": "Jackyun Sales Channel", "channel_id": f"AL-{self.suffix}",
			"channel_code": f"AL-{self.suffix}", "channel_name": self.store,
			"channel_type": "1", "disabled": 0, "deleted": 0,
			"online_platform_name": "阿里健康大药房（新）", "platform_shop_name": self.store,
			"company_name": self.company,
		}).insert()
		item_code = frappe.get_all("Item", filters={"disabled": 0}, pluck="name", limit=1)[0]
		listing = frappe.get_doc({
			"doctype": "Ecommerce Product Listing", "company": self.company,
			"channel_name": "阿里健康大药房（新）", "platform_code": "AL", "store_id": f"AL-{self.suffix}",
			"store_name": self.store, "platform_item_id": f"AL-{self.suffix}-1",
			"platform_item_name": "测试阿里健康商品",
		})
		listing.append("skus", {"platform_sku_id": f"AL-SKU-{self.suffix}", "item_code": item_code})
		listing.insert()
		self.batch = frappe.get_doc({
			"doctype": "Ecommerce Promotion Import Batch", "company": self.company,
			"import_file": "/files/test-alihealth-report.xlsx",
			"sales_channel": self.al_channel.name,
		}).insert()

	def test_alihealth_report_parses_compact_dates_and_infers_store(self):
		headers = [
			"账户id", "账户名称", "日期", "计划名称", "计划ID", "商品ID", "商品名称",
			"曝光量", "点击量", "消耗量", "订单量", "商品GMV", "总成交笔数", "总成交金额",
		]
		rows = [
			{"账户id": "4511", "账户名称": "医麦德-灵犀品牌自投", "日期": "20260824",
			 "计划名称": "全站推广", "计划ID": f"PLAN-{self.suffix}", "商品ID": f"AL-{self.suffix}-1",
			 "商品名称": "测试阿里健康商品", "曝光量": "3,655", "点击量": "439",
			 "消耗量": "575.48", "订单量": "10", "商品GMV": "1,500.00",
			 "总成交笔数": "61", "总成交金额": "2,300.00"},
			{"账户id": "4511", "账户名称": "医麦德-灵犀品牌自投", "日期": "20260825",
			 "计划名称": "全站推广", "计划ID": f"PLAN-{self.suffix}", "商品ID": f"AL-{self.suffix}-1",
			 "商品名称": "测试阿里健康商品", "曝光量": "800", "点击量": "120",
			 "消耗量": "150.00", "订单量": "3", "商品GMV": "500.00",
			 "总成交笔数": "9", "总成交金额": "700.00"},
		]

		for _run in range(2):  # 幂等
			result = _import_alihealth_product_report(self.batch, rows, headers)
			self.assertEqual(result["success_count"], 2)
			self.assertEqual(result["total_amount"], 725.48)

		expense = frappe.get_all(
			"Ecommerce Promotion Expense",
			filters={"import_batch": self.batch.name, "source_type": "阿里健康宝贝主体报表"},
			fields=["posting_date", "store_name", "amount", "match_status"],
			order_by="posting_date",
		)
		self.assertEqual(len(expense), 2)
		# 无链接档案 → 回退到平台唯一店铺
		self.assertEqual(expense[0]["store_name"], self.store)
		self.assertEqual(expense[0]["amount"], 575.48)
		self.assertEqual(expense[0]["match_status"], "已匹配")
		self.assertEqual(str(expense[0]["posting_date"]), "2026-08-24")
		performance = frappe.get_all(
			"Ecommerce Promotion Performance",
			filters={"import_batch": self.batch.name},
			fields=["attributed_sales", "direct_sales", "impressions"],
			order_by="attributed_sales",
		)
		self.assertEqual([p.attributed_sales for p in performance], [700.0, 2300.0])
		self.assertEqual([p.direct_sales for p in performance], [500.0, 1500.0])
		self.assertEqual(performance[0].impressions, 800)

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from io import BytesIO

import frappe
import xlsxwriter
from frappe import _
from frappe.desk.utils import provide_binary_file
from frappe.utils import cint, flt, getdate, now_datetime
from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file


TMALL_REQUIRED_COLUMNS = {
	"统计日期", "店铺名称", "转化周期", "场景名字", "计划ID", "计划名字",
	"主体ID", "主体类型", "主体名称", "展现量", "点击量", "花费",
	"总成交金额", "总成交笔数",
}
# 拼多多「商品推广_分天数据」报表：无店铺列，店铺由商品ID反查链接档案推断。
PDD_REQUIRED_COLUMNS = {"日期", "商品ID", "商品名称", "推广场景", "推广名称", "总花费(元)"}
PDD_CHANNEL = "拼多多"
# 阿里健康「宝贝主体基础报表」：日期无横杠（20260823），无店铺列，无归因周期。
ALIHEALTH_REQUIRED_COLUMNS = {"日期", "计划ID", "商品ID", "消耗量", "曝光量", "点击量"}
ALIHEALTH_CHANNEL = "阿里健康大药房（新）"
SUPPORTED_ATTRIBUTION_WINDOWS = {"1天转化", "15天转化"}

# 保留旧模板，方便历史人工费用文件继续使用；正式日常数据源是天猫商品推广报表。
COLUMNS = (
	("日期", True, "2026-08-21", "推广费用归属日期，格式 YYYY-MM-DD。"),
	("渠道", True, "天猫", "销售渠道的平台类型名称 online_platform_name。"),
	("店铺", True, "天猫旗舰店", "销售渠道的渠道名称 channel_name。"),
	("推广消耗", True, 1000, "当天实际推广消耗，不含货币符号。"),
	("推广账户", False, "主账户", "推广账户名称。"),
	("推广计划", False, "品牌词计划", "推广计划或单元名称。"),
	("平台商品编码", False, "ITEM-001", "平台商品或链接编码。"),
	("商品链接SkuID", False, "1929536092557", "子链接维度归集推广费时填写。"),
	("ERP物料", False, "ERP-ITEM-001", "ERPNext物料编码，存在时自动关联。"),
	("展现量", False, 10000, "整数。"),
	("点击量", False, 500, "整数。"),
	("推广成交订单", False, 20, "归因成交订单数。"),
	("推广成交金额", False, 3000, "归因成交金额。"),
)


def import_promotion_expenses(batch):
	"""导入天猫商品推广报表或兼容旧人工模板。

	天猫分析行同时保留两个归因周期；推广费另行按不含归因周期的键汇总，
	从而保证经营利润只扣一次费用。
	"""
	batch.check_permission("write")
	batch.db_set("status", "导入中")
	# 不用 read_only 流式模式：拼多多等平台导出的 xlsx 内部 dimension 声明有误，
	# 流式模式按声明截断只读到 1 格；完整模式几百行报表无性能问题。
	rows = read_xlsx_file_from_attached_file(file_url=batch.import_file)
	if not rows:
		frappe.throw(_("The uploaded workbook is empty."))

	headers = [_text(value) for value in rows[0]]
	data_rows = [dict(zip(headers, row, strict=False)) for row in rows[1:] if any(value not in (None, "") for value in row)]
	if TMALL_REQUIRED_COLUMNS.issubset(headers):
		return _import_tmall_product_report(batch, data_rows, headers)
	if PDD_REQUIRED_COLUMNS.issubset(headers):
		return _import_pdd_product_report(batch, data_rows, headers)
	if ALIHEALTH_REQUIRED_COLUMNS.issubset(headers):
		return _import_alihealth_product_report(batch, data_rows, headers)
	return _import_legacy_expense_report(batch, data_rows, headers)


def _import_tmall_product_report(batch, data_rows, headers):
	frappe.db.delete("Ecommerce Promotion Performance", {"import_batch": batch.name})
	errors = []
	success = matched = unmatched = 0
	# 同一投放事实可能在1天、15天归因行中各出现一次，费用必须只选一个周期。
	cost_by_fact = defaultdict(dict)
	subject_name_by_id = {}

	for row_no, row in enumerate(data_rows, start=2):
		savepoint = f"promotion_import_{row_no}"
		frappe.db.savepoint(savepoint)
		try:
			posting_date = getdate(row.get("统计日期"))
			window = _text(row.get("转化周期"))
			if window not in SUPPORTED_ATTRIBUTION_WINDOWS:
				frappe.throw(_("Only 1-day and 15-day attribution rows are supported."))
			report_store = _text(row.get("店铺名称"))
			channel, store = resolve_tmall_store(report_store, batch.company)
			campaign_id = _text(row.get("计划ID"))
			subject_id = _text(row.get("主体ID"))
			if not campaign_id or not subject_id:
				frappe.throw(_("Campaign ID and subject ID are required."))
			subject_name_by_id[subject_id] = _text(row.get("主体名称"))

			listing = match_product_listing(batch.company, channel, store, subject_id)
			match_status = "已匹配" if listing else "待匹配"
			matched += int(bool(listing))
			unmatched += int(not listing)
			amount = _number(row.get("花费"))
			if amount < 0:
				frappe.throw(_("Promotion expense cannot be negative."))

			key_parts = (batch.company, channel, store, str(posting_date), window, campaign_id, subject_id)
			performance = {
				"doctype": "Ecommerce Promotion Performance",
				"source_key": _source_key(*key_parts),
				"posting_date": posting_date,
				"company": batch.company,
				"channel_name": channel,
				"store_name": store,
				"source_store_name": report_store,
				"attribution_window": window,
				"promotion_scene": _text(row.get("场景名字")),
				"original_scene": _text(row.get("原二级场景名字")),
				"campaign_id": campaign_id,
				"campaign_name": _text(row.get("计划名字")),
				"subject_id": subject_id,
				"subject_type": _text(row.get("主体类型")),
				"subject_name": _text(row.get("主体名称")),
				"listing": listing,
				"match_status": match_status,
				"impressions": cint(_number(row.get("展现量"))),
				"clicks": cint(_number(row.get("点击量"))),
				"amount": amount,
				"attributed_orders": cint(_number(row.get("总成交笔数"))),
				"attributed_sales": _number(row.get("总成交金额")),
				"direct_orders": cint(_number(row.get("直接成交笔数"))),
				"direct_sales": _number(row.get("直接成交金额")),
				"cart_count": cint(_number(row.get("总购物车数"))),
				"favourite_count": cint(_number(row.get("收藏宝贝数"))),
				"new_customer_count": cint(_number(row.get("成交新客数"))),
				"raw_metrics_json": json.dumps(
					{header: _json_value(row.get(header)) for header in headers}, ensure_ascii=False, separators=(",", ":")
				),
				"import_batch": batch.name,
				"source_row_no": row_no,
			}
			_upsert_by_key("Ecommerce Promotion Performance", "source_key", performance)
			fact_key = (batch.company, channel, store, str(posting_date), campaign_id, subject_id)
			cost_by_fact[fact_key][window] = amount
			success += 1
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			errors.append(_("Row {0}: {1}").format(row_no, exc))

	fee_totals = defaultdict(float)
	for fact_key, windows in cost_by_fact.items():
		company, channel, store, posting_date, _campaign_id, subject_id = fact_key
		amount = windows.get("1天转化", windows.get("15天转化", 0))
		fee_totals[(company, channel, store, posting_date, subject_id)] += amount

	for (company, channel, store, posting_date, subject_id), amount in fee_totals.items():
		listing = match_product_listing(company, channel, store, subject_id)
		fee_key = _source_key("tmall-product-fee", company, channel, store, posting_date, subject_id)
		_upsert_by_key(
			"Ecommerce Promotion Expense", "fee_unique_key",
			{
				"doctype": "Ecommerce Promotion Expense",
				"fee_unique_key": fee_key,
				"posting_date": posting_date,
				"company": company,
				"channel_name": channel,
				"store_name": store,
				"amount": amount,
				"source_type": "天猫商品推广报表",
				"source_subject_id": subject_id,
				"source_subject_name": subject_name_by_id.get(subject_id, ""),
				# 待匹配费用不写链接ID，因此先进入经营仪表盘的店铺口径。
				"platform_item_code": subject_id if listing else None,
				"match_status": "已匹配" if listing else "待匹配",
				"import_batch": batch.name,
			},
		)
		fee_item = _ensure_promotion_fee_item()
		_upsert_by_key(
			"Ecommerce Fee Detail", "source_unique_key",
			{
				"doctype": "Ecommerce Fee Detail", "posting_date": posting_date,
				"company": company, "fee_item": fee_item, "amount": amount,
				"currency": frappe.db.get_value("Company", company, "default_currency"),
				"channel_name": channel, "store_name": store,
				"product_listing": listing, "allocation_method": "不分摊",
				"entry_type": "实际", "source_type": "天猫商品推广报表",
				"source_reference_doctype": "Ecommerce Promotion Import Batch",
				"source_reference_name": batch.name, "source_unique_key": fee_key,
				"status": "有效", "remarks": f"天猫商品主体ID：{subject_id}",
			},
		)

	total_amount = sum(fee_totals.values())
	return _finish_batch(batch, data_rows, success, errors, total_amount, matched, unmatched, "天猫商品推广报表")


def _safe_pdd_date(value):
	"""解析拼多多/阿里健康报表日期；「总计」/说明行返回 None。阿里健康为无横杠的 YYYYMMDD。"""
	text = _text(value)
	if len(text) == 8 and text.isdigit():
		try:
			return getdate(f"{text[:4]}-{text[4:6]}-{text[6:]}")
		except Exception:
			return None
	try:
		return getdate(text, parse_day_first=False)
	except Exception:
		return None


def _resolve_pdd_stores(company, subject_ids):
	return _resolve_report_stores(company, PDD_CHANNEL, subject_ids)


def _batch_store_override(batch, expected_channel):
	"""批次指定了店铺时返回 (渠道, 店铺)，并校验店铺平台与报表格式一致。"""
	selected = frappe.utils.cstr(getattr(batch, "sales_channel", "") or "").strip()
	if not selected:
		return None
	row = frappe.db.get_value(
		"Jackyun Sales Channel", selected,
		["channel_name", "online_platform_name"], as_dict=True,
	)
	if not row:
		frappe.throw(_("指定店铺不存在或已停用：{0}").format(selected))
	if row.online_platform_name != expected_channel:
		frappe.throw(
			_("指定店铺的平台是 {0}，与报表格式（{1}）不一致，请重新选择。").format(
				row.online_platform_name, expected_channel
			)
		)
	return expected_channel, row.channel_name


def _resolve_report_stores(company, channel, subject_ids):
	"""按商品ID反查渠道链接档案，返回 {商品ID: 店铺名}。

	无档案商品依次回退：该平台下唯一启用店铺 → 本文件主力店铺（命中最多）。
	两个回退都不可用时抛错提示先建链接档案。
	"""
	mapping = {}
	if not subject_ids:
		frappe.throw(_("No goods rows found in the report."))
	for start in range(0, len(subject_ids), 500):
		chunk = list(subject_ids)[start : start + 500]
		for row in frappe.get_all(
			"Ecommerce Product Listing",
			filters={
				"company": company,
				"channel_name": channel,
				"platform_item_id": ["in", chunk],
				"disabled": 0,
			},
			fields=["platform_item_id", "store_name"],
		):
			mapping[row.platform_item_id] = row.store_name
	if not mapping:
		# 平台唯一店铺回退（如阿里健康：报表无店铺列且链接档案未建）
		stores = frappe.get_all(
			"Jackyun Sales Channel",
			filters={
				"online_platform_name": channel,
				"channel_type": "1", "disabled": 0, "deleted": 0,
			},
			pluck="channel_name",
		)
		if len(stores) == 1:
			return {subject_id: stores[0] for subject_id in subject_ids}
		frappe.throw(
			_("报表里的商品ID都未在「渠道商品链接」建档，请先维护链接档案再导入。")
		)
	from collections import Counter
	dominant = Counter(mapping.values()).most_common(1)[0][0]
	return {subject_id: mapping.get(subject_id, dominant) for subject_id in subject_ids}


def _import_pdd_product_report(batch, data_rows, headers):
	"""导入拼多多「商品推广_分天数据」报表。

	报表无店铺列：店铺由商品ID反查链接档案推断（未建档商品归主力店铺）。
	拼多多无归因周期概念，统一存为「1天转化」；跳过「总计」与说明行。
	"""
	frappe.db.delete("Ecommerce Promotion Performance", {"import_batch": batch.name})
	errors = []
	success = matched = unmatched = 0
	fee_totals = defaultdict(float)
	override = _batch_store_override(batch, PDD_CHANNEL)
	if override:
		store_by_subject = None  # 指定店铺：跳过商品ID推断
	else:
		subject_ids = {
			_text(row.get("商品ID"))
			for row in data_rows
			if _text(row.get("商品ID")) and _safe_pdd_date(row.get("日期"))
		}
		store_by_subject = _resolve_pdd_stores(batch.company, subject_ids)

	for row_no, row in enumerate(data_rows, start=2):
		savepoint = f"promotion_import_{row_no}"
		frappe.db.savepoint(savepoint)
		try:
			subject_id = _text(row.get("商品ID"))
			# 跳过「总计」行、说明行和无商品ID的行
			posting_date = _safe_pdd_date(row.get("日期"))
			if not posting_date or not subject_id:
				continue
			store = override[1] if override else store_by_subject[subject_id]
			amount = _number(row.get("总花费(元)"))
			if amount < 0:
				frappe.throw(_("Promotion expense cannot be negative."))
			listing = match_product_listing(batch.company, PDD_CHANNEL, store, subject_id)
			match_status = "已匹配" if listing else "待匹配"
			matched += int(bool(listing))
			unmatched += int(not listing)
			campaign_name = _text(row.get("推广名称")) or _text(row.get("推广场景"))

			key_parts = (batch.company, PDD_CHANNEL, store, str(posting_date), "1天转化", campaign_name, subject_id)
			performance = {
				"doctype": "Ecommerce Promotion Performance",
				"source_key": _source_key(*key_parts),
				"posting_date": posting_date,
				"company": batch.company,
				"channel_name": PDD_CHANNEL,
				"store_name": store,
				"source_store_name": store,
				"attribution_window": "1天转化",
				"promotion_scene": _text(row.get("推广场景")),
				"original_scene": _text(row.get("出价方式")),
				"campaign_id": campaign_name,
				"campaign_name": campaign_name,
				"subject_id": subject_id,
				"subject_type": "商品",
				"subject_name": _text(row.get("商品名称")),
				"listing": listing,
				"match_status": match_status,
				"impressions": cint(_number(row.get("曝光量"))),
				"clicks": cint(_number(row.get("点击量"))),
				"amount": amount,
				"attributed_orders": cint(_number(row.get("成交笔数"))),
				"attributed_sales": _number(row.get("交易额(元)")),
				"direct_orders": cint(_number(row.get("直接成交笔数"))),
				"direct_sales": _number(row.get("直接交易额(元)")),
				"raw_metrics_json": json.dumps(
					{header: _json_value(row.get(header)) for header in headers}, ensure_ascii=False, separators=(",", ":")
				),
				"import_batch": batch.name,
				"source_row_no": row_no,
			}
			_upsert_by_key("Ecommerce Promotion Performance", "source_key", performance)
			fee_totals[(batch.company, PDD_CHANNEL, store, str(posting_date), subject_id)] += amount
			success += 1
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			errors.append(_("Row {0}: {1}").format(row_no, exc))

	for (company, channel, store, posting_date, subject_id), amount in fee_totals.items():
		listing = match_product_listing(company, channel, store, subject_id)
		fee_key = _source_key("pdd-product-fee", company, channel, store, posting_date, subject_id)
		_upsert_by_key(
			"Ecommerce Promotion Expense", "fee_unique_key",
			{
				"doctype": "Ecommerce Promotion Expense",
				"fee_unique_key": fee_key,
				"posting_date": posting_date,
				"company": company,
				"channel_name": channel,
				"store_name": store,
				"amount": amount,
				"source_type": "拼多多商品推广报表",
				"source_subject_id": subject_id,
				"source_subject_name": "",
				"platform_item_code": subject_id if listing else None,
				"match_status": "已匹配" if listing else "待匹配",
				"import_batch": batch.name,
			},
		)
		fee_item = _ensure_promotion_fee_item()
		_upsert_by_key(
			"Ecommerce Fee Detail", "source_unique_key",
			{
				"doctype": "Ecommerce Fee Detail", "posting_date": posting_date,
				"company": company, "fee_item": fee_item, "amount": amount,
				"currency": frappe.db.get_value("Company", company, "default_currency"),
				"channel_name": channel, "store_name": store,
				"product_listing": listing, "allocation_method": "不分摊",
				"entry_type": "实际", "source_type": "拼多多商品推广报表",
				"source_reference_doctype": "Ecommerce Promotion Import Batch",
				"source_reference_name": batch.name, "source_unique_key": fee_key,
				"status": "有效", "remarks": f"拼多多商品ID：{subject_id}",
			},
		)

	total_amount = sum(fee_totals.values())
	return _finish_batch(batch, data_rows, success, errors, total_amount, matched, unmatched, "拼多多商品推广报表")


def _import_alihealth_product_report(batch, data_rows, headers):
	"""导入阿里健康「宝贝主体基础报表」。

	无店铺列、无归因周期：店铺由商品ID反查链接档案（回退平台唯一店铺），
	事实统一存「1天转化」。消耗量=花费，总成交金额/总成交笔数=归因成交，
	商品GMV/订单量=直接成交。
	"""
	frappe.db.delete("Ecommerce Promotion Performance", {"import_batch": batch.name})
	errors = []
	success = matched = unmatched = 0
	fee_totals = defaultdict(float)
	override = _batch_store_override(batch, ALIHEALTH_CHANNEL)
	if override:
		store_by_subject = None
	else:
		subject_ids = {
			_text(row.get("商品ID"))
			for row in data_rows
			if _text(row.get("商品ID")) and _safe_pdd_date(row.get("日期"))
		}
		store_by_subject = _resolve_report_stores(batch.company, ALIHEALTH_CHANNEL, subject_ids)

	for row_no, row in enumerate(data_rows, start=2):
		savepoint = f"promotion_import_{row_no}"
		frappe.db.savepoint(savepoint)
		try:
			subject_id = _text(row.get("商品ID"))
			posting_date = _safe_pdd_date(row.get("日期"))
			if not posting_date or not subject_id:
				continue
			store = override[1] if override else store_by_subject[subject_id]
			amount = _number(row.get("消耗量"))
			if amount < 0:
				frappe.throw(_("Promotion expense cannot be negative."))
			listing = match_product_listing(batch.company, ALIHEALTH_CHANNEL, store, subject_id)
			match_status = "已匹配" if listing else "待匹配"
			matched += int(bool(listing))
			unmatched += int(not listing)
			campaign_id = _text(row.get("计划ID"))

			key_parts = (batch.company, ALIHEALTH_CHANNEL, store, str(posting_date), "1天转化", campaign_id, subject_id)
			performance = {
				"doctype": "Ecommerce Promotion Performance",
				"source_key": _source_key(*key_parts),
				"posting_date": posting_date,
				"company": batch.company,
				"channel_name": ALIHEALTH_CHANNEL,
				"store_name": store,
				"source_store_name": store,
				"attribution_window": "1天转化",
				"promotion_scene": "",
				"original_scene": _text(row.get("账户名称")),
				"campaign_id": campaign_id,
				"campaign_name": _text(row.get("计划名称")),
				"subject_id": subject_id,
				"subject_type": "商品",
				"subject_name": _text(row.get("商品名称")),
				"listing": listing,
				"match_status": match_status,
				"impressions": cint(_number(row.get("曝光量"))),
				"clicks": cint(_number(row.get("点击量"))),
				"amount": amount,
				"attributed_orders": cint(_number(row.get("总成交笔数"))),
				"attributed_sales": _number(row.get("总成交金额")),
				"direct_orders": cint(_number(row.get("订单量"))),
				"direct_sales": _number(row.get("商品GMV")),
				"raw_metrics_json": json.dumps(
					{header: _json_value(row.get(header)) for header in headers}, ensure_ascii=False, separators=(",", ":")
				),
				"import_batch": batch.name,
				"source_row_no": row_no,
			}
			_upsert_by_key("Ecommerce Promotion Performance", "source_key", performance)
			fee_totals[(batch.company, ALIHEALTH_CHANNEL, store, str(posting_date), subject_id)] += amount
			success += 1
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			errors.append(_("Row {0}: {1}").format(row_no, exc))

	for (company, channel, store, posting_date, subject_id), amount in fee_totals.items():
		listing = match_product_listing(company, channel, store, subject_id)
		fee_key = _source_key("alihealth-product-fee", company, channel, store, posting_date, subject_id)
		_upsert_by_key(
			"Ecommerce Promotion Expense", "fee_unique_key",
			{
				"doctype": "Ecommerce Promotion Expense",
				"fee_unique_key": fee_key,
				"posting_date": posting_date,
				"company": company,
				"channel_name": channel,
				"store_name": store,
				"amount": amount,
				"source_type": "阿里健康宝贝主体报表",
				"source_subject_id": subject_id,
				"source_subject_name": "",
				"platform_item_code": subject_id if listing else None,
				"match_status": "已匹配" if listing else "待匹配",
				"import_batch": batch.name,
			},
		)
		fee_item = _ensure_promotion_fee_item()
		_upsert_by_key(
			"Ecommerce Fee Detail", "source_unique_key",
			{
				"doctype": "Ecommerce Fee Detail", "posting_date": posting_date,
				"company": company, "fee_item": fee_item, "amount": amount,
				"currency": frappe.db.get_value("Company", company, "default_currency"),
				"channel_name": channel, "store_name": store,
				"product_listing": listing, "allocation_method": "不分摊",
				"entry_type": "实际", "source_type": "阿里健康宝贝主体报表",
				"source_reference_doctype": "Ecommerce Promotion Import Batch",
				"source_reference_name": batch.name, "source_unique_key": fee_key,
				"status": "有效", "remarks": f"阿里健康商品ID：{subject_id}",
			},
		)

	total_amount = sum(fee_totals.values())
	return _finish_batch(batch, data_rows, success, errors, total_amount, matched, unmatched, "阿里健康宝贝主体报表")


def _import_legacy_expense_report(batch, data_rows, headers):
	missing = {label for label, required, *_ in COLUMNS if required} - set(headers)
	if missing:
		frappe.throw(_("Missing required columns: {0}").format(", ".join(sorted(missing))))
	frappe.db.delete("Ecommerce Promotion Expense", {"import_batch": batch.name, "source_type": ["in", ["", "人工模板"]]})
	errors = []
	success = 0
	total_amount = 0.0
	for row_no, row in enumerate(data_rows, start=2):
		savepoint = f"promotion_import_{row_no}"
		frappe.db.savepoint(savepoint)
		try:
			posting_date = getdate(row.get("日期"))
			channel = _text(row.get("渠道"))
			store = _text(row.get("店铺"))
			amount = _number(row.get("推广消耗"))
			validate_channel_store(channel, store)
			if amount < 0:
				frappe.throw(_("Promotion expense cannot be negative."))
			item_code = _text(row.get("ERP物料"))
			if item_code and not frappe.db.exists("Item", item_code):
				frappe.throw(_("ERP item {0} does not exist.").format(item_code))
			frappe.get_doc({
				"doctype": "Ecommerce Promotion Expense", "posting_date": posting_date,
				"company": batch.company, "channel_name": channel, "store_name": store,
				"amount": amount, "promotion_account": _text(row.get("推广账户")),
				"campaign_name": _text(row.get("推广计划")),
				"platform_item_code": _text(row.get("平台商品编码")),
				"platform_sku": _text(row.get("商品链接SkuID") or row.get("平台SKU")),
				"item_code": item_code or None, "source_type": "人工模板",
				"impressions": cint(_number(row.get("展现量"))), "clicks": cint(_number(row.get("点击量"))),
				"attributed_orders": cint(_number(row.get("推广成交订单"))),
				"attributed_sales": _number(row.get("推广成交金额")),
				"import_batch": batch.name, "source_row_no": row_no,
			}).insert()
			success += 1
			total_amount += amount
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			errors.append(_("Row {0}: {1}").format(row_no, exc))
	return _finish_batch(batch, data_rows, success, errors, total_amount, 0, 0, "人工模板")


def _finish_batch(batch, data_rows, success, errors, total_amount, matched, unmatched, report_type):
	values = {
		"status": "导入成功" if not errors else ("部分失败" if success else "导入失败"),
		"report_type": report_type, "imported_at": now_datetime(), "imported_by": frappe.session.user,
		"total_rows": len(data_rows), "success_count": success, "failed_count": len(errors),
		"matched_count": matched, "unmatched_count": unmatched, "total_amount": total_amount,
		"error_log": "\n".join(errors),
	}
	batch.db_set(values)
	return values


def _upsert_by_key(doctype, key_field, values):
	name = frappe.db.get_value(doctype, {key_field: values[key_field]}, "name")
	if name:
		doc = frappe.get_doc(doctype, name)
		doc.update(values)
		doc.save()
		return doc
	return frappe.get_doc(values).insert()


def _ensure_promotion_fee_item():
	from yimed_ecommerce.setup import ensure_system_fee_items

	return ensure_system_fee_items()


def resolve_tmall_store(report_store: str, company: str | None = None):
	"""将天猫文件的店铺名称解析为经营仪表盘使用的规范渠道/店铺。"""
	report_store = _text(report_store)
	if not report_store:
		frappe.throw(_("Store is required."))
	if not frappe.db.table_exists("Jackyun Sales Channel"):
		frappe.throw(_("Sales channel master data is unavailable."))
	rows = frappe.get_all(
		"Jackyun Sales Channel", filters=_active_channel_filters(),
		fields=["online_platform_name", "channel_name", "platform_shop_name", "company_name"],
	)
	candidates = [
		row for row in rows
		if ("天猫" in (row.online_platform_name or "") or "淘宝" in (row.online_platform_name or ""))
		and report_store in {row.channel_name, row.platform_shop_name}
	]
	company_matches = [row for row in candidates if company and row.company_name == company]
	if len(company_matches) == 1:
		candidates = company_matches
	pairs = {(row.online_platform_name, row.channel_name) for row in candidates if row.channel_name}
	if len(pairs) != 1:
		if not pairs:
			frappe.throw(_("Tmall store {0} is not mapped in sales channel master data.").format(frappe.bold(report_store)))
		frappe.throw(_("Tmall store {0} maps to multiple active channels.").format(frappe.bold(report_store)))
	return next(iter(pairs))


def match_product_listing(company, channel, store, subject_id):
	if not subject_id or not frappe.db.table_exists("Ecommerce Product Listing"):
		return None
	return frappe.db.get_value(
		"Ecommerce Product Listing",
		{"company": company, "channel_name": channel, "store_name": store,
		 "platform_item_id": subject_id, "disabled": 0},
		"name",
	)


@frappe.whitelist()
def rematch_unmatched_promotions(company: str | None = None):
	"""重新匹配待处理主体，并把店铺级推广费转为链接直接归属。"""
	frappe.only_for(("Accounts User", "Accounts Manager", "System Manager"))
	filters = {"match_status": "待匹配"}
	if company:
		filters["company"] = company
	updated_performance = 0
	updated_expenses = 0
	for row in frappe.get_all(
		"Ecommerce Promotion Performance", filters=filters,
		fields=["name", "company", "channel_name", "store_name", "subject_id"],
	):
		listing = match_product_listing(row.company, row.channel_name, row.store_name, row.subject_id)
		if listing:
			frappe.db.set_value("Ecommerce Promotion Performance", row.name,
				{"listing": listing, "match_status": "已匹配"}, update_modified=False)
			updated_performance += 1
	for row in frappe.get_all(
		"Ecommerce Promotion Expense", filters={**filters, "source_type": "天猫商品推广报表"},
		fields=["name", "company", "channel_name", "store_name", "source_subject_id"],
	):
		listing = match_product_listing(row.company, row.channel_name, row.store_name, row.source_subject_id)
		if listing:
			frappe.db.set_value("Ecommerce Promotion Expense", row.name,
				{"platform_item_code": row.source_subject_id, "match_status": "已匹配"}, update_modified=False)
			fee_key = frappe.db.get_value("Ecommerce Promotion Expense", row.name, "fee_unique_key")
			if fee_key:
				fee_detail = frappe.db.get_value("Ecommerce Fee Detail", {"source_unique_key": fee_key}, "name")
				if fee_detail:
					frappe.db.set_value("Ecommerce Fee Detail", fee_detail,
						{"product_listing": listing, "allocation_method": "不分摊"}, update_modified=False)
			updated_expenses += 1
	return {"performance_rows": updated_performance, "expense_rows": updated_expenses}


def _source_key(*parts):
	return hashlib.sha256("\x1f".join(_text(value) for value in parts).encode()).hexdigest()


def _number(value):
	text = _text(value).replace(",", "")
	return 0.0 if not text or text.upper() == "NULL" else flt(text)


def _json_value(value):
	return None if value in (None, "") else str(value)


def _text(value):
	return "" if value in (None, "") else str(value).strip()


def validate_channel_store(channel_name: str | None = None, store_name: str | None = None):
	channel_name, store_name = _text(channel_name), _text(store_name)
	if not channel_name and not store_name:
		return
	if not frappe.db.table_exists("Jackyun Sales Channel"):
		frappe.throw(_("Sales channel master data is unavailable."))
	filters = _active_channel_filters()
	if channel_name:
		filters["online_platform_name"] = channel_name
	if store_name:
		filters["channel_name"] = store_name
	if frappe.db.exists("Jackyun Sales Channel", filters):
		return
	frappe.throw(_("Store {0} is not an active channel name under platform {1}.").format(frappe.bold(store_name), frappe.bold(channel_name)))


def _active_channel_filters():
	filters = {"channel_type": "1"} if frappe.db.has_column("Jackyun Sales Channel", "channel_type") else {}
	for fieldname in ("disabled", "deleted"):
		if frappe.db.has_column("Jackyun Sales Channel", fieldname):
			filters[fieldname] = 0
	return filters


def _check_scope_options_permission():
	if frappe.has_permission("Ecommerce Fee Rule", "read") or frappe.has_permission("Ecommerce Promotion Import Batch", "read"):
		return
	frappe.throw(_("Not permitted"), frappe.PermissionError)


@frappe.whitelist()
def get_channel_store_options(company: str | None = None):
	_check_scope_options_permission()
	if not frappe.db.table_exists("Jackyun Sales Channel"):
		return {"channels": [], "stores": []}
	rows = frappe.get_all("Jackyun Sales Channel", filters=_active_channel_filters(),
		fields=["online_platform_name", "channel_name"], order_by="online_platform_name, channel_name")
	pairs = {(row.online_platform_name, row.channel_name) for row in rows if row.online_platform_name and row.channel_name}
	return {"channels": sorted({channel for channel, _store in pairs}),
		"stores": [{"channel": channel, "store": store} for channel, store in sorted(pairs)]}


def backfill_legacy_store_names(dry_run: bool = True):
	"""幂等迁移旧 platform_shop_name，仅自动处理唯一匹配。"""
	dry_run = bool(cint(dry_run))
	if not frappe.db.table_exists("Jackyun Sales Channel"):
		return {"dry_run": dry_run, "updated": [], "ambiguous": [], "unmatched": []}
	channels = frappe.get_all(
		"Jackyun Sales Channel",
		filters={"deleted": 0} if frappe.db.has_column("Jackyun Sales Channel", "deleted") else {},
		fields=["online_platform_name", "channel_name", "platform_shop_name", "company_name"],
	)
	canonical = {(row.online_platform_name or "", row.channel_name or "") for row in channels if row.channel_name}
	canonical_stores = {row.channel_name for row in channels if row.channel_name}
	aliases, company_aliases, store_aliases, company_store_aliases = {}, {}, {}, {}
	for row in channels:
		if not row.channel_name or not row.platform_shop_name:
			continue
		aliases.setdefault((row.online_platform_name or "", row.platform_shop_name), set()).add(row.channel_name)
		company_aliases.setdefault((row.company_name or "", row.online_platform_name or "", row.platform_shop_name), set()).add(row.channel_name)
		store_aliases.setdefault(row.platform_shop_name, set()).add(row.channel_name)
		company_store_aliases.setdefault((row.company_name or "", row.platform_shop_name), set()).add(row.channel_name)
	result = {"dry_run": dry_run, "updated": [], "ambiguous": [], "unmatched": []}
	for doctype in ("Ecommerce Promotion Expense",):
		if not frappe.db.table_exists(doctype):
			continue
		for row in frappe.get_all(doctype, fields=["name", "company", "channel_name", "store_name"]):
			channel, store = row.channel_name or "", row.store_name or ""
			if not store or (channel and (channel, store) in canonical) or (not channel and store in canonical_stores):
				continue
			if channel:
				candidates = company_aliases.get((row.company or "", channel, store), set()) or aliases.get((channel, store), set())
			else:
				candidates = company_store_aliases.get((row.company or "", store), set()) or store_aliases.get(store, set())
			if len(candidates) == 1:
				target = next(iter(candidates))
				change = {"doctype": doctype, "name": row.name, "from": store, "to": target}
				result["updated"].append(change)
				if not dry_run:
					frappe.db.set_value(doctype, row.name, "store_name", target, update_modified=False)
			elif len(candidates) > 1:
				result["ambiguous"].append({"doctype": doctype, "name": row.name, "store": store, "candidates": sorted(candidates)})
			else:
				result["unmatched"].append({"doctype": doctype, "name": row.name, "store": store})
	return result


def build_import_template() -> bytes:
	output = BytesIO()
	workbook = xlsxwriter.Workbook(output, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False})
	header = workbook.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1, "align": "center"})
	required = workbook.add_format({"bold": True, "bg_color": "#FCE4D6", "font_color": "#C00000", "border": 1, "align": "center"})
	cell = workbook.add_format({"border": 1})
	sheet = workbook.add_worksheet("推广费导入")
	sheet.freeze_panes(1, 0)
	for column, (label, is_required, sample, description) in enumerate(COLUMNS):
		sheet.write(0, column, label, required if is_required else header)
		sheet.write(1, column, sample, cell)
		sheet.write_comment(0, column, ("必填；" if is_required else "选填；") + description)
		sheet.set_column(column, column, max(12, min(28, len(str(sample)) + 4)))
	sheet.autofilter(0, 0, 1, len(COLUMNS) - 1)
	workbook.close()
	output.seek(0)
	return output.getvalue()


@frappe.whitelist()
def download_import_template():
	frappe.has_permission("Ecommerce Promotion Import Batch", "read", throw=True)
	provide_binary_file("电商推广费导入模板", "xlsx", build_import_template())

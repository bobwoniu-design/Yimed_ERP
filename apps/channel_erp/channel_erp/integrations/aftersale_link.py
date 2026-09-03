"""吉客云售后单反查：SH 售后单号 → 原交易单号（tradeNo）。

销售退货入库单（inouttype=105）的 billNo 是售后单号（SH...），不含原交易单号；
通过 ass-business.returnchange.fullinfoget 反查建立映射，供退货归因到原订单日期。
"""
from __future__ import annotations

import frappe

from channel_erp.integrations.jackyun import JackYunAdapter

RETURNCHANGE_METHOD = "ass-business.returnchange.fullinfoget"

# 吉客云负数销售单类型：店铺利润表的「销售退货」数据源（订单类型=售后退货/仅退款）
REFUND_ORDER_TYPES = [8, 12]
REFUND_ORDER_CANCELLED_STATUSES = {4121, 4122, 5010, 5020, 5030}


def _get_adapter(connection_name=None) -> JackYunAdapter:
	if connection_name is None:
		connections = frappe.get_all("Jackyun Connection", filters={"enabled": 1}, pluck="name")
		if not connections:
			frappe.throw("没有启用的吉客云连接")
		connection_name = connections[0]
	return JackYunAdapter(frappe.get_doc("Jackyun Connection", connection_name))


def _extract_items(payload: dict) -> list[dict]:
	data = (payload.get("result") or {}).get("data") or {}
	if isinstance(data, dict):
		rows = data.get("returnChangeList") or []
		if isinstance(rows, list):
			return [row for row in rows if isinstance(row, dict)]
	return []


def resolve_source_trade_no(aftersale_no: str, adapter: JackYunAdapter | None = None) -> str:
	"""按售后单号反查原吉客云交易单号；查不到返回空串。"""
	aftersale_no = frappe.utils.cstr(aftersale_no).strip()
	if not aftersale_no:
		return ""
	adapter = adapter or _get_adapter()
	try:
		payload = adapter.request(
			RETURNCHANGE_METHOD,
			{"returnChangeNo": aftersale_no},
			page_index=0,
			page_size=5,
		)
	except Exception:
		frappe.log_error(
			title="吉客云售后单反查失败",
			message=f"{aftersale_no}: {frappe.get_traceback()}",
		)
		return ""
	for row in _extract_items(payload):
		if frappe.utils.cstr(row.get("returnChangeNo")).strip() == aftersale_no:
			return frappe.utils.cstr(row.get("tradeNo")).strip()
	return ""


def fetch_aftersale_trade_map(from_datetime, to_datetime, connection_name=None) -> dict[str, str]:
	"""按修改时间窗批量拉取售后单，返回 {售后单号: 原交易单号}。"""
	adapter = _get_adapter(connection_name)
	trade_map: dict[str, str] = {}
	page_index = 0
	while True:
		payload = adapter.request(
			RETURNCHANGE_METHOD,
			{
				"startModified": frappe.utils.get_datetime(from_datetime).strftime("%Y-%m-%d %H:%M:%S"),
				"endModified": frappe.utils.get_datetime(to_datetime).strftime("%Y-%m-%d %H:%M:%S"),
			},
			page_index=page_index,
			page_size=200,
		)
		items = _extract_items(payload)
		for row in items:
			aftersale_no = frappe.utils.cstr(row.get("returnChangeNo")).strip()
			trade_no = frappe.utils.cstr(row.get("tradeNo")).strip()
			if aftersale_no and trade_no:
				trade_map[aftersale_no] = trade_no
		if not items or len(items) < 200 or page_index > 100:
			break
		page_index += 1
	return trade_map


def backfill_return_source_trade_nos(dry_run: bool = False) -> dict:
	"""为缺少来源销售单号的退货单批量回填（历史数据修复）。

	原始记录的 goodsdocNo 即退货入库单号（CRK...），billNo 即售后单号（SH...）；
	按时间窗批量反查售后单得到 售后单号 → 原交易单号 后写回退货单。
	"""
	import json

	# 1. 原始记录建立 DN名 -> 售后单号 映射
	dn_to_aftersale: dict[str, str] = {}
	for row in frappe.get_all(
		"Jackyun Raw Record",
		filters={"resource": "Sales Return"},
		fields=["raw_data"],
	):
		try:
			raw = json.loads(row.raw_data)
		except (TypeError, ValueError):
			continue
		goodsdoc_no = frappe.utils.cstr(raw.get("goodsdocNo")).strip()
		aftersale_no = frappe.utils.cstr(raw.get("billNo")).strip()
		if goodsdoc_no and aftersale_no:
			dn_to_aftersale[goodsdoc_no] = aftersale_no

	# 2. 待回填的退货单
	pending = frappe.get_all(
		"Delivery Note",
		filters={
			"is_return": 1,
			"docstatus": 1,
			"custom_jackyun_source_trade_no": ["in", ["", None]],
		},
		pluck="name",
	)
	earliest = frappe.db.sql(
		"""select min(posting_date) from `tabDelivery Note`
		    where is_return=1 and docstatus=1 and name in %s""",
		(tuple(pending),),
	)[0][0]

	# 3. 时间窗批量反查（窗口向前多留 30 天覆盖售后创建早于退货入库的情况）
	if not pending:
		return {"pending": 0, "filled": 0}
	trade_map = fetch_aftersale_trade_map(
		frappe.utils.add_days(earliest, -30), frappe.utils.now_datetime()
	)

	filled, unmatched = 0, []
	for dn_name in pending:
		aftersale_no = dn_to_aftersale.get(dn_name)
		trade_no = trade_map.get(aftersale_no or "")
		if not trade_no:
			unmatched.append(dn_name)
			continue
		if not dry_run:
			frappe.db.set_value(
				"Delivery Note", dn_name,
				"custom_jackyun_source_trade_no", trade_no,
				update_modified=False,
			)
		filled += 1
	if not dry_run:
		frappe.db.commit()
	return {
		"pending": len(pending),
		"filled": filled,
		"unmatched": unmatched[:10],
		"unmatched_count": len(unmatched),
	}


def sync_aftersale_refunds(since=None) -> dict:
	"""同步售后退款镜像（吉客云净销售的扣减来源，含在途退货与仅退款）。

	增量游标取镜像里最大的 gmt_modified，回退 5 分钟重叠窗口防漏单。
	"""
	adapter = _get_adapter()
	if since is None:
		since = frappe.db.sql(
			"""select max(gmt_modified) - interval 5 minute
			     from `tabJackyun Aftersale Refund`"""
		)[0][0]
		if since is None:
			since = frappe.utils.add_days(frappe.utils.now_datetime(), -45)
	until = frappe.utils.now_datetime()

	created = updated = 0
	page_index = 0
	while True:
		payload = adapter.request(
			RETURNCHANGE_METHOD,
			{
				"startModified": frappe.utils.get_datetime(since).strftime("%Y-%m-%d %H:%M:%S"),
				"endModified": until.strftime("%Y-%m-%d %H:%M:%S"),
			},
			page_index=page_index,
			page_size=200,
		)
		items = _extract_items(payload)
		for row in items:
			if _upsert_aftersale_refund(row):
				created += 1
			else:
				updated += 1
		if not items or len(items) < 200 or page_index > 100:
			break
		page_index += 1
	frappe.db.commit()
	return {"since": str(since), "created": created, "updated": updated}


def _upsert_aftersale_refund(row: dict) -> bool:
	"""落一条售后退款镜像；返回是否新建。无退款金额的售后（如纯补发）跳过。"""
	import json as _json

	aftersale_no = frappe.utils.cstr(row.get("returnChangeNo")).strip()
	source_trade_no = frappe.utils.cstr(row.get("tradeNo")).strip()
	pay = row.get("returnChangePay") or {}
	refund_amount = frappe.utils.flt(pay.get("returnTotal"))
	if not aftersale_no or not source_trade_no:
		return False
	if refund_amount <= 0:
		return False

	values = {
		"source_trade_no": source_trade_no,
		"refund_amount": refund_amount,
		"pay_status": frappe.utils.cint(pay.get("payStatus")),
		"pay_time": pay.get("payTime") or None,
		"refund_type": frappe.utils.cstr(pay.get("refundType")),
		"channel": frappe.utils.cstr(row.get("shopId")).strip() or None,
		"goods_detail_json": _json.dumps(row.get("returnChangeGoodsDetail") or [], ensure_ascii=False),
		"gmt_modified": row.get("gmtModified") or None,
	}
	existing = frappe.db.exists("Jackyun Aftersale Refund", aftersale_no)
	if existing:
		frappe.db.set_value(
			"Jackyun Aftersale Refund", aftersale_no, values, update_modified=False
		)
		return False
	frappe.get_doc({"doctype": "Jackyun Aftersale Refund", "aftersale_no": aftersale_no, **values}).insert(
		ignore_permissions=True
	)
	return True


def backfill_sales_order_refund_status() -> dict:
	"""按每单最新原始记录回填销售订单行的退款状态（历史数据修复）。

	订单行构建时保持 goodsDetail 顺序，因此按行序对齐；行数不一致的跳过。
	"""
	import json

	latest: dict[str, str] = {}
	for row in frappe.get_all(
		"Jackyun Raw Record",
		filters={"resource": "Sales Order", "creation": [">=", "2026-08-15"]},
		fields=["raw_data"],
		order_by="creation asc",
		limit_page_length=0,
	):
		try:
			raw = json.loads(row.raw_data)
		except (TypeError, ValueError):
			continue
		trade_no = frappe.utils.cstr(raw.get("tradeNo")).strip()
		if trade_no:
			latest[trade_no] = row.raw_data

	updated_orders = updated_items = skipped = 0
	for so in frappe.get_all(
		"Sales Order",
		filters={"custom_jackyun_trade_no": ["is", "set"], "docstatus": ["!=", 2]},
		fields=["name", "custom_jackyun_trade_no"],
		limit_page_length=0,
	):
		raw_text = latest.get(so.custom_jackyun_trade_no)
		if not raw_text:
			skipped += 1
			continue
		try:
			raw = json.loads(raw_text)
		except (TypeError, ValueError):
			skipped += 1
			continue
		lines = raw.get("goodsDetail") or []
		items = frappe.get_all(
			"Sales Order Item",
			filters={"parenttype": "Sales Order", "parent": so.name},
			fields=["name", "idx", "custom_jackyun_refund_status"],
			order_by="idx asc",
		)
		if len(lines) != len(items):
			skipped += 1
			continue
		changed = False
		for item, line in zip(items, lines):
			status = frappe.utils.cint(line.get("refundStatus"))
			if frappe.utils.cint(item.custom_jackyun_refund_status) != status:
				frappe.db.set_value(
					"Sales Order Item", item.name,
					"custom_jackyun_refund_status", status,
					update_modified=False,
				)
				updated_items += 1
				changed = True
		if changed:
			updated_orders += 1
	frappe.db.commit()
	return {
		"updated_orders": updated_orders,
		"updated_items": updated_items,
		"skipped": skipped,
	}


def sync_refund_orders(since=None) -> dict:
	"""同步吉客云负数销售单（售后退货/仅退款）镜像。

	销售退货按退款单自己的下单日期归集（店铺利润表口径）；已取消的退款单不参与统计。
	增量游标取镜像最大 gmt_modified，回退 5 分钟重叠窗口。
	"""
	from channel_erp.integrations.jackyun import (
		METHOD_MAP,
		SALES_ORDER_PAGE_SIZE,
		extract_records_by_identity,
	)

	adapter = _get_adapter()
	if since is None:
		since = frappe.db.sql(
			"""select max(gmt_modified) - interval 5 minute
			     from `tabJackyun Refund Order`"""
		)[0][0]
		if since is None:
			since = frappe.utils.add_days(frappe.utils.now_datetime(), -45)
	until = frappe.utils.now_datetime()

	created = updated = skipped = 0
	from datetime import timedelta

	window_start = frappe.utils.get_datetime(since)
	while window_start <= until:
		window_end = min(window_start + timedelta(days=7) - timedelta(seconds=1), until)
		created, updated, skipped = _sync_refund_orders_window(
			adapter, window_start, window_end, created, updated, skipped
		)
		window_start = window_end + timedelta(seconds=1)
	frappe.db.commit()
	return {"since": str(since), "created": created, "updated": updated, "skipped": skipped}


def _sync_refund_orders_window(adapter, window_start, window_end, created, updated, skipped):
	from channel_erp.integrations.jackyun import (
		METHOD_MAP,
		SALES_ORDER_PAGE_SIZE,
		extract_records_by_identity,
	)

	scroll_id = ""
	previous_scroll_id = None
	while True:
		biz = {
			"startModified": window_start.strftime("%Y-%m-%d %H:%M:%S"),
			"endModified": window_end.strftime("%Y-%m-%d %H:%M:%S"),
			"fields": ",".join(
				[
					"tradeNo", "tradeId", "tradeType", "tradeTime", "gmtCreate",
					"gmtModified", "tradeStatus", "shopId", "shopName",
					"chargeCurrencyCode", "totalFee", "payment", "companyName",
					"goodsDetail.platGoodsId", "goodsDetail.platSkuId",
					"goodsDetail.sellTotal", "goodsDetail.divideSellTotal",
				]
			),
			"scrollId": scroll_id,
			"isTableSwitch": 1,
			"isDelete": "0",
			"tradeTypeList": REFUND_ORDER_TYPES,
		}
		payload = adapter.request(
			METHOD_MAP["Sales Order"], biz, page_index=0, page_size=SALES_ORDER_PAGE_SIZE
		)
		records = extract_records_by_identity(payload, ["tradeId"])
		for raw in records:
			outcome = _upsert_refund_order(raw)
			if outcome == "skipped":
				skipped += 1
			elif outcome == "created":
				created += 1
			else:
				updated += 1
		data = (payload.get("result") or {}).get("data") or {}
		next_scroll_id = data.get("scrollId") if isinstance(data, dict) else None
		if not records or len(records) < SALES_ORDER_PAGE_SIZE or not next_scroll_id:
			break
		if next_scroll_id == scroll_id or next_scroll_id == previous_scroll_id:
			break
		previous_scroll_id = scroll_id
		scroll_id = next_scroll_id
	return created, updated, skipped


def _upsert_refund_order(raw: dict) -> str:
	trade_no = frappe.utils.cstr(raw.get("tradeNo")).strip()
	if not trade_no:
		return "skipped"
	status = frappe.utils.cint(raw.get("tradeStatus"))
	currency = frappe.utils.cstr(raw.get("chargeCurrencyCode")).strip()
	if currency in {"RMB", "人民币"}:
		currency = "CNY"
	import json as _json

	lines = [
		{
			"plat_goods_id": frappe.utils.cstr(line.get("platGoodsId")).strip(),
			"plat_sku_id": frappe.utils.cstr(line.get("platSkuId")).strip(),
			"amount": abs(
				frappe.utils.flt(
					_pick_first(line, ["divideSellTotal", "sellTotal", "totalFee"])
				)
			),
		}
		for line in raw.get("goodsDetail") or []
		if isinstance(line, dict)
	]
	values = {
		"trade_type": frappe.utils.cint(raw.get("tradeType")),
		"refund_date": raw.get("tradeTime") or raw.get("gmtCreate"),
		# 店铺利润表口径：销售退货取实退金额 payment（部分退款与货值有差），缺省回退 totalFee
		"amount": abs(frappe.utils.flt(raw.get("payment")) or frappe.utils.flt(raw.get("totalFee"))),
		"currency": currency or "CNY",
		"trade_status": status,
		"channel": frappe.utils.cstr(raw.get("shopId")).strip() or None,
		"company_name": frappe.utils.cstr(raw.get("companyName")).strip() or None,
		"gmt_modified": raw.get("gmtModified") or None,
		"refund_lines_json": _json.dumps(lines, ensure_ascii=False),
	}
	existing = frappe.db.exists("Jackyun Refund Order", trade_no)
	if existing:
		frappe.db.set_value("Jackyun Refund Order", trade_no, values, update_modified=False)
		return "updated"
	frappe.get_doc(
		{"doctype": "Jackyun Refund Order", "refund_order_no": trade_no, **values}
	).insert(ignore_permissions=True)
	return "created"


def _run_refund_backfill():
	import traceback

	try:
		return sync_refund_orders(since="2026-08-19 00:00:00")
	except Exception:
		return traceback.format_exc()[-600:]


def _pick_first(line: dict, keys: list[str]):
	for key in keys:
		if line.get(key) not in (None, ""):
			return line.get(key)
	return None


def backfill_sales_order_discount_fee() -> dict:
	"""按每单最新原始记录回填销售订单的整单优惠（店铺利润表收入口径）。"""
	import json

	latest: dict[str, float] = {}
	for row in frappe.get_all(
		"Jackyun Raw Record",
		filters={"resource": "Sales Order", "creation": [">=", "2026-08-15"]},
		fields=["raw_data"],
		order_by="creation asc",
		limit_page_length=0,
	):
		try:
			raw = json.loads(row.raw_data)
		except (TypeError, ValueError):
			continue
		trade_no = frappe.utils.cstr(raw.get("tradeNo")).strip()
		if trade_no:
			latest[trade_no] = frappe.utils.flt(raw.get("discountFee"))

	updated = skipped = 0
	for so in frappe.get_all(
		"Sales Order",
		filters={"custom_jackyun_trade_no": ["is", "set"], "docstatus": ["!=", 2]},
		fields=["name", "custom_jackyun_trade_no", "custom_jackyun_discount_fee"],
		limit_page_length=0,
	):
		if so.custom_jackyun_trade_no not in latest:
			skipped += 1
			continue
		discount = latest[so.custom_jackyun_trade_no]
		if abs(frappe.utils.flt(so.custom_jackyun_discount_fee) - discount) > 0.005:
			frappe.db.set_value(
				"Sales Order", so.name,
				"custom_jackyun_discount_fee", discount,
				update_modified=False,
			)
			updated += 1
	frappe.db.commit()
	return {"updated": updated, "skipped": skipped}


def backfill_sales_order_line_sell_totals() -> dict:
	"""按每单最新原始记录回填订单行的销货金额 sellTotal（货款口径）。"""
	import json

	latest: dict[str, str] = {}
	for row in frappe.get_all(
		"Jackyun Raw Record",
		filters={"resource": "Sales Order", "creation": [">=", "2026-08-15"]},
		fields=["raw_data"],
		order_by="creation asc",
		limit_page_length=0,
	):
		try:
			raw = json.loads(row.raw_data)
		except (TypeError, ValueError):
			continue
		trade_no = frappe.utils.cstr(raw.get("tradeNo")).strip()
		if trade_no:
			latest[trade_no] = row.raw_data

	updated_orders = updated_items = skipped = 0
	for so in frappe.get_all(
		"Sales Order",
		filters={"custom_jackyun_trade_no": ["is", "set"], "docstatus": ["!=", 2]},
		fields=["name", "custom_jackyun_trade_no"],
		limit_page_length=0,
	):
		raw_text = latest.get(so.custom_jackyun_trade_no)
		if not raw_text:
			skipped += 1
			continue
		try:
			raw = json.loads(raw_text)
		except (TypeError, ValueError):
			skipped += 1
			continue
		lines = raw.get("goodsDetail") or []
		items = frappe.get_all(
			"Sales Order Item",
			filters={"parenttype": "Sales Order", "parent": so.name},
			fields=["name", "idx", "custom_jackyun_line_sell_total"],
			order_by="idx asc",
		)
		if len(lines) != len(items):
			skipped += 1
			continue
		changed = False
		for item, line in zip(items, lines):
			sell_total = frappe.utils.flt(line.get("sellTotal"))
			if abs(frappe.utils.flt(item.custom_jackyun_line_sell_total) - sell_total) > 0.005:
				frappe.db.set_value(
					"Sales Order Item", item.name,
					"custom_jackyun_line_sell_total", sell_total,
					update_modified=False,
				)
				updated_items += 1
				changed = True
		if changed:
			updated_orders += 1
	frappe.db.commit()
	return {
		"updated_orders": updated_orders,
		"updated_items": updated_items,
		"skipped": skipped,
	}


def backfill_sales_order_post_fees() -> dict:
	"""按每单最新原始记录回填应收邮资（邮资收入口径）。"""
	import json

	latest: dict[str, float] = {}
	for row in frappe.get_all(
		"Jackyun Raw Record",
		filters={"resource": "Sales Order", "creation": [">=", "2026-08-15"]},
		fields=["raw_data"],
		order_by="creation asc",
		limit_page_length=0,
	):
		try:
			raw = json.loads(row.raw_data)
		except (TypeError, ValueError):
			continue
		trade_no = frappe.utils.cstr(raw.get("tradeNo")).strip()
		if trade_no:
			latest[trade_no] = frappe.utils.flt(raw.get("receivedPostFee"))

	updated = skipped = 0
	for so in frappe.get_all(
		"Sales Order",
		filters={"custom_jackyun_trade_no": ["is", "set"], "docstatus": ["!=", 2]},
		fields=["name", "custom_jackyun_trade_no", "custom_jackyun_received_post_fee"],
		limit_page_length=0,
	):
		if so.custom_jackyun_trade_no not in latest:
			skipped += 1
			continue
		post_fee = latest[so.custom_jackyun_trade_no]
		if abs(frappe.utils.flt(so.custom_jackyun_received_post_fee) - post_fee) > 0.005:
			frappe.db.set_value(
				"Sales Order", so.name,
				"custom_jackyun_received_post_fee", post_fee,
				update_modified=False,
			)
			updated += 1
	frappe.db.commit()
	return {"updated": updated, "skipped": skipped}

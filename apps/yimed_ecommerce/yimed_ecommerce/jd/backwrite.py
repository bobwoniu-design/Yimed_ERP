# -*- coding: utf-8 -*-
"""分拣结果回写吉客云：生成销售订单+销售出库单，推送吉客云并审核出库。

业务流（京东采购的备料发货）：
    分拣完成 → 手工触发同步 →
    ① ERPNext 生成 Sales Order（客户=京东自营店铺客户）+ Delivery Note（带批次）
       → ERPNext 库存扣减
    ② 推送吉客云创建销售订单（oms.trade.ordercreate）+ 审核（oms.trade.audit.pass）
       → 吉客云生成出库单、库存扣减
    ③ 映射闭环：日批把吉客云侧单据拉回时，_upsert_delivery_note 依据
       custom_connector_source=ERPNext 的源销售订单跳过，不会重复扣减。

同步采用直推（非队列）：分拣页按钮场景需要即时成败反馈；
SO 提交钩子自动入队的 Create 消息会被本模块清理，避免双推。
"""
from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, today

from yimed_ecommerce.jd.workflow import _lock_batch, _batch_orders

SHOP_CHANNEL_NAME = "JD-医麦德京东自营旗舰店"
CUSTOMER_NAME = "电商客户 - JD-医麦德京东自营旗舰店"


@frappe.whitelist()
def sync_batch_to_jackyun(import_batch: str):
	"""按批次把分拣结果生成销售单据并回写吉客云。幂等：已回写的采购单跳过。"""
	batch = _lock_batch(import_batch)
	orders = _batch_orders(batch.name, for_update=True)
	allocations = frappe.get_all(
		"JD Sorting Allocation",
		filters={"import_batch": batch.name},
		fields=["purchase_order", "jd_sku", "stock_item", "warehouse", "batch_no", "sorted_qty"],
	)
	by_po = defaultdict(list)
	for row in allocations:
		if flt(row.sorted_qty) > 0:
			by_po[row.purchase_order].append(row)

	results = []
	for po in orders:
		rows = by_po.get(po.name) or []
		if not rows:
			results.append({"purchase_order": po.name, "status": "未分拣", "message": "无分拣分配，跳过"})
			continue
		if _po_already_synced(po.name):
			results.append({"purchase_order": po.name, "status": "已同步", "message": "销售订单已生成过，跳过"})
			continue
		try:
			info = _sync_one_order(po, rows, batch.company)
			results.append({"purchase_order": po.name, "status": "成功", **info})
			frappe.db.commit()
		except Exception as exc:
			frappe.db.rollback()
			results.append({"purchase_order": po.name, "status": "失败", "message": str(exc)[:200]})
	return {
		"import_batch": batch.name,
		"results": results,
		"success_count": sum(1 for r in results if r["status"] == "成功"),
		"failed_count": sum(1 for r in results if r["status"] == "失败"),
	}


def _po_already_synced(po_name: str) -> bool:
	return bool(
		frappe.db.exists(
			"Sales Order",
			{"docstatus": ["<", 2], "po_no": po_name},
		)
	)


def _jackyun_warehouse(erp_warehouse: str):
	"""ERP 仓库 → 吉客云 (warehouseCode, warehouseName)。"""
	mapping = frappe.db.get_value(
		"External ID Mapping",
		{"platform": "jackyun", "erpnext_doctype": "Warehouse", "erpnext_name": erp_warehouse, "external_id": ["like", "code:%"]},
		"external_id",
	)
	code = str(mapping or "").replace("code:", "") or None
	name = frappe.db.get_value("Warehouse", erp_warehouse, "warehouse_name") if erp_warehouse else None
	return code, name


def _sync_one_order(po, rows, batch_company):
	from channel_erp.integrations.adapter_registry import get_adapter
	from channel_erp.integrations.jackyun_outbound import (
		JackyunSalesOrderOutboundService,
		build_online_trade_no,
	)

	connection = frappe.get_doc("Jackyun Connection", "吉客云主账号")
	adapter = get_adapter(connection)
	service = JackyunSalesOrderOutboundService(adapter)
	channel = frappe.get_doc("Jackyun Sales Channel", {"channel_name": SHOP_CHANNEL_NAME})

	# 分拣行按 stock_item 聚合为销售行；价格取采购单行。
	# 单位必须用货品档案的 stock_uom（吉客云货品的真实单位）——
	# 采购单行的 purchase_uom 可能与吉客云档案不一致（如 个 vs 袋），
	# 吉客云创建订单时按档案单位校验，传错直接报货品单位不存在。
	price_by_sku = {row.jd_sku: flt(row.purchase_price) for row in po.items}
	agg = defaultdict(float)
	rate_agg = {}
	uom_agg = {}
	for row in rows:
		stock_item = row.get("stock_item") if hasattr(row, "get") else row.stock_item
		jd_sku = row.get("jd_sku") if hasattr(row, "get") else row.jd_sku
		agg[stock_item] += flt(row.get("sorted_qty") if hasattr(row, "get") else row.sorted_qty)
		rate_agg[stock_item] = price_by_sku.get(jd_sku) or 0
		uom_agg[stock_item] = frappe.db.get_value("Item", stock_item, "stock_uom")
	main_warehouse = rows[0].get("warehouse") if hasattr(rows[0], "get") else rows[0].warehouse

	so = frappe.get_doc({
		"doctype": "Sales Order",
		"customer": CUSTOMER_NAME,
		"company": po.company or batch_company,
		"transaction_date": today(),
		"delivery_date": today(),
		"set_warehouse": main_warehouse,
		"selling_price_list": "Standard Selling",
		"custom_jackyun_trade_type": "批发业务",
		"custom_jackyun_sales_channel": channel.name,
		"custom_connector_source": "ERPNext",
		"po_no": po.name,
		"items": [
			{"item_code": item, "qty": qty, "rate": rate_agg[item], "uom": uom_agg[item]}
			for item, qty in agg.items()
		],
	})
	so.flags.ignore_pricing_rule = 1
	so.insert(ignore_permissions=True)
	# 清掉 on_submit 自动入队的 Create 消息：本模块直推，避免队列双推
	frappe.db.delete(
		"Connector Outbound Message",
		{"reference_doctype": "Sales Order", "reference_name": so.name, "operation": "Create", "status": ["in", ["Preflight", "Pending"]]},
	)
	so.submit()

	# 组装吉客云 payload（onlineTradeNo 用 SO 单号做幂等键）
	online_trade_no = build_online_trade_no(so.name)
	wh_code, wh_name = _jackyun_warehouse(main_warehouse)
	details = []
	total_fee = 0.0
	for item, qty in agg.items():
		rate = flt(rate_agg[item])
		amount = flt(qty * rate, 2)
		total_fee += amount
		spec = frappe.db.get_value("Item", item, "custom_jd_specification") or ""
		barcode = frappe.db.get_value("Item Barcode", {"parent": item, "parenttype": "Item"}, "barcode") if frappe.db.exists("Item Barcode", {"parent": item}) else None
		detail = {
			"goodsNo": item,
			"goodsName": frappe.db.get_value("Item", item, "item_name"),
			"specName": spec,
			"barcode": barcode,
			"unit": uom_agg[item],
			"sellPrice": rate,
			"sellCount": qty,
			"sellTotal": amount,
			"sourceTradeNo": online_trade_no,
		}
		details.append({k: v for k, v in detail.items() if v not in (None, "")})
	payload = {
		"tradeOrder": {
			"tradeTime": now_datetime().strftime("%Y-%m-%d %H:%M:%S"),
			"shopName": channel.channel_name,
			"shopCode": channel.channel_code,
			"warehouseCode": wh_code,
			"warehouseName": wh_name,
			"tradeType": 9,
			"totalFee": flt(total_fee, 2),
			"discountFee": 0,
			"payment": flt(total_fee, 2),
			"chargeCurrency": "人民币",
			"chargeCurrencyCode": "CNY",
			"customerName": CUSTOMER_NAME,
			"receiverName": CUSTOMER_NAME,
			"payStatus": 0,
			"onlineTradeNo": online_trade_no,
			"chargeType": int(frappe.utils.cint(channel.settlement_type) or 5),
			"sellerMemo": f"京东采购单回写：{po.name}（批次 {po.import_batch}）",
			"tradeOrderDetails": details,
		}
	}

	# 创建：偶发网络超时——先探测确认未建成再安全重推同号（onlineTradeNo 幂等）
	create_result = None
	for _attempt in range(3):
		create_result = service.create(payload)
		if create_result.get("status") == "Succeeded":
			break
		if create_result.get("status") != "Uncertain":
			break
		import time as _time
		_time.sleep(3)
		probe = service.query_by_online_trade_no(online_trade_no)
		if probe.get("records"):
			# 超时但实际建成：取吉客云单号继续
			records = probe["records"]
			create_result = {"status": "Succeeded", "trade_no": records[0].get("tradeNo") or records[0].get("trade_no")}
			break
	if not create_result or create_result.get("status") != "Succeeded":
		so.db_set({
			"custom_external_sync_status": "Uncertain",
			"custom_external_sync_message": str(create_result.get("message") if create_result else "")[:300],
		}, update_modified=False)
		frappe.throw(_("吉客云创建失败/不确定（onlineTradeNo={0}）").format(online_trade_no))
	trade_no = create_result.get("trade_no")
	if not trade_no:
		frappe.throw(_("吉客云创建未返回单号：{0}").format(str(create_result)[:200]))
	audit_result = service.audit(trade_no, connection.audit_operator or "admin")
	so.db_set({
		"custom_jackyun_trade_no": trade_no,
		"custom_external_sync_status": "Succeeded",
		"custom_external_sync_message": f"已创建并审核：{trade_no}",
	}, update_modified=False)

	# ERPNext 销售出库单：行带批次（分拣分配明细）+ 行级销售订单关联
	so_details = {item.item_code: item.name for item in so.items}
	dn_rows = []
	for row in rows:
		get = (lambda k: row.get(k)) if hasattr(row, "get") else (lambda k: getattr(row, k))
		dn_rows.append({
			"item_code": get("stock_item"),
			"qty": flt(get("sorted_qty")),
			"rate": rate_agg.get(get("stock_item")) or 0,
			"warehouse": get("warehouse"),
			"batch_no": get("batch_no") or None,
			"use_serial_batch_fields": 1,
			"allow_zero_valuation_rate": 1,
			"against_sales_order": so.name,
			"so_detail": so_details.get(get("stock_item")),
		})
	dn = frappe.get_doc({
		"doctype": "Delivery Note",
		"customer": CUSTOMER_NAME,
		"company": so.company,
		"set_warehouse": main_warehouse,
		"posting_date": today(),
		"set_posting_time": 1,
		"items": dn_rows,
	})
	dn.flags.ignore_pricing_rule = 1
	dn.insert(ignore_permissions=True)
	dn.submit()
	return {
		"sales_order": so.name,
		"delivery_note": dn.name,
		"jackyun_trade_no": trade_no,
		"audit": audit_result.get("status") or "ok",
		"item_count": len(dn_rows),
	}

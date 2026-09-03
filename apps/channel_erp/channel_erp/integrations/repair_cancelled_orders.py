"""修复被误取消的吉客云销售订单。

背景：SALES_ORDER_CANCELLED_STATUSES 曾错误包含 4110/4112（待发货-已递交等
正常状态），导致大量已付款待发货订单被同步成「已提交+已取消」。

用法：
  bench --site yimed.local execute \
      channel_erp.integrations.repair_cancelled_orders.analyze
  bench --site yimed.local execute \
      channel_erp.integrations.repair_cancelled_orders.execute
"""
from __future__ import annotations

import json

import frappe

REAL_CANCEL_STATUSES = {"4121", "4122", "5010", "5020", "5030"}


def _latest_raw_status_by_trade_no() -> dict[str, tuple[str, str]]:
	"""扫描近期销售单原始记录，返回 trade_no -> (trade_status, status_explain)，取最新。"""
	latest: dict[str, tuple[str, str, str]] = {}
	for row in frappe.get_all(
		"Jackyun Raw Record",
		filters={"resource": "Sales Order", "creation": [">=", "2026-08-15"]},
		fields=["name", "creation", "raw_data"],
		order_by="creation asc",
	):
		try:
			raw = json.loads(row.raw_data)
		except (TypeError, ValueError):
			continue
		trade_no = frappe.utils.cstr(raw.get("tradeNo")).strip()
		if not trade_no:
			continue
		latest[trade_no] = (
			frappe.utils.cstr(raw.get("tradeStatus")),
			frappe.utils.cstr(raw.get("tradeStatusExplain")),
			str(row.creation),
		)
	return {
		trade_no: (status, explain, creation)
		for trade_no, (status, explain, creation) in latest.items()
	}


def analyze():
	"""Dry-run：列出误杀单、下游挂靠、缺失原始记录的单。"""
	cancelled = frappe.get_all(
		"Sales Order",
		filters={"docstatus": 2},
		fields=["name", "transaction_date", "company", "custom_jackyun_sales_channel"],
	)
	raw_status = _latest_raw_status_by_trade_no()

	wrongly, genuinely, unknown = [], [], []
	for so in cancelled:
		entry = raw_status.get(so.name)
		if entry is None:
			unknown.append(so.name)
		elif entry[0] in REAL_CANCEL_STATUSES:
			genuinely.append(so.name)
		else:
			wrongly.append((so.name, entry[0], entry[1]))

	# 下游挂靠检查（任何状态的出库单/发票引用了误杀销售单）
	linked_dn = set()
	if wrongly:
		names = [name for name, _code, _explain in wrongly]
		for start in range(0, len(names), 500):
			chunk = names[start : start + 500]
			linked_dn.update(
				frappe.get_all(
					"Delivery Note Item",
					filters={"against_sales_order": ["in", chunk]},
					pluck="parent",
				)
			)

	by_code: dict[str, int] = {}
	for _name, code, _explain in wrongly:
		by_code[f"{code} {_explain}"] = by_code.get(f"{code} {_explain}", 0) + 1

	result = {
		"cancelled_total": len(cancelled),
		"wrongly_cancelled": len(wrongly),
		"genuinely_cancelled": len(genuinely),
		"no_raw_record": len(unknown),
		"wrongly_by_jackyun_status": by_code,
		"wrongly_with_linked_delivery_notes": len(linked_dn),
		"linked_delivery_note_samples": sorted(linked_dn)[:10],
		"unknown_names": unknown[:10],
	}
	frappe.logger("channel_erp.repair").info(json.dumps(result, ensure_ascii=False, default=str))
	return result


def execute():
	"""删除误杀的取消单 + 删除其外部映射 + 按 tradeNo 重新拉取并按修复后的状态机落库。"""
	from channel_erp.integrations.jackyun import (
		METHOD_MAP,
		SALES_ORDER_FIELDS,
		SALES_ORDER_PAGE_SIZE,
		JackYunAdapter,
	)
	from channel_erp.integrations.tasks import extract_records_by_identity

	analysis = analyze()
	wrongly = _wrongly_cancelled_names()
	if not wrongly:
		return {"message": "没有需要修复的订单", "analysis": analysis}

	connections = frappe.get_all("Jackyun Connection", filters={"enabled": 1}, pluck="name")
	if not connections:
		frappe.throw("没有启用的吉客云连接")

	deleted, delete_failed, repaired, repair_failed = [], [], [], []
	for start in range(0, len(wrongly), 50):
		batch = wrongly[start : start + 50]

		# 1. 删除误杀的取消单与映射（先查链接，有挂靠的跳过）
		for name in batch:
			try:
				linked = frappe.db.exists(
					"Delivery Note Item", {"against_sales_order": name}
				)
				if linked:
					delete_failed.append((name, f"被出库单 {linked} 引用，跳过"))
					continue
				for mapping in frappe.get_all(
					"External ID Mapping",
					filters={"resource": "Sales Order", "erpnext_name": name},
					pluck="name",
				):
					frappe.delete_doc("External ID Mapping", mapping, ignore_permissions=True)
				# 商品链接等派生数据引用了该单；重拉会用同一单号重建，
				# force 跳过链接检查，删除后引用随重建自动恢复有效。
				frappe.delete_doc(
					"Sales Order", name, force=1, ignore_permissions=True
				)
				deleted.append(name)
			except Exception as exc:
				delete_failed.append((name, str(exc)))
		frappe.db.commit()

		# 2. 从吉客云按单号重拉（50 单一批），重新落库
		adapter = JackYunAdapter(frappe.get_doc("Jackyun Connection", connections[0]))
		try:
			payload = adapter.request(
				METHOD_MAP["Sales Order"],
				{
					"tradeNo": ",".join(deleted[start : start + 50]),
					"fields": SALES_ORDER_FIELDS,
					"isTableSwitch": 1,
					"isDelete": "0",
				},
				page_index=0,
				page_size=SALES_ORDER_PAGE_SIZE,
			)
			records = {frappe.utils.cstr(r.get("tradeNo")): r for r in extract_records_by_identity(payload, ["tradeId"])}
		except Exception as exc:
			for name in deleted[start : start + 50]:
				repair_failed.append((name, f"拉取失败: {exc}"))
			frappe.db.commit()
			continue

		for name in deleted[start : start + 50]:
			raw = records.get(name)
			if not raw:
				repair_failed.append((name, "吉客云未返回该单号"))
				continue
			try:
				mapped = adapter.transform("Sales Order", raw)
				erpnext_name, status = adapter.upsert("Sales Order", mapped)
				repaired.append((erpnext_name, status))
			except Exception as exc:
				repair_failed.append((name, str(exc)))
		frappe.db.commit()

	result = {
		"deleted": len(deleted),
		"repaired": len(repaired),
		"repair_failed": repair_failed[:20],
		"delete_failed": delete_failed[:20],
		"analysis": analysis,
	}
	frappe.logger("channel_erp.repair").info(json.dumps(result, ensure_ascii=False, default=str))
	return result


def _wrongly_cancelled_names() -> list[str]:
	raw_status = _latest_raw_status_by_trade_no()
	names = []
	for so in frappe.get_all("Sales Order", filters={"docstatus": 2}, pluck="name"):
		entry = raw_status.get(so)
		if entry and entry[0] not in REAL_CANCEL_STATUSES:
			names.append(so)
	return names


def _missing_recent_trade_nos() -> list[str]:
	"""8/23 之后创建、吉客云状态有效、但 ERPNext 无单据的订单（修复脚本误删后未重建的部分）。"""
	missing = frappe.db.sql(
		"""
		select distinct JSON_UNQUOTE(JSON_EXTRACT(r.raw_data, '$.tradeNo')) AS trade_no
		from `tabJackyun Raw Record` r
		where r.resource = 'Sales Order'
		  and r.creation >= '2026-08-15'
		  and JSON_UNQUOTE(JSON_EXTRACT(r.raw_data, '$.gmtCreate')) >= '2026-08-23'
		  and JSON_UNQUOTE(JSON_EXTRACT(r.raw_data, '$.tradeStatus')) not in
		      ('4121', '4122', '5010', '5020', '5030')
		  and JSON_UNQUOTE(JSON_EXTRACT(r.raw_data, '$.tradeNo')) not in
		      (select name from `tabSales Order`)
		""",
		as_dict=True,
	)
	return sorted({row.trade_no for row in missing if row.trade_no})


def recover_lost():
	"""重拉误删后未重建的订单。"""
	from channel_erp.integrations.jackyun import (
		METHOD_MAP,
		SALES_ORDER_FIELDS,
		SALES_ORDER_PAGE_SIZE,
		JackYunAdapter,
	)
	from channel_erp.integrations.tasks import extract_records_by_identity

	names = _missing_recent_trade_nos()
	if not names:
		return {"message": "没有需要恢复的订单", "recovered": 0}

	connections = frappe.get_all("Jackyun Connection", filters={"enabled": 1}, pluck="name")
	adapter = JackYunAdapter(frappe.get_doc("Jackyun Connection", connections[0]))

	recovered, failed = [], []
	for start in range(0, len(names), 50):
		batch = names[start : start + 50]
		try:
			payload = adapter.request(
				METHOD_MAP["Sales Order"],
				{
					"tradeNo": ",".join(batch),
					"fields": SALES_ORDER_FIELDS,
					"isTableSwitch": 1,
					"isDelete": "0",
				},
				page_index=0,
				page_size=SALES_ORDER_PAGE_SIZE,
			)
			records = {
				frappe.utils.cstr(r.get("tradeNo")): r
				for r in extract_records_by_identity(payload, ["tradeId"])
			}
		except Exception as exc:
			failed.extend((name, f"拉取失败: {exc}") for name in batch)
			continue

		for name in batch:
			raw = records.get(name)
			if not raw:
				failed.append((name, "吉客云未返回该单号"))
				continue
			try:
				mapped = adapter.transform("Sales Order", raw)
				erpnext_name, status = adapter.upsert("Sales Order", mapped)
				recovered.append(erpnext_name)
			except Exception as exc:
				failed.append((name, str(exc)))
		frappe.db.commit()

	return {"recovered": len(recovered), "failed": failed[:10], "total_missing": len(names)}


def purge_pre_start_date_orders():
	"""删除吉客云创建时间早于连接起始日期的销售订单（增量同步按修改时间带进来的历史单）。

	以吉客云 gmtCreate 判定，自动保留跨午夜边界（gmtCreate 在起始日但 tradeTime 在前一日）的单。
	"""
	connection = frappe.get_all("Jackyun Connection", filters={"enabled": 1}, pluck="name")
	if not connection:
		frappe.throw("没有启用的吉客云连接")
	start_date = frappe.utils.getdate(
		frappe.db.get_value("Jackyun Connection", connection[0], "sales_order_start_date")
	)
	if not start_date:
		frappe.throw("连接未配置 sales_order_start_date")

	created_by_trade_no = {}
	for row in frappe.get_all(
		"Jackyun Raw Record",
		filters={"resource": "Sales Order", "creation": [">=", "2026-08-01"]},
		fields=["raw_data"],
	):
		try:
			raw = json.loads(row.raw_data)
		except (TypeError, ValueError):
			continue
		trade_no = frappe.utils.cstr(raw.get("tradeNo")).strip()
		created = frappe.utils.getdate(raw.get("gmtCreate"))
		if trade_no and created:
			created_by_trade_no[trade_no] = created

	deleted, skipped_no_date, failed = [], [], []
	for so in frappe.get_all(
		"Sales Order",
		filters={"custom_jackyun_trade_no": ["is", "set"]},
		fields=["name"],
	):
		created = created_by_trade_no.get(so.name)
		if created is None or created >= start_date:
			continue
		try:
			for mapping in frappe.get_all(
				"External ID Mapping",
				filters={"resource": "Sales Order", "erpnext_name": so.name},
				pluck="name",
			):
				frappe.delete_doc("External ID Mapping", mapping, ignore_permissions=True)
			docstatus = frappe.db.get_value("Sales Order", so.name, "docstatus")
			if docstatus == 1:
				doc = frappe.get_doc("Sales Order", so.name)
				doc.flags.ignore_permissions = True
				doc.cancel()
			frappe.delete_doc("Sales Order", so.name, force=1, ignore_permissions=True)
			deleted.append(so.name)
		except Exception as exc:
			failed.append((so.name, str(exc)))
		if len(deleted) % 100 == 0 and deleted:
			frappe.db.commit()
	frappe.db.commit()

	return {
		"start_date": str(start_date),
		"deleted": len(deleted),
		"skipped_no_creation_date": len(skipped_no_date),
		"failed": failed[:10],
	}


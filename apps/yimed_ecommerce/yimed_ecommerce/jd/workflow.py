from __future__ import annotations

import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import date_diff, flt, getdate, now_datetime

from erpnext.stock.doctype.batch.batch import get_batch_qty

from yimed_ecommerce.jd.product_bundle import expand_platform_item, is_product_bundle
from yimed_ecommerce.yimed_ecommerce.doctype.jd_self_operated_settings.jd_self_operated_settings import (
	validate_warehouse_company,
)


@frappe.whitelist()
def get_workflow_batches(company: str | None = None, limit: int = 50):
	filters = {"company": company} if company else {}
	rows = frappe.get_list(
		"JD Purchase Import Batch",
		filters=filters,
		fields=["name", "company", "status", "imported_at", "purchase_order_count", "failed_count"],
		order_by="creation desc",
		limit_page_length=min(int(limit or 50), 200),
	)
	for row in rows:
		orders = frappe.get_all(
			"JD Purchase Order",
			filters={"import_batch": row.name},
			fields=["workflow_stage", "workflow_exception"],
		)
		# Show the current number of order documents rather than the historical
		# import counter, which can become stale after an order is removed/reworked.
		row["purchase_order_count"] = len(orders)
		row["step_counts"] = dict(_count_by(orders, "workflow_stage"))
		row["exception_count"] = sum(1 for order in orders if order.workflow_exception)
	return rows


@frappe.whitelist()
def get_workflow_overview(import_batch: str):
	batch = frappe.get_doc("JD Purchase Import Batch", import_batch)
	batch.check_permission("read")
	orders = frappe.get_all(
		"JD Purchase Order",
		filters={"import_batch": batch.name},
		fields=[
			"name", "jd_warehouse", "destination_city", "distribution_center", "mapping_status", "workflow_stage", "stocking_status",
			"sorting_status", "packing_status", "transfer_status", "workflow_exception",
			"shortage_qty", "shortage_reason", "total_purchase_qty", "total_cartons",
		],
		order_by="creation",
	)
	return {
		"batch": {"name": batch.name, "company": batch.company, "status": batch.status},
		"orders": orders,
		"step_counts": dict(_count_by(orders, "workflow_stage")),
		"exception_count": sum(1 for order in orders if order.workflow_exception),
	}


@frappe.whitelist()
def get_workflow_purchase_order(purchase_order: str):
	po = frappe.get_doc("JD Purchase Order", purchase_order)
	po.check_permission("read")
	return {
		"purchase_order": _po_state(po),
		"jd_skus": get_jd_sku_summary(po.name),
		"stocking": get_erp_stocking_summary(po.name),
		"sorting": frappe.get_all(
			"JD Sorting Allocation",
			filters={"purchase_order": po.name},
			fields=["name", "jd_sku", "platform_item", "stock_item", "stocking_pool_item", "warehouse", "batch_no", "required_qty", "sorted_qty", "shortage_qty", "shortage_reason", "status"],
			order_by="jd_sku, stock_item, creation",
		),
		"packing_gate": get_order_packing_gate(po.name),
	}


@frappe.whitelist()
def get_jd_sku_summary(purchase_order: str | None = None, import_batch: str | None = None):
	filters = {"parenttype": "JD Purchase Order"}
	if purchase_order:
		po = frappe.get_doc("JD Purchase Order", purchase_order)
		po.check_permission("read")
		filters["parent"] = po.name
	elif import_batch:
		batch = frappe.get_doc("JD Purchase Import Batch", import_batch)
		batch.check_permission("read")
		orders = frappe.get_all("JD Purchase Order", filters={"import_batch": batch.name}, pluck="name")
		filters["parent"] = ["in", orders]
	else:
		frappe.throw(_("Purchase Order or Import Batch is required."))
	rows = frappe.get_all(
		"JD Purchase Order Item",
		filters=filters,
		fields=["parent as purchase_order", "jd_sku", "jd_item_name", "platform_item", "platform_item_name", "purchase_uom", "purchase_qty"],
	)
	aggregated = {}
	for row in rows:
		key = (row.purchase_order, row.jd_sku)
		aggregated.setdefault(key, {**row, "purchase_qty": 0})
		aggregated[key]["purchase_qty"] = flt(aggregated[key]["purchase_qty"] + row.purchase_qty, 6)
	return list(aggregated.values())


@frappe.whitelist()
def get_erp_stocking_summary(purchase_order: str | None = None, import_batch: str | None = None):
	filters = {}
	if purchase_order:
		po = frappe.get_doc("JD Purchase Order", purchase_order)
		po.check_permission("read")
		filters["import_batch"] = po.import_batch
	elif import_batch:
		batch = frappe.get_doc("JD Purchase Import Batch", import_batch)
		batch.check_permission("read")
		filters["import_batch"] = batch.name
	else:
		frappe.throw(_("Purchase Order or Import Batch is required."))
	rows = frappe.get_all(
		"JD Stocking Pool Item",
		filters=filters,
		fields=["name", "import_batch", "stock_item", "warehouse", "batch_no", "required_qty", "stocked_qty", "available_qty_snapshot", "shortage_qty", "shortage_reason", "status"],
		order_by="stock_item, warehouse, batch_no",
	)
	for row in rows:
		row.update(_item_display(row.stock_item))
		row["is_requirement_placeholder"] = 0
	if rows:
		return rows

	# A new batch has no pool rows yet, but the operator still needs the expanded
	# stock-item requirements (including Product Bundle components) to perform the
	# first stocking confirmation.
	batch_name = filters["import_batch"]
	requirements = _batch_stock_requirements(_batch_orders(batch_name))
	return [
		{
			"name": None,
			"import_batch": batch_name,
			"stock_item": item,
			"warehouse": None,
			"batch_no": None,
			"required_qty": requirement["qty"],
			"stocked_qty": 0,
			"available_qty_snapshot": 0,
			"shortage_qty": 0,
			"shortage_reason": None,
			"status": "待备货",
			"stock_item_name": _item_display(item)["stock_item_name"],
			"uom": requirement["uom"],
			"is_requirement_placeholder": 1,
		}
		for item, requirement in requirements.items()
	]


@frappe.whitelist()
def get_batch_preview(import_batch: str):
	"""批次列表页的下方预览，产出三张作业清单：

	组合清单：组合装 → 组件构成（配比 × 套装数），指导实物组合；
	备货清单：京东仓 × ERP物料聚合，指导库存准备（组合装已拆到组件）；
	分拣清单：采购单 × ERP物料聚合，按单核对执行。
	未映射行不参与计算，单独一组提醒操作员。
	"""
	batch = frappe.get_doc("JD Purchase Import Batch", import_batch)
	batch.check_permission("read")
	orders = frappe.get_all(
		"JD Purchase Order",
		filters={"import_batch": batch.name},
		fields=["name", "jd_purchase_order_no", "status", "mapping_status", "destination_city", "jd_warehouse", "total_purchase_qty"],
		order_by="name",
	)

	warehouse_by_order = {o.name: (o.jd_warehouse or "—") for o in orders}
	unmapped = defaultdict(lambda: {"total_qty": 0, "jd_item_name": None})
	bundles = defaultdict(
		lambda: {"total_qty": 0, "jd_item_name": None, "uom": None, "components": {}}
	)
	product_summary = defaultdict(lambda: {"total_qty": 0, "uom": None, "from_bundle": False})
	# 分拣发生在组装完成之后，按订单行原样聚合（组合装显示成品编码，不展开组件）。
	sorting = defaultdict(
		lambda: {"total_qty": 0, "uom": None, "warehouse": None, "jd_item_name": None, "is_bundle": False}
	)

	if orders:
		rows = frappe.get_all(
			"JD Purchase Order Item",
			filters={"parenttype": "JD Purchase Order", "parent": ["in", [o.name for o in orders]]},
			fields=["parent", "jd_sku", "jd_item_name", "platform_item", "purchase_qty", "purchase_uom"],
		)
		for row in rows:
			warehouse = warehouse_by_order.get(row.parent, "—")
			if not row.platform_item:
				entry = unmapped.setdefault(row.jd_sku, {"total_qty": 0, "jd_item_name": row.jd_item_name})
				entry["total_qty"] = flt(entry["total_qty"] + row.purchase_qty, 6)
				if not entry["jd_item_name"]:
					entry["jd_item_name"] = row.jd_item_name
				continue

			components = expand_platform_item(row.platform_item, row.purchase_qty)
			for component in components:
				p_entry = product_summary.setdefault(
					component.stock_item, {"total_qty": 0, "uom": None, "from_bundle": False}
				)
				p_entry["total_qty"] = flt(p_entry["total_qty"] + component.qty, 6)
				p_entry["uom"] = component.stock_uom
				p_entry["from_bundle"] = p_entry["from_bundle"] or component.is_product_bundle

			bundle_flag = is_product_bundle(row.platform_item)
			o_key = (row.parent, row.platform_item)
			o_entry = sorting.setdefault(
				o_key,
				{"total_qty": 0, "uom": None, "warehouse": None, "jd_item_name": None, "is_bundle": bundle_flag},
			)
			o_entry["total_qty"] = flt(o_entry["total_qty"] + row.purchase_qty, 6)
			o_entry["uom"] = row.purchase_uom
			o_entry["warehouse"] = warehouse
			o_entry["is_bundle"] = bundle_flag
			if not o_entry["jd_item_name"]:
				o_entry["jd_item_name"] = row.jd_item_name

			if bundle_flag:
				entry = bundles.setdefault(
					row.platform_item,
					{"total_qty": 0, "jd_item_name": row.jd_item_name, "uom": row.purchase_uom, "components": {}},
				)
				entry["total_qty"] = flt(entry["total_qty"] + row.purchase_qty, 6)
				if not entry["jd_item_name"]:
					entry["jd_item_name"] = row.jd_item_name
				if not entry["uom"]:
					entry["uom"] = row.purchase_uom
				if row.purchase_qty > 0:
					for component in components:
						per_set = flt(component.qty / row.purchase_qty, 6)
						comp_entry = entry["components"].setdefault(
							component.stock_item, {"per_set": 0, "total_qty": 0}
						)
						comp_entry["per_set"] = per_set
						comp_entry["total_qty"] = flt(comp_entry["total_qty"] + component.qty, 6)

	bundle_list = [
		{
			"platform_item": platform_item,
			"jd_item_name": entry["jd_item_name"],
			"total_qty": entry["total_qty"],
			"uom": entry["uom"] or __("套"),
			"components": [
				{
					"stock_item": stock_item,
					**_item_display(stock_item),
					"per_set": comp["per_set"],
					"total_qty": comp["total_qty"],
				}
				for stock_item, comp in sorted(entry["components"].items())
			],
		}
		for platform_item, entry in sorted(bundles.items())
	]
	product_summary_list = [
		{
			"stock_item": stock_item,
			**_item_display(stock_item),
			"total_qty": entry["total_qty"],
			"uom": entry["uom"],
			"from_bundle": entry["from_bundle"],
		}
		for stock_item, entry in sorted(product_summary.items())
	]
	sorting_list = [
		{
			"purchase_order": purchase_order,
			"warehouse": entry["warehouse"],
			"platform_item": platform_item,
			"jd_item_name": entry["jd_item_name"],
			"is_bundle": entry["is_bundle"],
			"total_qty": entry["total_qty"],
			"uom": entry["uom"],
		}
		for (purchase_order, platform_item), entry in sorted(sorting.items(), key=lambda kv: (kv[0][0], kv[0][1]))
	]
	unmapped_list = [
		{
			"jd_sku": jd_sku,
			**entry,
		}
		for jd_sku, entry in sorted(unmapped.items())
	]
	return {
		"import_batch": batch.name,
		"orders": orders,
		"product_summary": product_summary_list,
		"bundle_list": bundle_list,
		"sorting_list": sorting_list,
		"unmapped": unmapped_list,
	}


@frappe.whitelist()
def get_stocking_workbench(import_batch: str):
	"""Return expanded batch requirements beside editable multi-location pool rows."""
	batch = frappe.get_doc("JD Purchase Import Batch", import_batch)
	batch.check_permission("read")
	requirements = _batch_stock_requirements(_batch_orders(batch.name))
	pool_rows = frappe.get_all(
		"JD Stocking Pool Item",
		filters={"import_batch": batch.name},
		fields=[
			"name", "import_batch", "stock_item", "warehouse", "batch_no",
			"required_qty", "stocked_qty", "available_qty_snapshot", "shortage_qty",
			"shortage_reason", "status",
		],
		order_by="stock_item, warehouse, batch_no, creation",
	)
	for row in pool_rows:
		row.update(_item_display(row.stock_item))
	# 备货两态：有池行=已确认（页面只读）；取消条件=未组装、未分拣、无转移
	bundle_items = {row.new_item_code for row in frappe.get_all("Product Bundle", fields=["new_item_code"])}
	assembled = any(row.stock_item in bundle_items for row in pool_rows)
	sorted_yet = bool(frappe.db.exists("JD Sorting Allocation", {"import_batch": batch.name}))
	# 组套件标识：本批次中该物料作为 Product Bundle 组件出现，备货后需用于组套
	bundle_info = {
		item: requirement
		for item, requirement in requirements.items()
		if requirement.get("from_bundle")
	}
	for row in pool_rows:
		info = bundle_info.get(row.stock_item)
		row["from_bundle"] = bool(info)
		row["bundle_parents"] = "、".join(sorted(info["bundle_parents"])) if info else ""
	return {
		"import_batch": batch.name,
		"stocking_confirmed": bool(pool_rows),
		"can_cancel": bool(pool_rows) and not assembled and not sorted_yet,
		"requirements": [
			{
				"stock_item": item,
				**_item_display(item),
				"required_qty": requirement["qty"],
				"uom": requirement["uom"],
				"from_bundle": requirement.get("from_bundle", False),
				"bundle_parents": "、".join(sorted(requirement.get("bundle_parents", ()))),
			}
			for item, requirement in requirements.items()
		],
		"pool_rows": pool_rows,
		"unmapped_requirements": _unmapped_stock_requirements(batch.name),
	}


def _unmapped_stock_requirements(import_batch: str):
	"""Aggregate not-yet-mapped JD item rows so the workbench can surface them.

	待映射的明细不参与备货/分拣计算，但对操作员必须可见：
	这里按京东 SKU 聚合，供工作台展示“待映射”区块并引导完成 JD SKU Mapping。
	"""
	orders = frappe.get_all(
		"JD Purchase Order", filters={"import_batch": import_batch}, pluck="name"
	)
	if not orders:
		return []
	rows = frappe.get_all(
		"JD Purchase Order Item",
		filters={
			"parenttype": "JD Purchase Order",
			"parent": ["in", orders],
		},
		fields=["parent as purchase_order", "jd_sku", "jd_item_name", "platform_item", "purchase_qty"],
		order_by="jd_sku",
	)
	rows = [row for row in rows if not row.platform_item]
	aggregated = {}
	for row in rows:
		key = (row.purchase_order, row.jd_sku)
		entry = aggregated.setdefault(
			key,
			{
				"purchase_order": row.purchase_order,
				"jd_sku": row.jd_sku,
				"jd_item_name": row.jd_item_name,
				"purchase_qty": 0,
			},
		)
		entry["purchase_qty"] = flt(entry["purchase_qty"] + row.purchase_qty, 6)
	return list(aggregated.values())


@frappe.whitelist()
def get_sorting_matrix(import_batch: str):
	"""Return the complete editable sorting surface, not only existing allocations."""
	batch = frappe.get_doc("JD Purchase Import Batch", import_batch)
	batch.check_permission("read")
	orders = _batch_orders(batch.name)
	requirements = []
	for po in orders:
		for row in _sorting_requirements(po):
			requirements.append({
				"purchase_order": po.name,
				"jd_sku": row["jd_sku"],
				"platform_item": row["platform_item"],
				"stock_item": row["stock_item"],
				"required_qty": row["qty"],
			})
	stocking = get_stocking_workbench(batch.name)
	return {
		"import_batch": batch.name,
		"pools": stocking["pool_rows"],
		"requirements": requirements,
		"allocations": frappe.get_all(
			"JD Sorting Allocation",
			filters={"import_batch": batch.name},
			fields=[
				"name", "purchase_order", "jd_sku", "platform_item", "stock_item",
				"stocking_pool_item", "warehouse", "batch_no", "required_qty",
				"sorted_qty", "shortage_qty", "shortage_reason", "status",
			],
			order_by="purchase_order, jd_sku, stock_item, creation",
		),
	}


@frappe.whitelist()
def get_batch_candidates(stock_item: str, warehouse: str, posting_date: str | None = None):
	frappe.has_permission("Item", "read", stock_item, throw=True)
	frappe.has_permission("Warehouse", "read", warehouse, throw=True)
	posting_date = getdate(posting_date)
	rows = frappe.get_all(
		"Batch",
		filters={"item": stock_item, "disabled": 0},
		fields=["name", "manufacturing_date", "expiry_date"],
		order_by="expiry_date, creation",
	)
	result = []
	for row in rows:
		actual = flt(get_batch_qty(row.name, warehouse, stock_item), 6)
		if actual <= 0:
			continue
		reserved = _reserved_qty(stock_item, warehouse, row.name)
		percent = _remaining_shelf_life(row.manufacturing_date, row.expiry_date, posting_date)
		result.append({
			"batch_no": row.name,
			"manufacturing_date": row.manufacturing_date,
			"expiry_date": row.expiry_date,
			"actual_qty": actual,
			"soft_reserved_qty": reserved,
			"available_qty": max(flt(actual - reserved, 6), 0),
			"remaining_shelf_life_percent": percent,
			# 无有效期数据的批次（吉客云侧不做效期管理的商品）不适用 2/3 效期约束
			"eligible": True if row.expiry_date is None else (percent is not None and percent >= 66.6667),
		})
	return result


@frappe.whitelist()
def cancel_stocking(import_batch: str):
	"""取消批次备货：删除备货池行，回到可编辑态。

	前提：未组装（无组合装成品备货行）、未分拣（无分拣分配）、未装箱/未交接
	（由下游改写拦截统一校验）。返回被取消的池行数据供页面回填草稿。
	"""
	batch = _lock_batch(import_batch)
	orders = _batch_orders(batch.name, for_update=True)
	_reject_downstream_rewrite(batch.name, orders)
	bundle_items = {row.new_item_code for row in frappe.get_all("Product Bundle", fields=["new_item_code"])}
	if frappe.db.exists("JD Stocking Pool Item", {"import_batch": batch.name, "stock_item": ["in", list(bundle_items) or [""]]}):
		frappe.throw(_("本批次已完成组装，请先取消组装（清空组装成品）再取消备货。"))
	if frappe.db.exists("JD Sorting Allocation", {"import_batch": batch.name}):
		frappe.throw(_("本批次已完成分拣，不能取消备货。"))
	pool_rows = frappe.get_all(
		"JD Stocking Pool Item",
		filters={"import_batch": batch.name},
		fields=["stock_item", "warehouse", "batch_no", "stocked_qty"],
		order_by="creation",
	)
	frappe.db.delete("JD Stocking Pool Item", {"import_batch": batch.name})
	for po in orders:
		po.db_set({
			"stocking_status": "待备货", "sorting_status": "待分拣",
			"workflow_stage": "备货", "workflow_exception": 0,
			"shortage_qty": 0, "shortage_reason": None,
		})
		_audit(po, "取消批次备货", f"批次 {batch.name} 备货池 {len(pool_rows)} 行已清空")
	return {
		"import_batch": batch.name,
		"pool_rows": pool_rows,
	}


@frappe.whitelist()
def confirm_stocking(import_batch: str, rows: str | list[dict]):
	batch = _lock_batch(import_batch)
	orders = _batch_orders(batch.name, for_update=True)
	_reject_downstream_rewrite(batch.name, orders)
	for po in orders:
		po.require_complete_mapping()
	data = _parse_rows(rows)
	requirements = _batch_stock_requirements(orders)
	if not data:
		frappe.throw(_("至少需要一行备货数据。"))

	grouped = defaultdict(list)
	for row in data:
		item = str(row.get("stock_item") or "").strip()
		warehouse = str(row.get("warehouse") or "").strip()
		qty = flt(row.get("stocked_qty"), 6)
		if item not in requirements or not warehouse or qty < 0:
			frappe.throw(_("每行备货都需要有效的库存物料、仓库和非负数量。"))
		validate_warehouse_company(warehouse, batch.company)
		batch_no = str(row.get("batch_no") or "").strip() or None
		if batch_no and frappe.db.get_value("Batch", batch_no, "item") != item:
			frappe.throw(_("批号 {0} 不属于物料 {1}。").format(batch_no, item))
		grouped[item].append({"stock_item": item, "warehouse": warehouse, "batch_no": batch_no, "stocked_qty": qty, "shortage_reason": (row.get("shortage_reason") or "").strip()})

	if set(grouped) != set(requirements):
		frappe.throw(_("备货明细必须覆盖全部批次需求物料。"))
	for item, item_rows in grouped.items():
		total = flt(sum(row["stocked_qty"] for row in item_rows), 6)
		if total > requirements[item]["qty"]:
			frappe.throw(_("物料 {0} 各仓库/批号的备货合计 {1}，超过批次需求 {2}。").format(item, total, requirements[item]["qty"]))
		_validate_soft_availability(batch.name, item_rows)

	frappe.db.delete("JD Stocking Pool Item", {"import_batch": batch.name})
	frappe.db.delete("JD Sorting Allocation", {"import_batch": batch.name})
	created = []
	total_shortage = 0
	missing_batch = False
	for item, item_rows in grouped.items():
		stocked_total = flt(sum(row["stocked_qty"] for row in item_rows), 6)
		shortage = flt(requirements[item]["qty"] - stocked_total, 6)
		total_shortage += shortage
		# 短缺原因字段已按业务要求移除（2026-09-24）：短缺允许无原因确认，
		# 状态仍记"备货异常"供下游 gate 拦截，操作员自行评估缺货影响
		reason = next((row["shortage_reason"] for row in item_rows if row.get("shortage_reason")), "")
		for index, row in enumerate(item_rows):
			has_batch = frappe.get_cached_value("Item", item, "has_batch_no")
			missing_batch = missing_batch or bool(has_batch and not row["batch_no"] and row["stocked_qty"])
			available = _actual_qty(item, row["warehouse"], row["batch_no"])
			doc = frappe.get_doc({
				"doctype": "JD Stocking Pool Item", "import_batch": batch.name,
				"company": batch.company, "stock_item": item,
				"warehouse": row["warehouse"], "batch_no": row["batch_no"],
				"required_qty": row["stocked_qty"] + (shortage if index == len(item_rows) - 1 else 0),
				"stocked_qty": row["stocked_qty"], "available_qty_snapshot": available,
				"shortage_qty": shortage if index == len(item_rows) - 1 else 0,
				"shortage_reason": reason if shortage else None,
				"status": "短缺" if shortage else "已备货", "confirmed_by": frappe.session.user,
				"confirmed_at": now_datetime(),
			})
			doc.flags.jd_workflow_controlled = True
			doc.insert()
			created.append(doc.name)

	status = "备货异常" if total_shortage else ("备货中" if missing_batch else "备货完成")
	stocked_total = sum(flt(r["stocked_qty"]) for rs in grouped.values() for r in rs)
	short_items = {item for item, item_rows in grouped.items() if sum(r["stocked_qty"] for r in item_rows) < requirements[item]["qty"]}
	for po in orders:
		po_short = bool(set(_stock_requirements(po)) & short_items)
		po.db_set({"workflow_stage":"备货", "stocking_status":"备货异常" if po_short else ("备货中" if missing_batch else "备货完成"), "sorting_status":"待分拣", "actual_stocked_qty":0, "actual_sorted_qty":0, "shortage_qty":0, "shortage_reason":_first_shortage_reason(grouped) if po_short else None, "workflow_exception":1 if po_short else 0})
		_audit(po, "批次确认备货", f"批次 {batch.name} 备货池 {len(created)} 行")
	return {"import_batch": batch.name, "stocking_status": status, "rows": created, "stocked_qty": stocked_total, "shortage_qty": total_shortage, "missing_batch": missing_batch}


@frappe.whitelist()
def auto_sort(import_batch: str):
	batch = _lock_batch(import_batch)
	orders = _batch_orders(batch.name, for_update=True)
	_validate_assembly_complete(batch.name)
	_reject_downstream_rewrite(batch.name, orders)
	pools = _locked_pools(batch.name)
	if not pools:
		frappe.throw(_("Confirm stocking before sorting."))
	frappe.db.delete("JD Sorting Allocation", {"import_batch": batch.name})
	used = defaultdict(float)
	created = []
	shortages = []
	for po in orders:
		for req in _sorting_requirements(po):
			remaining = req["qty"]
			for pool in [row for row in pools if row.stock_item == req["stock_item"]]:
				available = flt(pool.stocked_qty - used[pool.name], 6)
				qty = min(remaining, max(available, 0))
				if qty <= 0: continue
				created.append(_insert_sorting(po, req, pool, qty))
				used[pool.name] += qty; remaining = flt(remaining - qty, 6)
				if remaining <= 0: break
			if remaining > 0:
				shortages.append({"purchase_order":po.name, "jd_sku":req["jd_sku"], "stock_item":req["stock_item"], "shortage_qty":remaining})
	return _finish_batch_sorting(batch, orders, created, shortages, "自动分拣")


@frappe.whitelist()
def update_sorting(import_batch: str, rows: str | list[dict]):
	batch = _lock_batch(import_batch)
	orders = _batch_orders(batch.name, for_update=True)
	_validate_assembly_complete(batch.name)
	_reject_downstream_rewrite(batch.name, orders)
	orders_by_name = {po.name: po for po in orders}
	pools = {row.name: row for row in _locked_pools(batch.name)}
	requirements = {(po.name, row["jd_sku"], row["stock_item"]): row for po in orders for row in _sorting_requirements(po)}
	data = _parse_rows(rows)
	pool_used = defaultdict(float)
	req_used = defaultdict(float)
	prepared = []
	for row in data:
		pool = pools.get(row.get("stocking_pool_item"))
		po_name = str(row.get("purchase_order") or "")
		key = (po_name, str(row.get("jd_sku") or ""), str(row.get("stock_item") or ""))
		qty = flt(row.get("sorted_qty"), 6)
		if not pool or key not in requirements or pool.stock_item != key[2] or qty <= 0:
			frappe.throw(_("Every sorting row must reference this order's stocking row, JD SKU, stock Item and positive Quantity."))
		pool_used[pool.name] += qty
		req_used[key] += qty
		if pool_used[pool.name] > pool.stocked_qty or req_used[key] > requirements[key]["qty"]:
			frappe.throw(_("Sorting allocation exceeds stocked or required quantity."))
		prepared.append((orders_by_name[po_name], requirements[key], pool, qty))
	frappe.db.delete("JD Sorting Allocation", {"import_batch": batch.name})
	created = [_insert_sorting(po, req, pool, qty) for po, req, pool, qty in prepared]
	shortages = [{"purchase_order":key[0], "jd_sku":req["jd_sku"], "stock_item":req["stock_item"], "shortage_qty":flt(req["qty"] - req_used[key], 6)} for key, req in requirements.items() if flt(req["qty"] - req_used[key], 6) > 0]
	return _finish_batch_sorting(batch, orders, created, shortages, "人工分拣")


@frappe.whitelist()
def record_workflow_exception(purchase_order: str, stage: str, reason: str, shortage_qty: float = 0):
	po = _lock_po(purchase_order)
	reason = (reason or "").strip()
	if stage not in ("备货", "分拣", "装箱", "交接发运") or not reason:
		frappe.throw(_("A valid stage and exception reason are required."))
	values = {"workflow_stage": stage, "workflow_exception": 1, "shortage_reason": reason, "shortage_qty": flt(shortage_qty)}
	if stage == "备货": values["stocking_status"] = "备货异常"
	if stage == "分拣": values["sorting_status"] = "分拣异常"
	po.db_set(values)
	_audit(po, f"{stage}异常", reason)
	return _po_state(frappe.get_doc("JD Purchase Order", po.name))


@frappe.whitelist()
def get_order_packing_gate(purchase_order: str):
	po = frappe.get_doc("JD Purchase Order", purchase_order)
	po.check_permission("read")
	issues = []
	if _assembly_pending(po.import_batch):
		issues.append(_("请先完成组装确认，再进行分拣或装箱。"))
	batch_status = frappe.db.get_value("JD Purchase Import Batch", po.import_batch, "status")
	requires_workflow = batch_status in ("导入成功", "部分失败")
	allocations = frappe.get_all("JD Sorting Allocation", filters={"purchase_order": po.name}, fields=["stocking_pool_item", "stock_item", "warehouse", "batch_no", "sorted_qty", "shortage_qty"])
	pool_names = list({row.stocking_pool_item for row in allocations if row.stocking_pool_item})
	pools = frappe.get_all("JD Stocking Pool Item", filters={"name": ["in", pool_names]}, fields=["name", "stock_item", "warehouse", "batch_no", "stocked_qty", "shortage_qty"]) if pool_names else []
	batch_has_pool = bool(frappe.db.exists("JD Stocking Pool Item", {"import_batch": po.import_batch}))
	if requires_workflow and not batch_has_pool:
		issues.extend([_("Stocking has not been confirmed."), _("Sorting has not been completed.")])
	elif batch_has_pool:
		if po.stocking_status != "备货完成": issues.append(_("Stocking is not complete."))
		if not allocations:
			issues.append(_("Sorting has not been completed."))
		elif po.sorting_status != "分拣完成":
			issues.append(_("Sorting is not complete."))
	if pools:
		for row in pools:
			if frappe.get_cached_value("Item", row.stock_item, "has_batch_no") and not row.batch_no:
				issues.append(_("Batch assignment is incomplete.")); break
			# 效期检查已按业务要求取消（2026-09-23）：批次是否可发由操作员自行判断，
			# 备货环节的 FEFO 推荐与“不符合2/3效期”标记仍保留提示能力。
			if row.batch_no and _actual_qty(row.stock_item, row.warehouse, row.batch_no) < _reserved_qty(row.stock_item, row.warehouse, row.batch_no):
				issues.append(_("Current inventory no longer covers soft reservations.")); break
	return {"purchase_order": po.name, "workflow_stage": po.workflow_stage, "stocking_status": po.stocking_status, "sorting_status": po.sorting_status, "can_pack": not issues, "issues": issues}


def validate_order_ready_for_packing(purchase_order: str):
	gate = get_order_packing_gate(purchase_order)
	if not gate["can_pack"]:
		frappe.throw("<br>".join(gate["issues"]), title=_("Purchase Order is not ready for packing"))


def _lock_po(name):
	frappe.db.sql("select name from `tabJD Purchase Order` where name=%s for update", name)
	po = frappe.get_doc("JD Purchase Order", name); po.check_permission("write"); return po


def _lock_batch(name):
	frappe.db.sql("select name from `tabJD Purchase Import Batch` where name=%s for update", name)
	batch = frappe.get_doc("JD Purchase Import Batch", name)
	batch.check_permission("write")
	return batch


def _batch_orders(import_batch, for_update=False):
	if for_update:
		frappe.db.sql("select name from `tabJD Purchase Order` where import_batch=%s order by expected_arrival_date, order_datetime, name for update", import_batch)
	names = frappe.get_all("JD Purchase Order", filters={"import_batch":import_batch}, pluck="name", order_by="expected_arrival_date, order_datetime, name")
	return [frappe.get_doc("JD Purchase Order", name) for name in names]


def _reject_downstream_rewrite(import_batch, orders):
	"""Freeze stocking/sorting once a downstream document can depend on it."""
	order_names = tuple(po.name for po in orders)
	if order_names:
		carton = frappe.db.sql(
			"""select name, purchase_order from `tabJD Carton`
			where purchase_order in %(orders)s order by creation limit 1 for update""",
			{"orders": order_names},
			as_dict=True,
		)
		if carton:
			frappe.throw(
				_("采购单 {1} 已存在箱记录 {0}。请先在装箱工作台删除相关箱记录，再修改备货或分拣。").format(
					frappe.bold(carton[0].name), frappe.bold(carton[0].purchase_order)
				)
			)

		entry = frappe.db.sql(
			"""select name, custom_jd_purchase_order, docstatus from `tabStock Entry`
			where custom_jd_purchase_order in %(orders)s and docstatus < 2
			order by creation limit 1 for update""",
			{"orders": order_names},
			as_dict=True,
		)
		if entry:
			frappe.throw(
				_("采购单 {1} 已存在进行中的转移单 {0}。请先在 ERPNext 取消/删除该转移单，再修改备货或分拣。").format(
					frappe.bold(entry[0].name), frappe.bold(entry[0].custom_jd_purchase_order)
				)
			)

	handover = frappe.db.sql(
		"""select name from `tabJD Handover`
		where import_batch = %s and docstatus < 2 order by creation limit 1 for update""",
		import_batch,
		as_dict=True,
	)
	if handover:
		frappe.throw(
			_("该批次已存在进行中的交接单 {0}。请先作废该交接单，再修改备货或分拣。").format(
				frappe.bold(handover[0].name)
			)
		)


def _batch_stock_requirements(orders):
	req = defaultdict(lambda: {"qty": 0, "uom": None, "from_bundle": False, "bundle_parents": set()})
	for po in orders:
		for item, row in _stock_requirements(po).items():
			entry = req[item]
			entry["qty"] = flt(entry["qty"] + row["qty"], 6)
			entry["uom"] = row["uom"]
			if row.get("from_bundle"):
				entry["from_bundle"] = True
				entry["bundle_parents"] |= row.get("bundle_parents", set())
	return req


def _stock_requirements(po):
	req = defaultdict(lambda: {"qty": 0, "uom": None, "from_bundle": False, "bundle_parents": set()})
	for item in po.items:
		if not item.platform_item:
			# 待映射的商品行不参与备货需求；完成 JD SKU 映射后才会进入计算。
			continue
		for component in expand_platform_item(item.platform_item, item.purchase_qty):
			entry = req[component.stock_item]
			entry["qty"] = flt(entry["qty"] + component.qty, 6)
			entry["uom"] = component.stock_uom
			if component.is_product_bundle:
				entry["from_bundle"] = True
				entry["bundle_parents"].add(component.platform_item)
	return req


def _sorting_requirements(po):
	req = {}
	for item in po.items:
		if not item.platform_item:
			# 待映射的商品行不参与分拣矩阵；完成 JD SKU 映射后才会进入计算。
			continue
		if is_product_bundle(item.platform_item):
			# 组合装：先组装再分拣，按成品（套）分拣，不拆组件
			key = (item.jd_sku, item.platform_item)
			req.setdefault(key, {
				"jd_sku": item.jd_sku, "platform_item": item.platform_item,
				"stock_item": item.platform_item, "qty": 0,
			})
			req[key]["qty"] = flt(req[key]["qty"] + item.purchase_qty, 6)
			continue
		for component in expand_platform_item(item.platform_item, item.purchase_qty):
			key = (item.jd_sku, component.stock_item)
			req.setdefault(key, {"jd_sku": item.jd_sku, "platform_item": item.platform_item, "stock_item": component.stock_item, "qty": 0})
			req[key]["qty"] = flt(req[key]["qty"] + component.qty, 6)
	return list(req.values())


def _bundle_requirements(orders):
	"""批次组合装需求：platform_item → {total_qty, jd_item_name, uom, components{stock_item: per_set}}。"""
	bundles = defaultdict(lambda: {"total_qty": 0, "jd_item_name": None, "uom": None, "components": {}})
	for po in orders:
		for item in po.items:
			if not item.platform_item or not is_product_bundle(item.platform_item):
				continue
			entry = bundles[item.platform_item]
			entry["total_qty"] = flt(entry["total_qty"] + item.purchase_qty, 6)
			if not entry["jd_item_name"]:
				entry["jd_item_name"] = item.jd_item_name
			if not entry["uom"]:
				entry["uom"] = item.purchase_uom
			if item.purchase_qty > 0:
				for component in expand_platform_item(item.platform_item, item.purchase_qty):
					per_set = flt(component.qty / item.purchase_qty, 6)
					entry["components"].setdefault(component.stock_item, per_set)
	return bundles


@frappe.whitelist()
def get_assembly_workbench(import_batch: str):
	"""组装工作台：组合装需求、组件备货情况、已组装数量（成品池行）。"""
	batch = frappe.get_doc("JD Purchase Import Batch", import_batch)
	batch.check_permission("read")
	orders = _batch_orders(batch.name)
	bundles = _bundle_requirements(orders)
	pool_rows = frappe.get_all(
		"JD Stocking Pool Item",
		filters={"import_batch": batch.name},
		fields=["name", "stock_item", "warehouse", "batch_no", "stocked_qty"],
	)
	pool_by_item = defaultdict(list)
	for row in pool_rows:
		if flt(row.stocked_qty) > 0:
			pool_by_item[row.stock_item].append(row)

	assembly_rows = []
	for platform_item, entry in sorted(bundles.items()):
		assembled = flt(sum(flt(r.stocked_qty) for r in pool_by_item.get(platform_item, [])), 6)
		components = []
		components_ok = True
		for stock_item, per_set in sorted(entry["components"].items()):
			available = flt(sum(flt(r.stocked_qty) for r in pool_by_item.get(stock_item, [])), 6)
			need = flt(per_set * max(entry["total_qty"] - assembled, 0), 6)
			ok = available >= need - 1e-6
			components_ok = components_ok and ok
			components.append({
				"stock_item": stock_item,
				**_item_display(stock_item),
				"per_set": per_set,
				"need_qty": need,
				"available_qty": available,
				"batches": [
					{"warehouse": r.warehouse, "batch_no": r.batch_no, "stocked_qty": flt(r.stocked_qty)}
					for r in pool_by_item.get(stock_item, [])
				],
				"sufficient": ok,
			})
		assembly_rows.append({
			"platform_item": platform_item,
			**_item_display(platform_item),
			"jd_item_name": entry["jd_item_name"],
			"uom": entry["uom"] or _("套"),
			"required_qty": entry["total_qty"],
			"assembled_qty": assembled,
			"pending_qty": max(flt(entry["total_qty"] - assembled), 0),
			"components": components,
			"components_ready": components_ok,
			"done": assembled >= entry["total_qty"] - 1e-6,
		})
	cancel_reason = _assembly_downstream_reason(batch.name, list(bundles))
	return {
		"import_batch": batch.name,
		"can_cancel": any(row["assembled_qty"] > 0 for row in assembly_rows) and not cancel_reason,
		"cancel_reason": cancel_reason,
		"assembly_rows": assembly_rows,
		"all_done": bool(assembly_rows) and all(row["done"] for row in assembly_rows),
		"has_bundles": bool(assembly_rows),
	}


@frappe.whitelist()
def confirm_assembly(import_batch: str, rows: str | list[dict]):
	"""确认组装：扣减组件备货池行，生成组合装成品池行（分拣按成品进行）。

	rows: [{platform_item, assembled_qty}]，数量默认取剩余需求。
	幂等：成品池已满足需求的组合装自动跳过。
	"""
	batch = _lock_batch(import_batch)
	orders = _batch_orders(batch.name, for_update=True)
	_reject_downstream_rewrite(batch.name, orders)
	bundles = _bundle_requirements(orders)
	data = _parse_rows(rows)
	qty_by_item = {str(row.get("platform_item") or "").strip(): flt(row.get("assembled_qty"), 6) for row in data}

	pools = frappe.get_all(
		"JD Stocking Pool Item",
		filters={"import_batch": batch.name, "stocked_qty": [">", 0]},
		fields=["name", "stock_item", "warehouse", "stocked_qty"],
		order_by="creation",
	)
	pool_by_item = defaultdict(list)
	for row in pools:
		pool_by_item[row.stock_item].append(row)

	if _assembly_downstream_reason(batch.name, list(bundles)):
		frappe.throw(_("组合装已经分拣或装箱，不能重复确认组装。"))
	if not data or set(qty_by_item) - set(bundles):
		frappe.throw(_("请选择本批次有效的组合装。"))

	created = []
	for platform_item, entry in sorted(bundles.items()):
		remaining = flt(entry["total_qty"] - flt(sum(flt(r.stocked_qty) for r in pool_by_item.get(platform_item, []))), 6)
		if platform_item not in qty_by_item or remaining <= 1e-6:
			continue
		assembled_qty = flt(qty_by_item[platform_item], 6)
		if assembled_qty <= 0:
			continue
		if assembled_qty > remaining + 1e-6:
			frappe.throw(_("组合装 {0} 本次组装 {1} 超过剩余需求 {2}。").format(frappe.bold(platform_item), assembled_qty, remaining))
		# 成品入库仓库取组件备货量最多的仓库（扣减前统计）
		counter = defaultdict(float)
		for stock_item in entry["components"]:
			for pool in pool_by_item.get(stock_item, []):
				counter[pool.warehouse] += flt(pool.stocked_qty)
		warehouse = max(counter, key=counter.get) if counter else None
		if not warehouse:
			frappe.throw(_("组合装 {0} 没有可用的组件备货仓库。").format(frappe.bold(platform_item)))
		# 校验并扣减组件池
		for stock_item, per_set in entry["components"].items():
			need = flt(per_set * assembled_qty, 6)
			available = flt(sum(flt(r.stocked_qty) for r in pool_by_item.get(stock_item, [])), 6)
			if available < need - 1e-6:
				frappe.throw(_("物料 {0} 备货 {1}，不够组装 {2} 套 {3}（需 {4}）。请先补货或减少组装数量。").format(
					frappe.bold(stock_item), available, assembled_qty, frappe.bold(platform_item), need))
			for pool in list(pool_by_item[stock_item]):
				if need <= 0: break
				take = min(flt(pool.stocked_qty), need)
				if take <= 0: continue
				frappe.db.set_value("JD Stocking Pool Item", pool.name, "stocked_qty", flt(flt(pool.stocked_qty) - take, 6), update_modified=False)
				pool.stocked_qty = flt(pool.stocked_qty) - take
				need = flt(need - take, 6)
		# 生成成品池行
		doc = frappe.get_doc({
			"doctype": "JD Stocking Pool Item", "import_batch": batch.name,
			"company": batch.company, "stock_item": platform_item,
			"warehouse": warehouse, "batch_no": None,
			"required_qty": assembled_qty, "stocked_qty": assembled_qty,
			"available_qty_snapshot": 0, "shortage_qty": 0,
			"status": "已备货", "confirmed_by": frappe.session.user,
			"confirmed_at": now_datetime(),
		})
		doc.flags.jd_workflow_controlled = True
		doc.insert()
		created.append(doc.name)
		pool_by_item[platform_item].append(frappe._dict(name=doc.name, stock_item=platform_item, warehouse=warehouse, stocked_qty=assembled_qty))
	if created:
		for po in orders:
			_audit(po, "批次确认组装", f"批次 {batch.name}，生成 {len(created)} 行组合装成品")
	return {"import_batch": batch.name, "created_pool_rows": created, "assembled_count": len(created)}


def _assembly_downstream_reason(import_batch, bundle_items):
	if not bundle_items:
		return ""
	if frappe.db.exists("JD Sorting Allocation", {
		"import_batch": import_batch, "platform_item": ["in", bundle_items], "sorted_qty": [">", 0],
	}):
		return _("组合装已分拣，不能取消组装。")
	packed = frappe.db.sql("""select ci.name from `tabJD Carton Item` ci
		inner join `tabJD Carton` c on c.name = ci.parent
		inner join `tabJD Purchase Order` po on po.name = c.purchase_order
		where po.import_batch = %s and ci.platform_item in %s limit 1""",
		(import_batch, tuple(bundle_items)))
	return _("组合装已装箱，不能取消组装。") if packed else ""


def _assembly_restore_plan(bundles, pools):
	"""Restore each source row to its confirmed quantity, preserving warehouse/batch.

	Stocking stores original quantity as required_qty - shortage_qty. Verify the
	entire component deduction against finished bundles before restoring anything.
	This also supports assemblies created before cancellation was available.
	"""
	finished = [row for row in pools if row.stock_item in bundles and flt(row.stocked_qty) > 0]
	expected = defaultdict(float)
	for row in finished:
		for item, per_set in bundles[row.stock_item]["components"].items():
			expected[item] += flt(row.stocked_qty) * per_set
	restored = defaultdict(float)
	updates = []
	for row in pools:
		if row.stock_item not in expected:
			continue
		original = flt(flt(row.required_qty) - flt(row.shortage_qty), 6)
		deduction = flt(original - flt(row.stocked_qty), 6)
		if deduction < -1e-6:
			frappe.throw(_("组件备货数量异常，不能取消组装，请先核对备货记录。"))
		restored[row.stock_item] += deduction
		if deduction > 0:
			updates.append((row.name, original))
	if any(abs(restored[item] - qty) > 1e-6 for item, qty in expected.items()):
		frappe.throw(_("组件扣减数量与组装数量不一致，不能取消组装，请先核对备货记录。"))
	return finished, updates


@frappe.whitelist()
def cancel_assembly(import_batch: str):
	batch = _lock_batch(import_batch)
	orders = _batch_orders(batch.name, for_update=True)
	bundles = _bundle_requirements(orders)
	reason = _assembly_downstream_reason(batch.name, list(bundles))
	if reason:
		frappe.throw(reason)
	# Same batch/order locks as sorting and packing serialize downstream writes.
	_locked_pools(batch.name)
	pools = frappe.get_all("JD Stocking Pool Item", filters={"import_batch": batch.name},
		fields=["name", "stock_item", "required_qty", "stocked_qty", "shortage_qty"])
	finished, updates = _assembly_restore_plan(bundles, pools)
	for name, qty in updates:
		frappe.db.set_value("JD Stocking Pool Item", name, "stocked_qty", qty)
	for row in finished:
		frappe.db.delete("JD Stocking Pool Item", {"name": row.name})
	if finished:
		for po in orders:
			if any(item.platform_item in bundles for item in po.items):
				po.db_set({"workflow_stage": "备货", "sorting_status": "待分拣"})
			_audit(po, "取消组装", f"批次 {batch.name}，取消 {len(finished)} 行成品，恢复原仓库及批号的组件备货")
	return {"import_batch": batch.name, "cancelled_count": len(finished)}


def _validate_assembly_complete(import_batch):
	if _assembly_pending(import_batch):
		frappe.throw(_("请先完成组装确认，再进行分拣或装箱。"))


def _assembly_pending(import_batch: str) -> bool:
	"""组合装需求未全部生成成品池时视为组装未完成（无组合装的批次恒为 False）。"""
	batch = frappe.get_doc("JD Purchase Import Batch", import_batch)
	orders = _batch_orders(batch.name)
	bundles = _bundle_requirements(orders)
	if not bundles:
		return False
	for platform_item, entry in bundles.items():
		assembled = flt(frappe.db.sql(
			"""select ifnull(sum(ifnull(stocked_qty,0)),0) from `tabJD Stocking Pool Item`
			where import_batch=%s and stock_item=%s""",
			(import_batch, platform_item),
		)[0][0], 6)
		if assembled < entry["total_qty"] - 1e-6:
			return True
	return False


def _validate_soft_availability(import_batch, rows):
	by_location = defaultdict(float)
	for row in rows:
		by_location[(row["stock_item"], row["warehouse"], row["batch_no"])] += row["stocked_qty"]
	for (item, warehouse, batch_no), qty in by_location.items():
		frappe.db.sql("select name from `tabBin` where item_code=%s and warehouse=%s for update", (item, warehouse))
		frappe.db.sql("select name from `tabJD Stocking Pool Item` where stock_item=%s and warehouse=%s for update", (item, warehouse))
		available = _actual_qty(item, warehouse, batch_no) - _reserved_qty(item, warehouse, batch_no, import_batch)
		if qty > flt(available, 6): frappe.throw(_("Insufficient available inventory for Item {0} in Warehouse {1}.").format(item, warehouse))


def _actual_qty(item, warehouse, batch_no=None):
	if batch_no: return flt(get_batch_qty(batch_no, warehouse, item), 6)
	return flt(frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty"), 6)


def _reserved_qty(item, warehouse, batch_no=None, exclude_import_batch=None):
	filters = {"stock_item": item, "warehouse": warehouse, "stocked_qty": [">", 0]}
	if batch_no: filters["batch_no"] = batch_no
	if exclude_import_batch: filters["import_batch"] = ["!=", exclude_import_batch]
	return flt(sum(frappe.get_all("JD Stocking Pool Item", filters=filters, pluck="stocked_qty")), 6)


def _locked_pools(import_batch):
	frappe.db.sql("select name from `tabJD Stocking Pool Item` where import_batch=%s for update", import_batch)
	return frappe.get_all(
		"JD Stocking Pool Item",
		filters={"import_batch": import_batch},
		fields=["name", "stock_item", "warehouse", "batch_no", "stocked_qty", "shortage_qty"],
		order_by="creation",
	)


def _insert_sorting(po, req, pool, qty):
	doc = frappe.get_doc({"doctype":"JD Sorting Allocation", "import_batch":po.import_batch, "purchase_order":po.name, "company":po.company, "status":"已分拣", "jd_sku":req["jd_sku"], "platform_item":req["platform_item"], "stock_item":req["stock_item"], "stocking_pool_item":pool.name, "warehouse":pool.warehouse, "batch_no":pool.batch_no, "required_qty":req["qty"], "sorted_qty":qty})
	doc.flags.jd_workflow_controlled = True
	return doc.insert().name


def _finish_batch_sorting(batch, orders, created, shortages, action):
	shortage_by_po = defaultdict(float)
	for row in shortages: shortage_by_po[row["purchase_order"]] += row["shortage_qty"]
	for po in orders:
		shortage = flt(shortage_by_po[po.name], 6)
		sorted_qty = sum(frappe.get_all("JD Sorting Allocation", filters={"purchase_order":po.name}, pluck="sorted_qty"))
		status = "分拣异常" if shortage else "分拣完成"
		po.db_set({"workflow_stage":"分拣", "stocking_status":"备货异常" if shortage else "备货完成", "sorting_status":status, "actual_stocked_qty":sorted_qty, "actual_sorted_qty":sorted_qty, "shortage_qty":shortage, "shortage_reason":_("Insufficient stocked quantity during sorting.") if shortage else None, "workflow_exception":1 if shortage else 0})
		_audit(po, action, f"批次 {batch.name}，本单分拣 {sorted_qty}，短缺 {shortage}")
	total_shortage = flt(sum(shortage_by_po.values()), 6)
	return {"import_batch":batch.name, "sorting_status":"分拣异常" if total_shortage else "分拣完成", "rows":created, "shortages":shortages, "shortage_qty":total_shortage}


def _remaining_shelf_life(manufacturing_date, expiry_date, posting_date):
	if not manufacturing_date or not expiry_date: return None
	total = date_diff(expiry_date, manufacturing_date)
	return flt(date_diff(expiry_date, posting_date) * 100 / total, 2) if total > 0 else None


def _parse_rows(rows):
	data = json.loads(rows) if isinstance(rows, str) else rows
	if not isinstance(data, list): frappe.throw(_("Rows must be a JSON array."))
	return data


def _po_state(po):
	return {key: po.get(key) for key in ("name", "import_batch", "company", "mapping_status", "workflow_stage", "stocking_status", "sorting_status", "packing_status", "transfer_status", "actual_stocked_qty", "actual_sorted_qty", "shortage_qty", "shortage_reason", "workflow_exception")}


def _item_display(item):
	name, uom = frappe.get_cached_value("Item", item, ["item_name", "stock_uom"]) or (item, None)
	return {"stock_item_name": name or item, "uom": uom}


def _count_by(rows, field):
	counts = defaultdict(int)
	for row in rows: counts[row.get(field) or "未设置"] += 1
	return counts


def _first_shortage_reason(grouped):
	return next((row["shortage_reason"] for rows in grouped.values() for row in rows if row["shortage_reason"]), None)


def _audit(po, action, detail):
	po.add_comment("Info", f"<b>{frappe.utils.escape_html(action)}</b><br>{frappe.utils.escape_html(detail)}")

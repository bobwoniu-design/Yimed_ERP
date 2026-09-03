from __future__ import annotations

import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import date_diff, flt, getdate, now_datetime

from erpnext.stock.doctype.batch.batch import get_batch_qty

from yimed_ecommerce.jd.product_bundle import expand_platform_item
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
			"name", "destination_city", "mapping_status", "workflow_stage", "stocking_status",
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
	return {
		"import_batch": batch.name,
		"requirements": [
			{
				"stock_item": item,
				**_item_display(item),
				"required_qty": requirement["qty"],
				"uom": requirement["uom"],
			}
			for item, requirement in requirements.items()
		],
		"pool_rows": pool_rows,
	}


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
			"eligible": percent is not None and percent >= 66.6667,
		})
	return result


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
		frappe.throw(_("At least one stocking row is required."))

	grouped = defaultdict(list)
	for row in data:
		item = str(row.get("stock_item") or "").strip()
		warehouse = str(row.get("warehouse") or "").strip()
		qty = flt(row.get("stocked_qty"), 6)
		if item not in requirements or not warehouse or qty < 0:
			frappe.throw(_("Every stocking row needs a required stock Item, Warehouse and non-negative Quantity."))
		validate_warehouse_company(warehouse, batch.company)
		batch_no = str(row.get("batch_no") or "").strip() or None
		if batch_no and frappe.db.get_value("Batch", batch_no, "item") != item:
			frappe.throw(_("Batch {0} does not belong to Item {1}.").format(batch_no, item))
		grouped[item].append({"stock_item": item, "warehouse": warehouse, "batch_no": batch_no, "stocked_qty": qty, "shortage_reason": (row.get("shortage_reason") or "").strip()})

	if set(grouped) != set(requirements):
		frappe.throw(_("Stocking rows must cover every required stock Item."))
	for item, item_rows in grouped.items():
		if flt(sum(row["stocked_qty"] for row in item_rows), 6) > requirements[item]["qty"]:
			frappe.throw(_("Stocked quantity exceeds requirement for Item {0}.").format(item))
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
		reason = next((row["shortage_reason"] for row in item_rows if row["shortage_reason"]), "")
		if shortage and not reason:
			frappe.throw(_("A shortage reason is required for Item {0}.").format(item))
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
			if row.batch_no:
				batch = frappe.db.get_value("Batch", row.batch_no, ["manufacturing_date", "expiry_date"], as_dict=True)
				if not batch or (_remaining_shelf_life(batch.manufacturing_date, batch.expiry_date, getdate()) or 0) < 66.6667:
					issues.append(_("A batch is expired or below JD's shelf-life requirement.")); break
			if _actual_qty(row.stock_item, row.warehouse, row.batch_no) < _reserved_qty(row.stock_item, row.warehouse, row.batch_no):
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
				_("JD Carton {0} already exists for Purchase Order {1}. Clear downstream cartons through the rework flow before changing stocking or sorting.").format(
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
				_("Active Stock Entry {0} already exists for Purchase Order {1}. Cancel or delete it through the ERPNext/rework flow before changing stocking or sorting.").format(
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
			_("JD Handover {0} already exists for this Import Batch. Remove it through the rework flow before changing stocking or sorting.").format(
				frappe.bold(handover[0].name)
			)
		)


def _batch_stock_requirements(orders):
	req = defaultdict(lambda: {"qty":0, "uom":None})
	for po in orders:
		for item, row in _stock_requirements(po).items():
			req[item]["qty"] = flt(req[item]["qty"] + row["qty"], 6)
			req[item]["uom"] = row["uom"]
	return req


def _stock_requirements(po):
	req = defaultdict(lambda: {"qty": 0, "uom": None})
	for item in po.items:
		for component in expand_platform_item(item.platform_item, item.purchase_qty):
			req[component.stock_item]["qty"] = flt(req[component.stock_item]["qty"] + component.qty, 6)
			req[component.stock_item]["uom"] = component.stock_uom
	return req


def _sorting_requirements(po):
	req = {}
	for item in po.items:
		for component in expand_platform_item(item.platform_item, item.purchase_qty):
			key = (item.jd_sku, component.stock_item)
			req.setdefault(key, {"jd_sku": item.jd_sku, "platform_item": item.platform_item, "stock_item": component.stock_item, "qty": 0})
			req[key]["qty"] = flt(req[key]["qty"] + component.qty, 6)
	return list(req.values())


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

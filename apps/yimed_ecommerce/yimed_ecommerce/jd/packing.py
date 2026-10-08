from __future__ import annotations

import json
import math

import frappe
from frappe import _
from frappe.utils import flt, getdate, now_datetime

from yimed_ecommerce.jd.product_bundle import expand_platform_item


WHOLE_CARTON_UOMS = {"箱", "box"}


@frappe.whitelist()
def get_packing_workbench(purchase_order: str):
	"""Return one permission-checked, UI-ready view of PO packing progress."""
	po = frappe.get_doc("JD Purchase Order", purchase_order)
	po.check_permission("read")
	items = _get_aggregated_order_items(po)
	packed_by_sku = _get_packed_qty_by_sku(po.name)
	# 备货池批次（同一物料可能分布在多个批次，展示给操作员）
	# 备货池按 stock_item（组合装已拆开）存批次，需经组件展开映射回京东 SKU
	batches_by_sku: dict[str, list[str]] = {}
	pool_batches: dict[str, list[str]] = {}
	if po.import_batch:
		for row in frappe.get_all(
			"JD Stocking Pool Item",
			filters={"import_batch": po.import_batch, "batch_no": ["is", "set"]},
			fields=["stock_item", "batch_no"],
			order_by="creation",
		):
			if row.batch_no and row.batch_no not in pool_batches.setdefault(row.stock_item, []):
				pool_batches[row.stock_item].append(row.batch_no)
	for item in items:
		seen: list[str] = []
		for component in expand_platform_item(item["platform_item"], 1):
			for bn in pool_batches.get(component.stock_item, []):
				if bn not in seen:
					seen.append(bn)
		if seen:
			batches_by_sku[item["jd_sku"]] = seen
	item_rows = []
	for item in items:
		packed_qty = flt(packed_by_sku.get(item["jd_sku"]), 6)
		remaining_qty = flt(item["purchase_qty"] - packed_qty, 6)
		whole_carton = get_whole_carton_conversion(item["platform_item"], item["purchase_uom"])
		whole_qty = whole_carton["qty_in_purchase_uom"] if whole_carton else 0
		item_rows.append(
			{
				**item,
				"packed_qty": packed_qty,
				"remaining_qty": remaining_qty,
				"stock_batches": "、".join(batches_by_sku.get(item["jd_sku"], [])) or None,
				"whole_carton_uom": whole_carton["uom"] if whole_carton else None,
				"whole_carton_conversion_factor": whole_carton["conversion_factor"] if whole_carton else 0,
				"whole_carton_qty": whole_qty,
				"suggested_full_cartons": math.floor(remaining_qty / whole_qty) if whole_qty else 0,
				"suggested_remainder_qty": flt(remaining_qty % whole_qty, 6) if whole_qty else remaining_qty,
			}
		)

	cartons = []
	for carton_name in frappe.get_all(
		"JD Carton",
		filters={"purchase_order": po.name},
		pluck="name",
		order_by="carton_sequence",
	):
		carton = frappe.get_doc("JD Carton", carton_name)
		cartons.append(
			{
				"name": carton.name,
				"carton_sequence": carton.carton_sequence,
				"total_cartons": carton.total_cartons,
				"destination_city": carton.destination_city,
				"packing_date": carton.packing_date,
				"sku_count": carton.sku_count,
				"verified": carton.verified,
				"allocations": [
					{
						"jd_sku": row.jd_sku,
						"platform_item": row.platform_item,
						"platform_item_name": row.platform_item_name,
						"qty": row.qty,
						"uom": row.uom,
					}
					for row in carton.allocations
				],
				"components": [
					{
						"jd_sku": row.jd_sku,
						"platform_item": row.platform_item,
						"stock_item": row.stock_item,
						"stock_item_name": row.stock_item_name,
						"qty": row.qty,
						"uom": row.uom,
						"batch_no": row.batch_no,
						"manufacturing_date": row.manufacturing_date,
						"expiry_date": row.expiry_date,
						"remaining_shelf_life_percent": row.remaining_shelf_life_percent,
						"shelf_life_status": row.shelf_life_status,
					}
					for row in carton.components
				],
			}
		)

	from yimed_ecommerce.jd.workflow import get_order_packing_gate

	return {
		"purchase_order": {
			"name": po.name,
			"company": po.company,
			"status": po.status,
			"mapping_status": po.mapping_status,
			"packing_status": po.packing_status,
			"transfer_status": po.transfer_status,
			"stock_entry": po.stock_entry,
			"jd_warehouse": po.jd_warehouse,
			"distribution_center": po.distribution_center,
			"destination_city": po.destination_city,
			"expected_arrival_date": po.expected_arrival_date,
		},
		"items": item_rows,
		"cartons": cartons,
		"summary": {
			"sku_count": len(item_rows),
			"ordered_qty": flt(sum(row["purchase_qty"] for row in item_rows), 6),
			"packed_qty": flt(sum(row["packed_qty"] for row in item_rows), 6),
			"remaining_qty": flt(sum(row["remaining_qty"] for row in item_rows), 6),
			"carton_count": len(cartons),
			"verified_carton_count": sum(1 for row in cartons if row["verified"]),
		},
		"next_carton_sequence": max((row["carton_sequence"] for row in cartons), default=0) + 1,
		"packing_gate": get_order_packing_gate(po.name),
	}


@frappe.whitelist()
def generate_whole_carton_suggestions(
	purchase_order: str,
	packing_date: str | None = None,
	selected_skus: str | list | None = None,
):
	"""Create one single-SKU carton for every complete native Box/箱 UOM remaining.

	selected_skus：传入时只处理勾选的京东 SKU（自动装整箱按勾选执行）；
	不传时处理全部商品（原"整箱自动生成"行为）。
	零头（不足整箱）与未配置整箱单位的物料不做处理，留待手动装箱。
	"""
	po = _get_locked_po_for_packing(purchase_order)
	items = _get_aggregated_order_items(po)
	packed_by_sku = _get_packed_qty_by_sku(po.name)
	next_sequence = (
		frappe.db.get_value(
			"JD Carton", {"purchase_order": po.name}, "carton_sequence", order_by="carton_sequence desc"
		)
		or 0
	) + 1
	created = []
	results = []

	# 解析勾选 SKU
	if isinstance(selected_skus, str):
		try:
			selected_skus = json.loads(selected_skus)
		except (TypeError, ValueError):
			selected_skus = [s for s in selected_skus.split(",") if s]
	selected_set = set(selected_skus) if selected_skus else None

	for item in items:
		packed_qty = flt(packed_by_sku.get(item["jd_sku"]), 6)
		remaining_qty = flt(item["purchase_qty"] - packed_qty, 6)
		if selected_set is not None and item["jd_sku"] not in selected_set:
			continue
		if remaining_qty <= 0:
			results.append({"jd_sku": item["jd_sku"], "status": "无需装箱", "remainder_qty": 0})
			continue

		whole_carton = get_whole_carton_conversion(item["platform_item"], item["purchase_uom"])
		if not whole_carton:
			results.append(
				{
					"jd_sku": item["jd_sku"],
					"status": "未配置整箱单位",
					"message": _("物料 {0} 未配置有效的整箱单位换算，请在物料档案中设置每箱数量后重试。").format(item["platform_item"]),
					"remainder_qty": remaining_qty,
				}
			)
			continue

		qty_per_carton = whole_carton["qty_in_purchase_uom"]
		full_cartons = math.floor(remaining_qty / qty_per_carton)
		remainder_qty = flt(remaining_qty - full_cartons * qty_per_carton, 6)
		if full_cartons:
			validate_available_qty(po.name, item["jd_sku"], full_cartons * qty_per_carton)
			validate_sequence_range(po.name, next_sequence, full_cartons)
			for offset in range(full_cartons):
				created.append(
					create_carton(
						po,
						next_sequence + offset,
						packing_date,
						[
							{
								"jd_sku": item["jd_sku"],
								"platform_item": item["platform_item"],
								"qty": qty_per_carton,
								"uom": item["purchase_uom"],
								}
								],
								_pool_batch_map(po.import_batch),
								)
				)
			next_sequence += full_cartons

		results.append(
			{
				"jd_sku": item["jd_sku"],
				"status": "已生成" if full_cartons else "仅有零头",
				"whole_carton_uom": whole_carton["uom"],
				"whole_carton_conversion_factor": whole_carton["conversion_factor"],
				"qty_per_carton": qty_per_carton,
				"created_cartons": full_cartons,
				"remainder_qty": remainder_qty,
			}
		)

	if created:
		update_carton_totals(po.name)
	return {
		"purchase_order": po.name,
		"cartons": created,
		"carton_count": len(created),
		"items": results,
		"next_carton_sequence": next_sequence,
	}


@frappe.whitelist()
def update_carton_allocations(carton: str, allocations: str | list[dict]):
	"""Replace one editable carton's SKU allocations and rebuild its stock components."""
	carton_doc, po = _get_locked_editable_carton(carton)
	parsed = json.loads(allocations) if isinstance(allocations, str) else allocations
	if not isinstance(parsed, list) or not parsed:
		frappe.throw(_("Carton allocations must contain at least one row."))

	new_by_sku: dict[str, float] = {}
	for row in parsed:
		if not isinstance(row, dict):
			frappe.throw(_("Every carton allocation must be an object with JD SKU and Quantity."))
		jd_sku = str(row.get("jd_sku") or "").strip()
		qty = flt(row.get("qty"), 6)
		if not jd_sku or qty <= 0:
			frappe.throw(_("Every carton allocation must have a JD SKU and positive Quantity."))
		new_by_sku[jd_sku] = flt(new_by_sku.get(jd_sku, 0) + qty, 6)

	po.require_complete_mapping()
	new_allocations = []
	for jd_sku, qty in new_by_sku.items():
		order_item = get_order_item(po, jd_sku)
		_validate_replacement_qty(po.name, carton_doc.name, jd_sku, qty)
		new_allocations.append(
			{
				"jd_sku": jd_sku,
				"platform_item": order_item.platform_item,
				"qty": qty,
				"uom": order_item.purchase_uom,
			}
		)

	# JDCarton.validate rebuilds components with expand_platform_item and preserves
	# batch numbers from the still-present old component rows by SKU + stock item.
	# 重建前先按备货池给空批次组件填批次，新加入的 SKU 也能立即带出批次。
	pool_batches = _pool_batch_map(po.import_batch)
	if pool_batches:
		existing = {
			(row.jd_sku, row.stock_item): row.batch_no
			for row in carton_doc.components or []
		}
		provisional = []
		for jd_sku, qty in new_by_sku.items():
			order_item = get_order_item(po, jd_sku)
			for component in expand_platform_item(order_item.platform_item, qty):
				batch = existing.get((jd_sku, component.stock_item)) or pool_batches.get(component.stock_item) or ""
				provisional.append(
					{
						"jd_sku": jd_sku,
						"platform_item": order_item.platform_item,
						"stock_item": component.stock_item,
						"batch_no": batch,
					}
				)
		carton_doc.set("components", provisional)
	carton_doc.set("allocations", new_allocations)
	carton_doc.save()
	update_carton_totals(po.name)
	return _carton_result(carton_doc)


@frappe.whitelist()
def delete_unverified_carton(carton: str):
	"""Delete one editable carton without renumbering the remaining cartons."""
	carton_doc, po = _get_locked_editable_carton(carton)
	carton_name = carton_doc.name
	frappe.delete_doc("JD Carton", carton_name, ignore_permissions=True)
	update_carton_totals(po.name)
	remaining = frappe.db.count("JD Carton", {"purchase_order": po.name})
	packing_status = "装箱中" if remaining else "待装箱"
	order_status = "装箱中" if remaining else "待备货"
	frappe.db.set_value(
		"JD Purchase Order",
		po.name,
		{"packing_status": packing_status, "status": order_status},
		update_modified=False,
	)
	return {
		"purchase_order": po.name,
		"deleted_carton": carton_name,
		"carton_count": remaining,
		"packing_status": packing_status,
		"status": order_status,
	}


@frappe.whitelist()
def copy_carton(
	carton: str,
	repeat_count: int,
	start_sequence: int | None = None,
	packing_date: str | None = None,
):
	"""Copy an editable single- or mixed-SKU carton into a sequence range."""
	carton_doc, po = _get_locked_editable_carton(carton)
	po.require_complete_mapping()
	try:
		repeat_count_int = int(repeat_count)
	except (TypeError, ValueError):
		frappe.throw(_("Repeat Count must be a positive integer."))
	if repeat_count_int <= 0 or flt(repeat_count) != repeat_count_int:
		frappe.throw(_("Repeat Count must be a positive integer."))

	if start_sequence in (None, ""):
		start_sequence_int = (
			frappe.db.get_value(
				"JD Carton",
				{"purchase_order": po.name},
				"carton_sequence",
				order_by="carton_sequence desc",
			)
			or 0
		) + 1
	else:
		try:
			start_sequence_int = int(start_sequence)
		except (TypeError, ValueError):
			frappe.throw(_("Start Sequence must be a positive integer."))
		if start_sequence_int <= 0 or flt(start_sequence) != start_sequence_int:
			frappe.throw(_("Start Sequence must be a positive integer."))

	allocations = []
	copy_qty_by_sku: dict[str, float] = {}
	for row in carton_doc.allocations:
		order_item = get_order_item(po, row.jd_sku)
		if order_item.platform_item != row.platform_item:
			frappe.throw(
				_("JD SKU {0} mapping changed after this carton was created; update the carton before copying.").format(
					frappe.bold(row.jd_sku)
				)
			)
		copy_qty_by_sku[row.jd_sku] = flt(
			copy_qty_by_sku.get(row.jd_sku, 0) + row.qty * repeat_count_int,
			6,
		)
		allocations.append(
			{
				"jd_sku": row.jd_sku,
				"platform_item": row.platform_item,
				"qty": row.qty,
				"uom": row.uom,
			}
		)
	for jd_sku, qty in copy_qty_by_sku.items():
		validate_available_qty(po.name, jd_sku, qty)
	validate_sequence_range(po.name, start_sequence_int, repeat_count_int)

	batch_map = {
		f"{row.jd_sku}|{row.stock_item}": row.batch_no
		for row in carton_doc.components
		if row.jd_sku and row.stock_item and row.batch_no
	}
	created = [
		create_carton(
			po,
			start_sequence_int + offset,
			packing_date or carton_doc.packing_date,
			allocations,
			batch_map,
		)
		for offset in range(repeat_count_int)
	]
	update_carton_totals(po.name)
	return {
		"purchase_order": po.name,
		"source_carton": carton_doc.name,
		"cartons": created,
		"carton_count": len(created),
		"start_sequence": start_sequence_int,
		"next_carton_sequence": start_sequence_int + repeat_count_int,
	}


@frappe.whitelist()
def create_equal_cartons(
	purchase_order: str,
	jd_sku: str,
	total_qty: float,
	qty_per_carton: float,
	start_sequence: int = 1,
	packing_date: str | None = None,
	batch_map: str | dict | None = None,
):
	"""Generate many equal cartons plus a remainder carton in one operation."""
	total_qty = flt(total_qty)
	qty_per_carton = flt(qty_per_carton)
	start_sequence = int(start_sequence)
	if total_qty <= 0 or qty_per_carton <= 0:
		frappe.throw(_("Total Quantity and Quantity per Carton must be greater than zero."))
	if start_sequence <= 0:
		frappe.throw(_("Start Sequence must be greater than zero."))

	po = _get_locked_po_for_packing(purchase_order)
	order_item = get_order_item(po, jd_sku)
	validate_available_qty(po.name, jd_sku, total_qty)

	carton_count = math.ceil(total_qty / qty_per_carton)
	validate_sequence_range(po.name, start_sequence, carton_count)
	batches = parse_batch_map(batch_map)
	created = []
	remaining = total_qty
	for offset in range(carton_count):
		carton_qty = min(qty_per_carton, remaining)
		created.append(
			create_carton(
				po=po,
				sequence=start_sequence + offset,
				packing_date=packing_date,
				allocations=[
					{
						"jd_sku": jd_sku,
						"platform_item": order_item.platform_item,
						"qty": carton_qty,
						"uom": order_item.purchase_uom,
					}
				],
				batch_map=batches,
			)
		)
		remaining -= carton_qty

	update_carton_totals(po.name)
	return {"cartons": created, "carton_count": carton_count, "remainder_qty": flt(total_qty % qty_per_carton)}


@frappe.whitelist()
def create_mixed_cartons(
	purchase_order: str,
	template_items: str | list[dict],
	repeat_count: int,
	start_sequence: int = 1,
	packing_date: str | None = None,
	batch_map: str | dict | None = None,
):
	"""Repeat one mixed-SKU carton template without repeated manual entry."""
	items = json.loads(template_items) if isinstance(template_items, str) else template_items
	repeat_count = int(repeat_count)
	start_sequence = int(start_sequence)
	if not items or repeat_count <= 0 or start_sequence <= 0:
		frappe.throw(_("Template Items, Repeat Count and Start Sequence are required."))

	po = _get_locked_po_for_packing(purchase_order)
	allocation_qty_by_sku: dict[str, float] = {}
	for source in items:
		jd_sku = str(source.get("jd_sku") or "").strip()
		qty = flt(source.get("qty"))
		if not jd_sku or qty <= 0:
			frappe.throw(_("Every template row must have a JD SKU and positive Quantity."))
		allocation_qty_by_sku[jd_sku] = flt(allocation_qty_by_sku.get(jd_sku, 0) + qty, 6)

	allocations = []
	for jd_sku, qty in allocation_qty_by_sku.items():
		order_item = get_order_item(po, jd_sku)
		validate_available_qty(po.name, jd_sku, qty * repeat_count)
		allocations.append(
			{
				"jd_sku": jd_sku,
				"platform_item": order_item.platform_item,
				"qty": qty,
				"uom": order_item.purchase_uom,
			}
		)

	validate_sequence_range(po.name, start_sequence, repeat_count)
	batches = parse_batch_map(batch_map)
	# 手填批次优先，未填的组件用备货池选定的批次兜底
	for stock_item, batch_no in _pool_batch_map(po.import_batch).items():
		batches.setdefault(stock_item, batch_no)
	created = [
		create_carton(po, start_sequence + offset, packing_date, allocations, batches)
		for offset in range(repeat_count)
	]
	update_carton_totals(po.name)
	return {"cartons": created, "carton_count": repeat_count}


@frappe.whitelist()
def assign_batch_to_cartons(
	purchase_order: str,
	stock_item: str,
	batch_no: str,
	jd_sku: str | None = None,
	start_sequence: int | None = None,
	end_sequence: int | None = None,
	only_empty: int = 1,
):
	"""Assign one ERPNext Batch to matching component rows across many cartons."""
	po = _get_locked_po_for_packing(purchase_order)
	filters = _carton_filters(purchase_order, start_sequence, end_sequence)
	cartons = frappe.get_all("JD Carton", filters=filters, pluck="name", order_by="carton_sequence")
	for carton_name in cartons:
		frappe.db.sql("select name from `tabJD Carton` where name = %s for update", carton_name)
	updated_rows = 0
	for carton_name in cartons:
		carton = frappe.get_doc("JD Carton", carton_name)
		if carton.verified:
			# 补批次自动退回编辑态（无独立确认环节），装满后由自动确认恢复
			carton.verified = 0
			carton.flags.jd_allow_rework = True
			carton.save()
		changed = False
		for row in carton.components:
			if row.stock_item != stock_item or (jd_sku and row.jd_sku != jd_sku):
				continue
			if int(only_empty) and row.batch_no:
				continue
			row.batch_no = batch_no
			updated_rows += 1
			changed = True
		if changed:
			carton.save()
	if not updated_rows:
		frappe.throw(_("No matching carton component rows were found."))
	update_carton_totals(purchase_order)
	return {"updated_rows": updated_rows, "carton_count": len(cartons)}


@frappe.whitelist()
def undo_verify_cartons(
	purchase_order: str,
	start_sequence: int | None = None,
	end_sequence: int | None = None,
):
	"""撤销装箱确认：把已确认箱退回编辑态（受控返工）。

	前提：未生成转移单。撤销后可重新调整箱内商品/数量，再重新"确认装箱"。
	"""
	po = _get_locked_po_for_packing(purchase_order)
	filters = _carton_filters(purchase_order, start_sequence, end_sequence)
	cartons = frappe.get_all("JD Carton", filters=filters, pluck="name", order_by="carton_sequence")
	if not cartons:
		frappe.throw(_("No cartons were found."))
	undone = 0
	for carton_name in cartons:
		frappe.db.sql("select name from `tabJD Carton` where name = %s for update", carton_name)
		carton = frappe.get_doc("JD Carton", carton_name)
		if not carton.verified:
			continue
		carton.verified = 0
		carton.flags.jd_allow_rework = True
		carton.save()
		undone += 1
	if undone:
		po.db_set({"packing_status": "装箱中", "status": "装箱中"})
	return {"undone_count": undone}


@frappe.whitelist()
def verify_cartons(
	purchase_order: str,
	start_sequence: int | None = None,
	end_sequence: int | None = None,
):
	"""Validate shelf life and mark a carton sequence range as verified."""
	po = _get_locked_po_for_packing(purchase_order)
	from yimed_ecommerce.jd.workflow import validate_order_ready_for_packing

	validate_order_ready_for_packing(po.name)
	validate_packing_quantities(po.name)
	filters = _carton_filters(purchase_order, start_sequence, end_sequence)
	cartons = frappe.get_all("JD Carton", filters=filters, pluck="name", order_by="carton_sequence")
	if not cartons:
		frappe.throw(_("No cartons were found."))
	for carton_name in cartons:
		frappe.db.sql("select name from `tabJD Carton` where name = %s for update", carton_name)
		carton = frappe.get_doc("JD Carton", carton_name)
		carton.verified = 1
		carton.flags.jd_allow_verify = True
		carton.save()
	if not frappe.db.exists("JD Carton", {"purchase_order": purchase_order, "verified": 0}):
		po.db_set({"packing_status": "已装箱", "status": "已装箱", "workflow_stage": "装箱"})
	return {"verified_count": len(cartons)}


def validate_packing_quantities(purchase_order: str) -> None:
	"""Require packed quantities to exactly match ordered quantities by JD SKU."""
	discrepancies = get_packing_quantity_discrepancies(purchase_order)
	if not discrepancies:
		return

	messages = []
	for row in discrepancies:
		if row["difference"] < 0:
			messages.append(
				_("JD SKU {0}: ordered {1}, packed {2}, shortage {3}.").format(
					frappe.bold(row["jd_sku"]), row["ordered_qty"], row["packed_qty"], abs(row["difference"])
				)
			)
		else:
			messages.append(
				_("JD SKU {0}: ordered {1}, packed {2}, excess {3}.").format(
					frappe.bold(row["jd_sku"]), row["ordered_qty"], row["packed_qty"], row["difference"]
				)
			)
	frappe.throw("<br>".join(messages), title=_("Packing Quantity Mismatch"))


def packing_quantities_match(purchase_order: str) -> bool:
	return not get_packing_quantity_discrepancies(purchase_order)


def get_packing_quantity_discrepancies(purchase_order: str) -> list[dict]:
	"""Return shortages/excesses after aggregating duplicate purchase rows by JD SKU."""
	ordered_rows = frappe.db.sql(
		"""
		select jd_sku, coalesce(sum(purchase_qty), 0) as qty
		from `tabJD Purchase Order Item`
		where parent = %s and parenttype = 'JD Purchase Order'
		group by jd_sku
		""",
		purchase_order,
		as_dict=True,
	)
	packed_rows = frappe.db.sql(
		"""
		select a.jd_sku, coalesce(sum(a.qty), 0) as qty
		from `tabJD Carton Allocation` a
		inner join `tabJD Carton` c on c.name = a.parent
		where c.purchase_order = %s
		  and a.parenttype = 'JD Carton'
		  and a.parentfield = 'allocations'
		group by a.jd_sku
		""",
		purchase_order,
		as_dict=True,
	)
	ordered = {row.jd_sku: flt(row.qty, 6) for row in ordered_rows}
	packed = {row.jd_sku: flt(row.qty, 6) for row in packed_rows}
	discrepancies = []
	for jd_sku in sorted(set(ordered) | set(packed)):
		ordered_qty = ordered.get(jd_sku, 0)
		packed_qty = packed.get(jd_sku, 0)
		difference = flt(packed_qty - ordered_qty, 6)
		if difference:
			discrepancies.append(
				{
					"jd_sku": jd_sku,
					"ordered_qty": ordered_qty,
					"packed_qty": packed_qty,
					"difference": difference,
				}
			)
	return discrepancies


@frappe.whitelist()
def get_carton_names(purchase_order: str, cartons=None):
	po = frappe.get_doc("JD Purchase Order", purchase_order)
	po.check_permission("read")
	names = frappe.get_all(
		"JD Carton",
		filters={"purchase_order": purchase_order},
		pluck="name",
		order_by="carton_sequence",
	)
	if cartons is not None:
		selected = frappe.parse_json(cartons) if isinstance(cartons, str) else cartons
		if not isinstance(selected, list) or any(not isinstance(name, str) for name in selected):
			frappe.throw(_("Cartons must be a list of carton names."))
		if set(selected) - set(names):
			frappe.throw(_("Selected cartons do not belong to this Purchase Order."))
		names = [name for name in names if name in set(selected)]
	for name in names:
		current_count = frappe.db.get_value("JD Carton", name, "print_count") or 0
		frappe.db.set_value(
			"JD Carton",
			name,
			{
				"print_count": current_count + 1,
				"last_printed_by": frappe.session.user,
				"last_printed_at": now_datetime(),
			},
			update_modified=False,
		)
	return names


def create_carton(po, sequence, packing_date, allocations, batch_map):
	doc = frappe.new_doc("JD Carton")
	doc.purchase_order = po.name
	doc.carton_sequence = sequence
	doc.packing_date = getdate(packing_date)
	for allocation in allocations:
		doc.append("allocations", allocation)
		for component in expand_platform_item(allocation["platform_item"], allocation["qty"]):
			doc.append(
				"components",
				{
					"jd_sku": allocation["jd_sku"],
					"platform_item": allocation["platform_item"],
					"stock_item": component.stock_item,
					"batch_no": batch_map.get(f"{allocation['jd_sku']}|{component.stock_item}")
					or batch_map.get(component.stock_item),
				},
			)
	doc.insert()
	return doc.name


def get_order_item(po, jd_sku):
	rows = [row for row in po.items if row.jd_sku == jd_sku]
	if not rows:
		frappe.throw(_("JD SKU {0} is not in Purchase Order {1}.").format(frappe.bold(jd_sku), po.name))
	platform_items = {row.platform_item for row in rows}
	if len(platform_items) != 1:
		frappe.throw(_("JD SKU {0} has inconsistent Item mappings in the Purchase Order.").format(jd_sku))
	return rows[0]


def _get_locked_editable_carton(carton: str):
	if not carton or not frappe.db.exists("JD Carton", carton):
		frappe.throw(_("JD Carton {0} does not exist.").format(carton or ""))
	purchase_order = frappe.db.get_value("JD Carton", carton, "purchase_order")
	frappe.db.sql(
		"select name from `tabJD Purchase Order` where name = %s for update",
		purchase_order,
	)
	frappe.db.sql("select name from `tabJD Carton` where name = %s for update", carton)
	carton_doc = frappe.get_doc("JD Carton", carton)
	if carton_doc.purchase_order != purchase_order:
		frappe.throw(_("Carton's Purchase Order changed while locking; please retry."))
	po = frappe.get_doc("JD Purchase Order", purchase_order)
	po.check_permission("write")
	carton_doc.check_permission("write")
	_assert_no_active_transfer(po)
	if carton_doc.verified:
		# 待装=0 即完成，无独立确认环节：编辑已确认箱自动退回编辑态，
		# 调整后若再次装满会由 update_carton_totals 自动重新确认
		carton_doc.verified = 0
		carton_doc.flags.jd_allow_rework = True
		carton_doc.save()
		frappe.db.set_value(
			"JD Purchase Order",
			purchase_order,
			{"packing_status": "装箱中", "status": "装箱中"},
			update_modified=False,
		)
		carton_doc = frappe.get_doc("JD Carton", carton)
	return carton_doc, po


def _get_locked_po_for_packing(purchase_order: str):
	if not purchase_order or not frappe.db.exists("JD Purchase Order", purchase_order):
		frappe.throw(_("JD Purchase Order {0} does not exist.").format(purchase_order or ""))
	frappe.db.sql("select name from `tabJD Purchase Order` where name = %s for update", purchase_order)
	po = frappe.get_doc("JD Purchase Order", purchase_order)
	po.check_permission("write")
	po.require_complete_mapping()
	_assert_no_active_transfer(po)
	return po


def _assert_no_active_transfer(po) -> None:
	entry = frappe.db.get_value(
		"Stock Entry",
		{"custom_jd_purchase_order": po.name, "docstatus": ["<", 2]},
		["name", "docstatus"],
		as_dict=True,
	)
	if not entry:
		return
	if entry.docstatus == 1:
		frappe.throw(
			_("Stock Entry {0} is submitted. Cancel it through ERPNext before changing cartons.").format(
				frappe.bold(entry.name)
			)
		)
	frappe.throw(
		_("Draft Stock Entry {0} exists. Delete it before changing cartons.").format(
			frappe.bold(entry.name)
		)
	)


def _validate_replacement_qty(purchase_order: str, carton: str, jd_sku: str, replacement_qty: float) -> None:
	ordered = frappe.db.sql(
		"""
		select coalesce(sum(purchase_qty), 0)
		from `tabJD Purchase Order Item`
		where parent = %s and parenttype = 'JD Purchase Order' and jd_sku = %s
		""",
		(purchase_order, jd_sku),
	)[0][0]
	packed_elsewhere = frappe.db.sql(
		"""
		select coalesce(sum(a.qty), 0)
		from `tabJD Carton Allocation` a
		inner join `tabJD Carton` c on c.name = a.parent
		where c.purchase_order = %s
		  and c.name != %s
		  and a.parenttype = 'JD Carton'
		  and a.parentfield = 'allocations'
		  and a.jd_sku = %s
		""",
		(purchase_order, carton, jd_sku),
	)[0][0]
	if flt(packed_elsewhere, 6) + flt(replacement_qty, 6) > flt(ordered, 6):
		frappe.throw(
			_(
				"Updating this carton would exceed ordered quantity for JD SKU {0}: "
				"ordered {1}, packed in other cartons {2}, replacement {3}."
			).format(
				frappe.bold(jd_sku),
				flt(ordered, 6),
				flt(packed_elsewhere, 6),
				flt(replacement_qty, 6),
			)
		)


def _carton_result(carton_doc) -> dict:
	return {
		"name": carton_doc.name,
		"purchase_order": carton_doc.purchase_order,
		"carton_sequence": carton_doc.carton_sequence,
		"sku_count": carton_doc.sku_count,
		"verified": carton_doc.verified,
		"allocations": [
			{
				"jd_sku": row.jd_sku,
				"platform_item": row.platform_item,
				"qty": row.qty,
				"uom": row.uom,
			}
			for row in carton_doc.allocations
		],
		"components": [
			{
				"jd_sku": row.jd_sku,
				"platform_item": row.platform_item,
				"stock_item": row.stock_item,
				"qty": row.qty,
				"uom": row.uom,
				"batch_no": row.batch_no,
				"manufacturing_date": row.manufacturing_date,
				"expiry_date": row.expiry_date,
				"remaining_shelf_life_percent": row.remaining_shelf_life_percent,
				"shelf_life_status": row.shelf_life_status,
			}
			for row in carton_doc.components
		],
	}


def _get_aggregated_order_items(po) -> list[dict]:
	"""Aggregate duplicate JD SKU rows while preserving useful display metadata."""
	items: dict[str, dict] = {}
	item_metadata: dict[str, frappe._dict] = {}
	for row in po.items:
		if row.platform_item and row.platform_item not in item_metadata:
			item_metadata[row.platform_item] = frappe.db.get_value(
				"Item", row.platform_item, ["item_name", "stock_uom"], as_dict=True
			) or frappe._dict()
		metadata = item_metadata.get(row.platform_item, frappe._dict())
		purchase_uom = row.purchase_uom or metadata.get("stock_uom")
		if row.jd_sku not in items:
			items[row.jd_sku] = {
				"jd_sku": row.jd_sku,
				"jd_item_name": row.jd_item_name,
				"platform_item": row.platform_item,
				"platform_item_name": row.platform_item_name or metadata.get("item_name"),
				"purchase_qty": 0,
				"purchase_uom": purchase_uom,
				"mapping_status": row.mapping_status,
			}
		current = items[row.jd_sku]
		if current["platform_item"] != row.platform_item or current["purchase_uom"] != purchase_uom:
			frappe.throw(
				_("JD SKU {0} has inconsistent Item mappings or purchase UOMs in the Purchase Order.").format(
					frappe.bold(row.jd_sku)
				)
			)
		current["purchase_qty"] = flt(current["purchase_qty"] + row.purchase_qty, 6)
		if not current["jd_item_name"] and row.jd_item_name:
			current["jd_item_name"] = row.jd_item_name
	return list(items.values())


def _get_packed_qty_by_sku(purchase_order: str) -> dict[str, float]:
	rows = frappe.db.sql(
		"""
		select a.jd_sku, coalesce(sum(a.qty), 0) as qty
		from `tabJD Carton Allocation` a
		inner join `tabJD Carton` c on c.name = a.parent
		where c.purchase_order = %s
		  and a.parenttype = 'JD Carton'
		  and a.parentfield = 'allocations'
		group by a.jd_sku
		""",
		purchase_order,
		as_dict=True,
	)
	return {row.jd_sku: flt(row.qty, 6) for row in rows}


def get_whole_carton_conversion(platform_item: str | None, purchase_uom: str | None) -> dict | None:
	"""Resolve native Item UOM conversion and express one carton in the PO's UOM."""
	if not platform_item:
		return None
	item = frappe.db.get_value("Item", platform_item, ["stock_uom"], as_dict=True)
	if not item:
		return None
	uoms = frappe.get_all(
		"UOM Conversion Detail",
		filters={"parent": platform_item, "parenttype": "Item", "parentfield": "uoms"},
		fields=["uom", "conversion_factor"],
	)
	box_rows = [row for row in uoms if (row.uom or "").strip().lower() in WHOLE_CARTON_UOMS]
	if (item.stock_uom or "").strip().lower() in WHOLE_CARTON_UOMS:
		box_rows.append(frappe._dict(uom=item.stock_uom, conversion_factor=1))
	if not box_rows:
		return None
	box_rows.sort(key=lambda row: (0 if row.uom == "箱" else 1, row.uom))
	box = box_rows[0]
	box_factor = flt(box.conversion_factor, 6)
	if box_factor <= 0:
		return None

	purchase_uom = purchase_uom or item.stock_uom
	if purchase_uom == item.stock_uom:
		purchase_factor = 1
	else:
		purchase_factor = next(
			(flt(row.conversion_factor, 6) for row in uoms if row.uom == purchase_uom),
			0,
		)
		if purchase_factor <= 0:
			# 吉客云采购单的计量单位（如"个"）偶尔与货品主档库存单位（如"盒"）不一致
			# 且无换算行。整箱建议仅为作业参考，此时按 1:1 视同库存单位计算。
			purchase_factor = 1
	if purchase_factor <= 0:
		return None
	qty_in_purchase_uom = flt(box_factor / purchase_factor, 6)
	if qty_in_purchase_uom <= 0:
		return None
	return {
		"uom": box.uom,
		"conversion_factor": box_factor,
		"purchase_uom": purchase_uom,
		"purchase_uom_conversion_factor": purchase_factor,
		"qty_in_purchase_uom": qty_in_purchase_uom,
	}


def validate_available_qty(purchase_order, jd_sku, new_qty):
	ordered = frappe.db.sql(
		"""
		select coalesce(sum(purchase_qty), 0)
		from `tabJD Purchase Order Item`
		where parent = %s and parenttype = 'JD Purchase Order' and jd_sku = %s
		""",
		(purchase_order, jd_sku),
	)[0][0]
	packed = frappe.db.sql(
		"""
		select coalesce(sum(a.qty), 0)
		from `tabJD Carton Allocation` a
		inner join `tabJD Carton` c on c.name = a.parent
		where c.purchase_order = %s and a.parenttype = 'JD Carton' and a.jd_sku = %s
		""",
		(purchase_order, jd_sku),
	)[0][0]
	if flt(packed) + flt(new_qty) > flt(ordered):
		frappe.throw(
			_("Packing would exceed ordered quantity for JD SKU {0}: ordered {1}, packed {2}, new {3}.").format(
				frappe.bold(jd_sku), flt(ordered), flt(packed), flt(new_qty)
			)
		)


def validate_sequence_range(purchase_order, start_sequence, count):
	sequences = list(range(start_sequence, start_sequence + count))
	conflict = frappe.db.get_value(
		"JD Carton",
		{"purchase_order": purchase_order, "carton_sequence": ["in", sequences]},
		"carton_sequence",
	)
	if conflict:
		frappe.throw(_("Carton Sequence {0} already exists.").format(conflict))


def update_carton_totals(purchase_order):
	total = frappe.db.count("JD Carton", {"purchase_order": purchase_order})
	frappe.db.set_value("JD Carton", {"purchase_order": purchase_order}, "total_cartons", total, update_modified=False)
	frappe.db.set_value("JD Purchase Order", purchase_order, "total_cartons", total, update_modified=False)
	_auto_verify_if_complete(purchase_order)


def _auto_verify_if_complete(purchase_order):
	"""待装归零 = 装箱完成：自动填批次并确认全部箱，采购单状态置"已装箱"。

	无独立"确认"环节——修改箱（改数量/加删商品）会把状态带回"装箱中"，
	调整到再次装满时自动重新确认。
	"""
	try:
		items = _get_aggregated_order_items(frappe.get_doc("JD Purchase Order", purchase_order))
		packed_by_sku = _get_packed_qty_by_sku(purchase_order)
		for item in items:
			remaining = flt(item["purchase_qty"] - flt(packed_by_sku.get(item["jd_sku"]), 6), 6)
			if remaining > 0:
				# 还有待装：装箱进行中（有箱时状态统一由本函数管理）
				if frappe.db.count("JD Carton", {"purchase_order": purchase_order}):
					frappe.db.set_value(
						"JD Purchase Order",
						purchase_order,
						{"packing_status": "装箱中", "status": "装箱中"},
						update_modified=False,
					)
				return  # 装箱进行中
	except Exception:
		return
	pending = frappe.get_all("JD Carton", filters={"purchase_order": purchase_order, "verified": 0}, pluck="name")
	if not pending:
		return
	# 确认前从备货池继承批次号（备货时选定的批次 → 箱组件）
	po_doc = frappe.get_doc("JD Purchase Order", purchase_order)
	pool_map = {}
	for r in frappe.get_all(
		"JD Stocking Pool Item",
		filters={"import_batch": po_doc.import_batch, "batch_no": ["is", "set"]},
		fields=["stock_item", "batch_no"],
	):
		if r.stock_item not in pool_map:
			pool_map[r.stock_item] = r.batch_no
	verified_failed = False
	for carton_name in pending:
		frappe.db.sql("select name from `tabJD Carton` where name = %s for update", carton_name)
		carton = frappe.get_doc("JD Carton", carton_name)
		# 第一步：普通保存，补齐组件批次
		changed = False
		for row in carton.components:
			if not row.batch_no and pool_map.get(row.stock_item):
				row.batch_no = pool_map[row.stock_item]
				changed = True
		if changed:
			carton.save()
		# 预检查：批次商品的组件必须有批次号（确认前必须填批次）。
		# 仍缺的箱跳过确认，PO 保持"装箱中"；操作员用"批量填写批号"补齐后
		# （assign_batch 会再次触达本函数）自动完成。
		if any(
			not row.batch_no and frappe.get_cached_value("Item", row.stock_item, "has_batch_no")
			for row in carton.components
		):
			verified_failed = True
			continue
		# 第二步：单独确认（受控验证要求确认动作不伴随其他变更）
		if not frappe.db.get_value("JD Carton", carton_name, "verified"):
			carton = frappe.get_doc("JD Carton", carton_name)
			carton.verified = 1
			carton.flags.jd_allow_verify = True
			carton.save()
	if verified_failed:
		return
	frappe.get_doc("JD Purchase Order", purchase_order).db_set(
		{"packing_status": "已装箱", "status": "已装箱", "workflow_stage": "装箱"}
	)
	if pending:
		frappe.get_doc("JD Purchase Order", purchase_order).db_set(
			{"packing_status": "已装箱", "status": "已装箱", "workflow_stage": "装箱"}
		)


def parse_batch_map(batch_map) -> dict:
	if not batch_map:
		return {}
	return json.loads(batch_map) if isinstance(batch_map, str) else dict(batch_map)


def _pool_batch_map(import_batch: str) -> dict:
	"""备货池选定的批次 → {stock_item: batch_no}，装箱组件生成时自动继承。"""
	result = {}
	if not import_batch:
		return result
	for row in frappe.get_all(
		"JD Stocking Pool Item",
		filters={"import_batch": import_batch, "batch_no": ["is", "set"]},
		fields=["stock_item", "batch_no"],
		order_by="creation",
	):
		if row.stock_item and row.batch_no and row.stock_item not in result:
			result[row.stock_item] = row.batch_no
	return result


def _carton_filters(purchase_order, start_sequence=None, end_sequence=None):
	filters = {"purchase_order": purchase_order}
	start = int(start_sequence) if start_sequence else None
	end = int(end_sequence) if end_sequence else None
	if start and end and start > end:
		frappe.throw(_("Start Sequence cannot be greater than End Sequence."))
	if start and end:
		filters["carton_sequence"] = ["between", [start, end]]
	elif start:
		filters["carton_sequence"] = [">=", start]
	elif end:
		filters["carton_sequence"] = ["<=", end]
	return filters

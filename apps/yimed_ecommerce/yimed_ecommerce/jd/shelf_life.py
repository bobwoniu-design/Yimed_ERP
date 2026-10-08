from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import date_diff, flt, getdate


MIN_REMAINING_SHELF_LIFE_PERCENT = 66.6667
WARNING_REMAINING_SHELF_LIFE_PERCENT = 70.0


def apply_batch_shelf_life(row, posting_date=None) -> None:
	"""Fetch canonical Batch dates and record shelf-life status without a ratio gate."""
	if not row.batch_no:
		row.manufacturing_date = None
		row.expiry_date = None
		row.remaining_shelf_life_percent = 0
		row.shelf_life_status = "无需校验"
		return

	batch = frappe.db.get_value(
		"Batch",
		row.batch_no,
		["item", "manufacturing_date", "expiry_date"],
		as_dict=True,
	)
	if not batch:
		frappe.throw(_("Batch {0} does not exist.").format(frappe.bold(row.batch_no)))
	if batch.item != row.stock_item:
		frappe.throw(
			_("Batch {0} belongs to Item {1}, not {2}.").format(
				frappe.bold(row.batch_no), frappe.bold(batch.item), frappe.bold(row.stock_item)
			)
		)

	row.manufacturing_date = batch.manufacturing_date
	row.expiry_date = batch.expiry_date
	if not batch.manufacturing_date or not batch.expiry_date:
		frappe.throw(
			_("Batch {0} must have Manufacturing Date and Expiry Date before JD packing.").format(
				frappe.bold(row.batch_no)
			)
		)

	packing_date = getdate(posting_date)
	total_days = date_diff(batch.expiry_date, batch.manufacturing_date)
	remaining_days = date_diff(batch.expiry_date, packing_date)
	if total_days <= 0:
		frappe.throw(_("Batch {0} has an invalid shelf-life date range.").format(frappe.bold(row.batch_no)))

	row.remaining_shelf_life_percent = flt(remaining_days * 100 / total_days, 2)
	if row.remaining_shelf_life_percent < MIN_REMAINING_SHELF_LIFE_PERCENT:
		row.shelf_life_status = "不符合"
		# 剩余效期比例仅记录状态，不拦截生成、修改或确认装箱。
		return
	row.shelf_life_status = (
		"临期预警"
		if row.remaining_shelf_life_percent < WARNING_REMAINING_SHELF_LIFE_PERCENT
		else "符合"
	)

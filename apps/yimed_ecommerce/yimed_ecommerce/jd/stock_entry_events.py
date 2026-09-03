import frappe


def on_submit(doc, method=None):
	context = _get_jd_context(doc)
	if not context:
		return

	if doc.outgoing_stock_entry:
		transfer_status = "已完成"
		order_status = "已完成"
		frappe.db.set_value(
			"Stock Entry",
			doc.name,
			{
				"custom_jd_purchase_order": context.purchase_order,
				"custom_jd_transfer_mode": context.transfer_mode,
				"custom_jd_final_warehouse": context.final_warehouse,
			},
			update_modified=False,
		)
	else:
		transfer_status = "在途" if context.transfer_mode == "两步调拨" else "已完成"
		order_status = "已发货" if context.transfer_mode == "两步调拨" else "已完成"
	frappe.db.set_value(
		"JD Purchase Order",
		context.purchase_order,
		{
			"stock_entry": doc.outgoing_stock_entry or doc.name,
			"transfer_status": transfer_status,
			"status": order_status,
		},
		update_modified=False,
	)


def on_cancel(doc, method=None):
	_reset_purchase_order(doc)


def on_trash(doc, method=None):
	_reset_purchase_order(doc)


def _reset_purchase_order(doc):
	context = _get_jd_context(doc)
	if not context:
		return
	if doc.outgoing_stock_entry:
		frappe.db.set_value(
			"JD Purchase Order",
			context.purchase_order,
			{"transfer_status": "在途", "status": "已发货"},
			update_modified=False,
		)
		return

	linked_entry = frappe.db.get_value("JD Purchase Order", context.purchase_order, "stock_entry")
	if linked_entry != doc.name:
		return
	frappe.db.set_value(
		"JD Purchase Order",
		context.purchase_order,
		{
			"stock_entry": None,
			"transfer_status": "待调拨",
			"status": "已装箱",
		},
		update_modified=False,
	)


def _get_jd_context(doc):
	if doc.custom_jd_purchase_order:
		return frappe._dict(
			purchase_order=doc.custom_jd_purchase_order,
			transfer_mode=doc.custom_jd_transfer_mode,
			final_warehouse=doc.custom_jd_final_warehouse,
		)
	if not doc.outgoing_stock_entry:
		return None
	row = frappe.db.get_value(
		"Stock Entry",
		doc.outgoing_stock_entry,
		["custom_jd_purchase_order", "custom_jd_transfer_mode", "custom_jd_final_warehouse"],
		as_dict=True,
	)
	if not row or not row.custom_jd_purchase_order:
		return None
	return frappe._dict(
		purchase_order=row.custom_jd_purchase_order,
		transfer_mode=row.custom_jd_transfer_mode,
		final_warehouse=row.custom_jd_final_warehouse,
	)

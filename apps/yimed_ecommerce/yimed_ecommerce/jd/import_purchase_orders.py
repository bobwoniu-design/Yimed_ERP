from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import flt, now_datetime
from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file


REQUIRED_HEADERS = {"采购单号", "商品编号", "采购数量"}


def import_purchase_orders(batch) -> dict:
	batch.check_permission("write")
	batch.db_set("status", "导入中")

	purchase_order_count = 0
	item_row_count = 0
	failed_po_no = None

	try:
		rows = read_xlsx_file_from_attached_file(file_url=batch.import_file, read_only=True)
		if not rows:
			frappe.throw(_("上传的表格文件为空。"))

		headers = [normalize_text(value) for value in rows[0]]
		missing = REQUIRED_HEADERS - set(headers)
		if missing:
			frappe.throw(_("缺少必填列：{0}").format(", ".join(sorted(missing))))

		data_rows = [dict(zip(headers, row, strict=False)) for row in rows[1:] if any(value not in (None, "") for value in row)]
		grouped = defaultdict(list)
		for row in data_rows:
			purchase_order_no = normalize_identifier(row.get("采购单号"))
			if purchase_order_no:
				grouped[purchase_order_no].append(row)

		purchase_order_count = len(grouped)
		item_row_count = len(data_rows)

		# 原子导入：任何一个采购单失败，整体回滚，不导入部分
		for purchase_order_no, purchase_rows in grouped.items():
			failed_po_no = purchase_order_no
			create_purchase_order(batch, purchase_order_no, purchase_rows)
	except Exception as exc:
		frappe.db.rollback()
		frappe.clear_messages()
		error_msg = f"{failed_po_no}: {exc!s}" if failed_po_no else str(exc)
		batch.db_set(
			{
				"imported_at": now_datetime(),
				"imported_by": frappe.session.user,
				"purchase_order_count": purchase_order_count,
				"item_row_count": item_row_count,
				"success_count": 0,
				"failed_count": purchase_order_count,
				"error_log": error_msg,
				"status": "导入失败",
			}
		)
		return {
			"purchase_order_count": purchase_order_count,
			"item_row_count": item_row_count,
			"success_count": 0,
			"failed_count": purchase_order_count,
			"error": error_msg,
		}

	frappe.clear_messages()
	batch.db_set(
		{
			"imported_at": now_datetime(),
			"imported_by": frappe.session.user,
			"purchase_order_count": purchase_order_count,
			"item_row_count": item_row_count,
			"success_count": purchase_order_count,
			"failed_count": 0,
			"error_log": "",
			"status": "导入成功",
		}
	)
	return {
		"purchase_order_count": purchase_order_count,
		"item_row_count": item_row_count,
		"success_count": purchase_order_count,
		"failed_count": 0,
	}


@frappe.whitelist()
def run_import_by_name(batch_name: str) -> dict:
	"""按批次号执行导入。用独立函数而非 doc method，避免 frm.call 触发 check_if_latest 时间戳冲突。"""
	batch = frappe.get_doc("JD Purchase Import Batch", batch_name)
	batch.check_permission("write")
	return import_purchase_orders(batch)


@frappe.whitelist()
def validate_purchase_orders_file(file_url: str) -> dict:
	"""预检采购单文件：解析并统计可成功/会失败的采购单条数，不落库。"""
	frappe.has_permission("JD Purchase Import Batch", "create", throw=True)

	rows = read_xlsx_file_from_attached_file(file_url=file_url, read_only=True)
	if not rows:
		frappe.throw(_("上传的表格文件为空。"))

	headers = [normalize_text(value) for value in rows[0]]
	missing = REQUIRED_HEADERS - set(headers)
	if missing:
		frappe.throw(_("缺少必填列：{0}").format(", ".join(sorted(missing))))

	data_rows = [
		dict(zip(headers, row, strict=False))
		for row in rows[1:]
		if any(value not in (None, "") for value in row)
	]
	grouped = defaultdict(list)
	for row in data_rows:
		purchase_order_no = normalize_identifier(row.get("采购单号"))
		if purchase_order_no:
			grouped[purchase_order_no].append(row)

	success_count = 0
	errors = []
	for purchase_order_no, purchase_rows in grouped.items():
		reason = _predict_purchase_order_failure(purchase_order_no, purchase_rows)
		if reason:
			errors.append(f"{purchase_order_no}: {reason}")
		else:
			success_count += 1

	return {
		"total": len(grouped),
		"item_row_count": len(data_rows),
		"success_count": success_count,
		"failed_count": len(errors),
		"errors": errors,
	}


def _predict_purchase_order_failure(purchase_order_no: str, rows: list[dict]) -> str | None:
	"""轻量预检单条采购单：重复、缺少商品编号、采购数量无效。"""
	if frappe.db.exists("JD Purchase Order", purchase_order_no):
		return _("采购单已存在。")
	for row in rows:
		if not normalize_identifier(row.get("商品编号")):
			return _("存在缺少商品编号的行。")
		if flt(row.get("采购数量")) <= 0:
			return _("存在采购数量无效的行。")
	return None


def create_purchase_order(batch, purchase_order_no: str, rows: list[dict]):
	if frappe.db.exists("JD Purchase Order", purchase_order_no):
		frappe.throw(_("采购单已存在，未覆盖。"))

	first = rows[0]
	po = frappe.new_doc("JD Purchase Order")
	po.jd_purchase_order_no = purchase_order_no
	po.import_batch = batch.name
	po.company = batch.company
	po.order_datetime = first.get("订购时间")
	po.jd_order_status = normalize_text(first.get("订单状态"))
	po.jd_warehouse = normalize_text(first.get("京东仓库"))
	po.jd_warehouse_address = normalize_text(first.get("京东库房地址") or first.get("送货地址"))
	po.distribution_center = normalize_text(first.get("配送中心"))
	po.destination_city = normalize_text(first.get("京东站点") or first.get("配送中心") or first.get("京东仓库"))
	po.expected_arrival_date = first.get("预计送达时间")
	po.delivery_method = normalize_text(first.get("配送方式"))
	po.contact_person = normalize_text(first.get("收货负责人") or first.get("送货库房联系人"))
	po.contact_phone = normalize_identifier(first.get("收货电话") or first.get("送货库房联系电话"))
	po.remarks = normalize_text(first.get("备注"))

	for source in rows:
		po.append(
			"items",
			{
				"jd_sku": normalize_identifier(source.get("商品编号")),
				"jd_item_name": normalize_text(source.get("商品名称")),
				"purchase_qty": flt(source.get("采购数量")),
				"received_qty": flt(source.get("实收数量")),
				"purchase_uom": normalize_uom(source.get("采购单位")),
				"purchase_price": flt(source.get("采购价格")),
			},
		)
	po.insert()
	po.db_set("status", "待备货" if po.mapping_status == "已映射" else "待映射")
	return po


def normalize_identifier(value) -> str:
	if value in (None, ""):
		return ""
	if isinstance(value, Decimal):
		value = float(value)
	if isinstance(value, float) and value.is_integer():
		return str(int(value))
	return str(value).strip()


def normalize_text(value) -> str:
	if value in (None, ""):
		return ""
	if isinstance(value, (date, datetime)):
		return str(value)
	return normalize_identifier(value)


def normalize_uom(value):
	uom = normalize_text(value)
	return uom if uom and frappe.db.exists("UOM", uom) else None

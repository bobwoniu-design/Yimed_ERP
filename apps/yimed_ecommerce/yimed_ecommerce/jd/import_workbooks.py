from __future__ import annotations

from io import BytesIO

import frappe
import xlsxwriter
from frappe import _
from frappe.desk.utils import provide_binary_file


IMPORT_COLUMNS = (
	("采购单号", True, "158914786507952", "京东采购单号；同一采购单的多行商品必须保持一致。"),
	("商品编号", True, "100295959797", "京东 SKU 编码；导入后通过 SKU 映射匹配 ERP 商品。"),
	("商品名称", False, "示例商品", "京东商品名称。"),
	("采购数量", True, 10, "大于 0 的数字。"),
	("实收数量", False, 0, "京东已有实收数量；没有时可留空。"),
	("采购单位", False, "盒", "应与 ERPNext 中已存在的单位名称一致。"),
	("采购价格", False, 12.5, "数字，不要包含货币符号。"),
	("订购时间", False, "2026-08-19 09:30:00", "推荐格式：YYYY-MM-DD HH:MM:SS。"),
	("订单状态", False, "待送货", "京东采购单状态。"),
	("京东仓库", False, "上海仓", "京东收货仓库名称。"),
	("京东库房地址", False, "上海市示例路 1 号", "京东收货地址；京东文件中的“送货地址”也可识别。"),
	("配送中心", False, "上海", "配送中心。"),
	("京东站点", False, "上海", "目的城市或京东站点。"),
	("预计送达时间", False, "2026-08-20", "推荐格式：YYYY-MM-DD。"),
	("配送方式", False, "上门揽收", "配送或交接方式。"),
	("收货负责人", False, "张三", "京东收货联系人；“送货库房联系人”也可识别。"),
	("收货电话", False, "13800000000", "请按文本填写，避免丢失前导零。"),
	("备注", False, "", "其他需要保留的说明。"),
)


def _new_workbook():
	output = BytesIO()
	workbook = xlsxwriter.Workbook(
		output,
		{
			"in_memory": True,
			"strings_to_formulas": False,
			"strings_to_urls": False,
		},
	)
	return output, workbook


def build_import_template() -> bytes:
	output, workbook = _new_workbook()
	header = workbook.add_format(
		{"bold": True, "bg_color": "#D9EAF7", "border": 1, "align": "center", "valign": "vcenter"}
	)
	required_header = workbook.add_format(
		{"bold": True, "bg_color": "#FCE4D6", "font_color": "#C00000", "border": 1, "align": "center"}
	)
	example = workbook.add_format({"border": 1, "font_color": "#666666"})
	note_title = workbook.add_format(
		{"bold": True, "bg_color": "#1F4E78", "font_color": "#FFFFFF", "border": 1, "align": "center"}
	)
	note_cell = workbook.add_format({"border": 1, "valign": "top", "text_wrap": True})
	required = workbook.add_format({"border": 1, "font_color": "#C00000", "align": "center"})

	data_sheet = workbook.add_worksheet("采购订单导入")
	data_sheet.freeze_panes(1, 0)
	data_sheet.autofilter(0, 0, 1, len(IMPORT_COLUMNS) - 1)
	for column, (label, is_required, sample, description) in enumerate(IMPORT_COLUMNS):
		data_sheet.write(0, column, label, required_header if is_required else header)
		data_sheet.write(1, column, sample, example)
		data_sheet.write_comment(0, column, ("必填；" if is_required else "选填；") + description)
		width = max(12, min(28, max(len(label) * 2, len(str(sample)) + 2)))
		data_sheet.set_column(column, column, width)
	data_sheet.set_row(0, 24)

	note_sheet = workbook.add_worksheet("填写说明")
	note_sheet.set_column(0, 0, 18)
	note_sheet.set_column(1, 1, 10)
	note_sheet.set_column(2, 2, 24)
	note_sheet.set_column(3, 3, 66)
	note_sheet.merge_range(
		0,
		0,
		0,
		3,
		"可以直接上传京东后台导出的采购订单明细；本模板用于手工整理或核对字段。第一行字段名请勿修改。",
		workbook.add_format({"bold": True, "bg_color": "#FFF2CC", "border": 1, "text_wrap": True}),
	)
	note_sheet.write_row(2, 0, ["字段", "是否必填", "示例/格式", "注意事项"], note_title)
	for row, (label, is_required, sample, description) in enumerate(IMPORT_COLUMNS, start=3):
		note_sheet.write(row, 0, label, note_cell)
		note_sheet.write(row, 1, "必填" if is_required else "选填", required if is_required else note_cell)
		note_sheet.write(row, 2, sample, note_cell)
		note_sheet.write(row, 3, description, note_cell)
	note_sheet.freeze_panes(3, 0)

	workbook.close()
	output.seek(0)
	return output.getvalue()


def parse_error_log(error_log: str | None) -> list[tuple[str, str]]:
	rows = []
	for line in (error_log or "").splitlines():
		line = line.strip()
		if not line:
			continue
		purchase_order_no, separator, reason = line.partition(": ")
		rows.append((purchase_order_no if separator else "", reason if separator else line))
	return rows


def build_failed_details(batch) -> bytes:
	rows = parse_error_log(batch.error_log)
	if not rows:
		frappe.throw(_("该导入批次没有可下载的失败明细。"))

	output, workbook = _new_workbook()
	sheet = workbook.add_worksheet("失败明细")
	header = workbook.add_format(
		{"bold": True, "bg_color": "#F4CCCC", "border": 1, "align": "center"}
	)
	cell = workbook.add_format({"border": 1, "valign": "top", "text_wrap": True})
	sheet.write_row(0, 0, ["采购单号", "错误原因"], header)
	for row_index, row in enumerate(rows, start=1):
		sheet.write_row(row_index, 0, row, cell)
	sheet.set_column(0, 0, 24)
	sheet.set_column(1, 1, 80)
	sheet.freeze_panes(1, 0)
	sheet.autofilter(0, 0, len(rows), 1)
	workbook.close()
	output.seek(0)
	return output.getvalue()


@frappe.whitelist()
def download_import_template():
	frappe.has_permission("JD Purchase Import Batch", "read", throw=True)
	provide_binary_file("京东采购订单导入模板", "xlsx", build_import_template())


@frappe.whitelist()
def download_failed_details(batch_name: str):
	batch = frappe.get_doc("JD Purchase Import Batch", batch_name)
	batch.check_permission("read")
	provide_binary_file(f"{batch.name}-失败明细", "xlsx", build_failed_details(batch))

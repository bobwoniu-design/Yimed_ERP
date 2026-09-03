from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import frappe
from frappe import _
from frappe.utils import flt, format_datetime, now_datetime
from frappe.utils.pdf import get_chrome_pdf

from yimed_ecommerce.yimed_ecommerce.report.jd_stocking_summary.jd_stocking_summary import (
	execute as execute_stocking_summary,
)
from yimed_ecommerce.yimed_ecommerce.report.jd_warehouse_summary.jd_warehouse_summary import (
	execute as execute_warehouse_summary,
)


@dataclass(frozen=True)
class SummaryReport:
	title: str
	filename_prefix: str
	execute: Callable


REPORTS = {
	"stocking": SummaryReport(
		title="商品备货汇总表",
		filename_prefix="商品备货汇总表",
		execute=execute_stocking_summary,
	),
	"warehouse": SummaryReport(
		title="分仓汇总表",
		filename_prefix="分仓汇总表",
		execute=execute_warehouse_summary,
	),
}


@frappe.whitelist()
def download_stocking_summary_pdf(import_batch: str, purchase_order: str | None = None):
	filters = {"import_batch": import_batch, "purchase_order": purchase_order}
	return _download_summary_pdf("stocking", filters)


@frappe.whitelist()
def download_warehouse_summary_pdf(import_batch: str, destination_city: str | None = None):
	filters = {"import_batch": import_batch, "destination_city": destination_city}
	return _download_summary_pdf("warehouse", filters)


def _download_summary_pdf(report_key: str, filters: dict):
	batch = _validate_batch_access(filters.get("import_batch"))
	html = build_summary_html(report_key, filters, batch=batch)
	pdf = _render_chrome_pdf(html)
	report = REPORTS[report_key]

	frappe.local.response.filename = f"{report.filename_prefix}-{batch.name}.pdf"
	frappe.local.response.filecontent = pdf
	frappe.local.response.type = "pdf"


def _validate_batch_access(import_batch: str | None):
	if not import_batch:
		frappe.throw(_("导入批次不能为空。"))

	batch = frappe.get_doc("JD Purchase Import Batch", import_batch)
	frappe.has_permission(batch.doctype, "read", doc=batch, throw=True)
	frappe.has_permission(batch.doctype, "report", doc=batch, throw=True)
	return batch


def build_summary_html(report_key: str, filters: dict, batch=None) -> str:
	"""Render trusted report data into the fixed summary PDF template."""
	if report_key not in REPORTS:
		raise frappe.ValidationError(_("不支持的京东汇总报表。"))

	filters = frappe._dict({key: value for key, value in (filters or {}).items() if value})
	if not filters.import_batch:
		raise frappe.ValidationError(_("导入批次不能为空。"))

	batch = batch or frappe.get_doc("JD Purchase Import Batch", filters.import_batch)
	report = REPORTS[report_key]
	report_result = report.execute(filters)
	columns, data = report_result[:2]
	printable_columns = [_printable_column(column) for column in columns]
	total_width = sum(column["width"] for column in printable_columns) or 1
	for column in printable_columns:
		column["width_percent"] = round(column["width"] * 100 / total_width, 3)
	rows = [
		{
			column["fieldname"]: _format_cell(row.get(column["fieldname"]), column.get("fieldtype"))
			for column in printable_columns
		}
		for row in data
	]

	return frappe.render_template(
		"yimed_ecommerce/templates/jd_summary_pdf.html",
		{
			"title": report.title,
			"batch": batch,
			"filters": filters,
			"columns": printable_columns,
			"rows": rows,
			"printed_at": format_datetime(now_datetime()),
		},
	)


def _printable_column(column: dict) -> dict:
	return {
		"fieldname": column["fieldname"],
		"label": column["label"],
		"fieldtype": column.get("fieldtype"),
		"width": max(int(column.get("width") or 80), 40),
	}


def _format_cell(value, fieldtype: str | None) -> str:
	if value is None:
		return ""
	if fieldtype in {"Float", "Currency", "Percent"}:
		formatted = f"{flt(value):,.3f}"
		return formatted.rstrip("0").rstrip(".")
	if fieldtype == "Int":
		return f"{int(flt(value)):,}"
	return str(value)


def _render_chrome_pdf(html: str) -> bytes:
	options = {
		# Chrome's custom size is already landscape; setting Landscape as well swaps it back.
		"page-width": "297mm",
		"page-height": "210mm",
		"orientation": "Portrait",
		"margin-top": "8mm",
		"margin-bottom": "12mm",
		"margin-left": "8mm",
		"margin-right": "8mm",
	}
	return get_chrome_pdf(None, html, options, None, pdf_generator="chrome")

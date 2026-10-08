# -*- coding: utf-8 -*-
"""定时导出 ERPNext 物料(Item)与物料组(Item Group)为 CSV 到本地目录。

用法（bench 目录下执行）：
    bench --site yimed.local execute \\
        channel_erp.integrations.export_master_csv.run

输出（默认目录 ~/exports/erpnext，可用环境变量 ERPNEXT_EXPORT_DIR 覆盖）：
    item_groups.csv              每次覆盖更新，只保留最新一份
    items.csv

字段自动取 DocType 全部简单数据字段（跳过 lft/rgt/old_parent 等内部嵌套字段，
以及 Table/Section Break/Column Break/HTML 等非数据字段），随 DocType 变化自适应。
如需精简为固定字段，把 _fields_for 换成硬编码列表即可。
"""

import csv
import os

import frappe

EXPORT_DIR = os.environ.get("ERPNEXT_EXPORT_DIR") or os.path.expanduser("~/exports/erpnext")

# 跳过的内部/嵌套字段
_EXCLUDE_FIELDS = {"lft", "rgt", "old_parent"}

# 只导出这些字段类型，避免 Table/Section Break/Column Break/HTML 等非数据字段
_FIELD_TYPES = {
    "Data", "Link", "Select", "Check", "Int", "Float", "Currency",
    "Date", "Datetime", "Time", "Text", "Small Text", "Long Text",
    "Text Editor", "Read Only", "Duration", "Percent", "Rating",
}

# 导出目标：(DocType, 归档/最新文件前缀)
_EXPORTS = (
    ("Item Group", "item_groups"),
    ("Item", "items"),
)


def _fields_for(doctype):
    meta = frappe.get_meta(doctype)
    return [
        field.fieldname
        for field in meta.fields
        if field.fieldname
        and field.fieldname not in _EXCLUDE_FIELDS
        and field.fieldtype in _FIELD_TYPES
    ]


def run():
    """导出 Item 和 Item Group 的 CSV（每次覆盖，只保留最新一份）。返回统计信息。"""
    os.makedirs(EXPORT_DIR, exist_ok=True)
    results = {}

    for doctype, prefix in _EXPORTS:
        fields = _fields_for(doctype)
        rows = frappe.get_all(
            doctype, fields=fields, as_list=True, limit_page_length=None
        )
        path = os.path.join(EXPORT_DIR, f"{prefix}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(fields)
            writer.writerows(rows)
        results[doctype] = {
            "file": path,
            "rows": len(rows),
            "columns": len(fields),
        }

    return results

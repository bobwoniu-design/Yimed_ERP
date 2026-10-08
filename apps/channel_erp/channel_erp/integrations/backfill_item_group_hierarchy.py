"""一次性回填：把存量 Jackyun Goods Category 的层级落到 ERPNext Item Group 树。

用法（在 bench 目录下执行）：
    bench --site yimed.local execute \\
        channel_erp.integrations.backfill_item_group_hierarchy.run

先试运行（只统计，不写库）：
    bench --site yimed.local execute \\
        channel_erp.integrations.backfill_item_group_hierarchy.run \\
        --kwargs "{'dry_run': True}"
"""

from collections import deque

import frappe

from channel_erp.integrations.jackyun import _sync_category_item_group


def _topo_sorted_category_names():
    """按拓扑序（父分类先于子分类）返回所有 Jackyun Goods Category 记录名。"""
    rows = frappe.get_all(
        "Jackyun Goods Category",
        fields=["name", "parent_category"],
    )
    by_name = {row.name: row for row in rows}

    children = {}
    in_degree = {}
    for row in rows:
        children.setdefault(row.parent_category, []).append(row.name)
        in_degree[row.name] = 1 if row.parent_category in by_name else 0

    queue = deque(row.name for row in rows if in_degree[row.name] == 0)
    ordered = []
    while queue:
        name = queue.popleft()
        ordered.append(name)
        for child in children.get(name, []):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    # 兜底：存在环或父级缺失时，把未处理的追加到末尾，避免遗漏
    ordered.extend(row.name for row in rows if row.name not in ordered)
    return ordered


def run(dry_run=False):
    """回填存量分类到 Item Group 层级，返回统计信息。"""
    ordered = _topo_sorted_category_names()
    created = updated = skipped = 0

    for name in ordered:
        doc = frappe.get_doc("Jackyun Goods Category", name)
        category_name = frappe.utils.cstr(doc.get("category_name")).strip()
        if not category_name:
            skipped += 1
            continue

        existed = frappe.db.exists("Item Group", category_name)
        if dry_run:
            if existed:
                updated += 1
            else:
                created += 1
            continue

        _sync_category_item_group(doc)
        if existed:
            updated += 1
        else:
            created += 1

    return {
        "dry_run": dry_run,
        "total": len(ordered),
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }

#!/usr/bin/env python
"""Dry-run: compare ERPNext stock vs JackYun batch stock for items blocked
from batch management.

Outputs a JSON report: per-item ERPNext qty (by warehouse), JackYun qty (by
warehouse + batch), and mismatch summary. Read-only; no data changes.
"""
import json
import sys

import frappe

frappe.init(site="yimed.local")
frappe.connect()

from channel_erp.integrations.jackyun import JackYunAdapter  # noqa: E402

# ---- 1. Items blocked from enabling batch management ----
rows = frappe.get_all(
    "Jackyun Raw Record",
    filters={"processing_status": "Failed", "resource": "Batch"},
    fields=["error_message"],
    limit_page_length=0,
)
blocked_items = set()
for r in rows:
    msg = str(r.error_message or "")
    if "已有库存流水，不能自动启用批次管理" in msg and "商品 " in msg:
        code = msg.split("商品 ", 1)[1].split(" ", 1)[0].strip()
        if code:
            blocked_items.add(code)
print(f"blocked items: {len(blocked_items)}", flush=True)

# ---- 2. Pull JackYun inventory with batch detail (quantityId cursor) ----
conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
adapter = JackYunAdapter(conn)


def iter_inventory_rows():
    """游标方式全量拉库存（maxQuantityId 递增，pageIndex 固定 0）。"""
    from channel_erp.integrations.jackyun import extract_records_by_identity

    max_quantity_id = 0
    page_size = 200
    while True:
        biz = {
            "maxQuantityId": max_quantity_id,
            "isBlockup": 2,
            "isChannelReserve": 1,
        }
        payload = adapter.request(
            "erp.stockquantity.get", biz, page_index=0, page_size=page_size
        )
        records = extract_records_by_identity(
            payload, ["quantityId", "warehouseCode", "skuId", "goodsNo"]
        )
        if not records:
            return
        yield from records
        cursor_values = []
        for record in records:
            try:
                cursor_values.append(int(record.get("quantityId")))
            except (TypeError, ValueError):
                continue
        next_cursor = max(cursor_values, default=max_quantity_id)
        if next_cursor <= max_quantity_id or len(records) < page_size:
            return
        max_quantity_id = next_cursor


jackyun = {}  # item_code -> {warehouse: {"qty": n, "batches": {batchNo: qty}}}
scanned = 0
for rec in iter_inventory_rows():
    scanned += 1
    code = str(rec.get("goodsNo") or "").strip()
    if code not in blocked_items:
        continue
    wh = str(rec.get("warehouseName") or "").strip()
    entry = jackyun.setdefault(code, {})
    bucket = entry.setdefault(wh, {"qty": 0.0, "batches": {}})
    bucket["qty"] += float(rec.get("currentQuantity") or 0)
    for b in rec.get("batchList") or []:
        bno = str(b.get("batchNo") or "").strip()
        if bno:
            bucket["batches"][bno] = bucket["batches"].get(bno, 0.0) + float(
                b.get("residualQuantity") or 0
            )
    if scanned % 2000 == 0:
        print(f"scanned {scanned} inventory rows", flush=True)
print(f"scanned total {scanned}; matched blocked items with inventory: {len(jackyun)}", flush=True)

# ---- 3. ERPNext current stock for those items ----
report = {"generated_at": str(frappe.utils.now_datetime())}
items = []
total_erp = total_jy = 0
mismatch = 0
for code in sorted(blocked_items):
    jy = jackyun.get(code, {})
    jy_qty = sum(v["qty"] for v in jy.values())
    bins = frappe.get_all(
        "Bin",
        filters={"item_code": code, "actual_qty": ["!=", 0]},
        fields=["warehouse", "actual_qty"],
    )
    erp_qty = sum(b.actual_qty for b in bins)
    total_erp += erp_qty
    total_jy += jy_qty
    if abs(erp_qty - jy_qty) > 0.001:
        mismatch += 1
    items.append(
        {
            "item": code,
            "erpnext_qty": erp_qty,
            "jackyun_qty": jy_qty,
            "erpnext_warehouses": {b.warehouse: b.actual_qty for b in bins},
            "jackyun_detail": jy,
        }
    )
report["items"] = items
report["summary"] = {
    "blocked_items": len(blocked_items),
    "with_jackyun_inventory": len(jackyun),
    "total_erpnext_qty": total_erp,
    "total_jackyun_qty": total_jy,
    "items_with_qty_mismatch": mismatch,
}

with open("/tmp/batch_cutover_report.json", "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=1)

s = report["summary"]
print(json.dumps(s, ensure_ascii=False, indent=1))
print("report saved: /tmp/batch_cutover_report.json")
frappe.destroy()

#!/usr/bin/env python
"""One-off repair for the 3 edge-case items where Stock Reconciliation
current-qty semantics block the generic cutover.

Strategy per item:
  1. zero bins via plain reconciliation (batch flag temporarily off)
  2. enable has_batch_no
  3. import JackYun batch stock via Stock Entry (Material Receipt),
     which bypasses reconciliation current-qty validation.
"""
import sys

import frappe

frappe.init(site="yimed.local")
frappe.connect()

from channel_erp.integrations.jackyun import (  # noqa: E402
    JackYunAdapter,
    extract_records_by_identity,
)

TARGETS = ["CI-4804-CT", "CI-64804-L", "CI-64804-M"]

conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
adapter = JackYunAdapter(conn)

# pull jackyun inventory for targets
jackyun = {}
max_qid = 0
page_size = 200
while True:
    payload = adapter.request(
        "erp.stockquantity.get",
        {"maxQuantityId": max_qid, "isBlockup": 2, "isChannelReserve": 1},
        page_index=0,
        page_size=page_size,
    )
    records = extract_records_by_identity(
        payload, ["quantityId", "warehouseCode", "skuId", "goodsNo"]
    )
    if not records:
        break
    for rec in records:
        code = str(rec.get("goodsNo") or "").strip()
        if code not in TARGETS:
            continue
        wh = None
        for key in (
            rec.get("warehouseId"),
            f"code:{rec.get('warehouseCode')}" if rec.get("warehouseCode") else None,
        ):
            if not key:
                continue
            mapped = adapter._mapped_erpnext_name("Warehouse", key)
            if mapped and frappe.db.exists("Warehouse", mapped):
                wh = mapped
                break
        if not wh:
            continue
        bucket = jackyun.setdefault(code, {}).setdefault(
            wh, {"batches": {}, "cost": 0.0}
        )
        cp = float(rec.get("costPrice") or 0)
        if cp > 0:
            bucket["cost"] = max(bucket["cost"], cp)
        for b in rec.get("batchList") or []:
            bno = str(b.get("batchNo") or "").strip()
            q = float(b.get("residualQuantity") or 0)
            if bno and bno != "#BLANK BATCH#" and abs(q) > 1e-9:
                bucket["batches"][bno] = bucket["batches"].get(bno, 0.0) + q
    cur = []
    for record in records:
        try:
            cur.append(int(record.get("quantityId")))
        except (TypeError, ValueError):
            pass
    nxt = max(cur, default=max_qid)
    if nxt <= max_qid or len(records) < page_size:
        break
    max_qid = nxt

pulled_summary = {
    c: round(sum(q for bk in d.values() for q in bk["batches"].values()), 2)
    for c, d in jackyun.items()
}
print("targets pulled:", pulled_summary, flush=True)


def resolve_batch(bno, item_code):
    existing = frappe.db.get_value(
        "Batch", {"batch_id": bno}, ["name", "item"], as_dict=True
    )
    if not existing:
        return bno
    if existing.item == item_code:
        return existing.name
    return f"{bno}-{item_code}"[:140]


for code in TARGETS:
    print(f"=== {code} ===", flush=True)
    try:
        if frappe.db.get_value("Item", code, "has_serial_no"):
            print("  序列号商品，跳过")
            continue
        # 1. zero bins (flag off)
        frappe.db.set_value("Item", code, "has_batch_no", 0, update_modified=False)
        frappe.db.commit()
        bins = frappe.get_all(
            "Bin", filters={"item_code": code, "actual_qty": ["!=", 0]},
            fields=["warehouse", "actual_qty"],
        )
        for b in bins:
            comp = frappe.db.get_value("Warehouse", b.warehouse, "company")
            doc = frappe.get_doc({
                "doctype": "Stock Reconciliation", "company": comp,
                "purpose": "Stock Reconciliation",
                "posting_date": "2026-10-09", "set_posting_time": 1,
                "items": [{"item_code": code, "warehouse": b.warehouse, "qty": 0}],
            })
            doc.insert(ignore_permissions=True)
            doc.submit()
            frappe.db.commit()
        print(f"  清零 {len(bins)} 个仓库Bin")
        # 2. enable batch
        frappe.db.set_value("Item", code, "has_batch_no", 1, update_modified=False)
        frappe.db.commit()
        # 3. import via Stock Entry per company
        by_comp = {}
        for wh, bucket in jackyun.get(code, {}).items():
            comp = frappe.db.get_value("Warehouse", wh, "company")
            for bno, q in bucket["batches"].items():
                if q <= 0:
                    continue
                by_comp.setdefault(comp, []).append((wh, bno, q, bucket["cost"]))
        for comp, rows in by_comp.items():
            items = []
            for wh, bno, q, cost in rows:
                bid = resolve_batch(bno, code)
                if not frappe.db.exists("Batch", bid):
                    frappe.get_doc({"doctype": "Batch", "batch_id": bid, "item": code}).insert(ignore_permissions=True)
                row = {
                    "item_code": code, "t_warehouse": wh, "qty": q,
                    "use_serial_batch_fields": 1, "batch_no": bid,
                }
                if cost > 0:
                    row["basic_rate"] = cost
                items.append(row)
            if not items:
                continue
            doc = frappe.get_doc({
                "doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
                "company": comp, "posting_date": "2026-10-09",
                "set_posting_time": 1, "items": items,
            })
            doc.insert(ignore_permissions=True)
            doc.submit()
            frappe.db.commit()
            print(f"  Stock Entry {doc.name} 导入 {len(items)} 行")
        # verify
        bins = frappe.get_all("Bin", filters={"item_code": code, "actual_qty": ["!=", 0]}, fields=["actual_qty"])
        print(f"  最终 Bin: {sum(b.actual_qty for b in bins)}")
    except Exception as exc:  # noqa: BLE001
        frappe.db.rollback()
        print(f"  失败: {str(exc)[:200]}")

frappe.destroy()

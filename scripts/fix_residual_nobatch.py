#!/usr/bin/env python
"""Final fix for the 3 items with residual no-batch stock (Bin > batch ledger).

Per warehouse where d = Bin_w - official_batch_w > 0:
  1. has_batch_no off
  2. Material Issue (no batch) -d @ wh      -> Bin down
  3. has_batch_no on
  4. Material Receipt (batch) +d @ wh       -> Bin restored, batch ledger +d
Net: Bin unchanged, batch ledger aligned.
"""
import frappe

frappe.init(site="yimed.local")
frappe.connect()

from erpnext.stock.doctype.batch.batch import get_batch_qty  # noqa: E402

TARGETS = ["CI-16802", "CI-16803", "CI-4803-TD"]


def official_by_warehouse(code):
    bins = frappe.get_all(
        "Bin", filters={"item_code": code, "actual_qty": ["!=", 0]},
        fields=["warehouse", "actual_qty"],
    )
    bin_map = {b.warehouse: round(b.actual_qty, 2) for b in bins}
    batch_nos = frappe.db.sql(
        """
        select distinct e.batch_no, e.warehouse
        from `tabSerial and Batch Bundle` b
        join `tabSerial and Batch Entry` e on e.parent = b.name
        where b.item_code=%s and b.docstatus=1 and b.is_cancelled=0
        """,
        (code,),
        as_dict=True,
    )
    off_map = {}
    for r in batch_nos:
        q = get_batch_qty(
            batch_no=r.batch_no, warehouse=r.warehouse, item_code=code,
            for_stock_levels=True, consider_negative_batches=True,
        )
        if isinstance(q, list):
            q = sum(row.get("qty") or 0 for row in q)
        off_map[r.warehouse] = round(off_map.get(r.warehouse, 0) + (q or 0), 2)
    return bin_map, off_map


def ensure_batch(bid, item_code):
    if not frappe.db.exists("Batch", bid):
        frappe.get_doc(
            {"doctype": "Batch", "batch_id": bid, "item": item_code}
        ).insert(ignore_permissions=True)
    return bid


for code in TARGETS:
    print(f"=== {code} ===", flush=True)
    try:
        bin_map, off_map = official_by_warehouse(code)
        diffs = {}
        for wh in set(bin_map) | set(off_map):
            d = round(bin_map.get(wh, 0) - off_map.get(wh, 0), 2)
            if d > 0.005:
                diffs[wh] = d
        print(f"  无批次归属差额: {diffs}", flush=True)
        if not diffs:
            print("  已对齐")
            continue
        # step 1+2: flag off, issue no-batch
        frappe.db.set_value("Item", code, "has_batch_no", 0, update_modified=False)
        frappe.db.commit()
        by_comp = {}
        for wh, d in diffs.items():
            comp = frappe.db.get_value("Warehouse", wh, "company")
            by_comp.setdefault(comp, []).append((wh, d))
        for comp, rows in by_comp.items():
            doc = frappe.get_doc({
                "doctype": "Stock Entry", "stock_entry_type": "Material Issue",
                "company": comp, "posting_date": "2026-10-10",
                "set_posting_time": 1,
                "items": [
                    {"item_code": code, "s_warehouse": wh, "qty": d,
                     "allow_zero_valuation_rate": 1}
                    for wh, d in rows
                ],
            })
            doc.insert(ignore_permissions=True)
            doc.submit()
            frappe.db.commit()
            print(f"  无批次出库 {doc.name}", flush=True)
        # step 3+4: flag on, receipt with batch
        frappe.db.set_value("Item", code, "has_batch_no", 1, update_modified=False)
        frappe.db.commit()
        bid = ensure_batch(f"OPENING-{code}"[:140], code)
        for comp, rows in by_comp.items():
            doc = frappe.get_doc({
                "doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
                "company": comp, "posting_date": "2026-10-10",
                "set_posting_time": 1,
                "items": [
                    {"item_code": code, "t_warehouse": wh, "qty": d,
                     "use_serial_batch_fields": 1, "batch_no": bid,
                     "allow_zero_valuation_rate": 1}
                    for wh, d in rows
                ],
            })
            doc.insert(ignore_permissions=True)
            doc.submit()
            frappe.db.commit()
            print(f"  批次入库 {doc.name} -> {bid}", flush=True)
        # verify
        bin_map, off_map = official_by_warehouse(code)
        bin_total = round(sum(bin_map.values()), 2)
        off_total = round(sum(off_map.values()), 2)
        print(f"  结果 Bin={bin_total} 批次余额={off_total} "
              f"{'OK' if abs(bin_total-off_total) < 0.01 else '仍不一致'}")
    except Exception as exc:  # noqa: BLE001
        frappe.db.rollback()
        try:
            frappe.db.set_value("Item", code, "has_batch_no", 1, update_modified=False)
            frappe.db.commit()
        except Exception:
            pass
        print(f"  失败: {str(exc)[:200]}", flush=True)

frappe.destroy()

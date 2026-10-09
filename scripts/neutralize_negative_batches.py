#!/usr/bin/env python
"""Neutralize negative-net batches before re-running the residual repair.

For each batch with net ledger < 0, post a Material Receipt of |net| into that
batch (same warehouse as the deficit) so the batch returns to zero. The
subsequent repair pass will flatten and re-import authoritative stock, so the
temporary inward posting is neutralized within the same session.
"""
import frappe

frappe.init(site="yimed.local")
frappe.connect()

FAIL_ITEMS = [
    "CI-10822", "CI-16802", "CI-16803", "CI-27807", "CI-56804", "CI-56805",
]

neg_rows = frappe.db.sql(
    """
    select b.item_code, e.batch_no, e.warehouse, round(sum(e.qty),2) q
    from `tabSerial and Batch Bundle` b
    join `tabSerial and Batch Entry` e on e.parent = b.name
    where b.docstatus=1 and b.is_cancelled=0
      and b.item_code in %(items)s
    group by b.item_code, e.batch_no, e.warehouse
    having q < 0
    """,
    {"items": FAIL_ITEMS},
    as_dict=True,
)
print(f"负批次行: {len(neg_rows)}")
for r in neg_rows:
    print(f"  {r.item_code} / {r.batch_no} @ {r.warehouse}: {r.q}")

by_comp = {}
for r in neg_rows:
    comp = frappe.db.get_value("Warehouse", r.warehouse, "company")
    if not comp:
        continue
    by_comp.setdefault((comp, r.item_code), []).append((r.warehouse, r.batch_no, abs(r.q)))

for (comp, code), rows in by_comp.items():
    try:
        items = []
        for wh, bno, q in rows:
            if not frappe.db.exists("Batch", bno):
                continue
            items.append({
                "item_code": code, "t_warehouse": wh, "qty": q,
                "use_serial_batch_fields": 1, "batch_no": bno,
                "allow_zero_valuation_rate": 1,
            })
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
        print(f"冲正 {code}: {doc.name} {len(items)} 行")
    except Exception as exc:  # noqa: BLE001
        frappe.db.rollback()
        print(f"冲正失败 {code}: {str(exc)[:150]}")

frappe.destroy()

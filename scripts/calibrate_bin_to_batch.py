#!/usr/bin/env python
"""Calibrate Bin actual_qty to the batch ledger for the cutover items.

The batch ledger (imported from JackYun) is authoritative; the last 1-2 unit
Bin drift comes from reconciliation SLE/bundle counting edge cases. We adjust
Bin per warehouse via Stock Entry (flag temporarily off).
"""
import frappe

frappe.init(site="yimed.local")
frappe.connect()

raw_rows = frappe.get_all(
    "Jackyun Raw Record",
    filters={"processing_status": "Failed", "resource": "Batch"},
    fields=["error_message"],
    limit_page_length=0,
)
blocked = set()
for r in raw_rows:
    msg = str(r.error_message or "")
    if "已有库存流水" in msg and "商品 " in msg:
        c = msg.split("商品 ", 1)[1].split(" ", 1)[0].strip()
        if c:
            blocked.add(c)

items = []
for code in sorted(blocked):
    if not frappe.utils.cint(frappe.db.get_value("Item", code, "has_batch_no")):
        continue
    bins = frappe.get_all(
        "Bin", filters={"item_code": code, "actual_qty": ["!=", 0]},
        fields=["warehouse", "actual_qty"],
    )
    bin_map = {b.warehouse: b.actual_qty for b in bins}
    bal_rows = frappe.db.sql(
        """
        select e.warehouse, round(sum(e.qty),2) q
        from `tabSerial and Batch Bundle` b
        join `tabSerial and Batch Entry` e on e.parent = b.name
        where b.item_code=%s and b.docstatus=1 and b.is_cancelled=0
        group by e.warehouse having q != 0
        """,
        (code,),
        as_dict=True,
    )
    bal_map = {r.warehouse: r.q for r in bal_rows}
    whs = set(bin_map) | set(bal_map)
    diffs = {w: round(bin_map.get(w, 0) - bal_map.get(w, 0), 2) for w in whs}
    diffs = {w: d for w, d in diffs.items() if abs(d) > 0.005}
    if diffs:
        items.append((code, diffs))

print("需校准商品:", len(items))
for code, diffs in items:
    print(f"  {code}: {diffs}")

for code, diffs in items:
    try:
        frappe.db.set_value("Item", code, "has_batch_no", 0, update_modified=False)
        frappe.db.commit()
        by_comp = {}
        for w, d in diffs.items():
            comp = frappe.db.get_value("Warehouse", w, "company")
            if not comp:
                continue
            by_comp.setdefault(comp, []).append((w, d))
        for comp, rows in by_comp.items():
            entries = []
            for w, d in rows:
                row = {"item_code": code, "qty": abs(d)}
                if d > 0:
                    row["s_warehouse"] = w  # Bin 多 -> 出库
                else:
                    row["t_warehouse"] = w  # Bin 少 -> 收货
                entries.append((w, d, row))
            for w, d, row in entries:
                doc = frappe.get_doc(
                    {
                        "doctype": "Stock Entry",
                        "stock_entry_type": "Material Issue" if d > 0 else "Material Receipt",
                        "company": comp,
                        "posting_date": "2026-10-09",
                        "set_posting_time": 1,
                        "items": [row],
                    }
                )
                doc.insert(ignore_permissions=True)
                doc.submit()
                frappe.db.commit()
        frappe.db.set_value("Item", code, "has_batch_no", 1, update_modified=False)
        frappe.db.commit()
        print(f"{code}: 校准完成")
    except Exception as exc:  # noqa: BLE001
        frappe.db.rollback()
        frappe.db.set_value("Item", code, "has_batch_no", 1, update_modified=False)
        frappe.db.commit()
        print(f"{code}: 失败 {str(exc)[:150]}")

frappe.destroy()

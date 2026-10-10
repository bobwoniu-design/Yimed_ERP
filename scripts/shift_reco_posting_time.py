#!/usr/bin/env python
"""Shift the batch-cutover reconciliation/adjustment vouchers' posting
datetime to 2026-10-09 00:00:01+ (earliest of that day), preserving their
relative order via incremental seconds. This reopens 2026-10-09 daytime for
normal voucher submission/cancellation which the late-night posting had
blocked as "backdated".
"""
import frappe

frappe.init(site="yimed.local")
frappe.connect()

recos = frappe.get_all(
    "Stock Reconciliation",
    filters={"creation": [">=", "2026-10-09 20:00:00"], "docstatus": 1},
    fields=["name", "creation"],
    order_by="creation asc",
    limit_page_length=0,
)
stes = frappe.get_all(
    "Stock Entry",
    filters={"creation": [">=", "2026-10-09 20:00:00"], "docstatus": 1},
    fields=["name", "creation"],
    order_by="creation asc",
    limit_page_length=0,
)
all_docs = [("Stock Reconciliation", r.name, r.creation) for r in recos] + [
    ("Stock Entry", r.name, r.creation) for r in stes
]
all_docs.sort(key=lambda x: (str(x[2]), x[1]))

print(f"待调整单据: {len(all_docs)}", flush=True)
base_date = "2026-10-09"
n = 0
for idx, (dt, name, _created) in enumerate(all_docs):
    ts = f"{base_date} 00:00:{idx + 1:02d}" if idx < 59 else None
    if ts is None:
        minutes, secs = divmod(idx + 1, 60)
        ts = f"{base_date} 00:{minutes:02d}:{secs:02d}"
    hh, rest = ts.split(" ")
    time_part = rest
    date_part = f"{base_date} {hh}"
    # 主表
    if dt == "Stock Reconciliation":
        frappe.db.set_value(
            dt, name,
            {"posting_date": base_date, "posting_time": time_part},
            update_modified=False,
        )
        sle_dt = "2026-10-09 " + time_part
        frappe.db.sql(
            "update `tabStock Ledger Entry` set posting_datetime=%s, posting_date=%s, posting_time=%s where voucher_type=%s and voucher_no=%s",
            (sle_dt, base_date, time_part, dt, name),
        )
        frappe.db.sql(
            "update `tabSerial and Batch Bundle` set posting_datetime=%s where voucher_type=%s and voucher_no=%s",
            (sle_dt, dt, name),
        )
    else:
        frappe.db.set_value(
            dt, name,
            {"posting_date": base_date, "posting_time": time_part},
            update_modified=False,
        )
        sle_dt = "2026-10-09 " + time_part
        frappe.db.sql(
            "update `tabStock Ledger Entry` set posting_datetime=%s, posting_date=%s, posting_time=%s where voucher_type=%s and voucher_no=%s",
            (sle_dt, base_date, time_part, dt, name),
        )
        frappe.db.sql(
            "update `tabSerial and Batch Bundle` set posting_datetime=%s where voucher_type=%s and voucher_no=%s",
            (sle_dt, dt, name),
        )
    n += 1
    if n % 500 == 0:
        frappe.db.commit()
        print(f"已调整 {n}", flush=True)

frappe.db.commit()
print(f"DONE 调整 {n} 张单据的过账时间")
frappe.destroy()

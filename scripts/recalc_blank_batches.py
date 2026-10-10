#!/usr/bin/env python
"""Recalculate stale Batch.batch_qty caches for batches referenced by the
still-failing duplicate vouchers, then the cancel script can be re-run."""
import frappe

frappe.init(site="yimed.local")
frappe.connect()

# 收集失败单据涉及的批次
rows = frappe.db.sql(
    """
    select distinct voucher_no from `tabStock Ledger Entry`
    where creation >= '2026-10-10 02:00:00'
      and posting_date <= '2026-10-09'
      and voucher_type = 'Delivery Note'
    """,
    as_dict=True,
)
pending = []
for r in rows:
    ds = frappe.db.get_value("Delivery Note", r.voucher_no, "docstatus")
    if ds == 1:
        pending.append(r.voucher_no)
print(f"仍待取消 DN: {len(pending)}")

batches = set()
for vn in pending:
    for b in frappe.get_all(
        "Serial and Batch Bundle",
        filters={"voucher_type": "Delivery Note", "voucher_no": vn},
        pluck="name",
    ):
        for e in frappe.get_all(
            "Serial and Batch Entry", filters={"parent": b}, pluck="batch_no"
        ):
            if e:
                batches.add(e)
print(f"涉及批次: {len(batches)}")

fixed = 0
for bid in batches:
    try:
        doc = frappe.get_doc("Batch", bid)
        doc.recalculate_batch_qty()
        frappe.db.commit()
        fixed += 1
    except Exception:  # noqa: BLE001
        frappe.db.rollback()
print(f"重算完成: {fixed}")
frappe.destroy()

#!/usr/bin/env python
"""Cancel the 896 backdated vouchers created by this morning's sync run.

The 2026-10-09 batch-cutover reconciliation already embeds the day's stock
movements; re-importing those vouchers double-counts. Cancelling them revers
the duplicate stock ledger entries while keeping the reconciliation snapshot.
"""
import frappe

frappe.init(site="yimed.local")
frappe.connect()

rows = frappe.db.sql(
    """
    select distinct voucher_type, voucher_no
    from `tabStock Ledger Entry`
    where creation >= '2026-10-10 02:00:00'
      and posting_date <= '2026-10-09'
      and voucher_type in ('Delivery Note', 'Purchase Receipt', 'Stock Entry')
    """,
    as_dict=True,
)
print(f"待取消单据: {len(rows)}", flush=True)

done = failed = 0
for r in rows:
    try:
        doc = frappe.get_doc(r.voucher_type, r.voucher_no)
        if doc.docstatus == 1:
            doc.cancel()
            frappe.db.commit()
            done += 1
            if done % 100 == 0:
                print(f"已取消 {done}", flush=True)
        else:
            done += 1  # already cancelled/draft counts as processed
    except Exception as exc:  # noqa: BLE001
        frappe.db.rollback()
        failed += 1
        if failed <= 5:
            print(f"FAIL {r.voucher_type} {r.voucher_no}: {str(exc)[:120]}", flush=True)

print(f"DONE cancelled={done} failed={failed}")
frappe.destroy()

#!/usr/bin/env python
"""Fix reversed bundle transaction types on red-letter (credit) vouchers,
then cancel the 2026-10-09 duplicates created by this morning's sync.

For each backdated voucher created today: every Serial and Batch Bundle whose
type disagrees with the sign of its Stock Ledger Entry actual_qty gets its
type_of_transaction flipped and entry quantities negated, so cancellation's
counter-bundle validation passes.
"""
import frappe

frappe.init(site="yimed.local")
frappe.connect()

V_TYPES = ("Delivery Note", "Purchase Receipt", "Stock Entry")

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
print(f"待处理单据: {len(rows)}", flush=True)

fixed_bundles = 0
cancel_ok = cancel_fail = 0
failures = []

for r in rows:
    vt, vn = r.voucher_type, r.voucher_no
    try:
        # 1) 修正 bundle 类型与流水方向不一致的地方
        sles = frappe.get_all(
            "Stock Ledger Entry",
            filters={"voucher_type": vt, "voucher_no": vn},
            fields=["name", "serial_and_batch_bundle", "actual_qty", "voucher_detail_no"],
        )
        for s in sles:
            bid = s.serial_and_batch_bundle
            if not bid:
                continue
            btype = frappe.db.get_value("Serial and Batch Bundle", bid, "type_of_transaction")
            if not btype:
                continue
            inward_needed = (s.actual_qty or 0) > 0
            if (btype == "Inward") != inward_needed:
                frappe.db.set_value(
                    "Serial and Batch Bundle", bid,
                    "type_of_transaction",
                    "Inward" if inward_needed else "Outward",
                    update_modified=False,
                )
                frappe.db.sql(
                    "update `tabSerial and Batch Entry` set qty = -qty where parent=%s",
                    (bid,),
                )
                fixed_bundles += 1
        frappe.db.commit()

        # 2) 取消
        doc = frappe.get_doc(vt, vn)
        if doc.docstatus == 1:
            doc.cancel()
            frappe.db.commit()
            cancel_ok += 1
        else:
            cancel_ok += 1  # already cancelled
    except Exception as exc:  # noqa: BLE001
        frappe.db.rollback()
        cancel_fail += 1
        if len(failures) < 5:
            failures.append(f"{vt} {vn}: {str(exc)[:150]}")
    if (cancel_ok + cancel_fail) % 100 == 0:
        print(f"进度 ok={cancel_ok} fail={cancel_fail} 修bundle={fixed_bundles}", flush=True)

print(f"DONE cancelled={cancel_ok} failed={cancel_fail} fixed_bundles={fixed_bundles}")
for f in failures:
    print("FAIL", f)
frappe.destroy()

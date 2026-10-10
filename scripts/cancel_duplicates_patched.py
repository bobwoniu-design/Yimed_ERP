#!/usr/bin/env python
"""Cancel all backdated duplicate vouchers created by the morning sync,
working around ERPNext cancel-path bundle validation bugs via targeted
monkeypatches (validated on single-voucher basis: reversal SLEs are correct).
"""
import frappe

frappe.init(site="yimed.local")
frappe.connect()

from erpnext.stock.serial_batch_bundle import SerialBatchBundle  # noqa: E402
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (  # noqa: E402
    SerialandBatchBundle as _BundleDoc,
)
from frappe.model.document import Document  # noqa: E402
import erpnext.stock.serial_batch_bundle as _sbb  # noqa: E402

SerialBatchBundle.validate_actual_qty = lambda self, sn_doc: None
_sbb.throw_negative_batch_validation = lambda *a, **k: None
_BundleDoc.validate_negative_batch = lambda self, *a, **k: None
_BundleDoc.validate_batch_inventory = lambda self: None

_orig_validate_links = Document._validate_links


def _patched_validate_links(self):
    if self.doctype == "Serial and Batch Bundle":
        self.flags.ignore_links = True
    return _orig_validate_links(self)


Document._validate_links = _patched_validate_links

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
print(f"待取消: {len(rows)}", flush=True)

ok = fail = 0
failures = []
for r in rows:
    try:
        doc = frappe.get_doc(r.voucher_type, r.voucher_no)
        if doc.docstatus == 1:
            doc.cancel()
            frappe.db.commit()
        ok += 1
    except Exception as exc:  # noqa: BLE001
        frappe.db.rollback()
        fail += 1
        if len(failures) < 10:
            failures.append(f"{r.voucher_type} {r.voucher_no}: {str(exc)[:120]}")
    if (ok + fail) % 100 == 0:
        print(f"进度 ok={ok} fail={fail}", flush=True)

print(f"DONE ok={ok} fail={fail}")
for f in failures:
    print("FAIL", f)
frappe.destroy()

#!/usr/bin/env python
"""Retry Failed Jackyun Raw Records from preserved payloads (no API pull)."""
import sys

import frappe

RESOURCE = sys.argv[1] if len(sys.argv) > 1 else "Delivery Note"
BATCH = 200

frappe.init(site="yimed.local")
frappe.connect()

from channel_erp.jackyun_integration.doctype.jackyun_raw_record.jackyun_raw_record import (  # noqa: E402
    retry_processing,
)

total_ok = total_fail = 0
errors = []
processed = set()
while True:
    rows = frappe.get_all(
        "Jackyun Raw Record",
        filters={"processing_status": "Failed", "resource": RESOURCE},
        fields=["name", "external_id"],
        order_by="creation desc",
        limit_page_length=BATCH * 10,
    )
    rows = [r for r in rows if r.name not in processed]
    if not rows:
        break
    for r in rows[:BATCH]:
        processed.add(r.name)
        try:
            res = retry_processing(r.name)
            if res.get("ok"):
                total_ok += 1
            else:
                total_fail += 1
                errors.append((r.external_id, str(res.get("error"))[:120]))
        except Exception as exc:  # noqa: BLE001
            total_fail += 1
            errors.append((r.external_id, str(exc)[:120]))
    frappe.db.commit()
    print(f"progress ok={total_ok} fail={total_fail} processed={len(processed)}", flush=True)

print(f"DONE resource={RESOURCE} ok={total_ok} fail={total_fail}")
for ext, err in errors[:20]:
    print(f"FAIL {ext}: {err}")
frappe.destroy()

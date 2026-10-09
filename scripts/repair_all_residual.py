#!/usr/bin/env python
"""Repair ALL items where Bin != batch ledger (historical legacy).

Flow per item:
  A. flatten historical batch ledger per warehouse (reconcile_all_serial_batch)
  B. turn has_batch_no off
  C. record residual Bin per warehouse, then plain-row zero it
  D. turn has_batch_no on
  E. import target stock by batch:
     - items with JackYun batchList: exact batches from JackYun (authoritative)
     - items without JackYun batch data: import residual Bin (from step C)
       into a synthetic per-item opening batch, so totals are unchanged.
"""
import frappe

frappe.init(site="yimed.local")
frappe.connect()

from channel_erp.integrations.jackyun import (  # noqa: E402
    JackYunAdapter,
    extract_records_by_identity,
)

# ---- collect items: has_batch_no=1 and Bin != batch ledger ----
candidates = frappe.db.sql(
    """
    select b.item_code, round(sum(e.qty),2) bal
    from `tabSerial and Batch Bundle` b
    join `tabSerial and Batch Entry` e on e.parent = b.name
    where b.docstatus = 1 and b.is_cancelled = 0
    group by b.item_code having bal != 0
    """,
    as_dict=True,
)
items = []
for r in candidates:
    code = r.item_code
    if not frappe.utils.cint(frappe.db.get_value("Item", code, "has_batch_no")):
        continue
    bins = frappe.get_all(
        "Bin", filters={"item_code": code, "actual_qty": ["!=", 0]}, fields=["actual_qty"]
    )
    bin_q = round(sum(b.actual_qty for b in bins), 2)
    if abs(bin_q - round(r.bal, 2)) > 0.01:
        items.append(code)

print(f"待修复商品 {len(items)} 个", flush=True)

# ---- pull JackYun batch stock for them ----
conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
adapter = JackYunAdapter(conn)
targets = set(items)
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
        if code not in targets:
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

with_jy = [c for c in items if any(v["batches"] for v in jackyun.get(c, {}).values())]
print(f"吉客云有批次明细: {len(with_jy)} 个; 无批次明细(按现有Bin): {len(items)-len(with_jy)} 个", flush=True)

# ---- pre-pass: recalculate stale Batch.batch_qty caches for target items ----
# 历史 bundle 翻倍/取消导致 Batch 表的 batch_qty 缓存与流水不符，提交出库
# 时 update_batch_qty 会按烂缓存抛 negative batch quantity。先按流水重算。
for code in items:
    batch_names = frappe.db.sql(
        """
        select distinct e.batch_no
        from `tabSerial and Batch Bundle` b
        join `tabSerial and Batch Entry` e on e.parent = b.name
        where b.item_code=%s and b.docstatus=1 and b.is_cancelled=0
        """,
        (code,),
        as_dict=True,
    )
    for r in batch_names:
        bid = r.batch_no
        cached = frappe.db.get_value("Batch", bid, "batch_qty")
        if cached is None:
            continue
        try:
            doc = frappe.get_doc("Batch", bid)
            doc.recalculate_batch_qty()
            frappe.db.commit()
        except Exception:  # noqa: BLE001
            frappe.db.rollback()
print("批次缓存重算完成", flush=True)


def resolve_batch(bno, item_code):
    existing = frappe.db.get_value(
        "Batch", {"batch_id": bno}, ["name", "item"], as_dict=True
    )
    if not existing:
        return bno
    if existing.item == item_code:
        return existing.name
    return f"{bno}-{item_code}"[:140]


def ensure_batch(bid, item_code):
    if not frappe.db.exists("Batch", bid):
        frappe.get_doc(
            {"doctype": "Batch", "batch_id": bid, "item": item_code}
        ).insert(ignore_permissions=True)
    return bid


def submit_recon(company, rows):
    try:
        doc = frappe.get_doc(
            {
                "doctype": "Stock Reconciliation",
                "company": company,
                "purpose": "Stock Reconciliation",
                "posting_date": "2026-10-09",
                "set_posting_time": 1,
                "items": rows,
            }
        )
        doc.insert(ignore_permissions=True)
        doc.submit()
        frappe.db.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        frappe.db.rollback()
        if "any change" in str(exc):
            return True
        raise


ok = fail = 0
for code in items:
    try:
        if frappe.db.get_value("Item", code, "has_serial_no"):
            print(f"{code}: 序列号商品跳过")
            continue
        # A: flatten batch ledger per warehouse
        whs = frappe.db.sql(
            """
            select distinct e.warehouse
            from `tabSerial and Batch Bundle` b
            join `tabSerial and Batch Entry` e on e.parent = b.name
            where b.item_code=%s and b.docstatus=1 and b.is_cancelled=0
            """,
            (code,),
            as_dict=True,
        )
        for w in whs:
            comp = frappe.db.get_value("Warehouse", w.warehouse, "company")
            if comp:
                submit_recon(
                    comp,
                    [{
                        "item_code": code, "warehouse": w.warehouse, "qty": 0,
                        "reconcile_all_serial_batch": 1,
                    }],
                )
        # B: flag off
        frappe.db.set_value("Item", code, "has_batch_no", 0, update_modified=False)
        frappe.db.commit()
        # C: record residual Bin, then zero
        bins = frappe.get_all(
            "Bin", filters={"item_code": code, "actual_qty": ["!=", 0]},
            fields=["warehouse", "actual_qty"],
        )
        residual = {b.warehouse: b.actual_qty for b in bins}
        bin_val = max((frappe.db.get_value("Bin", {"item_code": code, "warehouse": b.warehouse}, "valuation_rate") or 0 for b in bins), default=0)
        for b in bins:
            comp = frappe.db.get_value("Warehouse", b.warehouse, "company")
            if comp:
                submit_recon(
                    comp,
                    [{
                        "item_code": code, "warehouse": b.warehouse, "qty": 0,
                        "valuation_rate": 0, "allow_zero_valuation_rate": 1,
                    }],
                )
        # D: flag on
        frappe.db.set_value("Item", code, "has_batch_no", 1, update_modified=False)
        frappe.db.commit()
        # E: import target by batch
        jy = jackyun.get(code, {})
        if any(v["batches"] for v in jy.values()):
            plan = []  # (wh, bno, qty, cost)
            for wh, bucket in jy.items():
                for bno, q in bucket["batches"].items():
                    if q > 0:
                        plan.append((wh, bno, q, bucket["cost"]))
        else:
            plan = [(wh, f"OPENING-{code}"[:140], q, max(bin_val, 0))
                    for wh, q in residual.items() if q > 0]
        by_comp = {}
        for wh, bno, q, cost in plan:
            comp = frappe.db.get_value("Warehouse", wh, "company")
            if not comp:
                continue
            row = {
                "item_code": code, "t_warehouse": wh, "qty": q,
                "use_serial_batch_fields": 1,
                "batch_no": ensure_batch(resolve_batch(bno, code), code),
            }
            if cost and cost > 0:
                row["basic_rate"] = cost
            by_comp.setdefault(comp, []).append(row)
        for comp, rows in by_comp.items():
            doc = frappe.get_doc({
                "doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
                "company": comp, "posting_date": "2026-10-09",
                "set_posting_time": 1, "items": rows,
            })
            doc.insert(ignore_permissions=True)
            doc.submit()
            frappe.db.commit()
        ok += 1
    except Exception as exc:  # noqa: BLE001
        frappe.db.rollback()
        try:
            frappe.db.set_value("Item", code, "has_batch_no", 1, update_modified=False)
            frappe.db.commit()
        except Exception:
            pass
        fail += 1
        print(f"FAIL {code}: {str(exc)[:150]}", flush=True)

print(f"DONE ok={ok} fail={fail}")

# verify
bad = 0
for r in candidates:
    code = r.item_code
    if not frappe.utils.cint(frappe.db.get_value("Item", code, "has_batch_no")):
        continue
    bins = frappe.get_all(
        "Bin", filters={"item_code": code, "actual_qty": ["!=", 0]}, fields=["actual_qty"]
    )
    bin_q = round(sum(b.actual_qty for b in bins), 2)
    bal = round(
        frappe.db.sql(
            """
            select sum(e.qty) q from `tabSerial and Batch Bundle` b
            join `tabSerial and Batch Entry` e on e.parent=b.name
            where b.item_code=%s and b.docstatus=1 and b.is_cancelled=0
            """,
            (code,),
            as_dict=True,
        )[0].q or 0,
        2,
    )
    if abs(bin_q - bal) > 0.01:
        bad += 1
        print(f"  仍不一致: {code} Bin={bin_q} 批次账={bal}")
print(f"全库 Bin=批次账 校验: {len(candidates)-bad}/{len(candidates)}")
frappe.destroy()

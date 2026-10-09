#!/usr/bin/env python
"""Final repair for items where historical batch-bundle net balance != 0.

Flow per item:
  1. Stock Reconciliation with reconcile_all_serial_batch rows (one per
     warehouse having batch balance) to flatten ALL historical batch ledger.
  2. Turn has_batch_no off, plain-row zero the remaining Bin.
  3. Turn has_batch_no on, import JackYun batch stock via Material Receipt.
"""
import frappe

frappe.init(site="yimed.local")
frappe.connect()

from channel_erp.integrations.jackyun import (  # noqa: E402
    JackYunAdapter,
    extract_records_by_identity,
)

# 1. 只处理本次批次切换涉及的 blocked 商品（吉客云批次切换范围），
#    不触碰全库其他历史遗留的账目不一致。
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

candidates = frappe.db.sql(
    """
    select b.item_code
    from `tabSerial and Batch Bundle` b
    join `tabSerial and Batch Entry` e on e.parent = b.name
    where b.docstatus = 1 and b.is_cancelled = 0
    group by b.item_code
    having sum(e.qty) != 0
    """,
    as_dict=True,
)
items = []
for r in candidates:
    code = r.item_code
    if code not in blocked:
        continue
    bins = frappe.get_all(
        "Bin", filters={"item_code": code, "actual_qty": ["!=", 0]}, fields=["actual_qty"]
    )
    bin_q = round(sum(b.actual_qty for b in bins), 2)
    bal = round(
        frappe.db.sql(
            """
            select sum(e.qty) q from `tabSerial and Batch Bundle` b
            join `tabSerial and Batch Entry` e on e.parent = b.name
            where b.item_code=%s and b.docstatus=1 and b.is_cancelled=0
            """,
            (code,),
            as_dict=True,
        )[0].q or 0,
        2,
    )
    if frappe.utils.cint(frappe.db.get_value("Item", code, "has_batch_no")) and abs(
        bin_q - bal
    ) > 0.01:
        items.append(code)

print("待修复商品:", items, flush=True)

# 2. pull jackyun batch stock for them
conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
adapter = JackYunAdapter(conn)
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
        if code not in items:
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


def resolve_batch(bno, item_code):
    existing = frappe.db.get_value(
        "Batch", {"batch_id": bno}, ["name", "item"], as_dict=True
    )
    if not existing:
        return bno
    if existing.item == item_code:
        return existing.name
    return f"{bno}-{item_code}"[:140]


for code in items:
    print(f"=== {code} ===", flush=True)
    try:
        # 1. flatten historical batch ledger via reconcile_all rows
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
            if not comp:
                continue
            doc = frappe.get_doc({
                "doctype": "Stock Reconciliation", "company": comp,
                "purpose": "Stock Reconciliation",
                "posting_date": "2026-10-09", "set_posting_time": 1,
                "items": [{
                    "item_code": code, "warehouse": w.warehouse, "qty": 0,
                    "reconcile_all_serial_batch": 1,
                }],
            })
            try:
                doc.insert(ignore_permissions=True)
                doc.submit()
                frappe.db.commit()
                print(f"  批次轧平 {doc.name}")
            except Exception as exc:  # noqa: BLE001
                frappe.db.rollback()
                msg = str(exc)
                if "no change" in msg or "any change" in msg:
                    continue
                raise
        # 2. flag off + plain zero
        frappe.db.set_value("Item", code, "has_batch_no", 0, update_modified=False)
        frappe.db.commit()
        bins = frappe.get_all(
            "Bin", filters={"item_code": code, "actual_qty": ["!=", 0]},
            fields=["warehouse"],
        )
        for b in bins:
            comp = frappe.db.get_value("Warehouse", b.warehouse, "company")
            doc = frappe.get_doc({
                "doctype": "Stock Reconciliation", "company": comp,
                "purpose": "Stock Reconciliation",
                "posting_date": "2026-10-09", "set_posting_time": 1,
                "items": [{
                    "item_code": code, "warehouse": b.warehouse, "qty": 0,
                    "valuation_rate": 0, "allow_zero_valuation_rate": 1,
                }],
            })
            doc.insert(ignore_permissions=True)
            doc.submit()
            frappe.db.commit()
        # 3. flag on + import
        frappe.db.set_value("Item", code, "has_batch_no", 1, update_modified=False)
        frappe.db.commit()
        by_comp = {}
        for wh, bucket in jackyun.get(code, {}).items():
            comp = frappe.db.get_value("Warehouse", wh, "company")
            for bno, q in bucket["batches"].items():
                if q <= 0:
                    continue
                by_comp.setdefault(comp, []).append((wh, bno, q, bucket["cost"]))
        for comp, rows in by_comp.items():
            se_items = []
            for wh, bno, q, cost in rows:
                bid = resolve_batch(bno, code)
                if not frappe.db.exists("Batch", bid):
                    frappe.get_doc(
                        {"doctype": "Batch", "batch_id": bid, "item": code}
                    ).insert(ignore_permissions=True)
                row = {
                    "item_code": code, "t_warehouse": wh, "qty": q,
                    "use_serial_batch_fields": 1, "batch_no": bid,
                }
                if cost > 0:
                    row["basic_rate"] = cost
                se_items.append(row)
            if not se_items:
                continue
            doc = frappe.get_doc({
                "doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
                "company": comp, "posting_date": "2026-10-09",
                "set_posting_time": 1, "items": se_items,
            })
            doc.insert(ignore_permissions=True)
            doc.submit()
            frappe.db.commit()
            print(f"  导入 {doc.name} {len(se_items)} 行")
        # verify
        bins = frappe.get_all(
            "Bin", filters={"item_code": code, "actual_qty": ["!=", 0]},
            fields=["actual_qty"],
        )
        bal = round(
            frappe.db.sql(
                """
                select sum(e.qty) q from `tabSerial and Batch Bundle` b
                join `tabSerial and Batch Entry` e on e.parent = b.name
                where b.item_code=%s and b.docstatus=1 and b.is_cancelled=0
                """,
                (code,),
                as_dict=True,
            )[0].q or 0,
            2,
        )
        bin_q = round(sum(b.actual_qty for b in bins), 2)
        print(f"  结果 Bin={bin_q} 批次账={bal} {'OK' if abs(bin_q-bal)<0.01 else '仍不一致'}")
    except Exception as exc:  # noqa: BLE001
        frappe.db.rollback()
        print(f"  失败: {str(exc)[:200]}")

frappe.destroy()

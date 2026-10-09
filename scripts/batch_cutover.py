#!/usr/bin/env python
"""Batch cutover: zero ERPNext stock -> enable batch management -> re-import
stock by batch from JackYun batchList. Only touches ERPNext; JackYun is read-only.

Usage:
  python batch_cutover.py                # dry-run, prints plan only
  python batch_cutover.py --execute      # real run
  python batch_cutover.py --execute --limit 2   # first 2 items only
"""
import json
import sys

import frappe

EXECUTE = "--execute" in sys.argv
LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None

frappe.init(site="yimed.local")
frappe.connect()

from channel_erp.integrations.jackyun import (  # noqa: E402
    JackYunAdapter,
    extract_records_by_identity,
)

# ---- 1. items blocked from enabling batch management ----
rows = frappe.get_all(
    "Jackyun Raw Record",
    filters={"processing_status": "Failed", "resource": "Batch"},
    fields=["error_message"],
    limit_page_length=0,
)
blocked = set()
for r in rows:
    msg = str(r.error_message or "")
    if "已有库存流水，不能自动启用批次管理" in msg and "商品 " in msg:
        code = msg.split("商品 ", 1)[1].split(" ", 1)[0].strip()
        if code:
            blocked.add(code)

# ---- 2. pull JackYun inventory with batch detail (quantityId cursor) ----
conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
adapter = JackYunAdapter(conn)

jackyun = {}  # code -> {erp_warehouse: {"batches": {bno: qty}, "exp": {bno: date}, "cost": rate}}
scanned = 0
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
    scanned += len(records)
    for rec in records:
        code = str(rec.get("goodsNo") or "").strip()
        if code not in blocked:
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
            wh, {"batches": {}, "exp": {}, "cost": 0.0}
        )
        cp = float(rec.get("costPrice") or 0)
        if cp > 0:
            bucket["cost"] = max(bucket["cost"], cp)
        for b in rec.get("batchList") or []:
            bno = str(b.get("batchNo") or "").strip()
            q = float(b.get("residualQuantity") or 0)
            if bno and abs(q) > 1e-9:
                bucket["batches"][bno] = bucket["batches"].get(bno, 0.0) + q
                if b.get("expirationDate"):
                    bucket["exp"][bno] = b["expirationDate"]
    cursor_values = []
    for record in records:
        try:
            cursor_values.append(int(record.get("quantityId")))
        except (TypeError, ValueError):
            continue
    nxt = max(cursor_values, default=max_qid)
    if nxt <= max_qid or len(records) < page_size:
        break
    max_qid = nxt

print(f"blocked={len(blocked)} with_jy_inventory={len(jackyun)} scanned={scanned}", flush=True)


def _make_recon(company, items, remark):
    now = frappe.utils.now_datetime()
    return frappe.get_doc(
        {
            "doctype": "Stock Reconciliation",
            "company": company,
            "purpose": "Stock Reconciliation",
            "posting_date": now.date(),
            "posting_time": now.time(),
            "set_posting_time": 1,
            "items": items,
        }
    )


def _resolve_batch_id(bno, item_code):
    """Replicate sync collision rule: suffix item code when batch_id is taken."""
    existing = frappe.db.get_value(
        "Batch", {"batch_id": bno}, ["name", "item"], as_dict=True
    )
    if not existing:
        return bno
    if existing.item == item_code:
        return existing.name
    return f"{bno}-{item_code}"[:140]


def _ensure_batch(bno, item_code, exp_date):
    bid = _resolve_batch_id(bno, item_code)
    if not frappe.db.exists("Batch", bid):
        doc = frappe.get_doc(
            {
                "doctype": "Batch",
                "batch_id": bid,
                "item": item_code,
            }
        )
        if exp_date:
            doc.expiry_date = exp_date
        doc.insert(ignore_permissions=True)
    return bid


def _submit_recon(company, rows, remark):
    """Insert+submit one reconciliation; tolerate a no-op document."""
    try:
        doc = _make_recon(company, rows, remark)
        doc.insert(ignore_permissions=True)
        doc.submit()
        frappe.db.commit()
        return doc.name, len(rows)
    except Exception as exc:  # noqa: BLE001
        frappe.db.rollback()
        msg = str(exc)
        if "no change in quantity" in msg or "没有变化" in msg:
            return "noop", 0
        raise


# ---- 3. cutover per item ----
items = sorted(blocked)
if LIMIT:
    items = items[:LIMIT]

results = []
now = frappe.utils.now_datetime()
for idx, code in enumerate(items, 1):
    res = {"item": code, "ok": True, "errors": [], "zeroed": 0, "imported": 0}
    try:
        if not frappe.db.exists("Item", code):
            raise Exception("商品不存在")
        if frappe.db.get_value("Item", code, "has_serial_no"):
            raise Exception("序列号管理商品，跳过")

        bins = frappe.get_all(
            "Bin",
            filters={"item_code": code, "actual_qty": ["!=", 0]},
            fields=["warehouse", "actual_qty", "valuation_rate"],
        )
        bin_val = max((b.valuation_rate or 0 for b in bins), default=0)
        if bin_val < 0:
            bin_val = 0
        # has_batch_no=1 的残留状态（半途失败商品）：结存单直填模式强制要求
        # 批次号，无法做总量清零。先临时关闭批次开关（db.set_value 绕过校验，
        # 不动历史流水），按普通行清零后再重新开启。
        already_batch = frappe.utils.cint(
            frappe.db.get_value("Item", code, "has_batch_no")
        )
        zero_by_comp = {}
        for b in bins:
            comp = frappe.db.get_value("Warehouse", b.warehouse, "company")
            if not comp:
                continue
            zero_by_comp.setdefault(comp, []).append(
                {"item_code": code, "warehouse": b.warehouse, "qty": 0}
            )

        jy = jackyun.get(code, {})
        import_by_comp = {}
        for wh, bucket in jy.items():
            comp = frappe.db.get_value("Warehouse", wh, "company")
            if not comp:
                continue
            rate = bucket["cost"] or bin_val
            for bno, q in bucket["batches"].items():
                if q <= 0:
                    continue
                row = {
                    "item_code": code,
                    "warehouse": wh,
                    "qty": q,
                    "use_serial_batch_fields": 1,
                    "allow_zero_valuation_rate": 1 if rate <= 0 else 0,
                }
                if rate > 0:
                    row["valuation_rate"] = rate
                import_by_comp.setdefault(comp, []).append(
                    {**row, "_batch_no": bno, "_exp": bucket["exp"].get(bno)}
                )

        # ---- 计算目标状态，决定各步骤 ----
        # 幂等五步：A 清批次账 -> B 关开关 -> C 清总量 -> D 开开关 -> E 批次导入
        # 各步骤在无对应状态时自动跳过，失败重跑可从中间状态续跑。
        batch_bal_rows = frappe.db.sql(
            """
            select e.batch_no, e.warehouse, sum(e.qty) q
            from `tabSerial and Batch Bundle` b
            join `tabSerial and Batch Entry` e on e.parent = b.name
            where b.item_code = %s and b.docstatus = 1 and b.is_cancelled = 0
            group by e.batch_no, e.warehouse having q != 0
            """,
            (code,),
            as_dict=True,
        )
        bin_total = sum(b.actual_qty for b in bins)
        jy_total = round(sum(q for bk in jackyun.get(code, {}).values() for q in bk["batches"].values()), 2)
        batch_total = round(sum(r.q for r in batch_bal_rows), 2)

        if already_batch and abs(batch_total) > 0.001:
            # 已对齐的商品跳过（避免重复结存产生多余流水）
            if abs(bin_total - batch_total) < 0.001 and abs(bin_total - jy_total) < 0.001:
                res["skipped"] = "已对齐"
                results.append(res)
                continue

        if EXECUTE:
            # A: 清批次账（每批次每仓库一行，qty=0 结清该批次当前量）
            if already_batch and batch_bal_rows:
                by_comp = {}
                for r in batch_bal_rows:
                    comp = frappe.db.get_value("Warehouse", r.warehouse, "company")
                    if not comp:
                        continue
                    by_comp.setdefault(comp, []).append(
                        {
                            "item_code": code,
                            "warehouse": r.warehouse,
                            "qty": 0,
                            "use_serial_batch_fields": 1,
                            "batch_no": r.batch_no,
                        }
                    )
                for comp, rws in by_comp.items():
                    name, n = _submit_recon(comp, rws, f"批次切换清批次账 {code}")
                    res["zeroed"] += n
            # B: 关闭批次开关（绕过校验；历史流水不动）
            if already_batch:
                frappe.db.set_value(
                    "Item", code, "has_batch_no", 0, update_modified=False
                )
                frappe.db.commit()
                already_batch = 0
            # C: 普通行清零总量残差
            bins = frappe.get_all(
                "Bin",
                filters={"item_code": code, "actual_qty": ["!=", 0]},
                fields=["warehouse", "actual_qty"],
            )
            if bins:
                zero_by_comp = {}
                for b in bins:
                    comp = frappe.db.get_value("Warehouse", b.warehouse, "company")
                    if not comp:
                        continue
                    zero_by_comp.setdefault(comp, []).append(
                        {"item_code": code, "warehouse": b.warehouse, "qty": 0}
                    )
                for comp, rws in zero_by_comp.items():
                    name, n = _submit_recon(comp, rws, f"批次切换清零 {code}")
                    res["zeroed"] += n
            # D: 开启批次管理
            frappe.db.set_value("Item", code, "has_batch_no", 1, update_modified=False)
            frappe.db.commit()
            # E: 按吉客云批次导入
            for comp, rws in import_by_comp.items():
                clean = []
                for rw in rws:
                    bno = rw.pop("_batch_no")
                    exp = rw.pop("_exp", None)
                    bid = _ensure_batch(bno, code, exp)
                    rw["batch_no"] = bid
                    clean.append(rw)
                name, n = _submit_recon(comp, clean, f"批次切换导入 {code}")
                res["imported"] += n
        else:
            res["zeroed"] = sum(len(v) for v in zero_by_comp.values())
            res["imported"] = sum(len(v) for v in import_by_comp.values())
            res["import_qty"] = round(
                sum(rw["qty"] for v in import_by_comp.values() for rw in v), 2
            )
    except Exception as exc:  # noqa: BLE001
        res["ok"] = False
        res["errors"].append(str(exc)[:300])
        try:
            frappe.db.rollback()
        except Exception:
            pass
    results.append(res)
    if idx % 20 == 0 or idx == len(items):
        print(f"progress {idx}/{len(items)} ok={sum(1 for r in results if r['ok'])}", flush=True)

ok = sum(1 for r in results if r["ok"])
fail = [r for r in results if not r["ok"]]
print(f"DONE ok={ok} fail={len(fail)}")
for r in fail[:20]:
    print(f"FAIL {r['item']}: {r['errors']}")
with open("/tmp/batch_cutover_result.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=1)
print("result saved: /tmp/batch_cutover_result.json")
frappe.destroy()

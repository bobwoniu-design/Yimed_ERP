"""对齐盘点 dry-run：用吉客云库存快照作为真值，算出 ERPNext 需要调整的差额。

只读，不生成任何 Stock Reconciliation。输出三类：
1) 需增加（吉客云 > ERPNext）
2) 需减少/清零（吉客云 < ERPNext，含吉客云为 0 但 ERPNext 有货）
3) ERPNext 独有（Bin 有、吉客云快照里完全没有该商品×仓库）
并标注哪些商品开了批次/序列号管理（这些无法用无批次盘点直接对齐）。
"""
import frappe


def cancel_reconciliations(names):
    """Cancel only the explicitly named reconciliation documents."""
    if isinstance(names, str):
        names = frappe.parse_json(names)
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock", 1)
    frappe.db.set_value(
        "Stock Settings", "Stock Settings", "allow_negative_stock_for_batch", 1
    )
    frappe.clear_cache()
    result = {"cancelled": [], "skipped": [], "failed": []}
    for name in names or []:
        try:
            doc = frappe.get_doc("Stock Reconciliation", name)
            if doc.docstatus == 1:
                doc.cancel()
                frappe.db.commit()
                result["cancelled"].append(name)
            elif doc.docstatus == 0:
                frappe.delete_doc("Stock Reconciliation", name, ignore_permissions=True)
                frappe.db.commit()
                result["cancelled"].append(name)
            else:
                result["skipped"].append(name)
        except Exception as exc:
            frappe.db.rollback()
            result["failed"].append({"name": name, "error": str(exc)})
    return result


def dry_run():
    # 吉客云快照聚合到 (item, warehouse)
    jy_rows = frappe.db.sql(
        """
        select item, warehouse, sum(current_quantity) as jy_qty
        from `tabJackyun Inventory Snapshot`
        where item is not null and item != '' and warehouse is not null and warehouse != ''
        group by item, warehouse
        """,
        as_dict=True,
    )
    jy = {(r["item"], r["warehouse"]): r["jy_qty"] for r in jy_rows}

    # ERPNext Bin 聚合到 (item_code, warehouse)
    bin_rows = frappe.db.sql(
        """
        select item_code, warehouse, sum(actual_qty) as actual_qty
        from `tabBin`
        where item_code is not null and item_code != '' and warehouse is not null and warehouse != ''
        group by item_code, warehouse
        """,
        as_dict=True,
    )
    bin_map = {(r["item_code"], r["warehouse"]): r["actual_qty"] for r in bin_rows}

    all_keys = set(jy) | set(bin_map)

    increase = []   # 吉客云 > ERPNext → 调增
    decrease = []   # 吉客云 < ERPNext → 调减（含清零）
    erp_only = []   # 吉客云快照没有、ERPNext 有货
    for item, wh in all_keys:
        jy_qty = jy.get((item, wh), 0)
        actual = bin_map.get((item, wh), 0)
        delta = jy_qty - actual
        if abs(delta) < 0.0001:
            continue
        row = {"item": item, "warehouse": wh, "erpnext": actual, "jackyun": jy_qty, "delta": delta}
        if (item, wh) not in jy:
            erp_only.append(row)
        elif delta > 0:
            increase.append(row)
        else:
            decrease.append(row)

    # 批次/序列号管理商品集合
    batch_items = set(
        frappe.db.get_all("Item", filters={"has_batch_no": 1}, pluck="name")
    )
    serial_items = set(
        frappe.db.get_all("Item", filters={"has_serial_no": 1}, pluck="name")
    )

    def flag(item):
        tags = []
        if item in batch_items:
            tags.append("批次")
        if item in serial_items:
            tags.append("序列号")
        return "+".join(tags) if tags else ""

    print("=" * 80)
    print("对齐盘点 DRY-RUN（只读，未生成任何单据）")
    print("=" * 80)
    print(f"吉客云快照 (item×warehouse) 组合数: {len(jy)}")
    print(f"ERPNext Bin   (item×warehouse) 组合数: {len(bin_map)}")
    print(f"需调整组合总数: {len(increase) + len(decrease) + len(erp_only)}")
    print(f"  调增 {len(increase)} | 调减 {len(decrease)} | ERPNext独有 {len(erp_only)}")

    # 按仓库汇总
    def wh_summary(rows, label):
        agg = {}
        for r in rows:
            agg[r["warehouse"]] = agg.get(r["warehouse"], 0) + r["delta"]
        print(f"\n[{label}] 按仓库净调整：")
        for wh, d in sorted(agg.items(), key=lambda x: abs(x[1]), reverse=True):
            print(f"  {wh:24s} {d:>15,.2f}")

    wh_summary(increase, "调增(吉客云>ERPNext)")
    wh_summary(decrease, "调减(吉客云<ERPNext)")
    wh_summary(erp_only, "ERPNext独有(吉客云无)")

    # 风险：批次/序列号商品
    risky_inc = [r for r in increase if flag(r["item"])]
    risky_dec = [r for r in decrease if flag(r["item"])]
    risky_erp = [r for r in erp_only if flag(r["item"])]
    print(f"\n[风险] 需调整但商品开了批次/序列号管理：")
    print(f"  调增里 {len(risky_inc)} 条 | 调减里 {len(risky_dec)} 条 | ERPNext独有里 {len(risky_erp)} 条")

    print("\n[调增样例 前 20]（ERPNext -> 吉客云）")
    for r in sorted(increase, key=lambda x: abs(x["delta"]), reverse=True)[:20]:
        f = flag(r["item"])
        print(f"  {r['item']:20s} {r['warehouse']:20s} ERP {r['erpnext']:>12,.2f} -> JY {r['jackyun']:>12,.2f}  Δ{'+' if r['delta']>0 else ''}{r['delta']:,.2f} {f}")

    print("\n[调减样例 前 20]（ERPNext -> 吉客云）")
    for r in sorted(decrease, key=lambda x: abs(x["delta"]), reverse=True)[:20]:
        f = flag(r["item"])
        print(f"  {r['item']:20s} {r['warehouse']:20s} ERP {r['erpnext']:>12,.2f} -> JY {r['jackyun']:>12,.2f}  Δ{'+' if r['delta']>0 else ''}{r['delta']:,.2f} {f}")

    print("\n[ERPNext独有样例 前 20]（吉客云快照里没有）")
    for r in sorted(erp_only, key=lambda x: abs(x["delta"]), reverse=True)[:20]:
        f = flag(r["item"])
        print(f"  {r['item']:20s} {r['warehouse']:20s} ERP {r['erpnext']:>12,.2f} -> JY {r['jackyun']:>12,.2f}  {f}")

    return {
        "increase": len(increase),
        "decrease": len(decrease),
        "erp_only": len(erp_only),
        "risky_increase": len(risky_inc),
        "risky_decrease": len(risky_dec),
        "risky_erp_only": len(risky_erp),
    }


def apply(dry_run=True, posting_date="2026-08-22", posting_time=None):
    """按吉客云快照生成对齐盘点（Stock Reconciliation）并提交。

    批次商品按 batch_data 拆成批次行；序列号商品跳过（需序列号，另处理）。
    """
    batch_items = set(frappe.db.get_all("Item", filters={"has_batch_no": 1}, pluck="name"))
    serial_items = set(frappe.db.get_all("Item", filters={"has_serial_no": 1}, pluck="name"))

    # ERPNext Batch (name, item) 映射，用于批次号跨商品重复时匹配正确的 batch 名
    batch_map = {}
    for b in frappe.db.get_all("Batch", fields=["name", "item"]):
        batch_map.setdefault(b["item"], set()).add(b["name"])

    def resolve_batch(item, bno):
        if bno in batch_map.get(item, set()):
            return bno
        suffixed = f"{bno}-{item}"[:140]
        if suffixed in batch_map.get(item, set()):
            return suffixed
        return None

    snap = frappe.db.sql(
        """
        select item, warehouse, current_quantity, batch_data, cost_price
        from `tabJackyun Inventory Snapshot`
        where item is not null and item != '' and warehouse is not null and warehouse != ''
        """,
        as_dict=True,
    )

    # ERPNext 当前库存（用于跳过无差异商品）
    bin_map = {
        (r["item_code"], r["warehouse"]): r["actual_qty"]
        for r in frappe.db.sql(
            """
            select item_code, warehouse, sum(actual_qty) as actual_qty
            from `tabBin`
            where item_code is not null and item_code != '' and warehouse is not null and warehouse != ''
            group by item_code, warehouse
            """,
            as_dict=True,
        )
    }

    by_wh = {}
    skipped = []
    for r in snap:
        wh = r["warehouse"]
        item = r["item"]
        if item in serial_items:
            skipped.append((item, wh, "序列号"))
            continue
        if item in batch_items:
            batches = (
                frappe.parse_json(r["batch_data"])
                if isinstance(r["batch_data"], str)
                else (r["batch_data"] or [])
            )
            if not batches:
                skipped.append((item, wh, "批次明细缺失"))
                continue
            d = by_wh.setdefault(wh, {}).setdefault(
                item, {"qty": 0.0, "batches": {}, "cost_price": frappe.utils.flt(r.get("cost_price"))}
            )
            for b in batches:
                bno = b.get("batchNo")
                q = frappe.utils.flt(b.get("residualQuantity"))
                if not bno or q == 0:
                    continue
                erp_batch = resolve_batch(item, bno)
                if not erp_batch:
                    skipped.append((item, wh, f"批次{bno}未匹配"))
                    continue
                d["batches"][erp_batch] = d["batches"].get(erp_batch, 0) + q
                d["qty"] += q
            if not d["batches"]:
                del by_wh[wh][item]
        else:
            d = by_wh.setdefault(wh, {}).setdefault(
                item, {"qty": 0.0, "batches": {}, "cost_price": frappe.utils.flt(r.get("cost_price"))}
            )
            d["qty"] += frappe.utils.flt(r.get("current_quantity"))

    total_rows = sum(
        sum(len(d["batches"]) if d["batches"] else 1 for d in wh_map.values())
        for wh_map in by_wh.values()
    )
    print(f"对齐盘点：{len(by_wh)} 个仓库，{total_rows} 行明细，跳过 {len(skipped)} 个（序列号/批次明细缺失/批次未匹配）")

    if dry_run:
        print("\n[按仓库汇总]")
        for wh, wh_map in sorted(by_wh.items(), key=lambda x: -sum(d['qty'] for d in x[1].values())):
            print(f"  {wh:24s} {len(wh_map):>5} 商品  {sum(d['qty'] for d in wh_map.values()):>15,.2f} 件")
        return {"recos": len(by_wh), "rows": total_rows, "skipped": len(skipped)}

    done = failed = 0
    errors = []
    for wh, wh_map in sorted(by_wh.items()):
        company = frappe.db.get_value("Warehouse", wh, "company")
        if not company:
            failed += 1
            errors.append(f"{wh}: 仓库无公司")
            continue
        items = []
        for item, d in sorted(wh_map.items()):
            if abs(d["qty"] - (bin_map.get((item, wh), 0) or 0)) < 0.0001:
                continue  # 商品总数量无差异，跳过
            valuation_rate = d.get("cost_price") or frappe.db.get_value("Item", item, "valuation_rate") or 1
            if d["batches"]:
                for bno, q in sorted(d["batches"].items()):
                    items.append({
                        "item_code": item, "warehouse": wh, "qty": q,
                        "batch_no": bno, "use_serial_batch_fields": 1, "valuation_rate": valuation_rate,
                    })
            else:
                items.append({"item_code": item, "warehouse": wh, "qty": d["qty"], "valuation_rate": valuation_rate})
        if not items:
            continue
        try:
            doc = frappe.get_doc({
                "doctype": "Stock Reconciliation",
                "company": company,
                "purpose": "Stock Reconciliation",
                "posting_date": posting_date,
                "posting_time": posting_time or frappe.utils.nowtime(),
                "set_posting_time": 1,
                "set_warehouse": wh,
                "items": items,
            })
            doc.insert(ignore_permissions=True)
            doc.submit()
            frappe.db.commit()
            done += 1
        except Exception as e:
            frappe.db.rollback()
            failed += 1
            errors.append(f"{wh}: {str(e)[:160]}")
    print(f"\n盘点单生成: 成功 {done} / 失败 {failed}")
    for e in errors[:30]:
        print("  ", e)
    return {"done": done, "failed": failed}



def check_batches():
    """检查吉客云快照里批次商品的 batchNo 在 ERPNext Batch 里的覆盖率。"""
    batch_items = set(frappe.db.get_all("Item", filters={"has_batch_no": 1}, pluck="name"))
    snap = frappe.db.sql(
        "select item, batch_data from `tabJackyun Inventory Snapshot` where batch_data is not null and batch_data != ''",
        as_dict=True,
    )
    all_bnos = set()
    for r in snap:
        if r["item"] not in batch_items:
            continue
        batches = frappe.parse_json(r["batch_data"]) if isinstance(r["batch_data"], str) else (r["batch_data"] or [])
        for b in batches:
            bno = b.get("batchNo")
            q = frappe.utils.flt(b.get("residualQuantity"))
            if bno and q != 0:
                all_bnos.add(bno)
    existing = set()
    if all_bnos:
        bnos = list(all_bnos)
        for i in range(0, len(bnos), 500):
            existing |= set(frappe.db.get_all("Batch", filters={"name": ["in", bnos[i:i+500]]}, pluck="name"))
    missing = all_bnos - existing
    print(f"快照批次号总数: {len(all_bnos)}")
    print(f"已存在于 tabBatch: {len(existing)}")
    print(f"缺失: {len(missing)}")
    for m in sorted(missing)[:30]:
        print(f"  缺失批次: {m}")


def submit_draft_recos():
    names = frappe.db.get_all(
        "Stock Reconciliation",
        filters={"docstatus": 0, "posting_date": frappe.utils.today()},
        pluck="name",
    )
    for n in names:
        try:
            doc = frappe.get_doc("Stock Reconciliation", n)
            doc.submit()
            frappe.db.commit()
            print(f"OK {n}")
        except Exception as e:
            frappe.db.rollback()
            print(f"FAIL {n}: {str(e)[:250]}")


def check_mismatch():
    batch_items = set(frappe.db.get_all("Item", filters={"has_batch_no": 1}, pluck="name"))
    batch_by_item = {}
    for b in frappe.db.get_all("Batch", fields=["name", "item"]):
        batch_by_item.setdefault(b["item"], set()).add(b["name"])
    snap = frappe.db.sql(
        "select item, batch_data from `tabJackyun Inventory Snapshot` where batch_data is not null and batch_data != ''",
        as_dict=True,
    )
    mismatch = []
    for r in snap:
        if r["item"] not in batch_items:
            continue
        batches = frappe.parse_json(r["batch_data"]) if isinstance(r["batch_data"], str) else (r["batch_data"] or [])
        for b in batches:
            bno = b.get("batchNo")
            q = frappe.utils.flt(b.get("residualQuantity"))
            if not bno or q == 0:
                continue
            item_batches = batch_by_item.get(r["item"], set())
            if bno in item_batches or f"{bno}-{r['item']}"[:140] in item_batches:
                continue
            owner = frappe.db.get_all("Batch", filters={"name": bno}, pluck="item")
            mismatch.append((r["item"], bno, owner, q))
    print(f"未匹配批次商品明细: {len(mismatch)} 条")
    for item, bno, owner, q in mismatch[:40]:
        print(f"  {item} 批次{bno}(归属:{owner}) qty={q}")


def fix_batch_items(dry_run=True):
    """找出「吉客云已清零、ERPNext 有库存、误开批次管理」的商品，关闭其 has_batch_no。

    只关闭库存流水里确实没有 batch_no 的商品（无批次库存），有批次库存的跳过需人工。
    """
    batch_items = set(frappe.db.get_all("Item", filters={"has_batch_no": 1}, pluck="name"))
    jy = {
        r["item"]: r["jy_qty"]
        for r in frappe.db.sql(
            "select item, sum(current_quantity) as jy_qty from `tabJackyun Inventory Snapshot` group by item",
            as_dict=True,
        )
    }
    bin_rows = frappe.db.sql(
        "select item_code, sum(actual_qty) as qty from `tabBin` group by item_code", as_dict=True
    )

    candidates = []
    for r in bin_rows:
        item = r["item_code"]
        if item in batch_items and r["qty"] > 0 and (jy.get(item, 0) or 0) == 0:
            has_batch_sle = frappe.db.sql(
                "select count(*) from `tabStock Ledger Entry` where item_code=%s and batch_no is not null and batch_no != ''",
                item,
            )[0][0]
            candidates.append((item, r["qty"], has_batch_sle))

    print(f"候选商品（批次管理 + ERPNext有库存 + 吉客云0）: {len(candidates)} 个")
    safe = 0
    for item, qty, has_batch_sle in candidates:
        if has_batch_sle:
            print(f"  [跳过] {item}: ERPNext {qty}, 有 {has_batch_sle} 条批次SLE")
        else:
            if not dry_run:
                frappe.db.set_value("Item", item, "has_batch_no", 0)
            print(f"  [{'关闭' if not dry_run else '待关闭'}] {item}: ERPNext {qty}")
            safe += 1
    print(f"可安全关闭: {safe} 个")
    if not dry_run:
        frappe.db.commit()


def fix_xianning():
    frappe.db.sql("update `tabBin` set reserved_qty=0 where warehouse=%s", ("咸宁总仓 - 医麦德",))
    frappe.db.commit()
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock", 1)
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock_for_batch", 1)
    frappe.clear_cache()
    print("已清咸宁总仓预留 + 允许负库存")


def restore_negative_stock():
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock", 0)
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock_for_batch", 0)
    frappe.clear_cache()
    print("负库存开关已恢复为 0")


def cancel_align_recos():
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock", 1)
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock_for_batch", 1)
    frappe.clear_cache()
    names = frappe.db.get_all("Stock Reconciliation", filters={"posting_date": "2026-08-25"}, pluck="name")
    ok = fail = 0
    for n in names:
        try:
            doc = frappe.get_doc("Stock Reconciliation", n)
            if doc.docstatus == 1:
                doc.cancel()
            elif doc.docstatus == 0:
                doc.delete()
            frappe.db.commit()
            ok += 1
        except Exception as e:
            frappe.db.rollback()
            fail += 1
            print(f"  FAIL {n}: {str(e)[:150]}")
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock", 0)
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock_for_batch", 0)
    frappe.clear_cache()
    print(f"取消/删除对齐盘点: 成功 {ok} / 失败 {fail}")


def fix_negative_all():
    frappe.db.sql("update `tabBin` set reserved_qty=0")
    frappe.db.commit()
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock", 1)
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock_for_batch", 1)
    frappe.clear_cache()
    print("已清所有预留 + 允许负库存")


def submit_all_drafts():
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock", 1)
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock_for_batch", 1)
    frappe.clear_cache()

    # 取消 08-24 提交的盘点单（future-dated 阻挡）
    recos = frappe.db.get_all("Stock Reconciliation", filters={"posting_date": "2026-08-24", "docstatus": 1}, pluck="name")
    c_ok = c_fail = 0
    for n in recos:
        try:
            frappe.get_doc("Stock Reconciliation", n).cancel()
            frappe.db.commit()
            c_ok += 1
        except Exception as e:
            frappe.db.rollback()
            c_fail += 1
            print(f"  取消盘点单失败 {n}: {str(e)[:120]}")
    print(f"取消 08-24 盘点单: 成功 {c_ok} / 失败 {c_fail}")

    # 提交草稿单据（采购收货、交货单、采购订单）
    for dt in ["Purchase Receipt", "Delivery Note", "Purchase Order"]:
        names = frappe.db.get_all(dt, filters={"docstatus": 0}, pluck="name")
        ok = fail = 0
        errors = []
        for n in names:
            try:
                frappe.get_doc(dt, n).submit()
                frappe.db.commit()
                ok += 1
            except Exception as e:
                frappe.db.rollback()
                fail += 1
                if len(errors) < 5:
                    errors.append(f"{n}: {str(e)[:100]}")
        print(f"{dt}: 提交 {ok} / 失败 {fail}")
        for e in errors:
            print(f"    {e}")

    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock", 0)
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock_for_batch", 0)
    frappe.clear_cache()
    print("负库存开关已恢复")


def cancel_all_recos():
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock", 1)
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock_for_batch", 1)
    frappe.clear_cache()
    names = frappe.db.get_all("Stock Reconciliation", filters={"docstatus": 1}, pluck="name")
    ok = fail = 0
    errors = []
    for n in names:
        try:
            frappe.get_doc("Stock Reconciliation", n).cancel()
            frappe.db.commit()
            ok += 1
        except Exception as e:
            frappe.db.rollback()
            fail += 1
            if len(errors) < 8:
                errors.append(f"{n}: {str(e)[:120]}")
    print(f"取消提交盘点单: 成功 {ok} / 失败 {fail}")
    for e in errors:
        print(f"  {e}")
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock", 0)
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock_for_batch", 0)
    frappe.clear_cache()


def add_home_shortcut():
    import json as _json

    ws = frappe.get_doc("Workspace", "Home")
    # 1. 加快捷方式行
    exists = frappe.db.exists(
        "Workspace Shortcut", {"parent": "Home", "label": "接口连接器"}
    )
    if not exists:
        ws.append(
            "shortcuts",
            {"label": "接口连接器", "type": "URL", "url": "/app/channel-erp"},
        )
    # 2. content 加 shortcut 块（在 Your Shortcuts header 后）
    content = _json.loads(ws.content or "[]")
    has_block = any(
        b.get("data", {}).get("shortcut_name") == "接口连接器" for b in content
    )
    if not has_block:
        content.append(
            {"id": "channel-erp-home-shortcut", "type": "shortcut",
             "data": {"shortcut_name": "接口连接器", "col": 3}}
        )
        ws.content = _json.dumps(content, ensure_ascii=False)
    ws.save(ignore_permissions=True)
    frappe.db.commit()
    print("Home 已加「接口连接器」快捷方式")


def move_home_shortcut():
    import json as _json

    ws = frappe.get_doc("Workspace", "Home")
    content = _json.loads(ws.content or "[]")
    # 找出接口连接器 shortcut 块并移除
    content = [b for b in content if not (
        b.get("type") == "shortcut" and b.get("data", {}).get("shortcut_name") == "接口连接器"
    )]
    # 找到「Your Shortcuts」header 的位置，插到它后面（首个快捷方式前）
    insert_idx = None
    for i, b in enumerate(content):
        if b.get("type") == "header" and "Shortcuts" in str(b.get("data", {}).get("text", "")):
            insert_idx = i + 1
            break
    if insert_idx is None:
        insert_idx = len(content)
    content.insert(insert_idx, {
        "id": "channel-erp-home-shortcut", "type": "shortcut",
        "data": {"shortcut_name": "接口连接器", "col": 3},
    })
    ws.content = _json.dumps(content, ensure_ascii=False)
    ws.save(ignore_permissions=True)
    frappe.db.commit()
    print("接口连接器快捷方式已移到快捷方式区")


def add_to_integrations():
    import json as _json

    ws = frappe.get_doc("Workspace", "Integrations")
    # 1. 加快捷方式行
    exists = frappe.db.exists(
        "Workspace Shortcut", {"parent": "Integrations", "label": "接口连接器"}
    )
    if not exists:
        ws.append(
            "shortcuts",
            {"label": "接口连接器", "type": "URL", "url": "/app/channel-erp"},
        )
    # 2. content 加 shortcut 块（在 Webhook shortcut 后）
    content = _json.loads(ws.content or "[]")
    has_block = any(
        b.get("data", {}).get("shortcut_name") == "接口连接器" for b in content
    )
    if not has_block:
        insert_idx = None
        for i, b in enumerate(content):
            if b.get("data", {}).get("shortcut_name") == "Webhook":
                insert_idx = i + 1
                break
        if insert_idx is None:
            insert_idx = len(content)
        content.insert(insert_idx, {
            "id": "jackyun-connector-shortcut", "type": "shortcut",
            "data": {"shortcut_name": "接口连接器", "col": 3},
        })
        ws.content = _json.dumps(content, ensure_ascii=False)
    ws.save(ignore_permissions=True)
    frappe.db.commit()
    print("Integrations 已加「接口连接器」快捷方式")

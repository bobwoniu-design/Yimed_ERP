"""一次性迁移：把已同步的吉客云交易单据从 ERPNext 自动编号改名为吉客云编号。

默认只做 dry-run（只输出计划、不改任何数据）：

    bench --site yimed.local execute channel_erp.jackyun_integration.migrate_names.migrate

确认无误后实跑：

    bench --site yimed.local execute channel_erp.jackyun_integration.migrate_names.migrate \
        --kwargs "{'dry_run': False}"

也可只迁移某几类、或先跑前 N 条：

    bench --site yimed.local execute channel_erp.jackyun_integration.migrate_names.migrate \
        --kwargs "{'dry_run': True, 'resources': ['Delivery Note'], 'limit': 100}"
"""
import frappe
from frappe.model.rename_doc import rename_doc

from channel_erp.integrations.jackyun import _jackyun_name, _pick

# 迁移范围：resource -> (目标 doctype, 吉客云编号在 raw_data 里的字段候选)
RESOURCES = {
    "Purchase Order": ("Purchase Order", ["orderNum"]),
    "Purchase Receipt": ("Purchase Receipt", ["goodsdocNo"]),
    "Delivery Note": ("Delivery Note", ["goodsdocNo"]),
    "Sales Return": ("Delivery Note", ["goodsdocNo"]),
    "Purchase Return": ("Purchase Receipt", ["goodsdocNo"]),
    "Stock Transfer": ("Stock Entry", ["allocateNo"]),
    "Stocktake": ("Stock Reconciliation", ["stocktakeId"]),
}


def _latest_raw(resource, external_id):
    rows = frappe.get_all(
        "Jackyun Raw Record",
        filters={"resource": resource, "external_id": external_id},
        fields=["raw_data"],
        order_by="creation desc",
        limit=1,
    )
    if not rows:
        return None
    raw = rows[0].raw_data
    return frappe.parse_json(raw) if isinstance(raw, str) else raw


def _number_for(resource, external_id, raw):
    fields = RESOURCES[resource][1]
    if raw:
        number = _pick(raw, fields)
        if number not in (None, ""):
            return frappe.utils.cstr(number).strip()
    # 兜底：Delivery Note 的 external_id 本身就是 goodsdocNo
    if resource == "Delivery Note":
        return frappe.utils.cstr(external_id).strip()
    return None


def _target_name(resource, external_id, number):
    if number in (None, ""):
        return None
    name = _jackyun_name(number)
    if resource == "Stock Transfer":
        if str(external_id).endswith(":out"):
            name = f"{name}-out" if name else None
        elif str(external_id).endswith(":in"):
            name = f"{name}-in" if name else None
    return name


def migrate(dry_run=True, resources=None, limit=None):
    resources = resources or list(RESOURCES)
    rename_plan = []
    seen = {}
    skipped_no_number = []
    skipped_same = 0
    skipped_missing = []
    collisions = []

    for resource in resources:
        if resource not in RESOURCES:
            frappe.throw(f"不支持的迁移资源：{resource}")
        doctype, _ = RESOURCES[resource]
        mappings = frappe.get_all(
            "External ID Mapping",
            filters={"resource": resource},
            fields=["external_id", "erpnext_name"],
            order_by="erpnext_name",
        )
        for m in mappings:
            if limit is not None and len(rename_plan) >= limit:
                break
            old = m.erpnext_name
            if not frappe.db.exists(doctype, old):
                # 已被改名过或已删除 → 可安全跳过（幂等）
                skipped_missing.append((resource, doctype, old))
                continue
            raw_key = m.external_id
            if resource == "Stock Transfer" and str(m.external_id).endswith((":out", ":in")):
                # 跨公司调拨的 :out/:in 映射，原始记录按基础 allocateId 存储
                raw_key = str(m.external_id).rsplit(":", 1)[0]
            raw = _latest_raw(resource, raw_key)
            number = _number_for(resource, m.external_id, raw)
            new = _target_name(resource, m.external_id, number)
            if not new:
                skipped_no_number.append((resource, m.external_id, old))
                continue
            if new == old:
                skipped_same += 1
                continue
            key = (doctype, old)
            if key in seen:
                # 跨公司调拨：同一 out 单既有 base 映射又有 :out 映射，带后缀者优先
                prev_idx = seen[key]
                prev = rename_plan[prev_idx]
                prev_suffixed = prev[2].endswith(("-out", "-in"))
                new_suffixed = new.endswith(("-out", "-in"))
                if prev_suffixed and not new_suffixed:
                    continue
                if new_suffixed and not prev_suffixed:
                    rename_plan[prev_idx] = (doctype, old, new, resource, m.external_id)
                continue
            if frappe.db.exists(doctype, new):
                collisions.append((doctype, old, new, resource, m.external_id))
                continue
            seen[key] = len(rename_plan)
            rename_plan.append((doctype, old, new, resource, m.external_id))

    print("=" * 72)
    print(f"{'DRY-RUN（未修改任何数据）' if dry_run else 'REAL RUN'}  吉客云交易单据改名迁移")
    print("=" * 72)
    print(f"待改名: {len(rename_plan)}")
    print(f"已同名(跳过): {skipped_same}")
    print(f"无编号(跳过): {len(skipped_no_number)}")
    print(f"旧单已不存在(幂等跳过): {len(skipped_missing)}")
    print(f"碰撞(新名已被占用): {len(collisions)}")

    if collisions:
        print("\n[碰撞明细]（新名已被别的单据占用，需先处理）")
        for doctype, old, new, resource, ext in collisions[:50]:
            print(f"  {resource:16s} {doctype:20s} {old}  ->  {new}")

    if skipped_no_number:
        print("\n[无编号样例]")
        for resource, ext, old in skipped_no_number[:20]:
            print(f"  {resource:16s} ext={ext}  old={old}")

    if rename_plan:
        print("\n[样例前 30 条]")
        for doctype, old, new, resource, ext in rename_plan[:30]:
            print(f"  {resource:16s} {doctype:20s} {old}  ->  {new}")

    if dry_run:
        print("\n(未做任何修改)")
        return {"renames": len(rename_plan), "collisions": len(collisions)}

    done = 0
    failed = []
    total = len(rename_plan)
    for idx, (doctype, old, new, resource, ext) in enumerate(rename_plan):
        try:
            rename_doc(
                doctype,
                old,
                new,
                force=True,
                ignore_permissions=True,
                show_alert=False,
                rebuild_search=False,
            )
            # 同步更新所有指向旧名的映射（主映射 + order:{num} 等别名）
            frappe.db.sql(
                "update `tabExternal ID Mapping` set erpnext_name=%s "
                "where erpnext_name=%s and erpnext_doctype=%s",
                (new, old, doctype),
            )
            done += 1
            if done % 50 == 0:
                frappe.db.commit()
                print(f"... {done}/{total}", flush=True)
        except Exception as exc:
            failed.append((doctype, old, new, str(exc)))
            frappe.db.rollback()

    frappe.db.commit()
    print("=" * 72)
    print(f"完成: {done}  失败: {len(failed)}")
    for doctype, old, new, err in failed[:50]:
        print(f"  [失败] {doctype} {old} -> {new}: {err}")
    return {"done": done, "failed": len(failed)}

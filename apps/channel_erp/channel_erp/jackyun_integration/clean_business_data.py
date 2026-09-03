"""清空业务数据：交易单据 + 库存账 + 映射 + 同步日志，保留主数据（商品/仓库/供应商/客户/批次等）。"""
import json

import frappe


def clean():
    # 1. 删除交易单据（明细 + 主表，SQL 绕过 docstatus 检查）
    doctypes = {
        "Sales Order": "Sales Order Item",
        "Purchase Order": "Purchase Order Item",
        "Delivery Note": "Delivery Note Item",
        "Purchase Receipt": "Purchase Receipt Item",
        "Stock Entry": "Stock Entry Detail",
        "Stock Reconciliation": "Stock Reconciliation Item",
    }
    for dt, child in doctypes.items():
        frappe.db.sql(f"delete from `tab{child}`")
        frappe.db.sql(f"delete from `tab{dt}`")
        print(f"删除 {dt}")

    # 2. 清空库存账
    frappe.db.sql("delete from `tabStock Ledger Entry`")
    frappe.db.sql("delete from `tabBin`")
    frappe.db.sql("delete from `tabSerial and Batch Entry`")
    frappe.db.sql("delete from `tabSerial and Batch Bundle`")
    print("清空库存账（SLE/Bin/Serial and Batch Bundle）")

    # 3. 删除业务单据的映射
    biz_resources = [
        "Sales Order", "Purchase Order", "Delivery Note", "Purchase Receipt",
        "Sales Return", "Purchase Return", "Stock Transfer", "Stock Movement", "Stocktake",
    ]
    frappe.db.sql("delete from `tabExternal ID Mapping` where resource in %s", (biz_resources,))
    print("删除业务单据映射")

    # 4. 清空同步日志（重置增量游标）
    frappe.db.sql("delete from `tabJackyun Sync Log`")
    print("清空同步日志")

    frappe.db.commit()
    print("清空完成")


def add_stock_movement_schedule():
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    for row in conn.sync_schedules:
        if row.resource == "Stock Movement":
            row.interval_minutes = 60
            conn.save()
            frappe.db.commit()
            print("Stock Movement 已在 schedule，更新为 60 分钟")
            return
    conn.append("sync_schedules", {"resource": "Stock Movement", "interval_minutes": 60, "enabled": 1})
    conn.save()
    frappe.db.commit()
    print("已加入 Stock Movement 到 schedule（60 分钟）")


def enable_negative_stock():
    old = frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock", 1)
    frappe.db.set_value("Stock Settings", "Stock Settings", "allow_negative_stock_for_batch", 1)
    frappe.clear_cache()
    print(f"allow_negative_stock: {old} -> 1，allow_negative_stock_for_batch -> 1")


def submit_drafts():
    for dt in ["Purchase Receipt", "Delivery Note", "Purchase Order", "Stock Entry"]:
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
                if len(errors) < 3:
                    errors.append(f"{n}: {str(e)[:120]}")
        print(f"{dt}: 提交 {ok} / 失败 {fail}")
        for e in errors:
            print(f"    {e}")


def submit_sales_order_drafts():
    from channel_erp.integrations.jackyun import sales_order_status_action

    frappe.db.sql(
        """
        delete ps1 from `tabPayment Schedule` ps1
        join `tabPayment Schedule` ps2 on ps2.parent = ps1.parent and ps2.name < ps1.name
        where ps1.parent in (select name from `tabSales Order` where docstatus=0)
        """
    )
    frappe.db.commit()

    names = frappe.db.get_all("Sales Order", filters={"docstatus": 0}, pluck="name")
    ok = fail = skip = 0
    for n in names:
        try:
            doc = frappe.get_doc("Sales Order", n)
            ext = frappe.db.get_value(
                "External ID Mapping",
                {"resource": "Sales Order", "erpnext_name": n, "external_id": ["not like", "trade:%"]},
                "external_id",
            )
            raw = frappe.db.get_value(
                "Jackyun Raw Record",
                {"resource": "Sales Order", "external_id": ext},
                "raw_data",
                order_by="creation desc",
            )
            trade_status = None
            if raw:
                data = frappe.parse_json(raw) if isinstance(raw, str) else raw
                trade_status = data.get("tradeStatus")
            if sales_order_status_action(trade_status) == "submit":
                doc.submit()
                frappe.db.commit()
                ok += 1
            else:
                skip += 1
        except Exception as e:
            frappe.db.rollback()
            fail += 1
            if fail <= 5:
                print(f"  FAIL {n}: {str(e)[:120]}")
    print(f"销售订单提交: 成功 {ok} / 失败 {fail} / 跳过(未发货) {skip}")


def test_payment_schedule():
    # 取一张已存在的草稿销售订单，复制 header + items，重新 insert 看付款计划条数
    src = frappe.db.get_all("Sales Order", filters={"docstatus": 0}, pluck="name", limit=1)
    if not src:
        print("无草稿销售订单")
        return
    src_doc = frappe.get_doc("Sales Order", src[0])
    header = {k: v for k, v in src_doc.as_dict().items() if k not in ("name", "items", "payment_schedule", "doctype")}
    items = [row.as_dict() for row in src_doc.get("items")]
    doc = frappe.get_doc({"doctype": "Sales Order", **header})
    doc.set("items", items)
    print(f"insert 前 payment_schedule 条数: {len(doc.get('payment_schedule') or [])}")
    doc.insert(ignore_permissions=True)
    print(f"insert 后 payment_schedule 条数: {len(doc.get('payment_schedule') or [])}")
    doc.submit()
    print(f"submit 后 payment_schedule 条数: {len(doc.get('payment_schedule') or [])}")


def cancel_3_recos():
    names = ["MAT-RECO-2026-00082", "MAT-RECO-2026-00089", "MAT-RECO-2026-00091"]
    for n in names:
        try:
            doc = frappe.get_doc("Stock Reconciliation", n)
            if doc.docstatus == 1:
                doc.cancel()
                frappe.db.commit()
                print(f"OK 取消 {n}")
        except Exception as e:
            frappe.db.rollback()
            print(f"FAIL {n}: {str(e)[:160]}")


def apply_connector_workspace():
    """把 Channel Erp 工作区精简为纯接口连接器门户，桌面图标改名为接口连接器。"""
    # 1. 桌面图标
    frappe.db.set_value("Desktop Icon", "Channel Erp", "label", "接口连接器")

    # 2. workspace: title/content + 重建 links
    ws = frappe.get_doc("Workspace", "Channel Erp")
    ws.title = "接口连接器"
    ws.content = json.dumps([
        {"id": "channel-erp-functions-header", "type": "header",
         "data": {"text": '<span class="h4"><b>接口连接器</b></span>', "col": 12}},
        {"id": "channel-erp-jackyun-card", "type": "card",
         "data": {"card_name": "接口连接器", "col": 12}},
    ], ensure_ascii=False)
    frappe.db.sql("delete from `tabWorkspace Link` where parent=%s", ("Channel Erp",))
    ws.set("links", [])
    for link in [
        {"type": "Card Break", "label": "接口连接器"},
        {"type": "Link", "label": "连接配置", "link_type": "DocType", "link_to": "Jackyun Connection"},
        {"type": "Link", "label": "同步日志", "link_type": "DocType", "link_to": "Jackyun Sync Log"},
        {"type": "Link", "label": "原始记录", "link_type": "DocType", "link_to": "Jackyun Raw Record"},
        {"type": "Link", "label": "外部ID映射", "link_type": "DocType", "link_to": "External ID Mapping"},
    ]:
        ws.append("links", link)
    ws.save(ignore_permissions=True)

    # 3. sidebar items 重建
    frappe.db.sql("delete from `tabWorkspace Sidebar Item` where parent=%s", ("Channel Erp",))
    sb = frappe.get_doc("Workspace Sidebar", "Channel Erp")
    sb.set("items", [])
    for item in [
        {"type": "Link", "label": "Home", "link_type": "Workspace", "link_to": "Channel Erp", "icon": "home"},
        {"type": "Section Break", "label": "接口连接器", "icon": "integration"},
        {"type": "Link", "label": "连接配置", "link_type": "DocType", "link_to": "Jackyun Connection"},
        {"type": "Link", "label": "同步日志", "link_type": "DocType", "link_to": "Jackyun Sync Log"},
        {"type": "Link", "label": "原始记录", "link_type": "DocType", "link_to": "Jackyun Raw Record"},
        {"type": "Link", "label": "外部ID映射", "link_type": "DocType", "link_to": "External ID Mapping"},
    ]:
        sb.append("items", item)
    sb.save(ignore_permissions=True)

    frappe.db.commit()
    print("桌面图标 -> 接口连接器；工作区精简完成")

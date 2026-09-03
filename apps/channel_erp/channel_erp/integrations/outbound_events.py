"""ERPNext document events for generic outbound connector messages."""
from __future__ import annotations

import re

import frappe

from channel_erp.integrations.connector_operations import (
    enqueue_outbound_internal,
    payload_digest,
)
from channel_erp.integrations.jackyun_outbound import (
    OutboundValidationError,
    build_sales_order_create_payload,
    cancel_payload,
)


def _active_connection(doc, include_existing=False):
    rows = frappe.get_all(
        "Jackyun Connection",
        filters={"enabled": 1, "outbound_enabled": 1},
        fields=[
            "name", "outbound_enabled_at", "outbound_test_mode",
            "default_cancel_reason", "audit_operator", "auto_audit_outbound",
        ],
        order_by="creation asc",
        limit=1,
    )
    if not rows:
        return None
    connection = rows[0]
    if not connection.outbound_enabled_at:
        return None
    if (
        not include_existing
        and frappe.utils.get_datetime(doc.creation)
        < frappe.utils.get_datetime(connection.outbound_enabled_at)
    ):
        return None
    return connection


def preserve_sales_order_series_after_delete(doc, method=None):
    """Never reuse a Sales Order number that may already exist externally.

    Frappe normally decrements ``tabSeries`` when the highest numbered document
    is deleted. That is convenient for local drafts but unsafe once the number
    has been used as an idempotency key or external order reference.
    """
    del method
    match = re.match(r"^(.*?)(\d+)$", doc.name or "")
    if not match:
        return
    prefix, counter = match.group(1), int(match.group(2))
    if frappe.db.sql("SELECT 1 FROM `tabSeries` WHERE `name` = %s", prefix):
        frappe.db.sql(
            "UPDATE `tabSeries` SET `current` = GREATEST(`current`, %s) WHERE `name` = %s",
            (counter, prefix),
        )


def _generic_connection(legacy_name):
    return frappe.db.get_value(
        "Connector Connection",
        {
            "platform": "jackyun",
            "credential_doctype": "Jackyun Connection",
            "credential_name": legacy_name,
            "enabled": 1,
        },
        "name",
    )


def _is_jackyun_source(doc):
    source = frappe.utils.cstr(doc.get("custom_connector_source")).strip().lower()
    if source in {"jackyun", "jikeyun", "吉客云"}:
        return True
    return bool(frappe.db.exists(
        "External ID Mapping",
        {
            "platform": "jackyun", "resource": "Sales Order",
            "erpnext_doctype": "Sales Order", "erpnext_name": doc.name,
        },
    ))


def _shipping(doc):
    address_name = doc.get("shipping_address_name") or doc.get("customer_address")
    address = frappe.get_doc("Address", address_name) if address_name and frappe.db.exists("Address", address_name) else frappe._dict()
    contact_name = doc.get("contact_person")
    contact = frappe.get_doc("Contact", contact_name) if contact_name and frappe.db.exists("Contact", contact_name) else frappe._dict()
    receiver = contact.get("full_name") or address.get("address_title") or doc.get("customer_name")
    return {
        "receiver_name": receiver,
        "phone": contact.get("phone") or contact.get("mobile_no") or address.get("phone"),
        "mobile": contact.get("mobile_no") or contact.get("phone") or address.get("phone"),
        "country": address.get("country"),
        "state": address.get("state"),
        "city": address.get("city"),
        "county": address.get("county"),
        "pincode": address.get("pincode"),
        "address_line1": address.get("address_line1"),
        "address_line2": address.get("address_line2"),
    }


def _enriched_order(doc):
    data = doc.as_dict()
    for row in data.get("items") or []:
        item = frappe.get_cached_doc("Item", row.item_code)
        barcodes = item.get("barcodes") or []
        row["barcode"] = (barcodes[0].barcode if barcodes else None)
        # JackYun accepts barcode, or goodsNo + specName. Imported SKUs normally
        # carry a barcode; item_name is the safe simple-SKU fallback.
        row["spec_name"] = item.get("variant_of") or item.get("item_name")
        # Older imported Items may predate the Item Barcode mapping. Their
        # authoritative JackYun SKU identifiers are still retained in the raw
        # record, so use those values for outbound matching instead of sending
        # ERPNext's display name as the SKU specification.
        raw_data = frappe.db.get_value(
            "Jackyun Raw Record",
            {"resource": "SKU", "external_id": row.item_code},
            "raw_data",
        )
        if raw_data:
            raw = frappe.parse_json(raw_data)
            row["goods_no"] = raw.get("goodsNo") or row.item_code
            row["barcode"] = row.get("barcode") or raw.get("skuBarcode")
            row["spec_name"] = raw.get("skuName") or row.get("spec_name")
    return data


def _root_sales_order_name(doc):
    root_name = doc.name
    parent = doc.get("amended_from")
    while parent:
        root_name = parent
        parent = frappe.db.get_value("Sales Order", parent, "amended_from")
    return root_name


def _build_create(doc, connection, revision_override=None):
    if not doc.get("custom_jackyun_sales_channel"):
        raise OutboundValidationError("销售渠道不能为空")
    channel = frappe.get_doc("Jackyun Sales Channel", doc.custom_jackyun_sales_channel)
    warehouse = {
        "warehouse_code": channel.get("warehouse_code"),
        "warehouse_name": channel.get("warehouse_name") or doc.get("set_warehouse"),
    }
    revision = (
        frappe.utils.cint(revision_override)
        if revision_override is not None
        else frappe.utils.cint(doc.get("custom_external_revision"))
    )
    order_data = _enriched_order(doc)
    if revision_override is not None:
        order_data["name"] = _root_sales_order_name(doc)
    elif doc.get("amended_from"):
        previous_name = doc.amended_from
        revision = frappe.utils.cint(
            frappe.db.get_value("Sales Order", previous_name, "custom_external_revision")
        ) + 1
        root_name = previous_name
        while True:
            parent = frappe.db.get_value("Sales Order", root_name, "amended_from")
            if not parent:
                break
            root_name = parent
        order_data["name"] = root_name
    else:
        # A deleted ERPNext order may later be recreated with the same naming
        # series value. Do not reuse the acknowledged Create identity or the
        # old JackYun onlineTradeNo; advance to the next revision instead.
        previous_messages = frappe.get_all(
            "Connector Outbound Message",
            filters={
                "reference_doctype": "Sales Order",
                "reference_name": doc.name,
                "resource": "Sales Order",
                "operation": "Create",
                "status": "Succeeded",
            },
            fields=["payload"],
        )
        if previous_messages:
            used_revisions = []
            for message in previous_messages:
                payload = frappe.parse_json(message.payload or "{}")
                online_no = ((payload or {}).get("tradeOrder") or {}).get("onlineTradeNo") or ""
                match = re.search(r"-R(\d+)$", online_no)
                used_revisions.append(int(match.group(1)) if match else 0)
            revision = max(revision, max(used_revisions) + 1)
    test_batch = frappe.utils.get_datetime(doc.creation).strftime("%Y%m%d")
    payload = build_sales_order_create_payload(
        order_data,
        channel=channel,
        shipping=_shipping(doc),
        warehouse=warehouse,
        revision=revision,
        test_mode=bool(connection.outbound_test_mode),
        test_batch=test_batch,
    )
    return payload, revision


def material_sales_order_snapshot(doc):
    """Return only fields whose remote value requires cancel/recreate.

    ERPNext status, delivery progress, remarks and all connector receipt fields
    are deliberately absent, so pull acknowledgements and queue callbacks cannot
    generate another outbound operation.
    """
    rows = []
    for row in doc.get("items") or []:
        rows.append({
            "item_code": frappe.utils.cstr(row.get("item_code")).strip(),
            "qty": frappe.utils.flt(row.get("qty"), 9),
            "rate": frappe.utils.flt(row.get("rate"), 9),
            "warehouse": frappe.utils.cstr(row.get("warehouse")).strip(),
            "uom": frappe.utils.cstr(row.get("uom") or row.get("stock_uom")).strip(),
        })
    # Line order has no business meaning in JackYun. Stable sorting keeps a UI
    # drag/reorder from cancelling a valid remote order while preserving
    # duplicate lines (which remain separate list entries).
    rows.sort(
        key=lambda row: (
            row["item_code"], row["warehouse"], row["uom"], row["qty"], row["rate"]
        )
    )
    return {
        "sales_channel": frappe.utils.cstr(
            doc.get("custom_jackyun_sales_channel")
        ).strip(),
        "set_warehouse": frappe.utils.cstr(doc.get("set_warehouse")).strip(),
        "items": rows,
    }


def has_material_sales_order_change(doc, previous):
    if not previous:
        return False
    return payload_digest(material_sales_order_snapshot(doc)) != payload_digest(
        material_sales_order_snapshot(previous)
    )


def _existing_rebuild_create(doc_name, source_event_id):
    name = frappe.db.get_value(
        "Connector Outbound Message",
        {
            "reference_doctype": "Sales Order",
            "reference_name": doc_name,
            "resource": "Sales Order",
            "operation": "Create",
            "source_event_id": source_event_id,
        },
        "name",
    )
    return frappe.get_doc("Connector Outbound Message", name) if name else None


def _rebuild_cancel_message(doc, connection, trade_no):
    existing = frappe.db.get_value(
        "Connector Outbound Message",
        {
            "platform": "jackyun",
            "resource": "Sales Order",
            "operation": "Cancel",
            "external_id": trade_no,
        },
        "name",
        order_by="creation desc",
    )
    if existing:
        return frappe.get_doc("Connector Outbound Message", existing)
    generic = _generic_connection(connection.name)
    return enqueue_outbound_internal(
        connector_connection=generic,
        connection=None if generic else connection.name,
        resource="Sales Order",
        operation="Cancel",
        reference_doctype="Sales Order",
        reference_name=doc.name,
        external_id=trade_no,
        payload=cancel_payload(
            trade_no, connection.default_cancel_reason or "420001"
        ),
        source_system="erpnext",
        source_event_id=f"Sales Order:{doc.name}:rebuild-cancel:{trade_no}",
        operation_identity=f"REBUILD-CANCEL:{trade_no}",
    )


def _retire_stale_rebuild_creates(doc_name, source_event_id):
    stale = frappe.get_all(
        "Connector Outbound Message",
        filters={
            "reference_doctype": "Sales Order",
            "reference_name": doc_name,
            "resource": "Sales Order",
            "operation": "Create",
            "source_event_id": ["like", f"Sales Order:{doc_name}:material-change:%"],
        },
        fields=["name", "status", "attempts", "source_event_id"],
    )
    for row in stale:
        if row.source_event_id == source_event_id:
            continue
        if row.status in {"Processing", "Succeeded", "Uncertain"} or frappe.utils.cint(row.attempts):
            frappe.throw(
                f"此前的吉客云订单重建任务 {row.name} 已开始执行，请先核实其结果"
            )
        if row.status in {"Preflight", "Pending", "Retrying"}:
            frappe.db.set_value(
                "Connector Outbound Message",
                row.name,
                {
                    "status": "Blocked",
                    "error_message": "销售订单再次发生变更，此重建版本已被更新版本取代",
                },
                update_modified=False,
            )


def _enqueue_cancel_rebuild(doc, connection, event_id=None):
    """Queue a cancel and a dependent create; never calls the remote API."""
    if doc.docstatus != 1:
        frappe.throw("只有已提交销售订单可以安排取消重建")
    if _is_jackyun_source(doc):
        frappe.throw("吉客云来源订单禁止从 ERPNext 回推重建")
    trade_no = frappe.utils.cstr(doc.get("custom_jackyun_trade_no")).strip()
    if not trade_no:
        frappe.throw("销售订单尚无已确认的吉客云销售单号，不能取消重建")

    snapshot_digest = payload_digest(material_sales_order_snapshot(doc))
    source_event_id = event_id or f"Sales Order:{doc.name}:material-change:{snapshot_digest}"
    existing_create = _existing_rebuild_create(doc.name, source_event_id)
    if existing_create:
        origin = (
            frappe.get_doc("Connector Outbound Message", existing_create.origin_message)
            if existing_create.get("origin_message")
            else None
        )
        return {"cancel": origin, "create": existing_create, "duplicate": True}

    _retire_stale_rebuild_creates(doc.name, source_event_id)
    cancel_message = _rebuild_cancel_message(doc, connection, trade_no)
    revision = max(1, frappe.utils.cint(doc.get("custom_external_revision")) + 1)
    create_payload, revision = _build_create(
        doc, connection, revision_override=revision
    )
    generic = _generic_connection(connection.name)
    create_message = enqueue_outbound_internal(
        connector_connection=generic,
        connection=None if generic else connection.name,
        resource="Sales Order",
        operation="Create",
        reference_doctype="Sales Order",
        reference_name=doc.name,
        payload=create_payload,
        source_system="erpnext",
        source_event_id=source_event_id,
        operation_identity=f"REBUILD-CREATE:{doc.name}:R{revision}:{snapshot_digest}",
        origin_message=cancel_message.name,
        max_attempts=1,
    )
    doc.db_set(
        {
            "custom_external_revision": revision,
            "custom_external_online_trade_no": create_payload["tradeOrder"]["onlineTradeNo"],
            "custom_external_sync_status": "Queued",
            "custom_external_sync_message": (
                f"实质变更已排队：先取消 {cancel_message.name}，确认后创建 {create_message.name}"
            ),
        },
        update_modified=False,
    )
    return {"cancel": cancel_message, "create": create_message, "duplicate": False}


def on_sales_order_update_after_submit(doc, method=None):
    del method
    connection = _active_connection(doc, include_existing=True)
    if not connection or _is_jackyun_source(doc):
        return
    previous = doc.get_doc_before_save()
    if not has_material_sales_order_change(doc, previous):
        return
    return _enqueue_cancel_rebuild(doc, connection)


@frappe.whitelist()
def enqueue_sales_order_rebuild(sales_order):
    """Safe compensation entry point for an already changed submitted order."""
    doc = frappe.get_doc("Sales Order", sales_order)
    doc.check_permission("write")
    connection = _active_connection(doc, include_existing=True)
    if not connection:
        frappe.throw("未找到已启用且允许写入的吉客云连接")
    return _enqueue_cancel_rebuild(doc, connection)


def before_sales_order_submit(doc, method=None):
    del method
    connection = _active_connection(doc)
    if not connection or _is_jackyun_source(doc):
        return
    if doc.get("amended_from"):
        original_trade_no = frappe.db.get_value("Sales Order", doc.amended_from, "custom_jackyun_trade_no")
        if original_trade_no and not frappe.db.exists(
            "Connector Outbound Message",
            {"reference_doctype": "Sales Order", "reference_name": doc.amended_from,
             "operation": "Cancel", "status": "Succeeded"},
        ):
            frappe.throw("原吉客云销售单尚未确认取消，不能提交修订订单")
    payload, revision = _build_create(doc, connection)
    doc.custom_external_revision = revision
    doc.custom_external_online_trade_no = payload["tradeOrder"]["onlineTradeNo"]
    doc.custom_external_sync_status = "Pending"
    doc.flags.connector_create_payload = payload


def on_sales_order_submit(doc, method=None):
    del method
    connection = _active_connection(doc)
    if not connection or _is_jackyun_source(doc):
        return
    try:
        payload = doc.flags.get("connector_create_payload") or _build_create(doc, connection)[0]
    except OutboundValidationError as exc:
        doc.db_set({"custom_external_sync_status": "Preflight Failed", "custom_external_sync_message": str(exc)})
        raise
    message = enqueue_outbound_internal(
        connector_connection=_generic_connection(connection.name),
        connection=None if _generic_connection(connection.name) else connection.name,
        resource="Sales Order", operation="Create",
        reference_doctype="Sales Order", reference_name=doc.name,
        payload=payload, source_system="erpnext",
        source_event_id=f"Sales Order:{doc.name}:submit:R{frappe.utils.cint(doc.custom_external_revision)}",
        max_attempts=1,
    )
    doc.db_set({
        "custom_external_sync_status": "Queued",
        "custom_external_online_trade_no": payload["tradeOrder"]["onlineTradeNo"],
        "custom_external_sync_message": f"已进入推送队列 {message.name}",
    }, update_modified=False)


@frappe.whitelist()
def refresh_sales_order_create_message(message_name):
    """Rebuild a safely retryable Sales Order Create message from its document."""
    msg = frappe.get_doc("Connector Outbound Message", message_name)
    msg.check_permission("write")
    if msg.resource != "Sales Order" or msg.operation != "Create":
        frappe.throw("仅支持重新生成销售订单创建消息")
    if msg.reference_doctype != "Sales Order" or not msg.reference_name:
        frappe.throw("消息未关联销售订单")
    if msg.status not in {"Preflight", "Pending", "Blocked"}:
        frappe.throw("当前消息状态不允许重新生成报文")
    if frappe.utils.cint(msg.attempts):
        probe = frappe.parse_json(msg.probe_data or "{}")
        if probe.get("status") != "Not Found":
            frappe.throw("已有写入尝试且尚未确认远端不存在，禁止重新生成并重试")

    order = frappe.get_doc("Sales Order", msg.reference_name)
    connection = _active_connection(order)
    if not connection:
        frappe.throw("未找到已启用且允许写入的吉客云连接")
    payload, revision = _build_create(order, connection)
    msg.payload = frappe.as_json(payload)
    msg.payload_digest = payload_digest(payload)
    msg.status = "Pending"
    msg.next_retry_at = frappe.utils.now_datetime()
    msg.error_message = "报文已按当前销售订单重新生成，等待安全重试"
    msg.save(ignore_permissions=True)
    order.db_set({
        "custom_external_revision": revision,
        "custom_external_online_trade_no": payload["tradeOrder"]["onlineTradeNo"],
        "custom_external_sync_status": "Pending",
        "custom_external_sync_message": f"队列 {msg.name} 已重新生成报文",
    }, update_modified=False)
    return msg.as_dict()


def on_sales_order_cancel(doc, method=None):
    del method
    connection = _active_connection(doc)
    trade_no = doc.get("custom_jackyun_trade_no")
    if not connection or _is_jackyun_source(doc) or not trade_no:
        return
    if frappe.db.exists(
        "Connector Outbound Message",
        {
            "platform": "jackyun",
            "resource": "Sales Order",
            "operation": "Cancel",
            "external_id": trade_no,
            "status": "Succeeded",
        },
    ):
        doc.db_set(
            {
                "custom_external_sync_status": "Cancelled",
                "custom_external_sync_message": "吉客云销售单此前已确认取消，无需重复发送",
            },
            update_modified=False,
        )
        return
    generic = _generic_connection(connection.name)
    message = enqueue_outbound_internal(
        connector_connection=generic,
        connection=None if generic else connection.name,
        resource="Sales Order", operation="Cancel",
        reference_doctype="Sales Order", reference_name=doc.name,
        external_id=trade_no,
        payload=cancel_payload(trade_no, connection.default_cancel_reason or "420001"),
        source_system="erpnext", source_event_id=f"Sales Order:{doc.name}:cancel",
    )
    doc.db_set({
        "custom_external_sync_status": "Queued",
        "custom_external_sync_message": f"取消请求已进入队列 {message.name}",
    }, update_modified=False)


def enqueue_test_order_cleanup(limit=50):
    """Queue exact cancellations for acknowledged ERPTEST orders only."""
    connections = frappe.get_all(
        "Jackyun Connection",
        filters={"enabled": 1, "outbound_enabled": 1, "outbound_test_mode": 1},
        fields=["name", "default_cancel_reason"],
    )
    queued = 0
    for connection in connections:
        generic = _generic_connection(connection.name)
        filters = {
            "platform": "jackyun", "resource": "Sales Order",
            "operation": "Create", "status": "Succeeded",
        }
        if generic:
            filters["connector_connection"] = generic
        else:
            filters["connection"] = connection.name
        messages = frappe.get_all(
            "Connector Outbound Message", filters=filters,
            fields=["name", "payload", "external_id", "reference_doctype", "reference_name"],
            order_by="acknowledged_at asc", limit=max(1, min(frappe.utils.cint(limit), 200)),
        )
        for source in messages:
            payload = frappe.parse_json(source.payload)
            online_no = ((payload or {}).get("tradeOrder") or {}).get("onlineTradeNo") or ""
            if not online_no.startswith("ERPTEST-") or not source.external_id:
                continue
            if frappe.db.exists(
                "Connector Outbound Message",
                {"platform": "jackyun", "resource": "Sales Order", "operation": "Cancel",
                 "external_id": source.external_id},
            ):
                continue
            enqueue_outbound_internal(
                connector_connection=generic,
                connection=None if generic else connection.name,
                resource="Sales Order", operation="Cancel",
                reference_doctype=source.reference_doctype,
                reference_name=source.reference_name,
                external_id=source.external_id,
                payload=cancel_payload(source.external_id, connection.default_cancel_reason or "420001"),
                source_system="erpnext",
                source_event_id=f"{source.name}:test-cleanup",
                operation_identity=f"ERPTEST-CANCEL:{source.external_id}",
            )
            queued += 1
    return {"queued": queued}

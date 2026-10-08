"""Generic connector controls: reconciliation, preview, alerts and outbound queue."""
from __future__ import annotations

import hashlib
import json

import frappe
from frappe import _

from channel_erp.integrations.adapter_registry import get_adapter
from channel_erp.integrations.connector_contracts import (
    ConnectorOperationError,
    ConnectorOperation,
    OperationBlockedError,
    OutboundStatus,
    UncertainOperationError,
    ensure_transition,
    normalize_operation,
    normalize_operation_result,
    normalize_probe_result,
)
from channel_erp.integrations.exception_classifier import classify_exception


RESOURCE_DOCTYPES = {
    "Company": "Company", "Department": "Department", "Category": "Jackyun Goods Category",
    "Sales Channel": "Jackyun Sales Channel", "Supplier": "Supplier", "Offline Customer": "Customer",
    "Warehouse": "Warehouse", "SKU": "Item", "Inventory": "Jackyun Inventory Snapshot",
    "Product Bundle": "Product Bundle", "Batch": "Batch", "Purchase Order": "Purchase Order",
    "Purchase Receipt": "Purchase Receipt", "Purchase Return": "Purchase Receipt",
    "Sales Order": "Sales Order", "Delivery Note": "Delivery Note", "Sales Return": "Delivery Note",
    "Stock Transfer": "Stock Entry", "Stock Movement": "Stock Entry", "Stocktake": "Stock Reconciliation",
}

DATE_FIELDS = {
    "Sales Order": "transaction_date", "Delivery Note": "posting_date",
    "Purchase Order": "transaction_date", "Purchase Receipt": "posting_date",
    "Stock Entry": "posting_date", "Stock Reconciliation": "posting_date",
}

SOURCE_DATE_FIELDS = {
    "Sales Order": ("tradeTime", "gmtCreate"),
    "Purchase Order": ("orderTime", "gmtCreate", "gmtModified"),
    "Purchase Receipt": ("inOutDate", "gmtCreate"),
    "Purchase Return": ("inOutDate", "gmtCreate"),
    "Delivery Note": ("inOutDate", "gmtCreate"),
    "Sales Return": ("inOutDate", "gmtCreate"),
    "Stock Transfer": ("inOutDate", "gmtCreate"),
    "Stock Movement": ("inOutDate", "gmtCreate"),
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _date_filters(doctype, company=None, from_date=None, to_date=None, sales_channel=None):
    filters = {}
    meta = frappe.get_meta(doctype)
    if company and meta.has_field("company"):
        filters["company"] = company
    date_field = DATE_FIELDS.get(doctype)
    if date_field and from_date and to_date:
        filters[date_field] = ["between", [from_date, to_date]]
    elif date_field and from_date:
        filters[date_field] = [">=", from_date]
    elif date_field and to_date:
        filters[date_field] = ["<=", to_date]
    if sales_channel:
        for fieldname in ("custom_jackyun_sales_channel", "custom_sales_channel"):
            if meta.has_field(fieldname):
                filters[fieldname] = sales_channel
                break
    return filters


def _source_counts(resource, doctype, company=None, from_date=None, to_date=None, sales_channel=None):
    """Compare unique source IDs with mappings inside the same business slice."""
    conditions = ["r.resource=%s", "r.processing_status='Succeeded'"]
    params = [resource]
    if company and resource in SOURCE_DATE_FIELDS:
        conditions.append("json_unquote(json_extract(r.raw_data, '$.companyName'))=%s")
        params.append(company)
    source_fields = SOURCE_DATE_FIELDS.get(resource)
    if source_fields and (from_date or to_date):
        raw_values = [f"nullif(json_unquote(json_extract(r.raw_data, '$.{field}')), 'null')" for field in source_fields]
        value = f"coalesce({','.join(raw_values)})"
        date_expr = f"date(case when {value} regexp '^[0-9]+$' then from_unixtime({value}/1000) else {value} end)"
        if from_date:
            conditions.append(f"{date_expr} >= %s")
            params.append(from_date)
        if to_date:
            conditions.append(f"{date_expr} <= %s")
            params.append(to_date)
    if sales_channel and resource in {"Sales Order", "Delivery Note", "Sales Return"}:
        conditions.append("coalesce(nullif(json_unquote(json_extract(r.raw_data, '$.shopName')),'null'), nullif(json_unquote(json_extract(r.raw_data, '$.channelName')),'null'))=%s")
        params.append(sales_channel)
    if resource == "Stocktake":
        conditions.append("json_unquote(json_extract(r.raw_data, '$.status')) = '5'")
    table = doctype.replace("`", "")
    mapping_resource = "Customer" if resource == "Offline Customer" else resource
    row = frappe.db.sql(
        f"""select count(*) source_count,
                   sum(case when m.name is not null then 1 else 0 end) mapped_count,
                   sum(case when m.name is not null and d.name is null then 1 else 0 end) dangling_count
              from (select distinct r.external_id from `tabJackyun Raw Record` r
                     where {' and '.join(conditions)}) src
              left join `tabExternal ID Mapping` m
                on m.platform='jackyun' and m.resource=%s and m.external_id=src.external_id
              left join `tab{table}` d on d.name=m.erpnext_name""",
        [*params, mapping_resource], as_dict=True,
    )[0]
    return int(row.source_count or 0), int(row.mapped_count or 0), int(row.dangling_count or 0)


@frappe.whitelist()
def run_reconciliation(connection=None, company=None, from_date=None, to_date=None, sales_channel=None, run_type="数据核对"):
    """Create a durable, filterable connector reconciliation snapshot."""
    started = frappe.utils.now_datetime()
    run = frappe.get_doc({
        "doctype": "Integration Reconciliation Run", "run_type": run_type,
        "status": "进行中", "connection": connection, "company": company,
        "from_date": from_date, "to_date": to_date, "sales_channel": sales_channel,
        "started_at": started,
    }).insert(ignore_permissions=True)
    details = []
    source_total = mapped_total = erp_total = differences = 0
    try:
        for resource, doctype in RESOURCE_DOCTYPES.items():
            if not frappe.db.exists("DocType", doctype):
                continue
            erp_count = frappe.db.count(doctype, _date_filters(doctype, company, from_date, to_date, sales_channel))
            raw_count, map_count, dangling = _source_counts(
                resource, doctype, company, from_date, to_date, sales_channel
            )
            source_total += raw_count
            mapped_total += map_count
            erp_total += erp_count
            missing_mapping = max(0, raw_count - map_count)
            diff = int(missing_mapping + dangling)
            differences += diff
            details.append({"resource": resource, "source": raw_count, "mapped": map_count,
                            "erp": erp_count, "missing_mapping": missing_mapping,
                            "dangling_mapping": int(dangling), "difference": diff})
        inventory = {"difference_count": 0, "sample": []}
        try:
            from channel_erp.inventory_cutover import inventory_difference_summary
            inventory = inventory_difference_summary(tolerance=0.01)
            differences += int(inventory.get("difference_count") or 0)
        except Exception as exc:
            inventory = {"error": str(exc)}
            differences += 1
        failed_raw = frappe.db.count("Jackyun Raw Record", {"processing_status": "Failed"})
        warnings = failed_raw
        run.db_set({
            "status": "通过" if not differences and not warnings else "有差异",
            "finished_at": frappe.utils.now_datetime(), "source_count": source_total,
            "mapped_count": mapped_total, "erp_count": erp_total,
            "difference_count": differences, "warning_count": warnings,
            "details": _json({"resources": details, "inventory": inventory, "failed_raw": failed_raw}),
        })
    except Exception as exc:
        run.db_set({"status": "失败", "finished_at": frappe.utils.now_datetime(), "details": _json({"error": str(exc)})})
        raise
    return frappe.get_doc(run.doctype, run.name).as_dict()


@frappe.whitelist()
def preview_pull(connection, resource, limit=20):
    """Read and validate source records without raw-record, mapping or ERP writes."""
    started = frappe.utils.now_datetime()
    doc = frappe.get_doc({"doctype":"Integration Reconciliation Run", "run_type":"同步预演",
                          "status":"进行中", "connection":connection, "started_at":started}).insert(ignore_permissions=True)
    samples, failures, pulled = [], [], 0
    try:
        adapter = get_adapter(frappe.get_doc("Jackyun Connection", connection))
        for raw in adapter.pull(resource):
            if pulled >= max(1, min(frappe.utils.cint(limit), 100)):
                break
            pulled += 1
            try:
                mapped = adapter.transform(resource, raw)
                samples.append({"external_id": adapter.get_external_id(resource, raw),
                                "target_doctype": mapped.get("doctype"),
                                "target_fields": sorted(k for k in mapped if k not in {"doctype", "external_id"})})
            except Exception as exc:
                failures.append({"error": str(exc), **classify_exception(exc)})
        doc.db_set({"status":"通过" if not failures else "有差异", "finished_at":frappe.utils.now_datetime(),
                    "source_count":pulled, "difference_count":len(failures), "warning_count":len(failures),
                    "details":_json({"resource":resource, "read_only":True, "samples":samples, "failures":failures})})
    except Exception as exc:
        doc.db_set({"status":"失败", "finished_at":frappe.utils.now_datetime(), "difference_count":1,
                    "details":_json({"resource":resource, "read_only":True, "error":str(exc)})})
        raise
    return frappe.get_doc(doc.doctype, doc.name).as_dict()


def daily_consistency_report():
    """Create daily runs for each configured company and alert only on differences."""
    today = frappe.utils.today()
    companies = frappe.get_all("Company", filters={"name":["in", [
        "武汉医麦德医疗用品有限公司", "咸宁医麦德实业有限公司"]]}, pluck="name")
    results = [run_reconciliation(company=company, from_date=today, to_date=today, run_type="每日一致性") for company in companies]
    for result in results:
        if result.status == "通过":
            continue
        for user in _admin_users():
            frappe.get_doc({"doctype":"Notification Log", "type":"Alert", "for_user":user,
                            "subject":f"接口每日一致性告警：{result.company or '全部公司'}",
                            "document_type":"Integration Reconciliation Run", "document_name":result.name,
                            "email_content":f"差异 {result.difference_count}，告警 {result.warning_count}。请打开核对记录查看明细。"}).insert(ignore_permissions=True)
    frappe.db.commit()
    return results


def _admin_users():
    from frappe.utils.user import get_system_managers
    return get_system_managers(only_name=True) or ["Administrator"]


def payload_digest(payload):
    """Stable SHA-256 digest independent of JSON key order."""
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def build_idempotency_key(
    connection_identity,
    resource,
    operation,
    reference_doctype=None,
    reference_name=None,
    external_id=None,
    source_event_id=None,
    payload=None,
    operation_identity=None,
):
    """Build a stable operation key.

    Create/Cancel identity deliberately excludes payload so a serialization
    change cannot create a second remote entity. Update/Audit/Query include the
    payload digest unless the caller supplies an explicit operation identity.
    """
    operation = normalize_operation(operation)
    identity = operation_identity or source_event_id or external_id
    if not identity and reference_doctype and reference_name:
        identity = f"{reference_doctype}:{reference_name}"
    if not identity:
        raise OperationBlockedError(
            "出站操作缺少稳定身份：请提供来源事件ID、外部ID或ERPNext单据引用"
        )
    parts = [str(connection_identity), str(resource), operation.value, str(identity)]
    if not operation_identity and operation in {
        ConnectorOperation.UPDATE,
        ConnectorOperation.AUDIT,
        ConnectorOperation.QUERY,
    }:
        parts.append(payload_digest(payload or {}))
    return hashlib.sha256(_json(parts).encode("utf-8")).hexdigest(), str(identity)


def is_echo_source(source_system, destination_platform):
    source = frappe.utils.cstr(source_system).strip().lower()
    destination = frappe.utils.cstr(destination_platform).strip().lower()
    return bool(source and destination and source == destination)


@frappe.whitelist()
def enqueue_outbound(
    connection=None,
    resource=None,
    operation=None,
    reference_doctype=None,
    reference_name=None,
    payload=None,
    external_id=None,
    connector_connection=None,
    source_system="erpnext",
    source_event_id=None,
    operation_identity=None,
    max_attempts=5,
    origin_message=None,
):
    """Persist an idempotent operation. No network call occurs here."""
    if not getattr(frappe.flags, "connector_internal_enqueue", False) and not frappe.has_permission("Connector Outbound Message", "create"):
        frappe.throw(_("无权创建连接器出站消息"), frappe.PermissionError)
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    if not isinstance(payload, dict):
        frappe.throw("出站 payload 必须是 JSON 对象")
    if not connector_connection and not connection:
        frappe.throw("通用连接与兼容连接至少填写一个")

    descriptor = _connection_descriptor(
        connector_connection=connector_connection,
        legacy_connection=connection,
        load_runtime=False,
    )
    connection_identity = connector_connection or f"Jackyun Connection:{connection}"
    key, stable_identity = build_idempotency_key(
        connection_identity,
        resource,
        operation,
        reference_doctype,
        reference_name,
        external_id,
        source_event_id,
        payload,
        operation_identity,
    )
    existing = frappe.db.get_value(
        "Connector Outbound Message", {"idempotency_key": key}, "name"
    )
    if existing:
        return frappe.get_doc("Connector Outbound Message", existing).as_dict()

    blocked_echo = is_echo_source(source_system, descriptor["platform"])
    doc = frappe.get_doc(
        {
            "doctype": "Connector Outbound Message",
            "connector_connection": connector_connection,
            "connection": connection,
            "platform": descriptor["platform"],
            "adapter_key": descriptor["adapter_key"],
            "resource": resource,
            "operation": normalize_operation(operation).value,
            "status": "Blocked" if blocked_echo else "Preflight",
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "external_id": external_id,
            "operation_identity": stable_identity,
            "idempotency_key": key,
            "payload_digest": payload_digest(payload),
            "source_system": source_system,
            "source_event_id": source_event_id,
            "origin_message": origin_message,
            "payload": _json(payload),
            "max_attempts": max(1, min(frappe.utils.cint(max_attempts) or 5, 20)),
            "error_message": (
                "来源平台与目标平台相同，已阻止同步回环" if blocked_echo else None
            ),
        }
    )
    return doc.insert(ignore_permissions=True).as_dict()


def enqueue_outbound_internal(**kwargs):
    """Trusted DocType-event entry point; never exposed as a whitelisted API."""
    previous = getattr(frappe.flags, "connector_internal_enqueue", False)
    frappe.flags.connector_internal_enqueue = True
    try:
        return enqueue_outbound(**kwargs)
    finally:
        frappe.flags.connector_internal_enqueue = previous


def _update_reference_status(msg):
    """Mirror durable queue state onto the referenced document when supported."""
    try:
        if not msg.reference_doctype or not msg.reference_name:
            return
        if not frappe.db.exists(msg.reference_doctype, msg.reference_name):
            return
        meta = frappe.get_meta(msg.reference_doctype)
        values = {}
        status = msg.status
        if msg.operation == "Cancel" and status == "Succeeded":
            status = "Cancelled"
        if meta.has_field("custom_external_sync_status"):
            values["custom_external_sync_status"] = status
        if meta.has_field("custom_external_sync_last_at"):
            values["custom_external_sync_last_at"] = frappe.utils.now_datetime()
        if meta.has_field("custom_external_sync_message"):
            values["custom_external_sync_message"] = msg.error_message or ""
        if msg.operation == "Create" and msg.external_id:
            if meta.has_field("custom_jackyun_trade_no"):
                values["custom_jackyun_trade_no"] = msg.external_id
        if values:
            frappe.db.set_value(
                msg.reference_doctype,
                msg.reference_name,
                values,
                update_modified=False,
            )
    except Exception:
        # Mirroring is observability only; it must never alter queue semantics.
        return


def _auto_audit_settings(msg):
    """Resolve the Jackyun Connection audit switches behind a queue message."""
    fields = ["auto_audit_outbound", "audit_operator", "outbound_test_mode"]
    if msg.get("connector_connection"):
        credential = frappe.db.get_value(
            "Connector Connection",
            msg.connector_connection,
            ["credential_doctype", "credential_name"],
            as_dict=True,
        )
        if (
            credential
            and credential.credential_doctype == "Jackyun Connection"
            and credential.credential_name
        ):
            return frappe.db.get_value(
                "Jackyun Connection", credential.credential_name, fields, as_dict=True
            )
        return None
    if msg.get("connection"):
        return frappe.db.get_value(
            "Jackyun Connection", msg.connection, fields, as_dict=True
        )
    return None


def _maybe_enqueue_auto_audit(msg):
    """Queue an oms.trade.audit.pass push once a Sales Order Create is acknowledged.

    Idempotency relies on the stable ``AUTO-AUDIT:{tradeNo}`` operation
    identity, so the queue worker, a probe recovery and any future retry path
    all converge on a single Audit message per remote trade.
    """
    if msg.resource != "Sales Order" or msg.operation != "Create":
        return
    if msg.status != "Succeeded":
        return
    trade_no = frappe.utils.cstr(msg.external_id).strip()
    if not trade_no:
        return
    try:
        settings = _auto_audit_settings(msg)
        if not settings or not frappe.utils.cint(settings.auto_audit_outbound):
            return
        if frappe.utils.cint(settings.outbound_test_mode):
            # Test orders are cleaned up by cancellation; auditing them would
            # drive the throwaway order into JackYun WMS processing.
            return
        payload = frappe.parse_json(msg.payload or "{}") or {}
        online_no = frappe.utils.cstr(
            (payload.get("tradeOrder") or {}).get("onlineTradeNo") or ""
        )
        if online_no.startswith("ERPTEST-"):
            return
        if frappe.db.exists(
            "Connector Outbound Message",
            {
                "platform": msg.platform or "jackyun",
                "resource": "Sales Order",
                "operation": "Cancel",
                "external_id": trade_no,
                "status": "Succeeded",
            },
        ):
            return
        from channel_erp.integrations.jackyun_outbound import audit_payload

        enqueue_outbound_internal(
            connector_connection=msg.get("connector_connection"),
            connection=None if msg.get("connector_connection") else msg.get("connection"),
            resource="Sales Order",
            operation="Audit",
            reference_doctype=msg.reference_doctype,
            reference_name=msg.reference_name,
            external_id=trade_no,
            payload=audit_payload(trade_no, settings.audit_operator),
            source_system="erpnext",
            source_event_id=(
                f"Sales Order:{msg.reference_name or trade_no}:auto-audit:{trade_no}"
            ),
            operation_identity=f"AUTO-AUDIT:{trade_no}",
            origin_message=msg.name,
        )
    except Exception:
        # The Create already succeeded; audit chaining is best-effort and must
        # never disturb the queue's receipt handling.
        try:
            frappe.log_error(
                title="吉客云自动审核入队失败",
                message=frappe.get_traceback(),
            )
        except Exception:
            pass


def evaluate_conflict(policy, source_modified_at, erp_modified_at, last_synced_at):
    """Return the deterministic action when both sides changed after last sync."""
    if not source_modified_at or not erp_modified_at or not last_synced_at:
        return "No Conflict"
    source = frappe.utils.get_datetime(source_modified_at)
    erp = frappe.utils.get_datetime(erp_modified_at)
    baseline = frappe.utils.get_datetime(last_synced_at)
    if source <= baseline or erp <= baseline:
        return "No Conflict"
    if policy == "吉客云优先":
        return "Source Wins"
    if policy == "ERPNext优先":
        return "ERPNext Wins"
    if policy == "最新修改优先":
        return "Source Wins" if source >= erp else "ERPNext Wins"
    return "Manual Review"


def _connection_descriptor(
    connector_connection=None, legacy_connection=None, load_runtime=True
):
    if connector_connection:
        generic = frappe.get_doc("Connector Connection", connector_connection)
        if not frappe.utils.cint(generic.enabled):
            raise OperationBlockedError(f"通用连接 {generic.name} 已停用")
        platform = frappe.utils.cstr(generic.platform).strip().lower()
        adapter_key = (
            frappe.utils.cstr(generic.adapter_key).strip().lower() or platform
        )
        runtime = generic
        if load_runtime and generic.credential_doctype and generic.credential_name:
            runtime = frappe.get_doc(
                generic.credential_doctype, generic.credential_name
            )
        if load_runtime:
            # Adapter registry historically reads ``platform`` from the runtime
            # credential. This is an in-memory override and is never saved.
            runtime.platform = adapter_key
        allowed = generic.enabled_operations()
        return {
            "platform": platform,
            "adapter_key": adapter_key,
            "runtime": runtime if load_runtime else None,
            "allowed_operations": allowed,
            "source_marker": generic.source_marker or "erpnext",
        }

    if not legacy_connection:
        raise OperationBlockedError("消息没有可用连接")
    runtime = frappe.get_doc("Jackyun Connection", legacy_connection) if load_runtime else None
    platform = "jackyun"
    if runtime:
        platform = frappe.utils.cstr(runtime.get("platform") or "jackyun").lower()
    return {
        "platform": platform,
        "adapter_key": platform,
        "runtime": runtime,
        # Legacy messages remain loadable, but the adapter must still explicitly
        # opt in before any write/query can execute.
        "allowed_operations": None,
        "source_marker": "erpnext",
    }


def _message_context(msg, descriptor):
    return {
        "message": msg.name,
        "platform": descriptor["platform"],
        "source_system": msg.get("source_system"),
        "source_event_id": msg.get("source_event_id"),
        "reference_doctype": msg.get("reference_doctype"),
        "reference_name": msg.get("reference_name"),
        # Adapters should put this marker into outbound payload metadata when
        # the remote contract supports it. Inbound event handlers can then
        # preserve it as ``source_system`` and avoid echoing the same event.
        "outbound_source_marker": descriptor.get("source_marker") or "erpnext",
    }


def _preflight(msg, adapter, descriptor, payload):
    operation = normalize_operation(msg.operation).value
    allowed = descriptor.get("allowed_operations")
    if allowed is not None and operation not in allowed:
        raise OperationBlockedError(
            f"连接 {msg.connector_connection} 未授权 {operation} 能力"
        )
    result = adapter.preflight_operation(
        msg.resource, operation, payload, _message_context(msg, descriptor)
    )
    msg.preflight_data = _json(
        {
            "allowed": bool(result.allowed),
            "reason": result.reason,
            "context": dict(result.context or {}),
        }
    )
    if not result.allowed:
        raise OperationBlockedError(result.reason or "适配器预检未通过")
    return dict(result.normalized_payload or payload)


def _set_status(msg, target):
    msg.status = ensure_transition(msg.status, target)


def _mark_uncertain(msg, exc, now):
    _set_status(msg, OutboundStatus.UNCERTAIN.value)
    msg.uncertain_since = msg.uncertain_since or now
    msg.next_retry_at = None
    msg.error_message = str(exc)
    context_id = getattr(exc, "context_id", None)
    response = getattr(exc, "response", None)
    if context_id:
        msg.response_context_id = context_id
    if response is not None:
        msg.response_data = _json(response)


def _dependency_decision(msg):
    """Return ``(ready, reason, terminal)`` for a serial predecessor.

    This check deliberately runs before adapter resolution so a dependent
    message can never reach preflight or transport while its predecessor is
    unresolved. ``Uncertain`` remains a wait state because a probe may still
    prove the predecessor succeeded.
    """
    origin = msg.get("origin_message")
    if not origin:
        return True, "", False
    if origin == msg.name:
        return False, "串行依赖不能引用消息自身", True
    upstream = frappe.db.get_value(
        "Connector Outbound Message",
        origin,
        ["status", "origin_message"],
        as_dict=True,
    )
    if not upstream:
        return False, f"上游消息 {origin} 不存在", True
    if upstream.status == OutboundStatus.SUCCEEDED.value:
        return True, "", False
    if upstream.status in {
        OutboundStatus.PREFLIGHT.value,
        OutboundStatus.PENDING.value,
        OutboundStatus.PROCESSING.value,
        OutboundStatus.RETRYING.value,
        OutboundStatus.UNCERTAIN.value,
    }:
        return False, f"等待上游消息 {origin}（{upstream.status}）", False
    return False, f"上游消息 {origin} 已处于 {upstream.status}，下游操作被阻断", True


def process_outbound_queue(limit=50):
    """Execute due operations through capability-aware adapters.

    An uncertain Create is intentionally excluded from this query and can only
    continue through ``probe_uncertain_outbound``.
    """
    now = frappe.utils.now_datetime()
    rows = frappe.db.sql(
        """select name from `tabConnector Outbound Message`
            where status in ('Preflight','Pending','Retrying')
              and (next_retry_at is null or next_retry_at <= %s)
            order by creation asc limit %s""",
        (now, max(1, min(frappe.utils.cint(limit), 200))), pluck=True,
    )
    stats = {"processed":0, "succeeded":0, "retrying":0, "failed":0,
             "blocked":0, "conflict":0, "uncertain":0, "waiting":0}
    for name in rows:
        msg = frappe.get_doc("Connector Outbound Message", name)
        try:
            dependency_ready, dependency_reason, dependency_terminal = (
                _dependency_decision(msg)
            )
            if not dependency_ready:
                if dependency_terminal:
                    _set_status(msg, "Blocked")
                    msg.next_retry_at = None
                    stats["blocked"] += 1
                else:
                    # Preserve the current executable state and defer polling;
                    # no attempt is consumed while the predecessor is pending.
                    msg.next_retry_at = frappe.utils.add_to_date(now, minutes=5)
                    stats["waiting"] += 1
                msg.error_message = dependency_reason
                msg.save(ignore_permissions=True)
                stats["processed"] += 1
                frappe.db.commit()
                continue
            descriptor = _connection_descriptor(
                msg.get("connector_connection"), msg.get("connection")
            )
            msg.platform = descriptor["platform"]
            msg.adapter_key = descriptor["adapter_key"]
            if is_echo_source(msg.get("source_system"), descriptor["platform"]):
                raise OperationBlockedError(
                    "来源平台与目标平台相同，已阻止同步回环"
                )
            adapter = get_adapter(descriptor["runtime"])
            payload = frappe.parse_json(msg.payload)
            payload = _preflight(msg, adapter, descriptor, payload)
            sync_meta = payload.pop("_sync_meta", {}) if isinstance(payload, dict) else {}
            action = evaluate_conflict(
                descriptor["runtime"].get("conflict_policy") or "人工复核",
                sync_meta.get("source_modified_at"), sync_meta.get("erp_modified_at"),
                sync_meta.get("last_synced_at"),
            )
            msg.conflict_status = action
            if action in {"Source Wins", "Manual Review"}:
                if msg.status == "Preflight":
                    _set_status(msg, "Pending")
                _set_status(msg, "Conflict")
                msg.conflict_details = "两端均在最近同步后发生修改；已按连接冲突规则停止推送。"
                stats["conflict"] += 1
                msg.save(ignore_permissions=True)
                _update_reference_status(msg)
                stats["processed"] += 1
                frappe.db.commit()
                continue

            if msg.status in {"Preflight", "Retrying"}:
                _set_status(msg, "Pending")
            _set_status(msg, "Processing")
            msg.last_attempt_at = now
            msg.attempts = frappe.utils.cint(msg.attempts) + 1
            context = _message_context(msg, descriptor)
            msg.request_data = _json(
                {
                    "resource": msg.resource,
                    "operation": msg.operation,
                    "payload": payload,
                    "idempotency_key": msg.idempotency_key,
                    "context": context,
                }
            )
            msg.save(ignore_permissions=True)
            response = adapter.execute_operation(
                msg.resource,
                msg.operation,
                payload,
                idempotency_key=msg.idempotency_key,
                context=context,
            )
            result = normalize_operation_result(response)
            msg.response_data = _json(result.response or {})
            msg.response_context_id = result.context_id
            if result.external_id:
                msg.external_id = result.external_id
            if result.outcome == "Uncertain":
                _mark_uncertain(
                    msg,
                    UncertainOperationError(
                        "适配器返回不确定结果",
                        context_id=result.context_id,
                        response=result.response,
                    ),
                    now,
                )
                stats["uncertain"] += 1
            elif result.outcome == "Succeeded":
                _set_status(msg, "Succeeded")
                msg.acknowledged_at = frappe.utils.now_datetime()
                msg.error_message = None
                msg.next_retry_at = None
                stats["succeeded"] += 1
            else:
                raise ConnectorOperationError(
                    f"适配器返回失败结果：{result.outcome}",
                    context_id=result.context_id,
                    response=result.response,
                )
        except (NotImplementedError, OperationBlockedError) as exc:
            if msg.status == "Preflight":
                _set_status(msg, "Blocked")
            elif msg.status in {"Pending", "Retrying", "Processing"}:
                _set_status(msg, "Blocked")
            else:
                msg.status = "Blocked"
            msg.error_message = str(exc)
            stats["blocked"] += 1
        except UncertainOperationError as exc:
            _mark_uncertain(msg, exc, now)
            stats["uncertain"] += 1
        except Exception as exc:
            result = classify_exception(exc)
            msg.error_message = str(exc)
            # Only transport failures make a Create result genuinely
            # ambiguous. A JackYun business rejection (invalid time, missing
            # SKU, validation error, etc.) definitively means the order was not
            # created and must be shown as Failed instead of Uncertain.
            error_category = str(getattr(exc, "category", "") or "").upper()
            error_text = str(exc).lower()
            untyped_transport_failure = (
                not error_category
                and result.get("retryable")
                and any(
                    marker in error_text
                    for marker in ("timeout", "timed out", "connection", "network", "http 5", "429")
                )
            )
            create_is_ambiguous = (
                msg.operation == ConnectorOperation.CREATE.value
                and msg.status == "Processing"
                and (
                    error_category in {"NETWORK", "TIMEOUT", "CONNECTION"}
                    or untyped_transport_failure
                )
            )
            if create_is_ambiguous:
                _mark_uncertain(msg, exc, now)
                stats["uncertain"] += 1
            elif result.get("retryable") and msg.attempts < msg.max_attempts:
                _set_status(msg, "Retrying")
                msg.next_retry_at = frappe.utils.add_to_date(now, minutes=min(360, 2 ** msg.attempts * 5))
                stats["retrying"] += 1
            else:
                if msg.status in {"Preflight", "Pending", "Processing", "Retrying"}:
                    _set_status(msg, "Failed")
                else:
                    msg.status = "Failed"
                stats["failed"] += 1
        # The Processing state was already persisted above. Use a direct
        # internal update for the final receipt so Frappe's optimistic-lock
        # timestamp does not reject the second write in the same queue cycle.
        db_update = getattr(msg, "db_update", None)
        if callable(db_update):
            db_update()
        else:
            msg.save(ignore_permissions=True)
        _update_reference_status(msg)
        _maybe_enqueue_auto_audit(msg)
        stats["processed"] += 1
        frappe.db.commit()
    return stats


@frappe.whitelist()
def probe_uncertain_outbound(name):
    """Probe one uncertain message; never repeats the write itself."""
    msg = frappe.get_doc("Connector Outbound Message", name)
    msg.check_permission("write")
    if msg.status != OutboundStatus.UNCERTAIN.value:
        frappe.throw("仅 Uncertain 消息允许执行 Probe")
    descriptor = _connection_descriptor(
        msg.get("connector_connection"), msg.get("connection")
    )
    adapter = get_adapter(descriptor["runtime"])
    payload = frappe.parse_json(msg.payload)
    now = frappe.utils.now_datetime()
    msg.probe_attempts = frappe.utils.cint(msg.probe_attempts) + 1
    msg.last_probe_at = now
    try:
        result = normalize_probe_result(
            adapter.probe_operation(
                msg.resource,
                msg.operation,
                idempotency_key=msg.idempotency_key,
                external_id=msg.external_id,
                payload=payload,
                context=_message_context(msg, descriptor),
            )
        )
        msg.probe_data = _json(result.response or {"outcome": result.outcome})
        if result.context_id:
            msg.response_context_id = result.context_id
        if result.external_id:
            msg.external_id = result.external_id
        if result.outcome == "Found":
            _set_status(msg, "Succeeded")
            msg.acknowledged_at = now
            msg.error_message = None
        elif result.outcome == "Not Found":
            # A definitive absence makes one idempotent retry safe.
            _set_status(msg, "Pending")
            msg.next_retry_at = now
            msg.error_message = "Probe 已确认远端不存在，可安全重试"
        else:
            msg.error_message = "Probe 尚不能确认远端结果；保持 Uncertain"
    except Exception as exc:
        msg.error_message = f"Probe 失败：{exc}"
        msg.probe_data = _json({"error": str(exc)})
    msg.save(ignore_permissions=True)
    _update_reference_status(msg)
    _maybe_enqueue_auto_audit(msg)
    return msg.as_dict()

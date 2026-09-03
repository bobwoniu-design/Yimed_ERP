import frappe
from frappe.model.document import Document

from channel_erp.integrations.exception_classifier import classify_exception
from frappe.utils import cint


class JackyunRawRecord(Document):
    pass


RECOVERABLE_DOCUMENT_RESOURCES = {
    "Sales Order",
    "Delivery Note",
    "Sales Return",
    "Purchase Order",
    "Purchase Receipt",
    "Purchase Return",
    "Stock Transfer",
    "Stock Movement",
}


def _mapping_target(resource, external_id):
    mapping = frappe.db.get_value(
        "External ID Mapping",
        {"platform": "jackyun", "resource": resource, "external_id": str(external_id)},
        ["erpnext_doctype", "erpnext_name"],
        as_dict=True,
    )
    if not mapping or not frappe.db.exists(mapping.erpnext_doctype, mapping.erpnext_name):
        return None
    return mapping


def _mark_entity_pending_as_succeeded(resource, external_id):
    frappe.db.set_value(
        "Jackyun Raw Record",
        {
            "resource": resource,
            "external_id": str(external_id),
            "processing_status": "Pending",
        },
        {
            "processed": 1,
            "processed_at": frappe.utils.now(),
            "processing_status": "Succeeded",
            "error_message": None,
        },
        update_modified=False,
    )


def recover_pending_records(resources=None, limit=1000, min_age_minutes=10):
    """Resolve interrupted raw rows and retry only genuinely unmapped documents."""
    resources = set(resources or [])
    limit = max(1, min(cint(limit) or 1000, 10000))
    cutoff = frappe.utils.add_to_date(
        frappe.utils.now_datetime(), minutes=-max(1, cint(min_age_minutes) or 10)
    )
    filters = {
        "processing_status": "Pending",
        "creation": ["<", cutoff],
    }
    if resources:
        filters["resource"] = ["in", list(resources)]
    rows = frappe.get_all(
        "Jackyun Raw Record",
        filters=filters,
        fields=["name", "resource", "external_id"],
        order_by="creation desc",
        limit_page_length=limit,
    )
    entities = {}
    for row in rows:
        entities.setdefault((row.resource, str(row.external_id)), row.name)

    summary = {
        "examined_entities": len(entities),
        "resolved_existing": 0,
        "retried": 0,
        "succeeded": 0,
        "failed": 0,
        "left_for_manual_rule": 0,
        "errors": [],
    }
    for index, ((resource, external_id), name) in enumerate(entities.items(), start=1):
        if _mapping_target(resource, external_id):
            _mark_entity_pending_as_succeeded(resource, external_id)
            summary["resolved_existing"] += 1
        elif resource in RECOVERABLE_DOCUMENT_RESOURCES:
            result = retry_processing(name)
            summary["retried"] += 1
            if result.get("ok"):
                summary["succeeded"] += 1
                if _mapping_target(resource, external_id):
                    _mark_entity_pending_as_succeeded(resource, external_id)
            else:
                summary["failed"] += 1
                summary["errors"].append(
                    {"name": name, "resource": resource, "error": result.get("error")}
                )
        else:
            # Batch and other protected master data require an explicit business
            # decision when no mapping exists; keep their raw payload visible.
            summary["left_for_manual_rule"] += 1
        if index % 50 == 0:
            frappe.db.commit()
    return summary


def verify_and_recover_sync_run(sync_log_name, resource):
    """Check source IDs from one completed run and immediately retry mapping gaps."""
    summary = {"examined": 0, "retried": 0, "succeeded": 0, "failed": 0, "errors": []}
    if resource not in RECOVERABLE_DOCUMENT_RESOURCES:
        return summary
    rows = frappe.get_all(
        "Jackyun Raw Record",
        filters={
            "sync_log": sync_log_name,
            "resource": resource,
            "processing_status": ["in", ["Pending", "Succeeded"]],
        },
        fields=["name", "external_id", "processing_status"],
        order_by="creation desc",
        limit_page_length=1000,
    )
    seen = set()
    for row in rows:
        external_id = str(row.external_id)
        if external_id in seen:
            continue
        seen.add(external_id)
        summary["examined"] += 1
        if _mapping_target(resource, external_id):
            if row.processing_status == "Pending":
                _mark_entity_pending_as_succeeded(resource, external_id)
            continue
        result = retry_processing(row.name)
        summary["retried"] += 1
        if result.get("ok"):
            summary["succeeded"] += 1
            if _mapping_target(resource, external_id):
                _mark_entity_pending_as_succeeded(resource, external_id)
        else:
            summary["failed"] += 1
            summary["errors"].append({"name": row.name, "error": result.get("error")})
    return summary


def cleanup_duplicate_snapshots(batch_size=5000, retention_days=90, dry_run=False):
    """Remove redundant raw payloads without touching mappings or ERPNext documents.

    Keeps every failure, unresolved pending payload, the newest successful copy of
    every exact payload, and the newest successful payload for every external ID.
    Distinct successful history is retained for ``retention_days``.
    """
    batch_size = max(100, min(cint(batch_size) or 5000, 20000))
    retention_days = max(cint(retention_days) or 90, 1)

    frappe.db.sql("drop temporary table if exists tmp_jackyun_success_snapshot")
    frappe.db.sql("drop temporary table if exists tmp_jackyun_latest_entity")
    frappe.db.sql("drop temporary table if exists tmp_jackyun_raw_delete")
    frappe.db.sql(
        """
        create temporary table tmp_jackyun_success_snapshot (
            resource varchar(140) not null,
            external_id varchar(140) not null,
            content_hash varchar(140) not null,
            keep_creation datetime(6) not null,
            primary key (resource, external_id, content_hash)
        ) engine=InnoDB
        """
    )
    frappe.db.sql(
        """
        create temporary table tmp_jackyun_latest_entity (
            resource varchar(140) not null,
            external_id varchar(140) not null,
            keep_creation datetime(6) not null,
            primary key (resource, external_id)
        ) engine=InnoDB
        """
    )
    frappe.db.sql(
        """
        create temporary table tmp_jackyun_raw_delete (
            name varchar(140) not null primary key,
            reason varchar(40) not null
        ) engine=InnoDB
        """
    )
    frappe.db.sql(
        """
        insert into tmp_jackyun_success_snapshot
            (resource, external_id, content_hash, keep_creation)
        select resource, external_id, content_hash, max(creation)
          from `tabJackyun Raw Record`
         where processing_status='Succeeded'
         group by resource, external_id, content_hash
        """
    )
    frappe.db.sql(
        """
        insert into tmp_jackyun_latest_entity (resource, external_id, keep_creation)
        select resource, external_id, max(creation)
          from `tabJackyun Raw Record`
         where processing_status='Succeeded'
         group by resource, external_id
        """
    )
    frappe.db.sql(
        """
        insert ignore into tmp_jackyun_raw_delete (name, reason)
        select raw.name, 'duplicate_success'
          from `tabJackyun Raw Record` raw
          join tmp_jackyun_success_snapshot keep
            on keep.resource=raw.resource
           and keep.external_id=raw.external_id
           and keep.content_hash=raw.content_hash
         where raw.processing_status='Succeeded'
           and raw.creation < keep.keep_creation
        """
    )
    frappe.db.sql(
        """
        insert ignore into tmp_jackyun_raw_delete (name, reason)
        select raw.name, 'resolved_pending'
          from `tabJackyun Raw Record` raw
          join tmp_jackyun_success_snapshot keep
            on keep.resource=raw.resource
           and keep.external_id=raw.external_id
           and keep.content_hash=raw.content_hash
         where raw.processing_status='Pending'
        """
    )
    frappe.db.sql(
        """
        insert ignore into tmp_jackyun_raw_delete (name, reason)
        select raw.name, 'expired_history'
          from `tabJackyun Raw Record` raw
          join tmp_jackyun_latest_entity keep
            on keep.resource=raw.resource
           and keep.external_id=raw.external_id
         where raw.processing_status='Succeeded'
           and raw.creation < date_sub(now(), interval %s day)
           and raw.creation < keep.keep_creation
        """,
        retention_days,
    )

    candidates = dict(
        frappe.db.sql(
            "select reason, count(*) from tmp_jackyun_raw_delete group by reason"
        )
    )
    total = sum(candidates.values())
    if dry_run:
        return {"dry_run": True, "candidates": candidates, "total": total}

    deleted = 0
    while True:
        names = frappe.db.sql(
            "select name from tmp_jackyun_raw_delete limit %s",
            batch_size,
            pluck=True,
        )
        if not names:
            break
        frappe.db.delete("Jackyun Raw Record", {"name": ["in", names]})
        frappe.db.sql(
            "delete from tmp_jackyun_raw_delete where name in %(names)s",
            {"names": names},
        )
        deleted += len(names)
        frappe.db.commit()

    return {"dry_run": False, "candidates": candidates, "deleted": deleted}


def scheduled_cleanup_duplicate_snapshots():
    return cleanup_duplicate_snapshots(batch_size=5000, retention_days=90, dry_run=False)


@frappe.whitelist()
def retry_processing(name):
    """Re-run transform/upsert from the preserved payload without another API pull."""
    record = frappe.get_doc("Jackyun Raw Record", name)
    record.check_permission("write")
    connection_name = None
    if record.sync_log and frappe.db.exists("Jackyun Sync Log", record.sync_log):
        connection_name = frappe.db.get_value("Jackyun Sync Log", record.sync_log, "connection")
    if not connection_name:
        # Old raw payloads can legitimately outlive retained sync logs. Reuse the
        # single enabled connection instead of making the preserved payload unretryable.
        enabled = frappe.get_all("Jackyun Connection", filters={"enabled": 1}, pluck="name")
        if len(enabled) != 1:
            frappe.throw("原同步日志已清理，且无法唯一确定当前启用连接")
        connection_name = enabled[0]
    connection = frappe.get_doc("Jackyun Connection", connection_name)

    from channel_erp.integrations.adapter_registry import get_adapter

    adapter = get_adapter(connection)
    raw = record.raw_data
    if isinstance(raw, str):
        raw = frappe.parse_json(raw)
    if not isinstance(raw, dict):
        frappe.throw("原始数据不是 JSON 对象，无法重试")

    record.retry_count = frappe.utils.cint(record.retry_count) + 1
    record.last_retry_at = frappe.utils.now()
    try:
        mapped = adapter.transform(record.resource, raw)
        erpnext_name, result = adapter.upsert(record.resource, mapped)
        if record.resource == "SKU":
            adapter.remember_sku_aliases(raw, erpnext_name)
        record.processed = 1
        record.processed_at = frappe.utils.now()
        record.processing_status = "Succeeded"
        record.error_message = None
        record.failure_category = None
        record.retryable = 0
        record.handling_suggestion = None
        frappe.db.set_value(
            record.doctype, record.name,
            {"retry_count": record.retry_count, "last_retry_at": record.last_retry_at,
             "processed": 1, "processed_at": record.processed_at,
             "processing_status": "Succeeded", "error_message": None,
             "failure_category": None, "retryable": 0, "handling_suggestion": None},
        )
        return {"ok": True, "erpnext_name": erpnext_name, "result": result}
    except Exception as exc:
        record.processed = 0
        record.processing_status = "Failed"
        record.error_message = str(exc)
        record.update(classify_exception(exc))
        frappe.db.set_value(
            record.doctype, record.name,
            {"retry_count": record.retry_count, "last_retry_at": record.last_retry_at,
             "processed": 0, "processing_status": "Failed",
             "error_message": record.error_message,
             "failure_category": record.failure_category,
             "retryable": record.retryable,
             "handling_suggestion": record.handling_suggestion},
        )
        return {"ok": False, "error": str(exc)}


@frappe.whitelist()
def refresh_failure_classifications(resource=None):
    """Re-evaluate failed-row metadata without retrying or changing business data."""
    frappe.only_for("System Manager")
    filters = {"processing_status": "Failed"}
    if resource:
        filters["resource"] = resource
    rows = frappe.get_all(
        "Jackyun Raw Record",
        filters=filters,
        fields=["name", "error_message"],
    )
    retryable = 0
    categories = {}
    for row in rows:
        classification = classify_exception(row.error_message)
        frappe.db.set_value(
            "Jackyun Raw Record",
            row.name,
            classification,
            update_modified=False,
        )
        category = classification["failure_category"]
        categories[category] = categories.get(category, 0) + 1
        retryable += frappe.utils.cint(classification["retryable"])
    return {
        "updated": len(rows),
        "retryable": retryable,
        "categories": categories,
    }


@frappe.whitelist()
def retry_failed_records(resources=None, limit=100):
    """Retry a bounded set of classified failures and return a compact summary."""
    frappe.only_for("System Manager")
    if isinstance(resources, str):
        resources = frappe.parse_json(resources)
    resources = list(resources or [])
    filters = {"processing_status": "Failed", "retryable": 1}
    if resources:
        filters["resource"] = ["in", resources]
    names = frappe.get_all(
        "Jackyun Raw Record",
        filters=filters,
        order_by="creation asc",
        limit_page_length=max(1, min(frappe.utils.cint(limit) or 100, 500)),
        pluck="name",
    )
    summary = {"attempted": 0, "succeeded": 0, "failed": 0, "by_resource": {}}
    errors = []
    for name in names:
        resource = frappe.db.get_value("Jackyun Raw Record", name, "resource")
        bucket = summary["by_resource"].setdefault(
            resource, {"attempted": 0, "succeeded": 0, "failed": 0}
        )
        result = retry_processing(name)
        summary["attempted"] += 1
        bucket["attempted"] += 1
        if result.get("ok"):
            summary["succeeded"] += 1
            bucket["succeeded"] += 1
        else:
            summary["failed"] += 1
            bucket["failed"] += 1
            errors.append({"name": name, "resource": resource, "error": result.get("error")})
        if summary["attempted"] % 20 == 0:
            frappe.db.commit()
    summary["errors"] = errors[:50]
    return summary

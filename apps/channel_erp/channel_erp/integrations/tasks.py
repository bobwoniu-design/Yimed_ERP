"""吉客云同步任务入口（由 scheduler_events 触发）。"""
import frappe
from frappe import _
from frappe.utils.background_jobs import enqueue, is_job_enqueued

from channel_erp.integrations.adapter_registry import get_adapter
from channel_erp.integrations.exception_classifier import classify_exception
from channel_erp.integrations.jackyun import (
    METHOD_MAP,
    SALES_ORDER_FIELDS,
    SALES_ORDER_PAGE_SIZE,
    extract_records_by_identity,
)


SYNC_RESOURCE_ORDER = [
    "Company", "Department", "Category", "Sales Channel", "Supplier",
    "Offline Customer", "Warehouse", "SKU", "Inventory", "Product Bundle",
    "Batch", "Purchase Order", "Purchase Receipt", "Purchase Return",
    "Sales Order", "Delivery Note", "Sales Return", "Stock Transfer", "Stock Movement", "Stocktake",
]

DEFAULT_SYNC_INTERVALS = {
    "Sales Order": 15,
    "Delivery Note": 15,
    "Sales Return": 15,
    "Purchase Receipt": 15,
    "Purchase Return": 30,
    "Stock Transfer": 30,
    "Purchase Order": 15,
    "Inventory": 15,
    "Stocktake": 60,
}

SUPPORTED_SYNC_INTERVALS = {1, 5, 15, 30, 60, 360, 1440}
INCREMENTAL_OVERLAP_MINUTES = 2

# 日批模式：interval_minutes >= 1440 时，每天在 daily_run_time（默认 12:01）执行一次，
# 同步窗口固定为前一日 00:00:00-23:59:59，游标推进到前一日末尾。
DAILY_BATCH_INTERVAL = 1440
DEFAULT_DAILY_RUN_TIME = "12:01:00"

# 生产环境的推荐拉取频率。交易单据追求及时性；库存快照用于核对，主数据则无需频繁全量拉取。
RECOMMENDED_PULL_INTERVALS = {
    "Sales Order": 15,
    "Delivery Note": 15,
    "Sales Return": 15,
    "Purchase Order": 15,
    "Purchase Receipt": 15,
    "Inventory": 15,
    "Company": 360,
    "Department": 360,
    "Category": 360,
    "Sales Channel": 360,
    "Supplier": 360,
    "Offline Customer": 360,
    "Warehouse": 360,
    "SKU": 360,
    "Product Bundle": 360,
    "Batch": 360,
}

# 线下客户的固定来源批次（与 sync_offline_customer 保持一致）。
# 定时/手动排程不携带筛选条件，须在此补上，否则适配器会拒绝执行。
OFFLINE_CUSTOMER_SOURCES = [1, 2, 7, 8, 9, 10]
PULL_DIRECTIONS = {"Pull Only", "Bidirectional"}


def get_resource_config(connection, resource):
    """Return the existing child-row config, keeping pre-migration connections compatible."""
    return next(
        (row for row in (connection.get("sync_schedules") or []) if row.resource == resource),
        None,
    )


def is_pull_enabled(connection, resource):
    config = get_resource_config(connection, resource)
    if not config:
        return True
    if not frappe.utils.cint(config.enabled):
        return False
    return (config.get("direction") or "Pull Only") in PULL_DIRECTIONS


def _successful_raw_snapshot_exists(resource, external_id, content_hash):
    """Return whether this exact source payload was already processed successfully."""
    return bool(
        frappe.db.exists(
            "Jackyun Raw Record",
            {
                "resource": resource,
                "external_id": str(external_id),
                "content_hash": content_hash,
                "processing_status": "Succeeded",
            },
        )
    )


def _new_raw_record(sync_log, resource, external_id, raw, content_hash):
    return frappe.get_doc(
        {
            "doctype": "Jackyun Raw Record",
            "sync_log": sync_log.name,
            "resource": resource,
            "external_id": str(external_id),
            "raw_data": raw,
            "content_hash": content_hash,
            "processed": 0,
            "processing_status": "Pending",
        }
    ).insert(ignore_permissions=True)


def _mark_raw_failure(raw_doc, exc):
    classification = classify_exception(exc)
    raw_doc.processing_status = "Failed"
    raw_doc.error_message = str(exc)
    raw_doc.update(classification)
    raw_doc.save(ignore_permissions=True)


def _apply_sync_start_floor(sync_since, configured_start):
    """Never let an incremental cursor move earlier than the configured start."""
    configured = (
        frappe.utils.get_datetime(configured_start) if configured_start else None
    )
    current = frappe.utils.get_datetime(sync_since) if sync_since else None
    if configured and (not current or current < configured):
        return configured
    return current


def _apply_incremental_overlap(sync_since, configured_start=None, minutes=None):
    """Overlap incremental reads without moving before the configured floor.

    Upserts and source-payload hashes make the overlap idempotent. The durable
    cursor remains ``sync_since`` and advances only after a complete success.
    """
    current = frappe.utils.get_datetime(sync_since) if sync_since else None
    if not current:
        return None
    overlap = INCREMENTAL_OVERLAP_MINUTES if minutes is None else frappe.utils.cint(minutes)
    request_since = frappe.utils.add_to_date(current, minutes=-max(0, overlap))
    floor = frappe.utils.get_datetime(configured_start) if configured_start else None
    return max(request_since, floor) if floor else request_since


def sync_resource(
    resource,
    limit=None,
    pull_kwargs=None,
    connection_name=None,
    advance_cursor=True,
    trigger_type="direct",
    scheduled_for=None,
):
    """同步某资源类型（如 SKU），遍历所有启用的连接。

    limit：测试用，限制处理的条数（None = 全量）。
    """
    from channel_erp.integrations.jackyun import is_quota_circuit_open

    if trigger_type == "scheduled" and is_quota_circuit_open():
        frappe.logger("channel_erp.scheduler").info(
            "JackYun sync skipped (quota circuit open) resource=%s", resource
        )
        return

    if resource == "Offline Customer" and not pull_kwargs:
        pull_kwargs = {"customer_sources": OFFLINE_CUSTOMER_SOURCES}

    filters = {"enabled": 1}
    if connection_name:
        filters["name"] = connection_name
    connections = frappe.get_all("Jackyun Connection", filters=filters)

    for conn in connections:
        connection = frappe.get_doc("Jackyun Connection", conn.name)
        if not is_pull_enabled(connection, resource):
            continue
        resource_config = get_resource_config(connection, resource)
        adapter = get_adapter(connection)

        if scheduled_for:
            queue_delay = max(
                0,
                frappe.utils.time_diff_in_seconds(
                    frappe.utils.now_datetime(), scheduled_for
                ),
            )
            frappe.logger("channel_erp.scheduler").info(
                "JackYun sync started connection=%s resource=%s queue_delay_seconds=%s",
                connection.name,
                resource,
                queue_delay,
            )

        sync_log = frappe.get_doc(
            {
                "doctype": "Jackyun Sync Log",
                "connection": connection.name,
                "resource": resource,
                "method": METHOD_MAP.get(resource),
                "status": "进行中",
                "trigger_type": trigger_type if trigger_type in {"scheduled", "manual", "direct"} else "direct",
                "started_at": frappe.utils.now(),
            }
        ).insert(ignore_permissions=True)

        created = updated = failed = skipped = pulled = duplicate_raw_count = 0
        error_log = []
        sync_since = None
        request_since = None
        sync_until = None
        incremental_resources = {
            "Product Bundle",
            "Inventory",
            "Supplier",
            "Offline Customer",
            "Sales Order",
            "Purchase Order",
            "Purchase Receipt",
            "Delivery Note",
            "Sales Return",
            "Purchase Return",
            "Batch",
            "Stock Movement",
        }
        if resource in incremental_resources:
            sync_since = frappe.db.get_value(
                "Jackyun Sync Log",
                {
                    "connection": connection.name,
                    "resource": resource,
                    "status": ["in", ["成功", "部分失败"]],
                },
                "last_modified_at",
                order_by="last_modified_at desc",
            )
            sync_since = _apply_sync_start_floor(
                sync_since,
                resource_config.get("sync_start_at") if resource_config else None,
            )
            request_since = _apply_incremental_overlap(
                sync_since,
                resource_config.get("sync_start_at") if resource_config else None,
            )
            if _is_daily_batch(resource_config):
                # 日批模式：窗口固定为前一日全天，避免把当天上午的数据提前拉进来。
                yesterday = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-1)
                sync_until = frappe.utils.get_datetime(
                    yesterday.strftime("%Y-%m-%d") + " 23:59:59"
                )
            else:
                sync_until = frappe.utils.now_datetime()

        request_from, request_to = _effective_request_window(
            request_since, sync_until, pull_kwargs
        )
        sync_log.request_from = request_from
        sync_log.request_to = request_to
        sync_log.cursor_start_at = sync_since

        truncated = False
        try:
            for idx, raw in enumerate(
                adapter.pull(
                    resource,
                    since=request_since,
                    until=sync_until,
                    **(pull_kwargs or {}),
                )
            ):
                if limit and idx >= limit:
                    truncated = True
                    break

                pulled += 1

                if not isinstance(raw, dict):
                    failed += 1
                    error_log.append(f"非对象记录: {raw}")
                    continue

                external_id = adapter.get_external_id(resource, raw)
                if not external_id:
                    failed += 1
                    error_log.append(f"缺失外部ID: {raw}")
                    continue

                content_hash = adapter.content_hash(raw)
                raw_doc = None
                if not _successful_raw_snapshot_exists(resource, external_id, content_hash):
                    raw_doc = _new_raw_record(
                        sync_log, resource, external_id, raw, content_hash
                    )
                else:
                    duplicate_raw_count += 1

                try:
                    mapped = adapter.transform(resource, raw)
                    erpnext_name, status = adapter.upsert(resource, mapped)
                    if resource == "SKU":
                        adapter.remember_sku_aliases(raw, erpnext_name)
                    if status == "created":
                        created += 1
                    elif status == "updated":
                        updated += 1
                    elif status == "skipped":
                        skipped += 1
                    if raw_doc:
                        raw_doc.processed = 1
                        raw_doc.processed_at = frappe.utils.now()
                        raw_doc.processing_status = "Succeeded"
                        raw_doc.error_message = None
                        raw_doc.save(ignore_permissions=True)
                except Exception as exc:
                    failed += 1
                    error_log.append(f"{external_id}: {exc}")
                    if not raw_doc:
                        raw_doc = _new_raw_record(
                            sync_log, resource, external_id, raw, content_hash
                        )
                    _mark_raw_failure(raw_doc, exc)

                # 周期性提交，避免单个大事务越跑越慢
                if idx % 100 == 0:
                    frappe.db.commit()

            sync_log.status = "成功" if not failed else ("部分失败" if (created + updated) else "失败")
            # 部分失败也推进游标：失败记录已保存原始数据（Raw Record），由本地重试
            # 通道兜底，避免游标卡死导致整个窗口被反复重拉、耗尽接口额度。
            if (
                resource in incremental_resources
                and sync_log.status in ("成功", "部分失败")
                and not truncated
                and advance_cursor
            ):
                sync_log.last_modified_at = sync_until
        except Exception as exc:
            sync_log.status = "失败"
            failed += 1
            error_log.append(f"同步中断: {exc}")
        finally:
            finished_at = frappe.utils.now()
            apply_sync_log_metrics(
                sync_log,
                finished_at,
                {
                    "total_pulled": pulled,
                    "total_created": created,
                    "total_updated": updated,
                    "total_skipped": skipped,
                    "duplicate_raw_count": duplicate_raw_count,
                    "total_failed": failed,
                },
                adapter,
                sync_since,
            )
            sync_log.error_log = "\n".join(error_log[:200])
            sync_log.save(ignore_permissions=True)

            # 正常任务结束时立即检查本轮是否存在“原始数据已保存、但映射/单据未落地”的漏单。
            # Worker 被强制终止时 finally 不会执行，定时健康检查还会再次兜底恢复。
            try:
                from channel_erp.jackyun_integration.doctype.jackyun_raw_record.jackyun_raw_record import (
                    verify_and_recover_sync_run,
                )

                recovery = verify_and_recover_sync_run(sync_log.name, resource)
                if recovery.get("failed"):
                    sync_log.status = "部分失败"
                    # 游标保持已推进的位置：恢复失败的记录由本地 Raw Record
                    # 重试通道处理，不再回退游标触发整窗口重拉。
                    sync_log.total_failed = frappe.utils.cint(sync_log.total_failed) + frappe.utils.cint(
                        recovery.get("failed")
                    )
                    recovery_errors = [
                        f"自动漏单恢复失败 {item['name']}: {item['error']}"
                        for item in recovery.get("errors", [])[:20]
                    ]
                    sync_log.error_log = "\n".join(
                        [item for item in [sync_log.error_log, *recovery_errors] if item]
                    )
                    sync_log.save(ignore_permissions=True)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "吉客云本轮同步完整性核对失败")

            if sync_log.status in ("失败", "部分失败"):
                try:
                    _notify_sync_failure(connection, resource, sync_log.status, failed, error_log)
                except Exception:
                    frappe.log_error(frappe.get_traceback(), "吉客云同步失败告警发送失败")

            _update_resource_summary(
                connection,
                resource,
                sync_log.status,
                finished_at,
                pulled=sync_log.total_pulled,
            )
            connection.last_sync_at = finished_at
            connection.save(ignore_permissions=True)
            frappe.db.commit()


def _is_daily_batch(resource_config):
    """Return whether the resource schedule runs as a once-a-day batch."""
    if not resource_config:
        return False
    return frappe.utils.cint(resource_config.get("interval_minutes")) >= DAILY_BATCH_INTERVAL


def _daily_slot_today(resource_config, now=None):
    """Compute today's fixed run time for a daily batch schedule."""
    now = now or frappe.utils.now_datetime()
    run_time = (
        resource_config.get("daily_run_time")
        if resource_config
        else None
    ) or DEFAULT_DAILY_RUN_TIME
    run_time_str = str(run_time)
    if len(run_time_str) == 5:  # "12:01" -> "12:01:00"
        run_time_str += ":00"
    return frappe.utils.get_datetime(now.strftime("%Y-%m-%d") + " " + run_time_str)


def _effective_request_window(sync_since, sync_until, pull_kwargs):
    filters = pull_kwargs or {}
    start = filters.get("created_since") or filters.get("since") or sync_since
    end = filters.get("created_until") or filters.get("until") or sync_until
    return (
        frappe.utils.get_datetime(start) if start else None,
        frappe.utils.get_datetime(end) if end else None,
    )


def apply_sync_log_metrics(sync_log, finished_at, counters, adapter, cursor_start=None):
    """Apply only observed counters and adapter telemetry to a sync-log document."""
    sync_log.finished_at = finished_at
    sync_log.duration_seconds = frappe.utils.time_diff_in_seconds(
        finished_at, sync_log.started_at
    )
    for fieldname in (
        "total_pulled",
        "total_created",
        "total_updated",
        "total_skipped",
        "duplicate_raw_count",
        "total_failed",
    ):
        sync_log.set(fieldname, frappe.utils.cint(counters.get(fieldname)))
    sync_log.api_request_count = frappe.utils.cint(
        getattr(adapter, "request_count", 0)
    )
    sync_log.context_id = getattr(adapter, "last_context_id", None)
    sync_log.cursor_end_at = sync_log.last_modified_at or cursor_start


def apply_schedule_result(schedule, status, finished_at, pulled=None):
    """Update one resource-health summary without querying or enqueueing work."""
    finished = frappe.utils.get_datetime(finished_at)
    schedule.last_status = status
    if pulled is not None:
        schedule.last_pulled_count = max(0, frappe.utils.cint(pulled))
    if status == "成功":
        schedule.last_success_at = finished
        schedule.consecutive_failures = 0
    else:
        schedule.consecutive_failures = frappe.utils.cint(
            schedule.get("consecutive_failures")
        ) + 1
    interval = max(frappe.utils.cint(schedule.interval_minutes), 1)
    base = frappe.utils.get_datetime(schedule.last_enqueued_at) if schedule.last_enqueued_at else finished
    schedule.next_run_at = frappe.utils.add_to_date(base, minutes=interval)


def _update_resource_summary(
    connection, resource, status, finished_at, pulled=None
):
    schedule = get_resource_config(connection, resource)
    if schedule:
        apply_schedule_result(schedule, status, finished_at, pulled=pulled)


def _notify_sync_failure(connection, resource, status, failed, error_log):
    """同步失败/部分失败时向管理员发送站内通知（Notification Log，不直接 sendmail）。

    通知开关：连接上的 notify_on_failure（默认开，仅显式关闭时不通知）。
    """
    notify_on_failure = connection.get("notify_on_failure")
    if notify_on_failure is not None and not frappe.utils.cint(notify_on_failure):
        return

    error_summary = "\n".join((error_log or [])[:5])
    if len(error_summary) > 500:
        error_summary = error_summary[:500] + "…"

    for user in _get_admin_recipients():
        frappe.get_doc(
            {
                "doctype": "Notification Log",
                "type": "Alert",
                "for_user": user,
                "subject": f"吉客云同步{status}：{connection.name} - {resource}",
                "document_type": "Jackyun Connection",
                "document_name": connection.name,
                "email_content": (
                    f"连接：{connection.name}\n"
                    f"资源类型：{resource}\n"
                    f"失败数：{failed}\n"
                    f"错误摘要：\n{error_summary}"
                ),
            }
        ).insert(ignore_permissions=True)


def _get_admin_recipients():
    """返回接收告警的管理员用户名；无系统管理员时回退 Administrator。"""
    from frappe.utils.user import get_system_managers

    return get_system_managers(only_name=True) or ["Administrator"]


def sync_sku(limit=None):
    """定时拉取 SKU。limit 用于测试时限制条数。"""
    sync_resource("SKU", limit)


def sync_company(limit=None):
    """同步公司并映射到连接指定的 ERPNext Company。"""
    sync_resource("Company", limit)


def sync_department(limit=None):
    """按公司编码同步吉客云部门到 ERPNext Department。"""
    sync_resource("Department", limit)


def sync_category(limit=None):
    """同步吉客云商品分类镜像。"""
    sync_resource("Category", limit)


def sync_sales_channel(limit=None):
    """同步吉客云销售渠道镜像。"""
    sync_resource("Sales Channel", limit)


def sync_warehouse(limit=None):
    """同步吉客云仓库到 ERPNext Warehouse。"""
    sync_resource("Warehouse", limit)


def sync_inventory(limit=None):
    """同步吉客云库存快照，不修改 ERPNext 库存账。"""
    sync_resource("Inventory", limit)


def sync_supplier(limit=None):
    """全量或按修改时间增量同步吉客云供应商。"""
    sync_resource("Supplier", limit)


def sync_customer_on_demand(customer_ids=None, customer_codes=None, limit=None):
    """按吉客云客户 ID/编号同步；无筛选条件时适配器会拒绝执行。"""
    customer_ids = _as_list(customer_ids)
    customer_codes = _as_list(customer_codes)
    sync_resource(
        "Customer",
        limit,
        {"customer_ids": customer_ids, "customer_codes": customer_codes},
    )


def sync_offline_customer(limit=None):
    """同步非电商来源的真实客户，并按修改时间增量更新。"""
    sync_resource(
        "Offline Customer",
        limit,
        {"customer_sources": OFFLINE_CUSTOMER_SOURCES},
    )


def _as_list(value):
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    value = str(value).strip()
    if value.startswith("["):
        parsed = frappe.parse_json(value)
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in value.split(",") if item.strip()]


def sync_product_bundle(limit=None):
    """拉取吉客云虚拟组套并同步为 ERPNext Product Bundle。"""
    sync_resource("Product Bundle", limit)


def sync_sales_order(limit=None):
    """从配置的开始日期同步吉客云销售单为 ERPNext 草稿销售订单。"""
    sync_resource("Sales Order", limit)


def backfill_sales_order_listings(from_date, to_date=None, connection_name=None):
    """Enrich listing IDs on existing mapped orders without creating Sales Orders."""
    filters = {"enabled": 1}
    if connection_name:
        filters["name"] = connection_name
    for connection_row in frappe.get_all("Jackyun Connection", filters=filters):
        connection = frappe.get_doc("Jackyun Connection", connection_row.name)
        adapter = get_adapter(connection)
        sync_log = frappe.get_doc(
            {
                "doctype": "Jackyun Sync Log",
                "connection": connection.name,
                "resource": "Sales Order",
                "method": METHOD_MAP["Sales Order"],
                "status": "进行中",
                "trigger_type": "direct",
                "started_at": frappe.utils.now(),
                "request_from": frappe.utils.get_datetime(from_date),
                "request_to": frappe.utils.get_datetime(to_date) if to_date else None,
            }
        ).insert(ignore_permissions=True)
        updated = failed = skipped = pulled = duplicate_raw_count = 0
        errors = []
        try:
            trade_nos = _mapped_sales_order_trade_nos(from_date, to_date)
            for offset in range(0, len(trade_nos), SALES_ORDER_PAGE_SIZE):
                batch = trade_nos[offset : offset + SALES_ORDER_PAGE_SIZE]
                payload = adapter.request(
                    METHOD_MAP["Sales Order"],
                    {
                        "tradeNo": ",".join(batch),
                        "fields": SALES_ORDER_FIELDS,
                        "isTableSwitch": 1,
                        "isDelete": "0",
                    },
                    page_index=0,
                    page_size=SALES_ORDER_PAGE_SIZE,
                )
                records = extract_records_by_identity(payload, ["tradeId"])
                pulled += len(records)
                skipped += max(0, len(batch) - len(records))
                for raw in records:
                    external_id = adapter.get_external_id("Sales Order", raw)
                    mapping_name = external_id and adapter.get_mapping_name(
                        "Sales Order", str(external_id)
                    )
                    sales_order = (
                        frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
                        if mapping_name
                        else None
                    )
                    if not sales_order or not frappe.db.exists("Sales Order", sales_order):
                        skipped += 1
                        continue
                    content_hash = adapter.content_hash(raw)
                    raw_doc = None
                    if not _successful_raw_snapshot_exists(
                        "Sales Order", external_id, content_hash
                    ):
                        raw_doc = _new_raw_record(
                            sync_log, "Sales Order", external_id, raw, content_hash
                        )
                    else:
                        duplicate_raw_count += 1
                    try:
                        adapter.upsert("Sales Order", adapter.transform("Sales Order", raw))
                        if raw_doc:
                            raw_doc.db_set(
                                {
                                    "processed": 1,
                                    "processed_at": frappe.utils.now(),
                                    "processing_status": "Succeeded",
                                    "error_message": None,
                                },
                                update_modified=False,
                            )
                        updated += 1
                    except Exception as exc:
                        failed += 1
                        errors.append(f"{external_id}: {exc}")
                        if not raw_doc:
                            raw_doc = _new_raw_record(
                                sync_log, "Sales Order", external_id, raw, content_hash
                            )
                        _mark_raw_failure(raw_doc, exc)
                frappe.db.commit()
            sync_log.status = "成功" if not failed else ("部分失败" if updated else "失败")
        except Exception as exc:
            sync_log.status = "失败"
            failed += 1
            errors.append(f"回补中断: {exc}")
        finally:
            finished_at = frappe.utils.now()
            sync_log.finished_at = finished_at
            sync_log.duration_seconds = frappe.utils.time_diff_in_seconds(
                finished_at, sync_log.started_at
            )
            sync_log.total_pulled = pulled
            sync_log.total_created = 0
            sync_log.total_updated = updated
            sync_log.total_skipped = skipped
            sync_log.duplicate_raw_count = duplicate_raw_count
            sync_log.total_failed = failed
            sync_log.api_request_count = frappe.utils.cint(
                getattr(adapter, "request_count", 0)
            )
            sync_log.context_id = getattr(adapter, "last_context_id", None)
            sync_log.error_log = "\n".join([f"跳过未映射订单：{skipped}", *errors[:199]])
            sync_log.save(ignore_permissions=True)
            frappe.db.commit()


def _mapped_sales_order_trade_nos(from_date, to_date=None):
    conditions = ["so.transaction_date >= %s"]
    params = [frappe.utils.getdate(from_date)]
    if to_date:
        conditions.append("so.transaction_date <= %s")
        params.append(frappe.utils.getdate(to_date))
    return frappe.db.sql(
        f"""
        select distinct json_unquote(json_extract(rr.raw_data, '$.tradeNo')) trade_no
          from `tabExternal ID Mapping` m
          join `tabSales Order` so on so.name=m.erpnext_name
          join `tabJackyun Raw Record` rr
            on rr.resource='Sales Order' and rr.external_id=m.external_id
         where m.resource='Sales Order'
           and m.external_id not like 'trade:%%'
           and {' and '.join(conditions)}
           and nullif(json_unquote(json_extract(rr.raw_data, '$.tradeNo')), 'null') is not null
         order by trade_no
        """,
        params,
        pluck=True,
    )


def sync_purchase_order(limit=None):
    """从配置的开始日期同步吉客云采购单为 ERPNext 草稿采购订单。"""
    sync_resource("Purchase Order", limit)


def sync_purchase_receipt(limit=None):
    """同步吉客云采购入库蓝单为 ERPNext 草稿采购收货单。"""
    sync_resource("Purchase Receipt", limit)


def sync_delivery_note(limit=None):
    """同步吉客云销售出库蓝单为 ERPNext 草稿交货单。"""
    sync_resource("Delivery Note", limit)


def sync_sales_return(limit=None):
    """同步销售退货入库为 ERPNext 草稿退货交货单。"""
    sync_resource("Sales Return", limit)


def sync_purchase_return(limit=None):
    """同步采购退货出库为 ERPNext 草稿退货采购收货单。"""
    sync_resource("Purchase Return", limit)


def sync_stock_transfer(limit=None):
    """同步调拨单为 ERPNext 草稿库存转移。"""
    sync_resource("Stock Transfer", limit)


def sync_stocktake(limit=None):
    """同步已完成盘点单为 ERPNext 草稿库存核对。"""
    sync_resource("Stocktake", limit)


def sync_stock_movement(limit=None):
    """同步非买卖、非调拨的出入库流水为 ERPNext 草稿库存分录。"""
    sync_resource("Stock Movement", limit)


def sync_batch(limit=None):
    """同步吉客云批次主档，不写库存账。"""
    sync_resource("Batch", limit)


def sync_catalog(limit=None):
    """按依赖顺序同步主数据、SKU 和组合装。"""
    sync_company(limit)
    sync_department(limit)
    sync_category(limit)
    sync_sales_channel(limit)
    sync_supplier(limit)
    sync_offline_customer(limit)
    sync_warehouse(limit)
    sync_sku(limit)
    sync_inventory(limit)
    sync_product_bundle(limit)
    sync_batch(limit)
    sync_purchase_order(limit)
    sync_purchase_receipt(limit)
    sync_purchase_return(limit)
    sync_sales_order(limit)
    sync_delivery_note(limit)
    sync_sales_return(limit)
    sync_stock_transfer(limit)
    sync_stock_movement(limit)
    sync_stocktake(limit)


def _job_id(connection_name, resource):
    safe_resource = frappe.scrub(resource).replace("_", "-")
    return f"jackyun-sync:{connection_name}:{safe_resource}"


def _integration_queue_name():
    """Use a dedicated configured queue, falling back to the existing long worker."""
    from frappe.utils.background_jobs import get_queues_timeout

    available = get_queues_timeout()
    configured = frappe.conf.get("channel_erp_integration_queue")
    if configured and configured in available:
        return configured
    if "integration" in available:
        return "integration"
    return "long"


def _resource_sync_in_progress(connection_name, resource):
    """Cover queued/started RQ jobs and direct runs represented by a live log."""
    if is_job_enqueued(_job_id(connection_name, resource)):
        return True
    return bool(
        frappe.db.exists(
            "Jackyun Sync Log",
            {
                "connection": connection_name,
                "resource": resource,
                "status": "进行中",
            },
        )
    )


def enqueue_sync_resource(
    connection_name, resource, source="scheduled", scheduled_for=None
):
    if resource not in METHOD_MAP:
        frappe.throw(_("不支持的吉客云同步类型：{0}").format(resource))
    job_id = _job_id(connection_name, resource)
    if _resource_sync_in_progress(connection_name, resource):
        return False
    queue = _integration_queue_name()
    job = enqueue(
        "channel_erp.integrations.tasks.sync_resource",
        queue=queue,
        timeout=7200,
        job_id=job_id,
        deduplicate=True,
        connection_name=connection_name,
        resource=resource,
        trigger_type=source,
        scheduled_for=scheduled_for,
    )
    if job:
        frappe.logger("channel_erp.scheduler").info(
            "JackYun sync enqueued connection=%s resource=%s queue=%s scheduled_for=%s",
            connection_name,
            resource,
            queue,
            scheduled_for,
        )
    return bool(job)


def process_due_schedules():
    """每分钟检查各连接的资源频率，到期后独立排队。"""
    finalize_abandoned_sync_logs()
    now = frappe.utils.now_datetime()
    from channel_erp.integrations.jackyun import is_quota_circuit_open

    quota_circuit_open = is_quota_circuit_open(now=now)
    if quota_circuit_open:
        frappe.logger("channel_erp.scheduler").info(
            "JackYun scheduler skipped (quota circuit open until cooldown ends)"
        )
        frappe.db.commit()
        return
    for row in frappe.get_all("Jackyun Connection", filters={"enabled": 1}, pluck="name"):
        connection = frappe.get_doc("Jackyun Connection", row)
        if not connection.sync_schedules:
            connection.set_default_sync_schedules(now=now)
            connection.save(ignore_permissions=True)
        changed = False
        for schedule in connection.sync_schedules:
            if (
                not schedule.enabled
                or schedule.resource not in METHOD_MAP
                or (schedule.get("direction") or "Pull Only") not in PULL_DIRECTIONS
            ):
                continue
            interval = max(frappe.utils.cint(schedule.interval_minutes), 1)
            last = frappe.utils.get_datetime(schedule.last_enqueued_at) if schedule.last_enqueued_at else None
            if interval >= DAILY_BATCH_INTERVAL:
                # 日批：每天在固定时刻执行一次，窗口为前一日全天。
                slot = _daily_slot_today(schedule, now=now)
                if now < slot:
                    continue
                if last and last >= slot:
                    continue
                scheduled_for = slot
            else:
                if last and frappe.utils.time_diff_in_seconds(now, last) < interval * 60:
                    continue
                scheduled_for = (
                    frappe.utils.add_to_date(last, minutes=interval)
                    if last
                    else (
                        frappe.utils.get_datetime(schedule.get("next_run_at"))
                        if schedule.get("next_run_at")
                        else now
                    )
                )
            delay_seconds = max(
                0, frappe.utils.time_diff_in_seconds(now, scheduled_for)
            )
            if delay_seconds >= 60:
                frappe.logger("channel_erp.scheduler").warning(
                    "JackYun schedule delayed connection=%s resource=%s delay_seconds=%s",
                    connection.name,
                    schedule.resource,
                    delay_seconds,
                )
            if enqueue_sync_resource(
                connection.name, schedule.resource, scheduled_for=scheduled_for
            ):
                schedule.last_enqueued_at = now
                if interval >= DAILY_BATCH_INTERVAL:
                    schedule.next_run_at = frappe.utils.add_to_date(
                        slot, days=1
                    )
                else:
                    schedule.next_run_at = frappe.utils.add_to_date(
                        now, minutes=interval
                    )
                changed = True
        if changed:
            connection.save(ignore_permissions=True)
    frappe.db.commit()


@frappe.whitelist()
def apply_recommended_pull_schedule(connection_name=None):
    """Apply the agreed production pull profile without changing push directions."""
    filters = {"enabled": 1}
    if connection_name:
        filters["name"] = connection_name
    now = frappe.utils.now_datetime()
    changed = {}
    for name in frappe.get_all("Jackyun Connection", filters=filters, pluck="name"):
        connection = frappe.get_doc("Jackyun Connection", name)
        if frappe.session.user != "Administrator":
            connection.check_permission("write")
        connection_changes = {}
        for schedule in connection.get("sync_schedules") or []:
            interval = RECOMMENDED_PULL_INTERVALS.get(schedule.resource)
            if not interval or frappe.utils.cint(schedule.interval_minutes) == interval:
                continue
            schedule.interval_minutes = interval
            schedule.next_run_at = frappe.utils.add_to_date(now, minutes=interval)
            connection_changes[schedule.resource] = interval
        if connection_changes:
            connection.save(ignore_permissions=True)
            changed[name] = connection_changes
    frappe.db.commit()
    return {"changed": changed, "profile": RECOMMENDED_PULL_INTERVALS}


def finalize_abandoned_sync_logs(max_age_minutes=10):
    """Close logs whose queue job disappeared without running the finally block."""
    cutoff = frappe.utils.add_to_date(
        frappe.utils.now_datetime(), minutes=-max(5, frappe.utils.cint(max_age_minutes))
    )
    closed = 0
    for row in frappe.get_all(
        "Jackyun Sync Log", filters={"status":"进行中", "started_at":["<", cutoff]},
        fields=["name", "connection", "resource"],
    ):
        if is_job_enqueued(_job_id(row.connection, row.resource)):
            continue
        frappe.db.set_value(
            "Jackyun Sync Log", row.name,
            {"status":"失败", "finished_at":frappe.utils.now_datetime(),
             "error_log":"后台任务已结束但未写入完成状态，已由健康检查关闭；资源会按计划重试。"},
            update_modified=False,
        )
        closed += 1
    return closed


def _health_notification(connection_name, key, subject, content, cooldown_minutes=60):
    """Send a deduplicated in-app connector alert."""
    marker = f"[吉客云健康:{key}]"
    cutoff = frappe.utils.add_to_date(
        frappe.utils.now_datetime(), minutes=-max(15, frappe.utils.cint(cooldown_minutes))
    )
    if frappe.db.exists(
        "Notification Log",
        {
            "subject": ["like", f"{marker}%"],
            "document_name": connection_name,
            "creation": [">=", cutoff],
        },
    ):
        return False
    for user in _get_admin_recipients():
        frappe.get_doc(
            {
                "doctype": "Notification Log",
                "type": "Alert",
                "for_user": user,
                "subject": f"{marker} {subject}",
                "document_type": "Jackyun Connection",
                "document_name": connection_name,
                "email_content": content,
            }
        ).insert(ignore_permissions=True)
    return True


def _integration_queue_depth():
    """Count only this connector's jobs on its configured/fallback queue."""
    try:
        from frappe.utils.background_jobs import get_queue

        queue = get_queue(_integration_queue_name())
        return sum(
            1 for job_id in queue.get_job_ids() if "jackyun-sync:" in str(job_id)
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "吉客云队列深度检查失败")
        return None


def monitor_connector_health():
    """Monitor timeout, cursor stagnation, connector backlog, and recover pending work."""
    closed = finalize_abandoned_sync_logs()
    from channel_erp.jackyun_integration.doctype.jackyun_raw_record.jackyun_raw_record import (
        recover_pending_records,
    )

    recovery = recover_pending_records(limit=1000, min_age_minutes=10)
    queue_depth = _integration_queue_depth()
    alerts = []
    for name in frappe.get_all("Jackyun Connection", filters={"enabled": 1}, pluck="name"):
        connection = frappe.get_doc("Jackyun Connection", name)
        if closed:
            if _health_notification(
                name,
                "abandoned-job",
                "后台同步任务异常退出",
                f"健康检查关闭了 {closed} 条未正常结束的同步日志；对应资源将按排程重试。",
            ):
                alerts.append("abandoned-job")

        if queue_depth is not None and queue_depth >= 10:
            if _health_notification(
                name,
                "queue-backlog",
                "同步队列积压",
                f"吉客云 {_integration_queue_name()} 队列中有 {queue_depth} 个待执行任务，请检查 Worker 运行状态。",
            ):
                alerts.append("queue-backlog")

        for schedule in connection.get("sync_schedules") or []:
            if not schedule.enabled or not is_pull_enabled(connection, schedule.resource):
                continue
            if frappe.utils.cint(schedule.get("consecutive_failures")) >= 2:
                key = f"repeated-failure-{frappe.scrub(schedule.resource)}"
                if _health_notification(
                    name,
                    key,
                    f"{schedule.resource} 连续同步失败",
                    f"资源 {schedule.resource} 已连续失败 {schedule.consecutive_failures} 次。",
                ):
                    alerts.append(key)

            logs = frappe.get_all(
                "Jackyun Sync Log",
                filters={"connection": name, "resource": schedule.resource, "status": "成功"},
                fields=["cursor_end_at", "total_pulled"],
                order_by="started_at desc",
                limit_page_length=2,
            )
            if (
                len(logs) == 2
                and logs[0].cursor_end_at
                and logs[0].cursor_end_at == logs[1].cursor_end_at
                and frappe.utils.cint(logs[0].total_pulled) > 0
            ):
                key = f"cursor-stalled-{frappe.scrub(schedule.resource)}"
                if _health_notification(
                    name,
                    key,
                    f"{schedule.resource} 增量游标未推进",
                    f"资源 {schedule.resource} 最近两轮成功同步的游标相同，已保留原游标避免漏单，请检查接口返回。",
                ):
                    alerts.append(key)

    frappe.db.commit()
    return {
        "abandoned_logs_closed": closed,
        "connector_queue_depth": queue_depth,
        "recovery": recovery,
        "alerts": alerts,
    }


@frappe.whitelist()
def enqueue_manual_sync(connection_name, resource=None):
    connection = frappe.get_doc("Jackyun Connection", connection_name)
    connection.check_permission("write")
    if not connection.enabled:
        frappe.throw(_("请先启用吉客云连接"))
    resources = [resource] if resource and resource != "All" else SYNC_RESOURCE_ORDER
    blocked = [item for item in resources if not is_pull_enabled(connection, item)]
    if resource and resource != "All" and blocked:
        frappe.throw(_("资源 {0} 未启用拉取方向").format(resource))
    resources = [item for item in resources if item not in blocked]
    queued = [item for item in resources if enqueue_sync_resource(connection_name, item, "manual")]
    return {
        "queued": queued,
        "skipped": blocked + [item for item in resources if item not in queued],
    }


@frappe.whitelist()
def get_sync_resources():
    return [{"value": resource, "label": resource} for resource in SYNC_RESOURCE_ORDER]


def setup_default_schedules(reset=False):
    now = frappe.utils.now_datetime()
    for name in frappe.get_all("Jackyun Connection", pluck="name"):
        connection = frappe.get_doc("Jackyun Connection", name)
        if reset or not connection.sync_schedules:
            connection.set("sync_schedules", [])
            connection.set_default_sync_schedules(now=now)
            connection.save(ignore_permissions=True)
    frappe.db.commit()

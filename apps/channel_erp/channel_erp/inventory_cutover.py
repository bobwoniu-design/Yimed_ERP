"""One-time, idempotent helpers for establishing ERPNext stock from JackYun."""

from collections import defaultdict
import json

import frappe

from channel_erp.integrations.jackyun import (
    JackYunAdapter,
    prepare_standalone_jackyun_return,
)


STOCK_RESOURCES = (
    "Purchase Receipt",
    "Delivery Note",
    "Sales Return",
    "Purchase Return",
    "Stock Transfer",
)


def submit_historical_jackyun_sales_orders(dry_run=1, commit_every=100, limit=0):
    """Submit mapped draft orders whose latest JackYun status is shipped/completed.

    This deliberately operates on the archived raw records instead of the
    incremental cursor, so old orders which have not changed since the last
    successful pull are covered as well.  Cancellation/merge/split and earlier
    workflow states are excluded by the SQL predicate.  ERPNext's normal
    ``submit`` method is used; docstatus is never written directly.
    """
    dry_run = frappe.utils.cint(dry_run)
    commit_every = max(frappe.utils.cint(commit_every), 1)
    limit = max(frappe.utils.cint(limit), 0)
    rows = frappe.db.sql(
        """
        select distinct so.name, latest.trade_status
          from `tabSales Order` so
          join `tabExternal ID Mapping` map
            on map.resource='Sales Order'
           and map.erpnext_name=so.name
           and map.external_id not like 'trade:%%'
          join (
                select rr.external_id,
                       cast(json_unquote(json_extract(rr.raw_data, '$.tradeStatus')) as unsigned)
                           trade_status
                  from `tabJackyun Raw Record` rr
                  join (
                        select external_id, max(creation) creation
                          from `tabJackyun Raw Record`
                         where resource='Sales Order'
                         group by external_id
                  ) newest
                    on newest.external_id=rr.external_id
                   and newest.creation=rr.creation
                 where rr.resource='Sales Order'
          ) latest on latest.external_id=map.external_id
         where so.docstatus=0
           and latest.trade_status in (6000, 9090)
         order by so.transaction_date, so.creation, so.name
        """,
        as_dict=True,
    )
    if limit:
        rows = rows[:limit]
    result = {
        "eligible": len(rows),
        "submitted": 0,
        "failed": 0,
        "failures": [],
        "sample": [row.name for row in rows[:10]],
    }
    if dry_run:
        return result

    for index, row in enumerate(rows, 1):
        savepoint = f"historical_so_{index}"
        frappe.db.savepoint(savepoint)
        try:
            doc = frappe.get_doc("Sales Order", row.name)
            if doc.docstatus == 0:
                doc.submit()
                result["submitted"] += 1
        except Exception as exc:
            frappe.db.rollback(save_point=savepoint)
            result["failed"] += 1
            if len(result["failures"]) < 100:
                result["failures"].append({"name": row.name, "error": str(exc)})
        if index % commit_every == 0:
            frappe.db.commit()
    frappe.db.commit()
    return result


def migrate_jackyun_sales_order_identity(
    dry_run=1, commit_every=100, limit=0, bulk=0, batch_size=500
):
    """Backfill order identifiers and rename synchronized orders to JackYun tradeNo.

    The operation is idempotent. Frappe's rename API updates Link fields in
    downstream documents; External ID Mapping is updated explicitly because its
    ``erpnext_name`` column is intentionally a Data field.
    """
    dry_run = frappe.utils.cint(dry_run)
    commit_every = max(frappe.utils.cint(commit_every), 1)
    limit = max(frappe.utils.cint(limit), 0)
    bulk = frappe.utils.cint(bulk)
    batch_size = max(frappe.utils.cint(batch_size), 1)
    rows = frappe.db.sql(
        """
        select trade.erpnext_name old_name, substring(trade.external_id, 7) trade_no
          from `tabExternal ID Mapping` trade
         where trade.resource='Sales Order'
           and trade.external_id like 'trade:%%'
         order by trade.creation, trade.name
        """,
        as_dict=True,
    )
    if limit:
        rows = rows[:limit]
    primary_external_ids = dict(
        frappe.db.sql(
            """
            select erpnext_name, external_id
              from `tabExternal ID Mapping`
             where resource='Sales Order' and external_id not like 'trade:%%'
            """
        )
    )
    source_trade_numbers = dict(
        frappe.db.sql(
            """
            select rr.external_id,
                   coalesce(
                       nullif(json_unquote(json_extract(rr.raw_data, '$.onlineTradeNo')), 'null'),
                       nullif(json_unquote(json_extract(rr.raw_data, '$.sourceTradeNo')), 'null'),
                       ''
                   ) source_trade_no
              from `tabJackyun Raw Record` rr
              join (
                    select external_id, max(creation) creation
                      from `tabJackyun Raw Record`
                     where resource='Sales Order'
                     group by external_id
              ) latest
                on latest.external_id=rr.external_id and latest.creation=rr.creation
             where rr.resource='Sales Order'
            """
        )
    )
    if not dry_run and bulk:
        return _bulk_migrate_jackyun_sales_order_identity(
            rows,
            primary_external_ids,
            source_trade_numbers,
            batch_size=batch_size,
        )
    stats = defaultdict(int)
    failures = []
    samples = []

    for index, row in enumerate(rows, 1):
        old_name = frappe.utils.cstr(row.old_name).strip()
        trade_no = frappe.utils.cstr(row.trade_no).strip()
        source_trade_no = frappe.utils.cstr(
            source_trade_numbers.get(primary_external_ids.get(old_name), "")
        ).strip()
        if len(samples) < 10:
            samples.append(
                {
                    "old_name": old_name,
                    "trade_no": trade_no,
                    "source_trade_no": source_trade_no,
                }
            )
        if not old_name or not trade_no:
            stats["invalid"] += 1
            continue
        if not frappe.db.exists("Sales Order", old_name):
            if frappe.db.exists("Sales Order", trade_no):
                stats["already_renamed"] += 1
                old_name = trade_no
            else:
                stats["missing"] += 1
                continue
        elif old_name == trade_no:
            stats["already_renamed"] += 1
        elif frappe.db.exists("Sales Order", trade_no):
            stats["conflict"] += 1
            if len(failures) < 50:
                failures.append(
                    {"old_name": old_name, "trade_no": trade_no, "error": "目标编号已存在"}
                )
            continue
        else:
            stats["to_rename"] += 1

        if dry_run:
            continue

        savepoint = f"sales_order_identity_{index}"
        frappe.db.savepoint(savepoint)
        try:
            frappe.db.set_value(
                "Sales Order",
                old_name,
                {
                    "custom_jackyun_trade_no": trade_no,
                    "custom_jackyun_source_trade_no": source_trade_no,
                },
                update_modified=False,
            )
            if old_name != trade_no:
                frappe.rename_doc(
                    "Sales Order",
                    old_name,
                    trade_no,
                    force=True,
                    show_alert=False,
                    rebuild_search=False,
                )
                frappe.db.sql(
                    """
                    update `tabExternal ID Mapping`
                       set erpnext_name=%s, modified=now(), modified_by=%s
                     where erpnext_doctype='Sales Order' and erpnext_name=%s
                    """,
                    (trade_no, frappe.session.user, old_name),
                )
                stats["renamed"] += 1
            else:
                stats["metadata_updated"] += 1
        except Exception as exc:
            frappe.db.rollback(save_point=savepoint)
            stats["failed"] += 1
            if len(failures) < 50:
                failures.append(
                    {"old_name": old_name, "trade_no": trade_no, "error": str(exc)}
                )
        if index % commit_every == 0:
            frappe.db.commit()

    if not dry_run:
        frappe.db.commit()
    return {
        "dry_run": bool(dry_run),
        "total": len(rows),
        "stats": dict(stats),
        "failures": failures,
        "sample": samples,
    }


def _bulk_migrate_jackyun_sales_order_identity(
    rows, primary_external_ids, source_trade_numbers, batch_size=500
):
    """Set-based equivalent of Frappe rename for a validated Sales Order batch."""
    from frappe.model.dynamic_links import get_dynamic_link_map
    from frappe.model.rename_doc import get_link_fields

    valid = []
    stats = defaultdict(int)
    failures = []
    samples = []
    for row in rows:
        old_name = frappe.utils.cstr(row.old_name).strip()
        trade_no = frappe.utils.cstr(row.trade_no).strip()
        source_trade_no = frappe.utils.cstr(
            source_trade_numbers.get(primary_external_ids.get(old_name), "")
        ).strip()
        if len(samples) < 10:
            samples.append(
                {
                    "old_name": old_name,
                    "trade_no": trade_no,
                    "source_trade_no": source_trade_no,
                }
            )
        if not old_name or not trade_no:
            stats["invalid"] += 1
            continue
        if not frappe.db.exists("Sales Order", old_name):
            if frappe.db.exists("Sales Order", trade_no):
                old_name = trade_no
                stats["already_renamed"] += 1
            else:
                stats["missing"] += 1
                continue
        elif old_name == trade_no:
            stats["already_renamed"] += 1
        elif frappe.db.exists("Sales Order", trade_no):
            stats["conflict"] += 1
            if len(failures) < 50:
                failures.append(
                    {"old_name": old_name, "trade_no": trade_no, "error": "目标编号已存在"}
                )
            continue
        valid.append((old_name, trade_no, source_trade_no))

    frappe.db.sql("drop temporary table if exists `_tmp_jackyun_so_identity`")
    frappe.db.sql(
        """
        create temporary table `_tmp_jackyun_so_identity` (
            old_name varchar(140) primary key,
            new_name varchar(140) not null unique,
            trade_no varchar(140) not null,
            source_trade_no varchar(140) null
        ) engine=InnoDB
        """
    )

    meta = frappe.get_meta("Sales Order")
    child_tables = sorted({field.options for field in meta.get_table_fields()})
    link_fields = {
        (field.parent, field.fieldname)
        for field in get_link_fields("Sales Order")
        if not field.issingle
    }
    dynamic_fields = set()
    for field in get_dynamic_link_map().get("Sales Order", []):
        field_meta = frappe.get_meta(field.parent)
        if not field_meta.is_virtual and not field_meta.issingle:
            dynamic_fields.add((field.parent, field.fieldname, field.options))

    for offset in range(0, len(valid), batch_size):
        batch = valid[offset : offset + batch_size]
        savepoint = f"bulk_sales_order_identity_{offset}"
        frappe.db.savepoint(savepoint)
        try:
            frappe.db.sql("delete from `_tmp_jackyun_so_identity`")
            placeholders = ",".join(["(%s,%s,%s,%s)"] * len(batch))
            parameters = tuple(
                value
                for old_name, new_name, source_trade_no in batch
                for value in (old_name, new_name, new_name, source_trade_no)
            )
            # row is old_name, new_name, source_trade_no; trade_no equals new_name.
            frappe.db.sql(
                f"""
                insert into `_tmp_jackyun_so_identity`
                    (old_name, new_name, trade_no, source_trade_no)
                values {placeholders}
                """,
                parameters,
            )
            frappe.db.sql(
                """
                update `tabSales Order` doc
                  join `_tmp_jackyun_so_identity` map on map.old_name=doc.name
                   set doc.custom_jackyun_trade_no=map.trade_no,
                       doc.custom_jackyun_source_trade_no=map.source_trade_no,
                       doc.name=map.new_name
                """
            )
            for child_table in child_tables:
                frappe.db.sql(
                    f"""
                    update `tab{child_table}` child
                      join `_tmp_jackyun_so_identity` map on map.old_name=child.parent
                       set child.parent=map.new_name
                     where child.parenttype='Sales Order'
                    """
                )
            for parent, fieldname in sorted(link_fields):
                frappe.db.sql(
                    f"""
                    update `tab{parent}` linked
                      join `_tmp_jackyun_so_identity` map
                        on map.old_name=linked.`{fieldname}`
                       set linked.`{fieldname}`=map.new_name
                    """
                )
            for parent, fieldname, doctype_field in sorted(dynamic_fields):
                frappe.db.sql(
                    f"""
                    update `tab{parent}` linked
                      join `_tmp_jackyun_so_identity` map
                        on map.old_name=linked.`{fieldname}`
                       set linked.`{fieldname}`=map.new_name
                     where linked.`{doctype_field}`='Sales Order'
                    """
                )
            frappe.db.sql(
                """
                update `tabExternal ID Mapping` mapping
                  join `_tmp_jackyun_so_identity` map
                    on map.old_name=mapping.erpnext_name
                   set mapping.erpnext_name=map.new_name,
                       mapping.modified=now(),
                       mapping.modified_by=%s
                 where mapping.erpnext_doctype='Sales Order'
                """,
                frappe.session.user,
            )
            stats["renamed"] += sum(1 for old, new, _source in batch if old != new)
            stats["metadata_updated"] += sum(1 for old, new, _source in batch if old == new)
            frappe.db.commit()
        except Exception as exc:
            frappe.db.rollback(save_point=savepoint)
            stats["failed_batches"] += 1
            failures.append(
                {
                    "offset": offset,
                    "rows": len(batch),
                    "error": str(exc),
                }
            )
            break

    frappe.db.sql("drop temporary table if exists `_tmp_jackyun_so_identity`")
    frappe.clear_cache(doctype="Sales Order")
    return {
        "dry_run": False,
        "bulk": True,
        "total": len(rows),
        "valid": len(valid),
        "stats": dict(stats),
        "failures": failures,
        "sample": samples,
    }


def validate_jackyun_sales_order_identity():
    """Verify identifiers and every Frappe Link/Dynamic Link after bulk rename."""
    from frappe.model.dynamic_links import get_dynamic_link_map
    from frappe.model.rename_doc import get_link_fields

    issues = []
    link_checks = 0
    for field in get_link_fields("Sales Order"):
        if field.issingle:
            continue
        link_checks += 1
        count = frappe.db.sql(
            f"""
            select count(*)
              from `tab{field.parent}` linked
              left join `tabSales Order` sales_order
                on sales_order.name=linked.`{field.fieldname}`
             where ifnull(linked.`{field.fieldname}`, '') <> ''
               and sales_order.name is null
            """
        )[0][0]
        if count:
            issues.append(
                {
                    "kind": "Link",
                    "doctype": field.parent,
                    "fieldname": field.fieldname,
                    "dangling": count,
                }
            )

    dynamic_checks = 0
    for field in get_dynamic_link_map().get("Sales Order", []):
        meta = frappe.get_meta(field.parent)
        if meta.is_virtual or meta.issingle:
            continue
        dynamic_checks += 1
        count = frappe.db.sql(
            f"""
            select count(*)
              from `tab{field.parent}` linked
              left join `tabSales Order` sales_order
                on sales_order.name=linked.`{field.fieldname}`
             where linked.`{field.options}`='Sales Order'
               and ifnull(linked.`{field.fieldname}`, '') <> ''
               and sales_order.name is null
            """
        )[0][0]
        if count:
            issues.append(
                {
                    "kind": "Dynamic Link",
                    "doctype": field.parent,
                    "fieldname": field.fieldname,
                    "dangling": count,
                }
            )

    child_checks = 0
    for field in frappe.get_meta("Sales Order").get_table_fields():
        child_checks += 1
        count = frappe.db.sql(
            f"""
            select count(*)
              from `tab{field.options}` child
              left join `tabSales Order` sales_order on sales_order.name=child.parent
             where child.parenttype='Sales Order' and sales_order.name is null
            """
        )[0][0]
        if count:
            issues.append(
                {
                    "kind": "Child",
                    "doctype": field.options,
                    "fieldname": "parent",
                    "dangling": count,
                }
            )

    summary = frappe.db.sql(
        """
        select count(*) mapped_orders,
               sum(mapping.erpnext_name=substring(mapping.external_id, 7)) matching_names,
               sum(ifnull(sales_order.custom_jackyun_trade_no, '')=
                   substring(mapping.external_id, 7)) matching_trade_fields,
               sum(ifnull(sales_order.custom_jackyun_source_trade_no, '')<>'')
                   with_online_trade_no
          from `tabExternal ID Mapping` mapping
          join `tabSales Order` sales_order on sales_order.name=mapping.erpnext_name
         where mapping.resource='Sales Order' and mapping.external_id like 'trade:%%'
        """,
        as_dict=True,
    )[0]
    dangling_mappings = frappe.db.sql(
        """
        select count(*)
          from `tabExternal ID Mapping` mapping
          left join `tabSales Order` sales_order on sales_order.name=mapping.erpnext_name
         where mapping.resource='Sales Order'
           and mapping.erpnext_doctype='Sales Order'
           and sales_order.name is null
        """
    )[0][0]
    return {
        "summary": summary,
        "dangling_mappings": dangling_mappings,
        "checks": {
            "link_fields": link_checks,
            "dynamic_links": dynamic_checks,
            "child_tables": child_checks,
        },
        "issues": issues,
    }


def calculate_opening_quantities():
    """Return item/warehouse opening quantities implied by snapshot and draft movements."""
    target = defaultdict(float)
    costs = defaultdict(list)
    for row in frappe.get_all(
        "Jackyun Inventory Snapshot",
        fields=["item", "warehouse", "current_quantity", "cost_price"],
    ):
        key = (row.item, row.warehouse)
        target[key] += frappe.utils.flt(row.current_quantity)
        if frappe.utils.flt(row.cost_price) > 0:
            costs[key].append(frappe.utils.flt(row.cost_price))

    movement = defaultdict(float)
    for row in frappe.db.sql(
        """
        select dni.item_code item, dni.warehouse, -sum(dni.stock_qty) delta
          from `tabDelivery Note Item` dni
          join `tabDelivery Note` dn on dn.name=dni.parent
          join `tabWarehouse` w on w.name=dni.warehouse
         where dn.docstatus=0 and w.company not like '%%(Demo)'
         group by dni.item_code, dni.warehouse
        """,
        as_dict=True,
    ):
        movement[(row.item, row.warehouse)] += frappe.utils.flt(row.delta)
    for row in frappe.db.sql(
        """
        select pri.item_code item, pri.warehouse, sum(pri.stock_qty) delta
          from `tabPurchase Receipt Item` pri
          join `tabPurchase Receipt` pr on pr.name=pri.parent
          join `tabWarehouse` w on w.name=pri.warehouse
         where pr.docstatus=0 and w.company not like '%%(Demo)'
         group by pri.item_code, pri.warehouse
        """,
        as_dict=True,
    ):
        movement[(row.item, row.warehouse)] += frappe.utils.flt(row.delta)
    for row in frappe.db.sql(
        """
        select sed.item_code item, sed.s_warehouse warehouse, -sum(sed.transfer_qty) delta
          from `tabStock Entry Detail` sed
          join `tabStock Entry` se on se.name=sed.parent
          join `tabWarehouse` w on w.name=sed.s_warehouse
         where se.docstatus=0 and ifnull(sed.s_warehouse, '') <> ''
           and w.company not like '%%(Demo)'
         group by sed.item_code, sed.s_warehouse
        union all
        select sed.item_code item, sed.t_warehouse warehouse, sum(sed.transfer_qty) delta
          from `tabStock Entry Detail` sed
          join `tabStock Entry` se on se.name=sed.parent
          join `tabWarehouse` w on w.name=sed.t_warehouse
         where se.docstatus=0 and ifnull(sed.t_warehouse, '') <> ''
           and w.company not like '%%(Demo)'
         group by sed.item_code, sed.t_warehouse
        """,
        as_dict=True,
    ):
        movement[(row.item, row.warehouse)] += frappe.utils.flt(row.delta)

    result = []
    for item, warehouse in sorted(set(target) | set(movement)):
        key = (item, warehouse)
        opening = target[key] - movement[key]
        result.append(
            {
                "item": item,
                "warehouse": warehouse,
                "target": target[key],
                "movement": movement[key],
                "opening": opening,
                "valuation_rate": costs[key][0] if costs[key] else 0,
            }
        )
    return result


def opening_dry_run():
    rows = calculate_opening_quantities()
    negatives = [row for row in rows if row["opening"] < -0.000001]
    positives = [row for row in rows if row["opening"] > 0.000001]
    return {
        "rows": len(rows),
        "positive_openings": len(positives),
        "negative_openings": len(negatives),
        "zero_cost_positive_openings": sum(
            1 for row in positives if not row["valuation_rate"]
        ),
        "negative_sample": negatives[:25],
    }


def submit_master_orders(commit_every=50):
    """Submit real-company Sales/Purchase Orders before stock documents."""
    stats = defaultdict(int)
    failures = []
    for doctype in ("Purchase Order", "Sales Order"):
        names = frappe.db.sql(
            f"""
            select name from `tab{doctype}`
             where docstatus=0 and company not like '%%(Demo)'
             order by transaction_date, name
            """,
            pluck=True,
        )
        for index, name in enumerate(names, 1):
            savepoint = f"submit_{doctype.replace(' ', '_')}_{index}"
            frappe.db.savepoint(savepoint)
            try:
                frappe.get_doc(doctype, name).submit()
                stats[f"{doctype}:submitted"] += 1
            except Exception as exc:
                frappe.db.rollback(save_point=savepoint)
                stats[f"{doctype}:failed"] += 1
                if len(failures) < 200:
                    failures.append({"doctype": doctype, "name": name, "error": str(exc)})
            if index % int(commit_every) == 0:
                frappe.db.commit()
        frappe.db.commit()
    return {"stats": dict(stats), "failures": failures}


def fill_missing_transfer_batches():
    """Assign a deterministic existing batch where JackYun transfers omit one."""
    rows = frappe.db.sql(
        """
        select sed.name, sed.item_code
          from `tabStock Entry Detail` sed
          join `tabStock Entry` se on se.name=sed.parent
          join `tabItem` i on i.name=sed.item_code
         where se.docstatus=0 and i.has_batch_no=1 and ifnull(sed.batch_no, '')=''
        """,
        as_dict=True,
    )
    updated = 0
    for row in rows:
        batch = frappe.db.get_value(
            "Batch", {"item": row.item_code, "disabled": 0}, "name", order_by="expiry_date asc, creation asc"
        )
        if batch:
            frappe.db.set_value(
                "Stock Entry Detail",
                row.name,
                {"batch_no": batch, "use_serial_batch_fields": 1},
                update_modified=False,
            )
            updated += 1
    frappe.db.commit()
    return {"required": len(rows), "updated": updated}


def _set_negative_stock(enabled):
    value = 1 if enabled else 0
    frappe.db.set_single_value("Stock Settings", "allow_negative_stock", value)
    frappe.db.set_single_value(
        "Stock Settings", "allow_negative_stock_for_batch", value
    )
    frappe.clear_cache(doctype="Stock Settings")
    frappe.db.commit()


def _prepare_cutover_document(doc):
    """Skip expensive source-accounting refreshes not used by this stock-only cutover."""
    prepare_standalone_jackyun_return(doc)
    doc.flags.ignore_links = True
    doc.flags.ignore_validate = True
    # Financial synchronization is intentionally out of scope, and these documents
    # have no invoices. The final current-time reconciliation supersedes historical
    # valuation reposts while preserving the actual stock ledger movements.
    doc.update_billing_status = lambda *args, **kwargs: None
    doc.update_reserved_qty = lambda *args, **kwargs: None
    doc.repost_future_sle_and_gle = lambda *args, **kwargs: None
    original_update_stock_ledger = doc.update_stock_ledger
    doc.update_stock_ledger = (
        lambda *args, **kwargs: original_update_stock_ledger(allow_negative_stock=True)
    )
    if getattr(doc, "status_updater", None):
        del doc.status_updater[:]


def rebuild_order_fulfilment_totals():
    """Recompute imported order fulfilment fields once, replacing per-voucher updates."""
    frappe.db.sql(
        """
        update `tabSales Order Item` soi
        join `tabSales Order` so on so.name=soi.parent
        join `tabExternal ID Mapping` m
          on m.platform='jackyun' and m.resource='Sales Order'
         and m.erpnext_doctype='Sales Order' and m.erpnext_name=so.name
        left join (
            select dni.so_detail, sum(dni.qty) delivered_qty
              from `tabDelivery Note Item` dni
              join `tabDelivery Note` dn on dn.name=dni.parent
             where dn.docstatus=1 and dn.is_return=0
               and ifnull(dni.so_detail, '') <> ''
             group by dni.so_detail
        ) delivered on delivered.so_detail=soi.name
           set soi.delivered_qty=ifnull(delivered.delivered_qty, 0)
        """
    )
    frappe.db.sql(
        """
        update `tabSales Order` so
        join (
            select parent,
                   least(100, 100 * sum(delivered_qty) / nullif(sum(qty), 0)) per_delivered
              from `tabSales Order Item`
             group by parent
        ) totals on totals.parent=so.name
        join `tabExternal ID Mapping` m
          on m.platform='jackyun' and m.resource='Sales Order'
         and m.erpnext_doctype='Sales Order' and m.erpnext_name=so.name
           set so.per_delivered=ifnull(totals.per_delivered, 0),
               so.status=case
                   when ifnull(totals.per_delivered, 0) >= 99.99 then 'To Bill'
                   else 'To Deliver and Bill'
               end
         where so.docstatus=1
        """
    )
    frappe.db.sql(
        """
        update `tabPurchase Order Item` poi
        join `tabPurchase Order` po on po.name=poi.parent
        join `tabExternal ID Mapping` m
          on m.platform='jackyun' and m.resource='Purchase Order'
         and m.erpnext_doctype='Purchase Order' and m.erpnext_name=po.name
        left join (
            select pri.purchase_order_item, sum(pri.qty) received_qty
              from `tabPurchase Receipt Item` pri
              join `tabPurchase Receipt` pr on pr.name=pri.parent
             where pr.docstatus=1 and pr.is_return=0
               and ifnull(pri.purchase_order_item, '') <> ''
             group by pri.purchase_order_item
        ) received on received.purchase_order_item=poi.name
           set poi.received_qty=ifnull(received.received_qty, 0)
        """
    )
    frappe.db.sql(
        """
        update `tabPurchase Order` po
        join (
            select parent,
                   least(100, 100 * sum(received_qty) / nullif(sum(qty), 0)) per_received
              from `tabPurchase Order Item`
             group by parent
        ) totals on totals.parent=po.name
        join `tabExternal ID Mapping` m
          on m.platform='jackyun' and m.resource='Purchase Order'
         and m.erpnext_doctype='Purchase Order' and m.erpnext_name=po.name
           set po.per_received=ifnull(totals.per_received, 0),
               po.status=case
                   when ifnull(totals.per_received, 0) >= 99.99 then 'To Bill'
                   else 'To Receive and Bill'
               end
         where po.docstatus=1
        """
    )
    frappe.db.commit()
    return {"sales_orders": frappe.db.count("Sales Order", {"docstatus": 1}),
            "purchase_orders": frappe.db.count("Purchase Order", {"docstatus": 1})}


def refresh_submitted_jackyun_delivery_note_statuses(commit_every=100):
    """Refresh derived status text for mapped, submitted Delivery Notes.

    The historical cutover intentionally disabled the expensive status updater
    while posting stock.  This follow-up uses the document's normal status
    calculation, rather than writing the status column directly.
    """
    commit_every = max(frappe.utils.cint(commit_every), 1)
    names = frappe.db.sql(
        """
        select distinct dn.name
          from `tabDelivery Note` dn
          join `tabExternal ID Mapping` map
            on map.platform='jackyun'
           and map.resource='Delivery Note'
           and map.erpnext_doctype='Delivery Note'
           and map.erpnext_name=dn.name
         where dn.docstatus=1 and dn.status='Draft'
         order by dn.name
        """,
        pluck=True,
    )
    result = {"eligible": len(names), "updated": 0, "failed": 0, "failures": []}
    for index, name in enumerate(names, 1):
        savepoint = f"refresh_dn_status_{index}"
        frappe.db.savepoint(savepoint)
        try:
            doc = frappe.get_doc("Delivery Note", name)
            doc.set_status(update=True)
            result["updated"] += 1
        except Exception as exc:
            frappe.db.rollback(save_point=savepoint)
            result["failed"] += 1
            if len(result["failures"]) < 100:
                result["failures"].append({"name": name, "error": str(exc)})
        if index % commit_every == 0:
            frappe.db.commit()
    frappe.db.commit()
    return result


def submit_stock_documents(
    commit_every=1, shard_count=1, shard_index=0, manage_settings=True
):
    """Submit real-company stock documents chronologically with temporary negative stock."""
    stats = defaultdict(int)
    failures = []
    shard_count = max(1, int(shard_count))
    shard_index = int(shard_index) % shard_count
    documents = frappe.db.sql(
        f"""
        select 'Purchase Receipt' doctype, name, posting_date, posting_time, 1 priority
          from `tabPurchase Receipt`
         where docstatus=0 and company not like '%%(Demo)'
           and mod(crc32(name), {shard_count})={shard_index}
        union all
        select 'Stock Entry' doctype, name, posting_date, posting_time, 2 priority
          from `tabStock Entry`
         where docstatus=0 and company not like '%%(Demo)'
           and mod(crc32(name), {shard_count})={shard_index}
        union all
        select 'Delivery Note' doctype, name, posting_date, posting_time, 3 priority
          from `tabDelivery Note`
         where docstatus=0 and company not like '%%(Demo)'
           and mod(crc32(name), {shard_count})={shard_index}
         order by posting_date, posting_time, priority, name
        """,
        as_dict=True,
    )
    original_allowance = frappe.db.get_single_value(
        "Stock Settings", "over_delivery_receipt_allowance"
    )
    originally_negative_batches = frappe.get_all(
        "Batch", filters={"allow_negative_stock_for_batch": 1}, pluck="name"
    )
    from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
        SerialandBatchBundle,
    )
    from erpnext.stock import serial_batch_bundle as serial_batch_bundle_module

    original_batch_validation = SerialandBatchBundle.validate_negative_batch
    original_batch_qty_validation = (
        serial_batch_bundle_module.throw_negative_batch_validation
    )
    SerialandBatchBundle.validate_negative_batch = lambda *args, **kwargs: None
    serial_batch_bundle_module.throw_negative_batch_validation = (
        lambda *args, **kwargs: None
    )
    if frappe.utils.cint(manage_settings):
        _set_negative_stock(True)
        frappe.db.set_single_value("Stock Settings", "over_delivery_receipt_allowance", 100000)
        frappe.db.sql("update `tabBatch` set allow_negative_stock_for_batch=1")
        frappe.clear_cache(doctype="Stock Settings")
        frappe.clear_cache(doctype="Batch")
        frappe.db.commit()
    frappe.flags.dont_execute_stock_reposts = True
    try:
        for index, row in enumerate(documents, 1):
            error = None
            for attempt in range(3):
                try:
                    doc = frappe.get_doc(row.doctype, row.name)
                    _prepare_cutover_document(doc)
                    original_posting_date = doc.posting_date
                    original_posting_time = doc.posting_time
                    cutover_time = frappe.utils.now_datetime()
                    # Failed historical documents now sit before thousands of already
                    # submitted SLEs. Post their stock movement at cutover time to avoid
                    # repeatedly rewriting that entire ledger, then restore the business
                    # date shown on the voucher. The raw JackYun record remains unchanged.
                    doc.posting_date = frappe.utils.getdate(cutover_time)
                    doc.posting_time = frappe.utils.get_time(cutover_time)
                    doc.set_posting_time = 1
                    doc.submit()
                    frappe.db.set_value(
                        row.doctype,
                        row.name,
                        {
                            "posting_date": original_posting_date,
                            "posting_time": original_posting_time,
                        },
                        update_modified=False,
                    )
                    frappe.db.commit()
                    stats[f"{row.doctype}:submitted"] += 1
                    error = None
                    break
                except frappe.QueryDeadlockError as exc:
                    frappe.db.rollback()
                    error = exc
                except Exception as exc:
                    frappe.db.rollback()
                    error = exc
                    break
            if error:
                stats[f"{row.doctype}:failed"] += 1
                if len(failures) < 300:
                    failures.append(
                        {"doctype": row.doctype, "name": row.name, "error": str(error)}
                    )
            if index % int(commit_every) == 0:
                frappe.db.commit()
        frappe.db.commit()
    finally:
        frappe.flags.pop("dont_execute_stock_reposts", None)
        SerialandBatchBundle.validate_negative_batch = original_batch_validation
        serial_batch_bundle_module.throw_negative_batch_validation = (
            original_batch_qty_validation
        )
        if frappe.utils.cint(manage_settings):
            frappe.db.sql("update `tabBatch` set allow_negative_stock_for_batch=0")
            if originally_negative_batches:
                frappe.db.sql(
                    "update `tabBatch` set allow_negative_stock_for_batch=1 where name in %(names)s",
                    {"names": tuple(originally_negative_batches)},
                )
            frappe.db.set_single_value(
                "Stock Settings", "over_delivery_receipt_allowance", original_allowance
            )
            _set_negative_stock(False)
    return {"stats": dict(stats), "failures": failures}


def submit_historical_jackyun_delivery_notes(dry_run=1, commit_every=1, limit=0):
    """Submit only mapped, non-return JackYun sales Delivery Notes.

    Historical vouchers are posted to stock at the cutover time to avoid a
    valuation repost across the complete ledger, while their displayed business
    date is restored afterwards.  Negative stock is enabled only inside this
    operation and is unconditionally restored in ``finally``.  The caller must
    reconcile to a fresh successful JackYun inventory snapshot immediately
    afterwards.
    """
    dry_run = frappe.utils.cint(dry_run)
    commit_every = max(frappe.utils.cint(commit_every), 1)
    limit = max(frappe.utils.cint(limit), 0)
    names = frappe.db.sql(
        """
        select distinct dn.name
          from `tabDelivery Note` dn
          join `tabExternal ID Mapping` map
            on map.platform='jackyun'
           and map.resource='Delivery Note'
           and map.erpnext_doctype='Delivery Note'
           and map.erpnext_name=dn.name
         where dn.docstatus=0 and dn.is_return=0
           and dn.company not like '%%(Demo)'
         order by dn.posting_date, dn.posting_time, dn.name
        """,
        pluck=True,
    )
    if limit:
        names = names[:limit]
    result = {
        "eligible": len(names),
        "submitted": 0,
        "failed": 0,
        "failures": [],
        "sample": names[:10],
    }
    if dry_run:
        return result

    original_allowance = frappe.db.get_single_value(
        "Stock Settings", "over_delivery_receipt_allowance"
    )
    originally_negative_batches = frappe.get_all(
        "Batch", filters={"allow_negative_stock_for_batch": 1}, pluck="name"
    )
    from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
        SerialandBatchBundle,
    )
    from erpnext.stock import serial_batch_bundle as serial_batch_bundle_module

    original_batch_validation = SerialandBatchBundle.validate_negative_batch
    original_batch_qty_validation = (
        serial_batch_bundle_module.throw_negative_batch_validation
    )
    SerialandBatchBundle.validate_negative_batch = lambda *args, **kwargs: None
    serial_batch_bundle_module.throw_negative_batch_validation = (
        lambda *args, **kwargs: None
    )
    _set_negative_stock(True)
    frappe.db.set_single_value("Stock Settings", "over_delivery_receipt_allowance", 100000)
    frappe.db.sql("update `tabBatch` set allow_negative_stock_for_batch=1")
    frappe.clear_cache(doctype="Stock Settings")
    frappe.clear_cache(doctype="Batch")
    frappe.db.commit()
    frappe.flags.dont_execute_stock_reposts = True
    try:
        for index, name in enumerate(names, 1):
            try:
                doc = frappe.get_doc("Delivery Note", name)
                if doc.docstatus != 0:
                    continue
                _prepare_cutover_document(doc)
                original_posting_date = doc.posting_date
                original_posting_time = doc.posting_time
                cutover_time = frappe.utils.now_datetime()
                doc.posting_date = frappe.utils.getdate(cutover_time)
                doc.posting_time = frappe.utils.get_time(cutover_time)
                doc.set_posting_time = 1
                doc.submit()
                frappe.db.set_value(
                    "Delivery Note",
                    doc.name,
                    {
                        "posting_date": original_posting_date,
                        "posting_time": original_posting_time,
                    },
                    update_modified=False,
                )
                frappe.db.commit()
                result["submitted"] += 1
            except Exception as exc:
                frappe.db.rollback()
                result["failed"] += 1
                if len(result["failures"]) < 300:
                    result["failures"].append({"name": name, "error": str(exc)})
            if index % commit_every == 0:
                frappe.db.commit()
    finally:
        frappe.flags.pop("dont_execute_stock_reposts", None)
        SerialandBatchBundle.validate_negative_batch = original_batch_validation
        serial_batch_bundle_module.throw_negative_batch_validation = (
            original_batch_qty_validation
        )
        frappe.db.sql("update `tabBatch` set allow_negative_stock_for_batch=0")
        if originally_negative_batches:
            frappe.db.sql(
                "update `tabBatch` set allow_negative_stock_for_batch=1 where name in %(names)s",
                {"names": tuple(originally_negative_batches)},
            )
        frappe.db.set_single_value(
            "Stock Settings", "over_delivery_receipt_allowance", original_allowance
        )
        _set_negative_stock(False)
    return result


def submit_one(doctype, name):
    _set_negative_stock(True)
    frappe.flags.dont_execute_stock_reposts = True
    try:
        doc = frappe.get_doc(doctype, name)
        _prepare_cutover_document(doc)
        doc.submit()
        frappe.db.commit()
        return {"doctype": doctype, "name": name, "docstatus": doc.docstatus}
    finally:
        frappe.flags.pop("dont_execute_stock_reposts", None)
        _set_negative_stock(False)


def _inventory_snapshot_targets():
    """Build current JackYun targets, preserving batch quantities where applicable."""
    adapter = JackYunAdapter(None)
    item_flags = {
        row.name: row
        for row in frappe.get_all(
            "Item", fields=["name", "has_batch_no", "has_serial_no"]
        )
    }
    plain_targets = defaultdict(lambda: {"qty": 0.0, "rates": []})
    batch_targets = defaultdict(lambda: {"qty": 0.0, "rates": []})
    batch_expected = defaultdict(float)
    batch_totals = defaultdict(float)

    snapshots = frappe.get_all(
        "Jackyun Inventory Snapshot",
        fields=["item", "warehouse", "current_quantity", "cost_price", "batch_data"],
    )
    for row in snapshots:
        if not row.item or not row.warehouse or row.item not in item_flags:
            continue
        flags = item_flags[row.item]
        if flags.has_serial_no:
            frappe.throw(f"商品 {row.item} 启用了序列号管理，不能仅凭库存快照自动校准")
        rate = frappe.utils.flt(row.cost_price)
        key = (row.item, row.warehouse)
        if not flags.has_batch_no:
            plain_targets[key]["qty"] += frappe.utils.flt(row.current_quantity)
            if rate > 0:
                plain_targets[key]["rates"].append(rate)
            continue

        batch_expected[key] += frappe.utils.flt(row.current_quantity)
        raw_batches = row.batch_data
        if isinstance(raw_batches, str):
            try:
                raw_batches = json.loads(raw_batches or "[]")
            except (TypeError, ValueError):
                raw_batches = []
        for raw_batch in raw_batches or []:
            qty = frappe.utils.flt(raw_batch.get("residualQuantity"))
            if abs(qty) < 0.000001:
                continue
            batch_no = adapter._resolve_transaction_batch(row.item, raw_batch.get("batchNo"))
            if not batch_no:
                continue
            batch_key = (row.item, row.warehouse, batch_no)
            batch_targets[batch_key]["qty"] += qty
            batch_totals[key] += qty
            if rate > 0:
                batch_targets[batch_key]["rates"].append(rate)

    for key, expected in batch_expected.items():
        delta = expected - batch_totals[key]
        if abs(delta) > 0.000001:
            item, warehouse = key
            fallback = adapter._resolve_transaction_batch(item, "JYK-UNALLOCATED")
            batch_key = (item, warehouse, fallback)
            batch_targets[batch_key]["qty"] += delta
            batch_totals[key] += delta

    # Anything currently in ERPNext but absent from the snapshot must be brought to zero.
    current_plain_nonzero = set()
    for row in frappe.db.sql(
        """
        select b.item_code item, b.warehouse, b.actual_qty
          from `tabBin` b
          join `tabItem` i on i.name=b.item_code
         where i.has_batch_no=0 and i.has_serial_no=0
           and abs(ifnull(b.actual_qty, 0)) > 0.000001
        """,
        as_dict=True,
    ):
        key = (row.item, row.warehouse)
        current_plain_nonzero.add(key)
        plain_targets[key]

    for row in frappe.db.sql(
        """
        select sb.item_code item, sbe.warehouse, sbe.batch_no, sum(sbe.qty) qty
          from `tabSerial and Batch Entry` sbe
          join `tabSerial and Batch Bundle` sb on sb.name=sbe.parent
         where sb.docstatus=1 and ifnull(sbe.batch_no, '') <> ''
         group by sb.item_code, sbe.warehouse, sbe.batch_no
        having abs(sum(sbe.qty)) > 0.000001
        """,
        as_dict=True,
    ):
        batch_targets[(row.item, row.warehouse, row.batch_no)]

    rows = []
    for (item, warehouse), values in plain_targets.items():
        if abs(values["qty"]) < 0.000001 and (item, warehouse) not in current_plain_nonzero:
            continue
        rates = values["rates"]
        rows.append(
            {
                "item_code": item,
                "warehouse": warehouse,
                "qty": values["qty"],
                "valuation_rate": rates[0] if rates else 0,
                "allow_zero_valuation_rate": 1,
            }
        )
    for (item, warehouse, batch_no), values in batch_targets.items():
        rates = values["rates"]
        rows.append(
            {
                "item_code": item,
                "warehouse": warehouse,
                "batch_no": batch_no,
                "qty": values["qty"],
                "valuation_rate": rates[0] if rates else 0,
                "allow_zero_valuation_rate": 1,
                "use_serial_batch_fields": 1,
                "reconcile_all_serial_batch": 0,
            }
        )
    return rows


def reconcile_current_inventory(chunk_size=150):
    """Set ERPNext current stock to the latest JackYun inventory snapshot."""
    rows_by_company = defaultdict(list)
    for row in _inventory_snapshot_targets():
        company = frappe.db.get_value("Warehouse", row["warehouse"], "company")
        if company and not company.endswith("(Demo)"):
            rows_by_company[company].append(row)

    stats = defaultdict(int)
    failures = []
    posting = frappe.utils.now_datetime()
    from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
        SerialandBatchBundle,
    )
    from erpnext.stock import serial_batch_bundle as serial_batch_bundle_module

    original_batch_validation = SerialandBatchBundle.validate_negative_batch
    original_batch_qty_validation = (
        serial_batch_bundle_module.throw_negative_batch_validation
    )
    SerialandBatchBundle.validate_negative_batch = lambda *args, **kwargs: None
    serial_batch_bundle_module.throw_negative_batch_validation = (
        lambda *args, **kwargs: None
    )
    _set_negative_stock(True)
    frappe.db.sql("update `tabBatch` set allow_negative_stock_for_batch=1")
    frappe.db.commit()
    frappe.flags.dont_execute_stock_reposts = True
    try:
        for company, rows in rows_by_company.items():
            rows.sort(key=lambda d: (d["warehouse"], d["item_code"], d.get("batch_no") or ""))
            for offset in range(0, len(rows), int(chunk_size)):
                chunk = rows[offset : offset + int(chunk_size)]
                doc = frappe.get_doc(
                    {
                        "doctype": "Stock Reconciliation",
                        "company": company,
                        "purpose": "Stock Reconciliation",
                        "posting_date": frappe.utils.getdate(posting),
                        "posting_time": frappe.utils.get_time(posting),
                        "set_posting_time": 1,
                        "items": chunk,
                    }
                )
                try:
                    doc.insert(ignore_permissions=True)
                    doc._submit()
                    frappe.db.commit()
                    if doc.docstatus != 1:
                        frappe.throw(f"库存校准单 {doc.name} 未成功提交")
                    stats["submitted"] += 1
                    stats["rows"] += len(chunk)
                except Exception as exc:
                    frappe.db.rollback()
                    stats["failed"] += 1
                    if len(failures) < 100:
                        failures.append(
                            {
                                "company": company,
                                "offset": offset,
                                "rows": len(chunk),
                                "error": str(exc),
                            }
                        )
        frappe.db.commit()
    finally:
        frappe.flags.pop("dont_execute_stock_reposts", None)
        SerialandBatchBundle.validate_negative_batch = original_batch_validation
        serial_batch_bundle_module.throw_negative_batch_validation = (
            original_batch_qty_validation
        )
        frappe.db.sql("update `tabBatch` set allow_negative_stock_for_batch=0")
        _set_negative_stock(False)
    return {"stats": dict(stats), "failures": failures}


def submit_pending_inventory_reconciliations():
    """Submit cutover reconciliations directly and remove their stale queued jobs."""
    from frappe.utils.background_jobs import get_queue
    from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
        SerialandBatchBundle,
    )
    from erpnext.stock import serial_batch_bundle as serial_batch_bundle_module

    names = frappe.db.sql(
        """
        select name from `tabStock Reconciliation`
         where docstatus=0 and company not like '%%(Demo)'
           and creation >= curdate()
         order by creation
        """,
        pluck=True,
    )
    name_set = set(names)
    removed_jobs = 0
    queue = get_queue("default")
    for job in list(queue.jobs):
        nested = job.kwargs.get("kwargs", {})
        if (
            job.kwargs.get("method") == "frappe.model.document.execute_action"
            and nested.get("__doctype") == "Stock Reconciliation"
            and nested.get("__name") in name_set
        ):
            queue.remove(job.id)
            job.delete()
            removed_jobs += 1

    original_batch_validation = SerialandBatchBundle.validate_negative_batch
    original_batch_qty_validation = (
        serial_batch_bundle_module.throw_negative_batch_validation
    )
    SerialandBatchBundle.validate_negative_batch = lambda *args, **kwargs: None
    serial_batch_bundle_module.throw_negative_batch_validation = (
        lambda *args, **kwargs: None
    )
    _set_negative_stock(True)
    frappe.db.sql("update `tabBatch` set allow_negative_stock_for_batch=1")
    frappe.db.commit()
    submitted = 0
    failures = []
    frappe.flags.dont_execute_stock_reposts = True
    try:
        for name in names:
            doc = frappe.get_doc("Stock Reconciliation", name)
            doc.unlock()
            try:
                doc._submit()
                frappe.db.commit()
                submitted += 1
            except Exception as exc:
                frappe.db.rollback()
                failures.append({"name": name, "error": str(exc)})
    finally:
        frappe.flags.pop("dont_execute_stock_reposts", None)
        SerialandBatchBundle.validate_negative_batch = original_batch_validation
        serial_batch_bundle_module.throw_negative_batch_validation = (
            original_batch_qty_validation
        )
        frappe.db.sql("update `tabBatch` set allow_negative_stock_for_batch=0")
        _set_negative_stock(False)
    return {
        "queued": len(names),
        "removed_jobs": removed_jobs,
        "submitted": submitted,
        "failures": failures,
    }


def inventory_reconciliation_dry_run():
    rows = _inventory_snapshot_targets()
    return {
        "rows": len(rows),
        "batch_rows": sum(1 for row in rows if row.get("batch_no")),
        "positive_rows": sum(1 for row in rows if row["qty"] > 0.000001),
        "zero_rows": sum(1 for row in rows if abs(row["qty"]) <= 0.000001),
        "negative_rows": sum(1 for row in rows if row["qty"] < -0.000001),
    }


def inventory_difference_summary(tolerance=0.000001):
    """Compare ERPNext Bin quantities with the latest JackYun snapshot."""
    target = defaultdict(float)
    for row in frappe.get_all(
        "Jackyun Inventory Snapshot", fields=["item", "warehouse", "current_quantity"]
    ):
        target[(row.item, row.warehouse)] += frappe.utils.flt(row.current_quantity)
    actual = {
        (row.item_code, row.warehouse): frappe.utils.flt(row.actual_qty)
        for row in frappe.db.sql(
            """
            select b.item_code, b.warehouse, b.actual_qty
              from `tabBin` b
              join `tabWarehouse` w on w.name=b.warehouse
             where w.company not like '%%(Demo)'
            """,
            as_dict=True,
        )
    }
    differences = []
    for key in sorted(set(target) | set(actual)):
        delta = actual.get(key, 0) - target.get(key, 0)
        if abs(delta) > frappe.utils.flt(tolerance):
            differences.append(
                {
                    "item": key[0],
                    "warehouse": key[1],
                    "jackyun": target.get(key, 0),
                    "erpnext": actual.get(key, 0),
                    "difference": delta,
                }
            )
    return {"difference_count": len(differences), "sample": differences[:100]}


def reconcile_residual_inventory_totals(tolerance=0.01, dry_run=1):
    """Close residual item/warehouse differences after batch-level reconciliation.

    ERPNext's per-batch reconciliation can leave the item total unchanged when
    historical bundle metadata is inconsistent.  ``reconcile_all_serial_batch``
    is the framework-supported way to set the complete batch-managed item total.
    """
    tolerance = frappe.utils.flt(tolerance)
    dry_run = frappe.utils.cint(dry_run)
    target = defaultdict(float)
    for row in frappe.get_all(
        "Jackyun Inventory Snapshot", fields=["item", "warehouse", "current_quantity"]
    ):
        target[(row.item, row.warehouse)] += frappe.utils.flt(row.current_quantity)
    actual = {
        (row.item_code, row.warehouse): frappe.utils.flt(row.actual_qty)
        for row in frappe.db.sql(
            """
            select b.item_code, b.warehouse, b.actual_qty
              from `tabBin` b
              join `tabWarehouse` w on w.name=b.warehouse
             where w.company not like '%%(Demo)'
            """,
            as_dict=True,
        )
    }
    rows_by_company = defaultdict(list)
    for item, warehouse in sorted(set(target) | set(actual)):
        qty = target.get((item, warehouse), 0)
        if abs(actual.get((item, warehouse), 0) - qty) <= tolerance:
            continue
        company = frappe.db.get_value("Warehouse", warehouse, "company")
        flags = frappe.db.get_value(
            "Item", item, ["has_batch_no", "has_serial_no"], as_dict=True
        )
        if not company or not flags:
            continue
        if flags.has_serial_no:
            frappe.throw(f"商品 {item} 启用了序列号管理，不能做总量兜底盘点")
        rate = frappe.db.get_value("Item", item, "valuation_rate") or 0
        row = {
            "item_code": item,
            "warehouse": warehouse,
            "qty": qty,
            "valuation_rate": rate,
            "allow_zero_valuation_rate": 1,
        }
        if flags.has_batch_no:
            row["reconcile_all_serial_batch"] = 1
        rows_by_company[company].append(row)
    preview = {company: len(rows) for company, rows in rows_by_company.items()}
    if dry_run:
        return {"rows": sum(preview.values()), "by_company": preview}

    posting = frappe.utils.now_datetime()
    result = {"submitted": [], "failed": []}
    _set_negative_stock(True)
    try:
        for company, rows in rows_by_company.items():
            doc = frappe.get_doc(
                {
                    "doctype": "Stock Reconciliation",
                    "company": company,
                    "purpose": "Stock Reconciliation",
                    "posting_date": frappe.utils.getdate(posting),
                    "posting_time": frappe.utils.get_time(posting),
                    "set_posting_time": 1,
                    "items": rows,
                }
            )
            try:
                doc.insert(ignore_permissions=True)
                doc.submit()
                frappe.db.commit()
                result["submitted"].append({"name": doc.name, "rows": len(rows)})
            except Exception as exc:
                frappe.db.rollback()
                result["failed"].append({"company": company, "error": str(exc)})
    finally:
        _set_negative_stock(False)
    return result


def repost_residual_inventory_ledgers(tolerance=0.01):
    """Rebuild qty-after-transaction and Bin totals for residual differences."""
    from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost

    tolerance = frappe.utils.flt(tolerance)
    target = defaultdict(float)
    for row in frappe.get_all(
        "Jackyun Inventory Snapshot", fields=["item", "warehouse", "current_quantity"]
    ):
        target[(row.item, row.warehouse)] += frappe.utils.flt(row.current_quantity)
    actual = {
        (row.item_code, row.warehouse): frappe.utils.flt(row.actual_qty)
        for row in frappe.db.sql(
            """
            select b.item_code, b.warehouse, b.actual_qty
              from `tabBin` b
              join `tabWarehouse` w on w.name=b.warehouse
             where w.company not like '%%(Demo)'
            """,
            as_dict=True,
        )
    }
    rows = []
    for item, warehouse in sorted(set(target) | set(actual)):
        if abs(actual.get((item, warehouse), 0) - target.get((item, warehouse), 0)) <= tolerance:
            continue
        first = frappe.db.get_value(
            "Stock Ledger Entry",
            {"item_code": item, "warehouse": warehouse, "is_cancelled": 0},
            ["posting_date", "posting_time", "creation"],
            as_dict=True,
            order_by="posting_datetime asc, creation asc",
        )
        if first:
            rows.append(
                frappe._dict(
                    item_code=item,
                    warehouse=warehouse,
                    posting_date=first.posting_date,
                    posting_time=first.posting_time,
                    creation=first.creation,
                )
            )
    if rows:
        _set_negative_stock(True)
        try:
            for row in rows:
                company = frappe.db.get_value("Warehouse", row.warehouse, "company")
                repost_doc = frappe.get_doc(
                    {
                        "doctype": "Repost Item Valuation",
                        "based_on": "Item and Warehouse",
                        "item_code": row.item_code,
                        "warehouse": row.warehouse,
                        "posting_date": row.posting_date,
                        "posting_time": row.posting_time,
                        "company": company,
                        "allow_negative_stock": 1,
                        "allow_zero_rate": 1,
                    }
                )
                repost_doc.insert(ignore_permissions=True)
                repost(repost_doc)
                frappe.db.commit()
        finally:
            _set_negative_stock(False)
    return {"reposted": len(rows)}


def repair_orphan_serial_batch_bundles():
    """Finish any bundle left draft by an interrupted stock submission."""
    from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
        SerialandBatchBundle,
    )

    rows = frappe.db.sql(
        """
        select distinct sb.name bundle, sle.item_code, sle.warehouse
          from `tabStock Ledger Entry` sle
          join `tabSerial and Batch Bundle` sb
            on sb.name=sle.serial_and_batch_bundle
         where sle.is_cancelled=0 and sb.docstatus=0
        """,
        as_dict=True,
    )
    original_validation = SerialandBatchBundle.validate_negative_batch
    original_future_validation = SerialandBatchBundle.check_future_entries_exists
    SerialandBatchBundle.validate_negative_batch = lambda *args, **kwargs: None
    SerialandBatchBundle.check_future_entries_exists = lambda *args, **kwargs: None
    _set_negative_stock(True)
    frappe.db.sql("update `tabBatch` set allow_negative_stock_for_batch=1")
    frappe.db.commit()
    repaired = []
    try:
        for row in rows:
            doc = frappe.get_doc("Serial and Batch Bundle", row.bundle)
            doc.unlock()
            doc._submit()
            for batch_no in frappe.get_all(
                "Serial and Batch Entry",
                filters={"parent": row.bundle, "batch_no": ("is", "set")},
                pluck="batch_no",
            ):
                batch_qty = frappe.db.sql(
                    """
                    select ifnull(sum(sbe.qty), 0)
                      from `tabSerial and Batch Entry` sbe
                      join `tabSerial and Batch Bundle` sb on sb.name=sbe.parent
                     where sb.docstatus=1 and sb.is_cancelled=0
                       and sbe.batch_no=%s
                    """,
                    batch_no,
                )[0][0]
                frappe.db.set_value("Batch", batch_no, "batch_qty", batch_qty)

            target = frappe.db.sql(
                """
                select ifnull(sum(current_quantity), 0)
                  from `tabJackyun Inventory Snapshot`
                 where item=%s and warehouse=%s
                """,
                (row.item_code, row.warehouse),
            )[0][0]
            frappe.db.set_value(
                "Bin",
                {"item_code": row.item_code, "warehouse": row.warehouse},
                "actual_qty",
                target,
                update_modified=False,
            )
            latest_sle = frappe.db.get_value(
                "Stock Ledger Entry",
                {
                    "item_code": row.item_code,
                    "warehouse": row.warehouse,
                    "is_cancelled": 0,
                },
                "name",
                order_by="posting_datetime desc, creation desc",
            )
            if latest_sle:
                frappe.db.set_value(
                    "Stock Ledger Entry",
                    latest_sle,
                    "qty_after_transaction",
                    target,
                    update_modified=False,
                )
            frappe.db.commit()
            repaired.append(row.bundle)
    finally:
        SerialandBatchBundle.validate_negative_batch = original_validation
        SerialandBatchBundle.check_future_entries_exists = original_future_validation
        frappe.db.sql("update `tabBatch` set allow_negative_stock_for_batch=0")
        _set_negative_stock(False)
    return {"repaired": repaired}


def retry_failed_stock_raw_records(resources=None, limit=500):
    """Retry selected stock payloads during cutover with temporary batch allowance."""
    from channel_erp.jackyun_integration.doctype.jackyun_raw_record.jackyun_raw_record import (
        retry_processing,
    )
    from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
        SerialandBatchBundle,
    )
    from erpnext.stock import serial_batch_bundle as serial_batch_bundle_module

    resources = tuple(resources or ("Delivery Note", "Stock Movement"))
    limit = max(1, min(frappe.utils.cint(limit) or 500, 2000))
    names = frappe.get_all(
        "Jackyun Raw Record",
        filters={
            "processing_status": "Failed",
            "resource": ["in", resources],
        },
        order_by="creation asc",
        limit_page_length=limit,
        pluck="name",
    )
    original_batch_validation = SerialandBatchBundle.validate_negative_batch
    original_batch_qty_validation = (
        serial_batch_bundle_module.throw_negative_batch_validation
    )
    SerialandBatchBundle.validate_negative_batch = lambda *args, **kwargs: None
    serial_batch_bundle_module.throw_negative_batch_validation = lambda *args, **kwargs: None
    _set_negative_stock(True)
    frappe.db.sql("update `tabBatch` set allow_negative_stock_for_batch=1")
    frappe.clear_cache(doctype="Batch")
    frappe.db.commit()
    frappe.flags.dont_execute_stock_reposts = True
    result = {"attempted": 0, "succeeded": 0, "failed": 0, "failures": []}
    try:
        for name in names:
            outcome = retry_processing(name)
            result["attempted"] += 1
            if outcome.get("ok"):
                result["succeeded"] += 1
            else:
                result["failed"] += 1
                if len(result["failures"]) < 100:
                    result["failures"].append(
                        {"name": name, "error": outcome.get("error")}
                    )
            frappe.db.commit()
    finally:
        frappe.flags.pop("dont_execute_stock_reposts", None)
        SerialandBatchBundle.validate_negative_batch = original_batch_validation
        serial_batch_bundle_module.throw_negative_batch_validation = (
            original_batch_qty_validation
        )
        frappe.db.sql("update `tabBatch` set allow_negative_stock_for_batch=0")
        _set_negative_stock(False)
    return result


def sync_sku_goods_numbers(goods_numbers):
    """Fetch a small explicit SKU set needed by an inventory-sync error."""
    from channel_erp.integrations.jackyun import METHOD_MAP, extract_items

    if isinstance(goods_numbers, str):
        goods_numbers = [value.strip() for value in goods_numbers.split(",") if value.strip()]
    requested = {frappe.utils.cstr(value).strip() for value in goods_numbers or []}
    synced = []
    for connection_row in frappe.get_all("Jackyun Connection", filters={"enabled": 1}):
        connection = frappe.get_doc("Jackyun Connection", connection_row.name)
        adapter = JackYunAdapter(connection)
        payload = adapter.request(
            METHOD_MAP["SKU"], {"goodsNo": ",".join(sorted(requested))}, 0, 200
        )
        for raw in extract_items(payload):
            goods_no = frappe.utils.cstr(raw.get("goodsNo")).strip()
            if goods_no not in requested:
                continue
            item_name, _status = adapter.upsert("SKU", adapter.transform("SKU", raw))
            adapter.remember_sku_aliases(raw, item_name)
            synced.append({"goods_no": goods_no, "item": item_name})
    frappe.db.commit()
    return {"requested": sorted(requested), "synced": synced}


def remove_queued_scheduled_jobs():
    """Remove only stale scheduler dispatches while workers are stopped."""
    from frappe.utils.background_jobs import get_queue

    queue = get_queue("default")
    removed = []
    for job in list(queue.jobs):
        kwargs = job.kwargs or {}
        method = kwargs.get("method")
        if kwargs.get("job_name") != "scheduled_job" and method != (
            "frappe.core.doctype.scheduled_job_type.scheduled_job_type.run_scheduled_job"
        ):
            continue
        queue.remove(job.id)
        job.delete()
        removed.append(job.id)
    return {"queue": queue.name, "removed": removed, "count": len(removed)}


def rebuild_draft_documents(resources=None, commit_every=100):
    """Re-apply latest stored payloads without calling JackYun's API."""
    resources = tuple(resources or STOCK_RESOURCES)
    adapter = JackYunAdapter(None)
    stats = defaultdict(int)
    failures = []

    for resource in resources:
        rows = frappe.db.sql(
            """
            select rr.external_id, rr.raw_data
              from `tabJackyun Raw Record` rr
              join (
                    select external_id, max(creation) creation
                      from `tabJackyun Raw Record`
                     where resource=%s
                     group by external_id
              ) latest
                on latest.external_id=rr.external_id
               and latest.creation=rr.creation
             where rr.resource=%s
             order by rr.external_id
            """,
            (resource, resource),
            as_dict=True,
        )
        for index, row in enumerate(rows, 1):
            savepoint = f"cutover_{index}"
            frappe.db.savepoint(savepoint)
            try:
                raw = frappe.parse_json(row.raw_data) if isinstance(row.raw_data, str) else row.raw_data
                adapter.upsert(resource, adapter.transform(resource, raw))
                stats[f"{resource}:ok"] += 1
            except Exception as exc:
                frappe.db.rollback(save_point=savepoint)
                stats[f"{resource}:failed"] += 1
                if len(failures) < 100:
                    failures.append(
                        {"resource": resource, "external_id": row.external_id, "error": str(exc)}
                    )
            if index % int(commit_every) == 0:
                frappe.db.commit()
        frappe.db.commit()

    return {"stats": dict(stats), "failures": failures}


def unify_ecommerce_customers():
    """Use one shared customer for channels carrying platform/shop metadata."""
    adapter = JackYunAdapter(None)
    customer = adapter._ensure_shared_ecommerce_customer()
    frappe.db.sql(
        """
        update `tabSales Order` so
          join `tabJackyun Sales Channel` ch
            on ch.name=so.custom_jackyun_sales_channel
           set so.customer=%s, so.customer_name=%s
         where so.docstatus=0
           and (
                ifnull(ch.online_platform_code, '') <> ''
             or ifnull(ch.online_platform_name, '') <> ''
             or ifnull(ch.platform_shop_id, '') <> ''
           )
        """,
        (customer, customer),
    )
    frappe.db.sql(
        """
        update `tabDelivery Note` dn
          join `tabJackyun Sales Channel` ch
            on ch.name=dn.custom_jackyun_sales_channel
           set dn.customer=%s, dn.customer_name=%s
         where dn.docstatus=0
           and (
                ifnull(ch.online_platform_code, '') <> ''
             or ifnull(ch.online_platform_name, '') <> ''
             or ifnull(ch.platform_shop_id, '') <> ''
           )
        """,
        (customer, customer),
    )
    frappe.db.commit()
    return {
        "customer": customer,
        "sales_orders": frappe.db.count("Sales Order", {"docstatus": 0, "customer": customer}),
        "delivery_notes": frappe.db.count("Delivery Note", {"docstatus": 0, "customer": customer}),
    }

"""Audit and recoverable deactivation for JackYun pre-cutover documents.

The dry run is strictly read-only. The separately named deactivation entry
cancels only submitted, unprotected stock documents and never hard-deletes
vouchers, mappings, raw payloads or stock reconciliations.
"""

from collections import Counter, defaultdict

import frappe


DEFAULT_CUTOFF = "2026-08-23"
DEFAULT_COMPANIES = (
    "武汉医麦德医疗用品有限公司",
    "咸宁医麦德实业有限公司",
)

DOC_SPECS = {
    "Sales Order": ("transaction_date", "tabSales Order Item"),
    "Delivery Note": ("posting_date", "tabDelivery Note Item"),
    "Purchase Receipt": ("posting_date", "tabPurchase Receipt Item"),
    "Stock Entry": ("posting_date", "tabStock Entry Detail"),
}


def dry_run(cutoff_date=DEFAULT_CUTOFF, companies=None, sample_limit=20):
    """Return and print a strictly read-only pre-cutover cleanup plan.

    Candidate scope is deliberately narrow: JackYun External ID Mapping rows
    whose target is SO/DN/PR/Stock Entry, belongs to one of the two configured
    companies and has a business date before ``cutoff_date``.
    """
    cutoff_date = str(frappe.utils.getdate(cutoff_date))
    companies = tuple(companies or DEFAULT_COMPANIES)
    sample_limit = max(0, min(frappe.utils.cint(sample_limit) or 20, 100))

    identified = []
    for doctype, (date_field, _child_table) in DOC_SPECS.items():
        identified.extend(_mapped_documents(doctype, date_field, cutoff_date, companies))

    by_key = {(row.doctype, row.name): row for row in identified}
    dependencies = _dependencies(by_key, cutoff_date)
    protected_reasons = defaultdict(list)

    for key, refs in dependencies.items():
        active_refs = [ref for ref in refs if frappe.utils.cint(ref.get("docstatus")) != 2]
        if active_refs:
            labels = sorted({f"{ref['doctype']} {ref['name']}" for ref in active_refs})
            protected_reasons[key].append(
                f"存在未取消下游引用：{', '.join(labels[:5])}"
            )
        if key[0] == "Sales Order":
            later = [
                ref
                for ref in active_refs
                if ref.get("business_date")
                and str(ref.business_date) >= cutoff_date
            ]
            if later:
                protected_reasons[key].append(
                    f"销售订单存在 {cutoff_date} 当日或之后的下游单据"
                )

    for key, row in by_key.items():
        if key[0] == "Stock Entry" and str(row.get("is_opening") or "").lower() in {
            "yes",
            "1",
            "true",
        }:
            protected_reasons[key].append("Stock Entry 标记为期初库存")

    protected_keys = set(protected_reasons)
    candidate_keys = set(by_key) - protected_keys
    impacts = {
        "identified": _impact(by_key),
        "unprotected_candidates": _impact(
            {key: by_key[key] for key in candidate_keys}
        ),
        "protected": _impact({key: by_key[key] for key in protected_keys}),
    }
    stock_reconciliation = _stock_reconciliation_protection(companies, cutoff_date)

    dependency_counts = Counter()
    for refs in dependencies.values():
        for ref in refs:
            dependency_counts[ref.doctype] += 1

    result = {
        "dry_run": True,
        "cutoff_date": cutoff_date,
        "companies": list(companies),
        "identified": len(by_key),
        "unprotected_candidates": len(candidate_keys),
        "protected": len(protected_keys),
        "by_doctype": _summarize_documents(by_key, candidate_keys, protected_keys),
        "by_company": _summarize_companies(by_key, candidate_keys, protected_keys),
        "protected_reasons": dict(Counter(
            reason
            for reasons in protected_reasons.values()
            for reason in reasons
        )),
        "dependency_counts": dict(sorted(dependency_counts.items())),
        "impacts": impacts,
        "stock_reconciliation_protected": stock_reconciliation,
        "candidate_samples": _samples(candidate_keys, by_key, sample_limit),
        "protected_samples": _protected_samples(
            protected_keys, by_key, protected_reasons, sample_limit
        ),
        "warning": (
            "候选仅表示未发现当前规则覆盖的直接下游引用，不等于已批准删除；"
            "停用入口仅执行可恢复的取消，不执行硬删除，并保护期初及库存核对单。"
        ),
    }
    _print_report(result)
    return result


def deactivate_unprotected(cutoff_date=DEFAULT_CUTOFF, companies=None):
    """Cancel, but do not delete, unprotected pre-cutover stock documents.

    The operation preserves the source voucher, mappings and raw payload for
    audit.  Protected Sales Orders and all Stock Reconciliations remain intact.
    Inventory must be reconciled to a fresh JackYun snapshot immediately after.
    """
    from channel_erp.inventory_cutover import _prepare_cutover_document, _set_negative_stock
    from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
        SerialandBatchBundle,
    )
    from erpnext.stock import serial_batch_bundle as serial_batch_bundle_module

    cutoff_date = str(frappe.utils.getdate(cutoff_date))
    companies = tuple(companies or DEFAULT_COMPANIES)
    identified = []
    for doctype, (date_field, _child_table) in DOC_SPECS.items():
        identified.extend(_mapped_documents(doctype, date_field, cutoff_date, companies))
    by_key = {(row.doctype, row.name): row for row in identified}
    dependencies = _dependencies(by_key, cutoff_date)
    candidates = []
    for key, row in by_key.items():
        active_refs = [
            ref for ref in dependencies.get(key, [])
            if frappe.utils.cint(ref.get("docstatus")) != 2
        ]
        if active_refs:
            continue
        if key[0] == "Sales Order":
            continue
        if key[0] == "Stock Entry" and str(row.get("is_opening") or "").lower() in {
            "yes", "1", "true",
        }:
            continue
        if frappe.utils.cint(row.docstatus) == 1:
            candidates.append(row)

    candidates.sort(
        key=lambda row: (str(row.business_date), row.doctype, row.name), reverse=True
    )
    original_batch_validation = SerialandBatchBundle.validate_negative_batch
    original_batch_qty_validation = serial_batch_bundle_module.throw_negative_batch_validation
    SerialandBatchBundle.validate_negative_batch = lambda *args, **kwargs: None
    serial_batch_bundle_module.throw_negative_batch_validation = lambda *args, **kwargs: None
    _set_negative_stock(True)
    frappe.db.sql("update `tabBatch` set allow_negative_stock_for_batch=1")
    frappe.db.commit()
    frappe.flags.dont_execute_stock_reposts = True
    result = {"eligible": len(candidates), "cancelled": 0, "failed": 0, "failures": []}
    try:
        for row in candidates:
            try:
                doc = frappe.get_doc(row.doctype, row.name)
                original_date = doc.get(DOC_SPECS[row.doctype][0])
                original_time = doc.get("posting_time") if row.doctype != "Sales Order" else None
                _prepare_cutover_document(doc)
                if row.doctype != "Sales Order":
                    now = frappe.utils.now_datetime()
                    doc.posting_date = frappe.utils.getdate(now)
                    doc.posting_time = frappe.utils.get_time(now)
                    doc.set_posting_time = 1
                doc.cancel()
                restore = {DOC_SPECS[row.doctype][0]: original_date}
                if original_time is not None:
                    restore["posting_time"] = original_time
                frappe.db.set_value(row.doctype, row.name, restore, update_modified=False)
                frappe.db.commit()
                result["cancelled"] += 1
            except Exception as exc:
                frappe.db.rollback()
                result["failed"] += 1
                if len(result["failures"]) < 100:
                    result["failures"].append(
                        {"doctype": row.doctype, "name": row.name, "error": str(exc)}
                    )
    finally:
        frappe.flags.pop("dont_execute_stock_reposts", None)
        SerialandBatchBundle.validate_negative_batch = original_batch_validation
        serial_batch_bundle_module.throw_negative_batch_validation = original_batch_qty_validation
        frappe.db.sql("update `tabBatch` set allow_negative_stock_for_batch=0")
        _set_negative_stock(False)
    return result


def _mapped_documents(doctype, date_field, cutoff_date, companies):
    opening_field = "d.is_opening" if doctype == "Stock Entry" else "NULL"
    return frappe.db.sql(
        f"""
        select %(doctype)s as doctype, d.name, d.company, d.docstatus,
               d.`{date_field}` as business_date, {opening_field} as is_opening,
               group_concat(distinct m.resource order by m.resource separator ', ') resources,
               count(distinct m.name) mapping_count
          from `tabExternal ID Mapping` m
          join `tab{doctype}` d on d.name=m.erpnext_name
         where m.platform='jackyun'
           and m.erpnext_doctype=%(doctype)s
           and d.company in %(companies)s
           and d.`{date_field}` < %(cutoff)s
         group by d.name
        """,
        {"doctype": doctype, "companies": companies, "cutoff": cutoff_date},
        as_dict=True,
    )


def _dependencies(documents, cutoff_date):
    del cutoff_date  # business dates are returned for the caller's later-ref rule
    result = defaultdict(list)
    names = defaultdict(list)
    for doctype, name in documents:
        names[doctype].append(name)

    _add_refs(
        result,
        "Sales Order",
        names["Sales Order"],
        """
        select dni.against_sales_order source_name, 'Delivery Note' doctype,
               dn.name, dn.docstatus, dn.posting_date business_date
          from `tabDelivery Note Item` dni
          join `tabDelivery Note` dn on dn.name=dni.parent
         where dni.against_sales_order in %(names)s
        union all
        select sii.sales_order, 'Sales Invoice', si.name, si.docstatus, si.posting_date
          from `tabSales Invoice Item` sii
          join `tabSales Invoice` si on si.name=sii.parent
         where sii.sales_order in %(names)s
        union all
        select pli.sales_order, 'Pick List', pl.name, pl.docstatus, date(pl.creation)
          from `tabPick List Item` pli
          join `tabPick List` pl on pl.name=pli.parent
         where pli.sales_order in %(names)s
        union all
        select wo.sales_order, 'Work Order', wo.name, wo.docstatus, date(wo.creation)
          from `tabWork Order` wo
         where wo.sales_order in %(names)s
        """,
    )
    _add_refs(
        result,
        "Delivery Note",
        names["Delivery Note"],
        """
        select sii.delivery_note source_name, 'Sales Invoice' doctype,
               si.name, si.docstatus, si.posting_date business_date
          from `tabSales Invoice Item` sii
          join `tabSales Invoice` si on si.name=sii.parent
         where sii.delivery_note in %(names)s
        union all
        select dn.return_against, 'Delivery Note', dn.name, dn.docstatus, dn.posting_date
          from `tabDelivery Note` dn
         where dn.return_against in %(names)s
        """,
    )
    _add_refs(
        result,
        "Purchase Receipt",
        names["Purchase Receipt"],
        """
        select pii.purchase_receipt source_name, 'Purchase Invoice' doctype,
               pi.name, pi.docstatus, pi.posting_date business_date
          from `tabPurchase Invoice Item` pii
          join `tabPurchase Invoice` pi on pi.name=pii.parent
         where pii.purchase_receipt in %(names)s
        union all
        select pr.return_against, 'Purchase Receipt', pr.name, pr.docstatus, pr.posting_date
          from `tabPurchase Receipt` pr
         where pr.return_against in %(names)s
        """,
    )
    _add_refs(
        result,
        "Stock Entry",
        names["Stock Entry"],
        """
        select se.outgoing_stock_entry source_name, 'Stock Entry',
               se.name, se.docstatus, se.posting_date business_date
          from `tabStock Entry` se
         where se.outgoing_stock_entry in %(names)s
        """,
    )

    # Amendments are direct dependencies for every selected doctype.
    for doctype, selected_names in names.items():
        if not selected_names:
            continue
        date_field = DOC_SPECS[doctype][0]
        refs = frappe.db.sql(
            f"""
            select amended_from source_name, %(doctype)s doctype, name,
                   docstatus, `{date_field}` business_date
              from `tab{doctype}`
             where amended_from in %(names)s
            """,
            {"doctype": doctype, "names": tuple(selected_names)},
            as_dict=True,
        )
        for ref in refs:
            result[(doctype, ref.source_name)].append(ref)
    return result


def _add_refs(result, source_doctype, names, sql):
    if not names:
        return
    for ref in frappe.db.sql(sql, {"names": tuple(names)}, as_dict=True):
        if ref.source_name:
            result[(source_doctype, ref.source_name)].append(ref)


def _impact(documents):
    if not documents:
        return {
            "documents": 0,
            "mappings": 0,
            "raw_records": 0,
            "child_rows": 0,
            "stock_ledger_entries": 0,
            "serial_batch_bundles": 0,
        }
    totals = Counter(documents=0, mappings=0, raw_records=0, child_rows=0,
                     stock_ledger_entries=0, serial_batch_bundles=0)
    by_doctype = defaultdict(list)
    for (doctype, name), row in documents.items():
        by_doctype[doctype].append(name)
        totals["documents"] += 1
        totals["mappings"] += frappe.utils.cint(row.mapping_count)

    for doctype, names in by_doctype.items():
        names = tuple(names)
        child_table = DOC_SPECS[doctype][1]
        totals["child_rows"] += frappe.db.sql(
            f"select count(*) from `{child_table}` where parent in %(names)s",
            {"names": names},
        )[0][0]
        totals["stock_ledger_entries"] += frappe.db.sql(
            """
            select count(*) from `tabStock Ledger Entry`
             where voucher_type=%(doctype)s and voucher_no in %(names)s
            """,
            {"doctype": doctype, "names": names},
        )[0][0]
        totals["serial_batch_bundles"] += frappe.db.sql(
            """
            select count(*) from `tabSerial and Batch Bundle`
             where voucher_type=%(doctype)s and voucher_no in %(names)s
            """,
            {"doctype": doctype, "names": names},
        )[0][0]
        totals["raw_records"] += frappe.db.sql(
            """
            select count(distinct r.name)
              from `tabJackyun Raw Record` r
              join `tabExternal ID Mapping` m
                on m.resource=r.resource
               and m.external_id=r.external_id
             where m.platform='jackyun'
               and m.erpnext_doctype=%(doctype)s
               and m.erpnext_name in %(names)s
            """,
            {"doctype": doctype, "names": names},
        )[0][0]
    return dict(totals)


def _stock_reconciliation_protection(companies, cutoff_date):
    rows = frappe.db.sql(
        """
        select docstatus,
               case when posting_date < %(cutoff)s then 'before' else 'on_after' end period,
               count(*) documents
          from `tabStock Reconciliation`
         where company in %(companies)s
         group by docstatus, period
        """,
        {"companies": companies, "cutoff": cutoff_date},
        as_dict=True,
    )
    sle = frappe.db.sql(
        """
        select count(*) from `tabStock Ledger Entry` sle
          join `tabWarehouse` w on w.name=sle.warehouse
         where sle.voucher_type='Stock Reconciliation'
           and w.company in %(companies)s
        """,
        {"companies": companies},
    )[0][0]
    return {
        "excluded_from_scope": True,
        "documents": sum(row.documents for row in rows),
        "stock_ledger_entries": sle,
        "by_status_and_period": [dict(row) for row in rows],
    }


def _summarize_documents(documents, candidate_keys, protected_keys):
    summary = defaultdict(lambda: Counter(identified=0, unprotected_candidates=0, protected=0))
    for key, row in documents.items():
        bucket = summary[row.doctype]
        bucket["identified"] += 1
        bucket["docstatus_%s" % frappe.utils.cint(row.docstatus)] += 1
        if key in protected_keys:
            bucket["protected"] += 1
        elif key in candidate_keys:
            bucket["unprotected_candidates"] += 1
    return {doctype: dict(values) for doctype, values in sorted(summary.items())}


def _summarize_companies(documents, candidate_keys, protected_keys):
    summary = defaultdict(lambda: Counter(identified=0, unprotected_candidates=0, protected=0))
    for key, row in documents.items():
        bucket = summary[row.company]
        bucket["identified"] += 1
        if key in protected_keys:
            bucket["protected"] += 1
        elif key in candidate_keys:
            bucket["unprotected_candidates"] += 1
    return {company: dict(values) for company, values in sorted(summary.items())}


def _samples(keys, documents, limit):
    return [
        {
            "doctype": key[0],
            "name": key[1],
            "company": documents[key].company,
            "business_date": str(documents[key].business_date),
            "docstatus": documents[key].docstatus,
            "resources": documents[key].resources,
        }
        for key in sorted(keys)[:limit]
    ]


def _protected_samples(keys, documents, reasons, limit):
    rows = _samples(keys, documents, limit)
    for row in rows:
        row["reasons"] = reasons[(row["doctype"], row["name"])]
    return rows


def _print_report(result):
    print("=" * 80)
    print("吉客云切换日前业务数据清理 DRY-RUN（只读）")
    print("=" * 80)
    print(f"切换日: {result['cutoff_date']}（仅识别该日期之前）")
    print(
        f"识别 {result['identified']} | 未发现直接保护条件 {result['unprotected_candidates']} "
        f"| 受保护 {result['protected']}"
    )
    for doctype, values in result["by_doctype"].items():
        print(f"  {doctype}: {values}")
    for company, values in result["by_company"].items():
        print(f"  {company}: {values}")
    print("影响统计:")
    for scope, values in result["impacts"].items():
        print(f"  {scope}: {values}")
    print("Stock Reconciliation/期初保护:")
    print(f"  {result['stock_reconciliation_protected']}")
    print(result["warning"])

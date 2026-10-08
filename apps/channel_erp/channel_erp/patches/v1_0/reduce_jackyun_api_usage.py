import frappe

# 出库/退货类交易单据从 5 分钟降到 15 分钟，降低吉客云接口调用量。
TRANSACTION_RESOURCES = ("Sales Order", "Delivery Note", "Sales Return")

# 出库与采购入库数据对实时性要求不高，切换为日批：每天 12:01 同步前一日数据。
DAILY_BATCH_RESOURCES = ("Delivery Note", "Purchase Receipt")
DAILY_RUN_TIME = "12:01:00"


def execute():
    for name in frappe.get_all("Jackyun Connection", pluck="name"):
        doc = frappe.get_doc("Jackyun Connection", name)
        changed = False
        for row in doc.sync_schedules:
            if row.resource in TRANSACTION_RESOURCES and frappe.utils.cint(row.interval_minutes) == 5:
                row.interval_minutes = 15
                changed = True
            if row.resource in DAILY_BATCH_RESOURCES:
                row.interval_minutes = 1440
                row.daily_run_time = DAILY_RUN_TIME
                row.next_run_at = None
                changed = True
        if changed:
            doc.save(ignore_permissions=True)
    frappe.db.commit()

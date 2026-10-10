"""吉客云批次库存快照（只读镜像，不产生任何单据或库存流水）。

设计：
- 数据源 erp.stockquantity.get 的 batchList（residualQuantity/lockedQuantity/
  productionDate/expirationDate），quantityId 游标分页；
- 增量：gmtModified 最近 11 分钟窗口，白天 6:00~23:00 每 10 分钟由调度器触发；
- 全量：无时间窗口游标遍历，每日凌晨跑一次兜底校准，并清除不再出现的行；
- 写入用 INSERT ... ON DUPLICATE KEY UPDATE 批量 upsert，纯数据镜像。
"""
from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

PAGE_SIZE = 200
MAX_PAGES_PER_RUN = 30
FULL_REFRESH_HOUR = 2  # 每日全量兜底在凌晨（由 cron 单独触发）


class JackyunBatchInventory(Document):
	"""吉客云批次库存快照行（只读镜像）。"""

	def validate(self):
		# 纯镜像表：可用数量始终由剩余-锁定推导，防止外部写入错值
		if self.quantity is not None and self.locked_quantity is not None:
			self.available_quantity = frappe.utils.flt(
				self.quantity
			) - frappe.utils.flt(self.locked_quantity)


def _adapter():
    from channel_erp.integrations.jackyun import JackYunAdapter

    connections = frappe.get_all(
        "Jackyun Connection", filters={"enabled": 1}, pluck="name"
    )
    if not connections:
        return None
    return JackYunAdapter(frappe.get_doc("Jackyun Connection", connections[0]))


def _warehouse_map(adapter):
    """吉客云仓库标识 -> ERPNext 仓库名。"""
    rows = frappe.get_all(
        "External ID Mapping",
        filters={"platform": "jackyun", "resource": "Warehouse"},
        fields=["external_id", "erpnext_name"],
    )
    out = {}
    for r in rows:
        wh = r.erpnext_name
        if wh and frappe.db.exists("Warehouse", wh):
            out[str(r.external_id)] = wh
            if str(r.external_id).startswith("code:"):
                out[str(r.external_id)[5:]] = wh
    return out


def _upsert_rows(rows):
    """批量 upsert 快照行。rows: list[dict] 字段齐备。"""
    if not rows:
        return
    now = now_datetime()
    columns = [
        "name", "creation", "modified", "modified_by", "owner", "docstatus",
        "item_code", "item_name", "warehouse_code", "warehouse",
        "jackyun_warehouse_name", "batch_no", "quantity", "locked_quantity",
        "available_quantity", "quantity_id", "snapshot_at",
        "production_date", "expiry_date",
    ]
    values_sql = []
    params = []
    for row in rows:
        name = f"{row['item_code']}~{row['warehouse_code']}~{row['batch_no']}"[:140]
        placeholders = ["%s"] * len(columns)
        values_sql.append("(" + ", ".join(placeholders) + ")")
        params.extend([
            name, now, now, "Administrator", "Administrator", 0,
            row["item_code"], row["item_name"], row["warehouse_code"],
            row["warehouse"], row["jackyun_warehouse_name"], row["batch_no"],
            row["quantity"], row["locked_quantity"], row["available_quantity"],
            row["quantity_id"], row["snapshot_at"],
            row.get("production_date"), row.get("expiry_date"),
        ])
    update_cols = [
        "item_code", "item_name", "warehouse", "jackyun_warehouse_name",
        "quantity", "locked_quantity", "available_quantity", "quantity_id",
        "snapshot_at", "production_date", "expiry_date", "modified",
    ]
    update_clause = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in update_cols)
    frappe.db.sql(
        f"insert into `tabJackyun Batch Inventory` ({', '.join('`' + c + '`' for c in columns)}) "
        f"values {', '.join(values_sql)} "
        f"on duplicate key update {update_clause}",
        tuple(params),
    )


def _fetch_and_store(since=None, until=None, max_pages=None):
    """按 gmtModified 窗口 + quantityId 游标拉取并 upsert。

    返回 (行数, 页数)。增量传 since（datetime）；全量不传。
    全量约 56 页，max_pages 默认放开以一次跑完。
    """
    from channel_erp.integrations.jackyun import extract_records_by_identity

    adapter = _adapter()
    if not adapter:
        return 0, 0
    wh_map = _warehouse_map(adapter)
    now = now_datetime()
    page_limit = max_pages or MAX_PAGES_PER_RUN
    biz = {"isBlockup": 2, "isChannelReserve": 1}
    if since:
        biz["gmtModifiedStart"] = since.strftime("%Y-%m-%d %H:%M:%S")
        biz["gmtModifiedEnd"] = (until or now).strftime("%Y-%m-%d %H:%M:%S")

    total_rows = 0
    pages = 0
    max_qid = 0
    while pages < page_limit:
        payload = adapter.request(
            "erp.stockquantity.get",
            {**biz, "maxQuantityId": max_qid},
            page_index=0,
            page_size=PAGE_SIZE,
        )
        records = extract_records_by_identity(
            payload, ["quantityId", "warehouseCode", "skuId", "goodsNo"]
        )
        if not records:
            break
        pages += 1
        rows = []
        for rec in records:
            wh = wh_map.get(str(rec.get("warehouseId") or "")) or wh_map.get(
                f"code:{rec.get('warehouseCode')}"
            )
            if not wh:
                continue
            for b in rec.get("batchList") or []:
                bno = str(b.get("batchNo") or "").strip()
                if not bno:
                    continue
                qty = float(b.get("residualQuantity") or 0)
                locked = float(b.get("lockedQuantity") or 0)
                rows.append({
                    "item_code": str(rec.get("goodsNo") or "").strip(),
                    "item_name": str(rec.get("goodsName") or "")[:200],
                    "warehouse_code": str(rec.get("warehouseCode") or ""),
                    "warehouse": wh,
                    "jackyun_warehouse_name": str(rec.get("warehouseName") or ""),
                    "batch_no": bno,
                    "quantity": qty,
                    "locked_quantity": locked,
                    "available_quantity": qty - locked,
                    "quantity_id": str(rec.get("quantityId") or ""),
                    "snapshot_at": now,
                    "production_date": b.get("productionDate"),
                    "expiry_date": b.get("expirationDate"),
                })
        _upsert_rows(rows)
        total_rows += len(rows)
        frappe.db.commit()

        cursor_values = []
        for record in records:
            try:
                cursor_values.append(int(record.get("quantityId")))
            except (TypeError, ValueError):
                continue
        nxt = max(cursor_values, default=max_qid)
        if nxt <= max_qid or len(records) < PAGE_SIZE:
            break
        max_qid = nxt

    return total_rows, pages


def refresh_snapshot_tick():
    """调度器入口：白天（6:00-23:00）每 10 分钟增量刷新，夜间直接返回。"""
    now = now_datetime()
    if now.hour < 6 or now.hour >= 23:
        return
    if frappe.db.exists("DocType", "Jackyun Batch Inventory"):
        try:
            rows, pages = _fetch_and_store(since=now - timedelta(minutes=11))
            if rows:
                frappe.logger("channel_erp.snapshot").info(
                    "batch inventory incremental rows=%s pages=%s", rows, pages
                )
        except Exception as exc:  # noqa: BLE001
            frappe.logger("channel_erp.snapshot").error(
                "batch inventory incremental failed: %s", str(exc)[:300]
            )


def refresh_snapshot_full():
    """每日凌晨全量兜底：游标遍历全部库存并清除不再出现的行。"""
    if not frappe.db.exists("DocType", "Jackyun Batch Inventory"):
        return
    try:
        rows, pages = _fetch_and_store(max_pages=100)
        # 清除本次全量未覆盖的行（下架商品/清零批次）
        frappe.db.sql(
            "delete from `tabJackyun Batch Inventory` where snapshot_at < %s",
            (now_datetime() - timedelta(minutes=30),),
        )
        frappe.db.commit()
        frappe.logger("channel_erp.snapshot").info(
            "batch inventory full rows=%s pages=%s", rows, pages
        )
        return {"rows": rows, "pages": pages}
    except Exception as exc:  # noqa: BLE001
        frappe.logger("channel_erp.snapshot").error(
            "batch inventory full failed: %s", str(exc)[:300]
        )
        return {"error": str(exc)[:300]}


@frappe.whitelist()
def manual_refresh(full: int = 0):
    """手动刷新（页面按钮/调试用）。full=1 时全量。"""
    if frappe.utils.cint(full):
        return refresh_snapshot_full() or {}
    now = now_datetime()
    rows, pages = _fetch_and_store(since=now - timedelta(minutes=11))
    frappe.db.commit()
    return {"rows": rows, "pages": pages}

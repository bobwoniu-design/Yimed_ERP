"""吉客云（JackYun）适配器 —— 忠实翻译自 YimedOS 的 jikeyun-adapter.ts。

鉴权：appkey + secret + md5 签名
签名：md5((secret + 排序后的 key+value 拼接 + secret).lower())
网关：https://open.jackyun.com/open/openapi/do
"""
import hashlib
import importlib
import json
import random
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import frappe
import requests

from channel_erp.integrations.base import BaseAdapter
from channel_erp.integrations.connector_contracts import OperationResult, PreflightResult, ProbeResult

# 字段参数名因接口而异（注意 "selelctFields" 是吉客云侧的真实拼写，不是笔误）
FIELD_PARAM_BY_METHOD = {
    "erp.goodsdocin.v2": "selelctFields",
    "erp.goodsdocout.v2": "selelctFields",
    "erp.storage.goodsdocin.v2": "selelctFields",
    "erp.storage.goodsdocout.v2": "selelctFields",
    "erp.combined.get.v2": "selelctFields",
    "erp.allocate.search": "cols",
    "erp.purch.search": "cols",
    "oms.trade.fullinfoget": "fields",
}

# 分页用嵌套 pageInfo 对象的方法（其余接口用平铺 pageIndex/pageSize）
NESTED_PAGE_INFO_METHODS = {"oms.customerQuotation.query"}

# 时间窗参数名因接口而异；未列出的方法保持 startModified/endModified 原样（吉客云会静默忽略）
WINDOW_PARAM_BY_METHOD = {
    "erp.goodsdocin.v2": {"end": "gmtModifiedEnd", "start": "gmtModifiedStart"},
    "erp.goodsdocout.v2": {"end": "gmtModifiedEnd", "start": "gmtModifiedStart"},
    "erp.storage.goodsdocin.v2": {"end": "gmtModifiedEnd", "start": "gmtModifiedStart"},
    "erp.storage.goodsdocout.v2": {"end": "gmtModifiedEnd", "start": "gmtModifiedStart"},
    "erp.combined.get.v2": {"end": "gmtModifiedEnd", "start": "gmtModifiedStart"},
    "erp.allocate.search": {"end": "endCreateTime", "start": "startCreateTime"},
    "wms.stocktake.get": {"end": "endDate", "start": "startDate"},
}

# 资源类型 -> 吉客云方法名
METHOD_MAP = {
    "Company": "erp.company.query",
    "Department": "erp.depart.query",
    "Category": "erp.goodscate.get",
    "Sales Channel": "erp.sales.get",
    "SKU": "erp.storage.goodslist",
    "Product Bundle": "erp-goods.goods.listgoodspackage",
    "Sales Order": "oms.trade.fullinfoget",
    "Purchase Order": "erp.purch.search",
    "Purchase Receipt": "erp-busiorder.goodsdocin.search",
    "Delivery Note": "erp-busiorder.goodsdocout.search",
    "Sales Return": "erp-busiorder.goodsdocin.search",
    "Purchase Return": "erp-busiorder.goodsdocout.search",
    "Stock Transfer": "erp.allocate.search",
    "Stocktake": "wms.stocktake.get",
    "Batch": "erp.goodsbatchinfo.get",
    "Inventory": "erp.stockquantity.get",
    "Supplier": "erp.vend.get",
    "Customer": "crm.customer.list.customized",
    "Offline Customer": "crm.customer.list.customized",
    "Warehouse": "erp.warehouse.get",
    "Stock Movement": "erp-busiorder.goodsdocin/goodsdocout.search",
}

# 业务单据切换日以前的采购订单不作为新同步收货单的来源单据。
# 这些历史收货仍可作为独立 Purchase Receipt 保存，但不得链接历史或已取消 PO。
PURCHASE_RECEIPT_LINK_CUTOFF = "2026-08-23"

# 缺失的出入库流水类型 -> ERPNext Stock Entry 类型（纯流水镜像，不建工单、不关联 BOM）。
# 统一用最基础的 Material Receipt / Material Issue 记数量增减，生产/翻新/组装等业务语义
# 只保留在 remarks 的 inouttypeName 里，避免 Manufacture/Consumption 类型的成品/工单校验。
STOCK_MOVEMENT_INBOUND_TYPES = {
    "103": "Material Receipt",   # 盘盈入库
    "104": "Material Receipt",   # 其他入库
    "106": "Material Receipt",   # 生产完工入库（成品入库）
    "107": "Material Receipt",   # 组装拆卸入库
    "108": "Material Receipt",   # 翻新入库
    "109": "Material Receipt",   # 报废入库
    "113": "Material Receipt",   # 退料入库
    "120": "Material Receipt",   # 生产其他入库
}
STOCK_MOVEMENT_OUTBOUND_TYPES = {
    "204": "Material Issue",     # 其他出库
    "206": "Material Issue",     # 生产领料
    "208": "Material Issue",     # 翻新出库
}

DIRECT_KEYS = ["data", "items", "list", "rows", "result", "records"]

DEFAULT_TIMEOUT_S = 90
MAX_RETRIES = 3
RETRY_BASE_S = 0.4

# ---------------------------------------------------------------------------
# 额度不足熔断：检测到吉客云返回“额度不足”（0130000609）时写入 Redis 标志，
# 在冷却时间内跳过所有定时同步，避免无效调用继续消耗额度并刷爆错误日志。
QUOTA_CIRCUIT_KEY = "channel_erp:jackyun:quota_circuit_until"
DEFAULT_QUOTA_CIRCUIT_HOURS = 2


def _is_quota_exhausted_message(message):
    text = str(message or "")
    return "0130000609" in text or "额度不足" in text


def trip_quota_circuit(hours=None):
    """Trip the quota circuit breaker with a configurable cooldown window."""
    hours = hours or frappe.conf.get("jackyun_quota_circuit_hours") or DEFAULT_QUOTA_CIRCUIT_HOURS
    until = datetime.now() + timedelta(hours=hours)
    frappe.cache().set_value(
        QUOTA_CIRCUIT_KEY, until.timestamp(), expires_in_sec=int(hours * 3600)
    )
    frappe.logger("channel_erp.scheduler").warning(
        "JackYun quota circuit tripped for %s hours (until %s)", hours, until
    )


def is_quota_circuit_open(now=None):
    try:
        until = frappe.cache().get_value(QUOTA_CIRCUIT_KEY)
    except Exception:
        return False
    if not until:
        return False
    current = (now or datetime.now()).timestamp()
    return current < float(until)


def clear_quota_circuit():
    frappe.cache().delete_value(QUOTA_CIRCUIT_KEY)
    frappe.logger("channel_erp.scheduler").info("JackYun quota circuit cleared")
    return {"ok": True}


@frappe.whitelist()
def reset_quota_circuit():
    """额度充值后手动解除熔断（管理员权限）。"""
    if frappe.session.user != "Administrator":
        frappe.only_for("System Manager")
    return clear_quota_circuit()

PAGE_SIZE = 100
PACKAGE_PAGE_SIZE = 200
STOCK_PAGE_SIZE = 200
SALES_ORDER_PAGE_SIZE = 100
PURCHASE_ORDER_PAGE_SIZE = 100
STRICT_SUCCESS_METHODS = {
    "erp.company.query",
    "erp.depart.query",
    "erp.goodscate.get",
    "erp.sales.get",
    "erp.warehouse.get",
    "erp.stockquantity.get",
    "erp.vend.get",
    "crm.customer.list.customized",
    "oms.trade.fullinfoget",
    "erp.purch.search",
    "erp.storage.goodsdocin.v2",
    "erp-busiorder.goodsdocin.search",
    "erp-busiorder.goodsdocin.search.count",
    "erp.storage.goodsdocserial",
    "erp-busiorder.goodsdocout.search",
    "erp-busiorder.goodsdocout.search.count",
    "erp.allocate.search",
    "wms.stocktake.get",
    "erp.goodsbatchinfo.get",
    "erp-goods.goods.listgoodspackage",
    "oms.trade.ordercreate",
    "oms.trade.ordercancel",
    "oms.trade.audit.pass",
    "oms.trade.orderloglist",
}

# These methods were explicitly approved for the first outbound Sales Order
# workflow. A connection-level push_method_map may override them per tenant.
APPROVED_SALES_ORDER_PUSH_METHODS = {
    "Create": "oms.trade.ordercreate",
    "Cancel": "oms.trade.ordercancel",
    "Audit": "oms.trade.audit.pass",
    "Query": "oms.trade.fullinfoget",
    "Log": "oms.trade.orderloglist",
}

SALES_ORDER_FIELDS = ",".join(
    [
        "tradeId", "tradeNo", "onlineTradeNo", "sourceTradeNo", "tradeStatus", "tradeStatusExplain",
        "tradeType", "tradeFrom", "tradeTime", "gmtCreate", "gmtModified", "payTime",
        "companyName", "shopId", "shopName", "channelCode", "warehouseId", "warehouseCode",
        "warehouseName", "customerCode", "customerName", "customerAccount",
        "email", "country", "state", "city", "district", "town", "zip",
        "payerName", "payerPhone", "payerAddress",
        "chargeCurrencyCode", "chargeExchangeRate", "totalFee", "discountFee",
        "receivedPostFee", "payment", "receivedTotal", "buyerMemo", "sellerMemo",
        "isDelete", "goodsDetail.goodsNo", "goodsDetail.goodsId", "goodsDetail.specId",
        "goodsDetail.goodsName", "goodsDetail.specName", "goodsDetail.barcode",
        "goodsDetail.sellCount", "goodsDetail.unit", "goodsDetail.sellPrice",
        "goodsDetail.sellTotal", "goodsDetail.divideSellTotal", "goodsDetail.discountFee",
        "goodsDetail.taxRate", "goodsDetail.taxFee", "goodsDetail.isGift",
        "goodsDetail.refundStatus", "goodsDetail.outerId", "goodsDetail.outerSkuId",
        "goodsDetail.platGoodsId", "goodsDetail.platSkuId", "goodsDetail.platCode",
        "goodsDetail.sourceTradeNo", "goodsDetail.sourceSubtradeNo",
        "goodsDetail.subTradeId", "scrollId",
    ]
)
SALES_ORDER_TYPES = [1, 2, 3, 4, 5, 6, 7, 9, 10, 14]  # 7=补发单，店铺利润表计入
SALES_ORDER_SUBMIT_STATUSES = {6000, 9090}
# 4121/4122 are cancellation in progress/completed before the terminal 5010
# state. They are blocked as well, even though neither is in the submit set.
# 4110/4112/4113 (待发货-未递交/已递交/递交失败) are valid awaiting-shipment
# statuses and must NOT be treated as cancelled.
SALES_ORDER_CANCELLED_STATUSES = {4121, 4122, 5010, 5020, 5030}
SALES_ORDER_LISTING_FIELDS = (
    "custom_ecommerce_listing_id",
    "custom_ecommerce_platform_sku_id",
    "custom_ecommerce_platform_code",
    "custom_ecommerce_platform_item_name",
    "custom_ecommerce_platform_sku",
    "custom_jackyun_outer_id",
    "custom_jackyun_outer_sku_id",
    "custom_jackyun_source_trade_no",
    "custom_jackyun_source_subtrade_no",
)


def sales_order_status_action(trade_status):
    """Map a JackYun sales-order status to an ERPNext action.

    取消(取消中/已取消) -> cancel；其余(未发货/已发货/已完成) -> submit。
    同步进来的销售订单不再保留草稿状态。
    """
    status = frappe.utils.cint(trade_status)
    if status in SALES_ORDER_CANCELLED_STATUSES:
        return "cancel"
    return "submit"


def delivery_note_can_auto_submit(outbound_type, red_status):
    """Whether a JackYun outbound is an effective sales blue document."""
    return (
        frappe.utils.cint(outbound_type) == 201
        and frappe.utils.cint(red_status) == 1
    )

PURCHASE_ORDER_FIELDS = ",".join(
    [
        "orderNum", "id", "headId", "goodsId", "skuId", "gmtCreate", "quantity",
        "unitName", "warehouseId", "recDate", "price", "amount", "taxrate",
        "companyName", "goodsName", "goodsNo", "warehouseName", "skuBarcode",
        "rowRemark", "lineColse", "vendId", "vendCode", "vendName", "revwStatus",
        "vendOrderId", "memo", "orderTime", "gmtModified", "currencyCode",
        "currencyName", "inStatusName", "settStatusName", "inQuantity",
        "surInQuantity", "payableAmount", "taxAmount", "noTaxAmount",
    ]
)

PURCHASE_RECEIPT_FIELDS = ",".join(
    [
        "docId", "recId", "goodsdocNo", "billNo", "inOutDate", "gmtCreate",
        "inouttype", "inouttypeName", "vendCustomerName", "vendCode", "currencyCode",
        "currencyRate", "warehouseCode", "warehouseName", "companyName",
        "sourceBillNo", "redStatus", "goodsdocRemark", "receiveGoodsRemark",
        "vendCustomerId", "vendCustomerCode", "channelCode",
        "goodsNo", "goodsName", "skuName", "skuBarcode", "unitName", "quantity",
        "baceCurrencyCostPrice", "baceCurrencyCostAmount",
        "baceCurrencyWithTaxPrice", "baceCurrencyWithTaxAmount", "taxRate",
        "isCertified", "goodsDetailRemark", "batchNo", "serialNo",
        "serialSourceId", "productionDate", "expirationDate",
    ]
)

STOCK_TRANSFER_FIELDS = ",".join(
    [
        "allocateId", "allocateNo", "isAllocateDifferentCompany", "outWarehouseId",
        "outWarehouseCode", "inWarehouseId", "inWarehouseCode", "allocateType",
        "applyUserName", "applyDepartName", "applyDate", "operator", "auditUserName",
        "auditDate", "status", "outStatus", "inStatus", "memo", "goodsCount",
        "skuCount", "totalAmount", "gmtCreate", "gmtModified", "companyId",
        "companyName", "planOutDate", "planInDate", "logisticNo", "reason",
        "sourceNo", "allocateDetailId", "goodsId", "goodsName", "skuId", "skuName",
        "unitName", "goodsSkuCount", "skuPrice", "goodsTotalAmount", "outCount",
        "inCount", "isCertified", "rowRemark", "skuBarcode", "goodsNo", "outSkuCode",
    ]
)

DELIVERY_NOTE_FIELDS = ",".join(
    [
        "recId", "goodsdocNo", "inOutDate", "gmtCreate", "inouttype", "inouttypeName",
        "warehouseName", "billNo", "sourceBillNo", "deliveryNo", "currencyCode",
        "currencyRate", "companyName", "vendCustomerName", "vendCode",
        "goodsdocRemark", "receiveGoodsRemark", "goodsNo", "goodsName", "skuName",
        "skuBarcode", "unitName", "quantity", "baceCurrencyCostPrice",
        "baceCurrencyCostAmount", "baceCurrencyWithTaxPrice",
        "baceCurrencyWithTaxAmount", "taxRate", "isCertified", "goodsDetailRemark",
        "batchNo", "serialNo", "serialSourceId", "warehouseCode", "channelCode",
        "logisticNo", "logisticName", "outBillNo", "trackInOutNo", "flagName",
        "redStatus", "productionDate", "expirationDate",
    ]
)


class JikeyunError(Exception):
    def __init__(self, message, category, status_code=None):
        super().__init__(message)
        self.category = category
        self.status_code = status_code


def sign_jikeyun_params(secret, params):
    canonical = "".join(f"{k}{params.get(k) or ''}" for k in sorted(params))
    raw = f"{secret}{canonical}{secret}".lower()
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def build_public_params(app_key, method, bizcontent, secret):
    # 必须与 JS 的 JSON.stringify 完全一致：无空格、不转义非 ASCII
    params = {
        "appkey": app_key,
        "bizcontent": json.dumps(bizcontent or {}, ensure_ascii=False, separators=(",", ":")),
        "contenttype": "json",
        "method": method,
        # JackYun validates this value as Beijing wall-clock time.  Using the
        # operating-system timezone made otherwise valid requests expire when
        # the ERP host ran in another timezone (for example Asia/Bangkok).
        "timestamp": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
        "version": "v1.0",
    }
    params["sign"] = sign_jikeyun_params(secret, params)
    return params


def collect_candidate_arrays(value):
    if isinstance(value, list):
        return [value]
    if not isinstance(value, dict):
        return []
    for key in DIRECT_KEYS:
        if isinstance(value.get(key), list):
            return [value[key]]
    result = []
    for child in value.values():
        result.extend(collect_candidate_arrays(child))
    return result


def extract_items(payload):
    candidates = collect_candidate_arrays(payload)
    return candidates[0] if candidates else []


def extract_context_id(payload):
    """Return a real contextId from known JackYun response envelopes, if present."""
    candidates = [payload]
    if isinstance(payload, dict):
        result = payload.get("result")
        candidates.append(result)
        if isinstance(result, dict):
            candidates.append(result.get("data"))
    for value in candidates:
        if isinstance(value, dict):
            context_id = value.get("contextId") or value.get("contextid")
            if context_id not in (None, ""):
                return str(context_id)
    return None


def extract_product_packages(payload):
    """Return package parent records without mistaking component rows for packages."""
    result = payload.get("result") if isinstance(payload, dict) else None
    data = result.get("data") if isinstance(result, dict) else None

    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict) and isinstance(data.get("goodsPackageDetail"), list):
        return [data]

    packages = []

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("goodsPackageDetail"), list) and value.get("skuId"):
                packages.append(value)
                return
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return packages


def extract_records_by_identity(payload, identity_keys):
    """Extract records from either list or single-object JackYun response shapes."""
    records = []

    def walk(value):
        if isinstance(value, dict):
            if any(value.get(key) not in (None, "") for key in identity_keys):
                records.append(value)
                return
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return records


def find_numeric_by_keys(value, keys):
    if isinstance(value, dict):
        for key in keys:
            try:
                if value.get(key) not in (None, ""):
                    return int(value[key])
            except (TypeError, ValueError):
                pass
        for child in value.values():
            found = find_numeric_by_keys(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_numeric_by_keys(child, keys)
            if found is not None:
                return found
    return None


def find_string_by_keys(value, keys):
    if not isinstance(value, dict):
        return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, (int, float)):
            return str(candidate)
    for child in value.values():
        nested = find_string_by_keys(child, keys)
        if nested:
            return nested
    return None


def find_boolean_by_keys(value, keys):
    if not isinstance(value, dict):
        return None
    for key in keys:
        if isinstance(value.get(key), bool):
            return value[key]
    for child in value.values():
        nested = find_boolean_by_keys(child, keys)
        if nested is not None:
            return nested
    return None


def classify_business_error(payload):
    code = find_string_by_keys(payload, ["code", "errcode", "errorCode", "error_code"])
    message = find_string_by_keys(payload, ["message", "msg", "errmsg", "errorMessage", "error_msg"]) or ""
    sub_code = find_string_by_keys(payload, ["subCode", "sub_code", "subMsg", "sub_msg"]) or ""
    success = find_boolean_by_keys(payload, ["success", "flag"])

    error_text = f"{message} {sub_code}".lower()
    has_signature = "sign" in error_text or "签名" in error_text
    has_subscription = any(k in error_text for k in ("未订阅", "subscribe", "unauthorized"))

    is_business_error = (
        success is False
        or (code is not None and code.lower() not in ("0", "200", "success"))
        or has_signature
        or has_subscription
    )

    if is_business_error:
        if has_signature:
            return ("SIGNATURE", "吉客云拒绝请求：签名错误")
        if has_subscription:
            return ("UNSUBSCRIBED", "吉客云拒绝请求：接口未订阅")
        return ("PARAMETER", "吉客云拒绝请求：参数错误")

    if message and (
        "不能为空" in message or "short-circuited" in message.lower() or "fallback failed" in message.lower()
    ):
        return ("PARAMETER", "吉客云拒绝请求：参数错误")

    return None


def _parse_payload(raw_text):
    try:
        return json.loads(raw_text)
    except (ValueError, TypeError):
        return raw_text


def _backoff(attempt):
    return RETRY_BASE_S * (2 ** attempt) + random.uniform(0, 0.12)


def _pick(raw, paths):
    for p in paths:
        v = raw.get(p)
        if v not in (None, ""):
            return v
    return None


def _as_check(value):
    return 1 if str(value or "").strip().lower() in {"1", "true", "yes"} else 0


def _jackyun_datetime(value):
    if value in (None, "", "-"):
        return None
    text = str(value).strip()
    if text.isdigit():
        timestamp = int(text)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return frappe.utils.convert_utc_to_system_timezone(datetime.utcfromtimestamp(timestamp))
    return frappe.utils.get_datetime(value)


def _jackyun_name(value):
    """把吉客云业务编号规整成可安全用作 ERPNext 单据名的字符串。

    直接以吉客云编号作为单据名时，需先去掉空白与非法字符并截断到
    ERPNext 名字段长度上限（140）；空值返回 None 以回退自动编号。
    """
    name = re.sub(
        r"[^A-Za-z0-9._-]+", "-", frappe.utils.cstr(value or "").strip()
    ).strip(".-")
    return name[:140] or None


def _ensure_item_group(name, parent_item_group="All Item Groups"):
    name = frappe.utils.cstr(name).strip()
    if not name:
        return "All Item Groups"
    parent_item_group = frappe.utils.cstr(parent_item_group).strip() or "All Item Groups"
    if frappe.db.exists("Item Group", name):
        doc = frappe.get_doc("Item Group", name)
        if doc.parent_item_group != parent_item_group:
            doc.parent_item_group = parent_item_group
            doc.save(ignore_permissions=True)
        return name
    frappe.get_doc(
        {
            "doctype": "Item Group",
            "item_group_name": name,
            "parent_item_group": parent_item_group,
            "is_group": 0,
        }
    ).insert(ignore_permissions=True)
    return name


def _sync_category_item_group(category_doc):
    """把 Jackyun Goods Category 落到 ERPNext Item Group 树。

    Item Group 名使用吉客云末级分类名（与 SKU 的 cateName 对齐），
    通过 parent_category 递归建立 parent_item_group 层级；
    有子分类的节点标记为 is_group=1。
    """
    category_name = frappe.utils.cstr(category_doc.get("category_name")).strip()
    if not category_name:
        return None

    parent_group = "All Item Groups"
    if category_doc.get("parent_category"):
        parent_name = frappe.db.get_value(
            "Jackyun Goods Category", category_doc.parent_category, "category_name"
        )
        parent_name = frappe.utils.cstr(parent_name).strip()
        if parent_name:
            parent_group = parent_name
            frappe.db.set_value("Item Group", parent_group, "is_group", 1)

    return _ensure_item_group(category_name, parent_group)


def _ensure_uom(name):
    if not name:
        return None
    if frappe.db.exists("UOM", name):
        return name
    frappe.get_doc({"doctype": "UOM", "uom_name": name}).insert(ignore_permissions=True)
    return name


def _ensure_brand(name):
    if not name:
        return None
    if frappe.db.exists("Brand", name):
        return name
    frappe.get_doc({"doctype": "Brand", "brand": name}).insert(ignore_permissions=True)
    return name


def _ensure_supplier_group(name):
    name = frappe.utils.cstr(name).strip() or "All Supplier Groups"
    if frappe.db.exists("Supplier Group", name):
        return name
    frappe.get_doc(
        {
            "doctype": "Supplier Group",
            "supplier_group_name": name,
            "parent_supplier_group": "All Supplier Groups",
            "is_group": 0,
        }
    ).insert(ignore_permissions=True)
    return name


def _ensure_customer_group(name):
    name = frappe.utils.cstr(name).strip() or "Individual"
    if frappe.db.exists("Customer Group", name):
        return name
    frappe.get_doc(
        {
            "doctype": "Customer Group",
            "customer_group_name": name,
            "parent_customer_group": "All Customer Groups",
            "is_group": 0,
        }
    ).insert(ignore_permissions=True)
    return name


def _country_name(country):
    aliases = {"中国": "China", "中华人民共和国": "China"}
    value = aliases.get(frappe.utils.cstr(country).strip(), country)
    return value if value and frappe.db.exists("Country", value) else None


def _customer_territory(country):
    territory = _country_name(country) or "All Territories"
    return territory if frappe.db.exists("Territory", territory) else "All Territories"


# SKU（goods）字段映射：ERPNext Item 字段 -> 吉客云字段候选（第一个非空生效）
# 基于真实响应（erp.storage.goodslist）实测：货品编号在 goodsNo，价格在 retailPrice
# 注：价格（retailPrice）不在 Item 上设 standard_rate（会触发 Item Price 唯一性校验），
#     后续单独建 Item Price 单据导入。
GOODS_SOURCES = {
    "item_code": ["goodsNo", "goodsCode", "code", "skuBarcode"],
    "item_name": ["goodsName", "skuName", "name"],
    "item_group": ["cateName", "categoryName"],
    "stock_uom": ["unitName", "unit", "baseUnitName"],
    "description": ["goodsDesc", "goodsAlias", "spec"],
    # 医疗器械资质信息（吉客云自定义字段槽位）：
    # goodsField3=注册证号、goodsField7/4=生产厂家（两槽位内容相同，7 覆盖略广）、
    # skuName=规格名称（如 "YK-I型 500mL、容量允差为±5%"）
    "custom_jd_registration": ["goodsField3"],
    "custom_jd_manufacturer": ["goodsField7", "goodsField4"],
    "custom_jd_specification": ["skuName"],
}


def prepare_standalone_jackyun_return(doc, method=None):
    """Allow JackYun return stock documents that have no ERPNext source voucher.

    ERPNext's return status updater assumes ``return_against`` is always present.
    JackYun can provide valid standalone return movements, so only the source-voucher
    percentage updater is removed; normal validation and stock ledger posting remain.
    """
    if not doc.get("is_return") or doc.get("return_against"):
        return
    if not frappe.db.exists(
        "External ID Mapping",
        {"platform": "jackyun", "erpnext_doctype": doc.doctype, "erpnext_name": doc.name},
    ):
        return
    # There is no ERPNext source row whose delivered/received, billing, or reserved
    # quantity can be updated. Stock ledger posting remains unchanged.
    del doc.status_updater[:]
    doc.update_billing_status = lambda *args, **kwargs: None
    doc.update_reserved_qty = lambda *args, **kwargs: None


class JackYunAdapter(BaseAdapter):
    platform = "jackyun"

    def __init__(self, connection):
        self.connection = connection
        self.request_count = 0
        self.last_context_id = None

    # ---- 平台相关 ----

    def sign(self, params, secret):
        return sign_jikeyun_params(secret, params)

    def request(
        self,
        method,
        bizcontent,
        page_index=0,
        page_size=PAGE_SIZE,
        *,
        include_pagination=True,
        max_retries=MAX_RETRIES,
    ):
        self.request_count += 1
        biz = dict(bizcontent or {})
        if include_pagination:
            if method in NESTED_PAGE_INFO_METHODS:
                biz["pageInfo"] = {"pageIndex": page_index, "pageSize": page_size}
            else:
                biz["pageIndex"] = page_index
                biz["pageSize"] = page_size

        secret = self.connection.get_password("app_secret")
        public = build_public_params(self.connection.app_key, method, biz, secret)

        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(self.connection.gateway, data=public, timeout=DEFAULT_TIMEOUT_S)
                raw_text = resp.text

                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt < max_retries:
                        time.sleep(_backoff(attempt))
                        continue

                if resp.status_code != 200:
                    raise JikeyunError(
                        f"吉客云请求失败 HTTP {resp.status_code}",
                        "NETWORK" if resp.status_code == 429 else "PARAMETER",
                        resp.status_code,
                    )

                payload = _parse_payload(raw_text)
                if method in STRICT_SUCCESS_METHODS and isinstance(payload, dict):
                    code = str(payload.get("code") or "")
                    if code != "200":
                        message = payload.get("msg") or "未知错误"
                        sub_code = payload.get("subCode") or ""
                        if _is_quota_exhausted_message(f"{message} {sub_code}"):
                            trip_quota_circuit()
                        raise JikeyunError(
                            f"吉客云接口 {method} 查询失败：{message} {sub_code}".strip(),
                            "PARAMETER",
                        )
                err = classify_business_error(payload)
                if err:
                    if _is_quota_exhausted_message(err[1]):
                        trip_quota_circuit()
                    raise JikeyunError(err[1], err[0])

                context_id = extract_context_id(payload)
                if context_id:
                    self.last_context_id = context_id
                return payload
            except JikeyunError:
                raise
            except Exception as exc:
                if attempt < max_retries:
                    time.sleep(_backoff(attempt))
                    continue
                raise JikeyunError(str(exc), "NETWORK")

        raise JikeyunError("吉客云请求重试耗尽", "NETWORK")

    def outbound_capabilities(self, resource=None):
        if resource == "Sales Order":
            return {"Create", "Cancel", "Audit", "Query"}
        return set()

    def preflight_operation(self, resource, operation, payload, context=None):
        result = super().preflight_operation(resource, operation, payload, context)
        if not result.allowed:
            return result
        if not frappe.utils.cint(self.connection.get("outbound_enabled")):
            return PreflightResult(False, "吉客云写入总开关未启用")
        if (
            resource == "Sales Order"
            and operation == "Create"
            and frappe.utils.cint(self.connection.get("outbound_test_mode"))
        ):
            online_no = ((payload or {}).get("tradeOrder") or {}).get("onlineTradeNo") or ""
            if not str(online_no).startswith("ERPTEST-"):
                return PreflightResult(False, "测试模式仅允许 ERPTEST- 前缀订单")
        return result

    def push(
        self,
        resource,
        operation,
        payload,
        *,
        idempotency_key=None,
        context=None,
    ):
        """Push through an explicitly approved resource/operation method map.

        Write APIs differ by tenant and must never be guessed. The connection
        stores JSON such as {"Sales Order.Create": "approved.api.method"}.
        """
        del idempotency_key, context
        configured = self.connection.get("push_method_map") or "{}"
        try:
            methods = frappe.parse_json(configured) if isinstance(configured, str) else configured
        except Exception as exc:
            raise ValueError(f"推送接口映射不是有效 JSON：{exc}") from exc
        method = (methods or {}).get(f"{resource}.{operation}")
        if not method and resource == "Sales Order":
            method = APPROVED_SALES_ORDER_PUSH_METHODS.get(operation)
        if not method:
            raise NotImplementedError(f"吉客云尚未批准 {resource}.{operation} 推送接口")
        # Create is intentionally attempted once. A timeout can mean that
        # JackYun committed the order but its response was lost; blindly
        # retrying would create a duplicate. The outbound service probes by
        # onlineTradeNo and reports the message as Uncertain instead.
        return self.request(
            method,
            payload or {},
            page_index=0,
            page_size=1,
            include_pagination=False,
            max_retries=0 if resource == "Sales Order" and operation == "Create" else MAX_RETRIES,
        )

    def execute_operation(
        self,
        resource,
        operation,
        payload,
        *,
        idempotency_key,
        context=None,
    ):
        """Execute approved JackYun operations and normalize business receipts."""
        del idempotency_key, context
        if not self.supports_operation(resource, operation):
            return super().execute_operation(
                resource, operation, payload, idempotency_key="unsupported"
            )
        from channel_erp.integrations.jackyun_outbound import (
            parse_audit_response,
            parse_cancel_response,
            parse_create_response,
            parse_query_response,
        )

        try:
            response = self.push(resource, operation, payload)
        except JikeyunError as exc:
            if resource == "Sales Order" and operation == "Create" and exc.category == "NETWORK":
                return OperationResult.uncertain(response={"error": str(exc), "category": exc.category})
            raise
        parser = {
            "Create": parse_create_response,
            "Cancel": parse_cancel_response,
            "Audit": parse_audit_response,
            "Query": parse_query_response,
        }[operation]
        result = parser(response)
        external_id = result.get("trade_no")
        if operation == "Query" and result.get("records"):
            external_id = str((result["records"][0] or {}).get("tradeNo") or "") or None
        return OperationResult(
            outcome=result.get("status") or "Failed",
            external_id=external_id,
            context_id=result.get("context_id"),
            response=result,
        )

    def query_operation(self, resource, operation, payload, *, context=None):
        return self.execute_operation(
            resource,
            operation,
            payload,
            idempotency_key="query",
            context=context,
        )

    def probe_operation(
        self,
        resource,
        operation,
        *,
        idempotency_key,
        external_id=None,
        payload=None,
        context=None,
    ):
        del idempotency_key, external_id, context
        if resource != "Sales Order" or operation != "Create":
            return ProbeResult.unknown(response={"reason": "仅销售单创建支持远端核实"})
        from channel_erp.integrations.jackyun_outbound import JackyunSalesOrderOutboundService

        online_no = ((payload or {}).get("tradeOrder") or {}).get("onlineTradeNo")
        result = JackyunSalesOrderOutboundService(self).probe_create(online_no)
        if result.get("status") == "Reconciled":
            return ProbeResult.found(external_id=result.get("trade_no"), response=result)
        if result.get("status") == "Not Found":
            return ProbeResult.not_found(response=result)
        return ProbeResult.unknown(response=result)

    def pull(self, resource: str, since=None, until=None, **filters):
        if resource == "Product Bundle":
            yield from self._pull_product_bundles(since=since, until=until)
            return
        if resource == "Inventory":
            yield from self._pull_inventory(since=since, until=until)
            return
        if resource == "Supplier":
            yield from self._pull_suppliers(since=since, until=until)
            return
        if resource == "Sales Order":
            yield from self._pull_sales_orders(since=since, until=until, **filters)
            return
        if resource == "Purchase Order":
            yield from self._pull_purchase_orders(since=since, until=until)
            return
        if resource == "Purchase Receipt":
            yield from self._pull_purchase_receipts(
                since=since, until=until, resource=resource, inouttype="101"
            )
            return
        if resource == "Delivery Note":
            yield from self._pull_delivery_notes(
                since=since, until=until, resource=resource, inouttype="201"
            )
            return
        if resource == "Sales Return":
            yield from self._pull_purchase_receipts(
                since=since, until=until, resource=resource, inouttype="105"
            )
            return
        if resource == "Purchase Return":
            yield from self._pull_delivery_notes(
                since=since, until=until, resource=resource, inouttype="205"
            )
            return
        if resource == "Stock Transfer":
            yield from self._pull_stock_transfers(since=since, until=until)
            return
        if resource == "Stocktake":
            yield from self._pull_stocktakes(since=since, until=until)
            return
        if resource == "Batch":
            yield from self._pull_batches(since=since, until=until)
            return
        if resource == "Stock Movement":
            yield from self._pull_stock_movements(since=since, until=until)
            return
        if resource in {"Customer", "Offline Customer"}:
            yield from self._pull_customers_on_demand(since=since, until=until, **filters)
            return
        if resource in {"Company", "Department", "Category", "Sales Channel", "Warehouse"}:
            yield from self._pull_dictionary(resource)
            return

        method = METHOD_MAP[resource]
        page_index = 0
        previous_digest = None

        while True:
            payload = self.request(method, {}, page_index)
            items = extract_items(payload)

            # 部分接口忽略 pageIndex 永远返回第一页——连续两页完全一致即视为不翻页
            digest = hashlib.md5(
                json.dumps(items, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()
            if previous_digest is not None and digest == previous_digest:
                return
            previous_digest = digest

            for item in items:
                yield item

            if len(items) < PAGE_SIZE:
                return
            page_index += 1

    def _pull_dictionary(self, resource):
        identity_keys = {
            "Company": ["companyId", "companyCode"],
            "Department": ["departId", "departCode"],
            "Category": ["cateId", "cateCode"],
            "Sales Channel": ["channelId", "channelCode"],
            "Warehouse": ["warehouseId", "warehouseCode"],
        }[resource]
        method = METHOD_MAP[resource]
        page_index = 0
        previous_digest = None
        fetched = 0
        collected = []

        while True:
            bizcontent = {}
            if resource in {"Company", "Department"} and self.connection.get("company_codes"):
                bizcontent["companyCodes"] = self.connection.company_codes
            if resource == "Warehouse":
                bizcontent["includeDeleteAndBlockup"] = 1
            payload = self.request(method, bizcontent, page_index, PAGE_SIZE)
            records = extract_records_by_identity(payload, identity_keys)
            digest = hashlib.md5(
                json.dumps(records, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()
            if previous_digest is not None and digest == previous_digest:
                break
            previous_digest = digest

            collected.extend(records)
            fetched += len(records)

            total = find_numeric_by_keys(payload, ["total", "totalCount"])
            if not records or (total is not None and fetched >= total):
                break
            if total is None and len(records) < PAGE_SIZE:
                break
            page_index += 1

        if resource == "Category":
            by_id = {
                str(_pick(row, ["cateId", "categoryId", "id"])): row
                for row in collected
                if _pick(row, ["cateId", "categoryId", "id"]) not in (None, "")
            }
            ordered = []
            visiting = set()
            visited = set()

            def visit(category_id):
                if category_id in visited or category_id in visiting:
                    return
                visiting.add(category_id)
                row = by_id[category_id]
                parent_id = _pick(row, ["parentCateId", "parentCategoryId"])
                if parent_id not in (None, "") and str(parent_id) in by_id:
                    visit(str(parent_id))
                visiting.discard(category_id)
                visited.add(category_id)
                ordered.append(row)

            for category_id in by_id:
                visit(category_id)
            collected = ordered

        if resource == "Warehouse" and self.connection.get("company_codes"):
            allowed_codes = {
                code.strip() for code in self.connection.company_codes.split(",") if code.strip()
            }
            collected = [
                row
                for row in collected
                if str(_pick(row, ["warehouseCompanyCode", "companyCode"]) or "")
                in allowed_codes
            ]
            collected = [
                row
                for row in collected
                if not _as_check(row.get("isDelete"))
                or self.get_mapping_name(
                    "Warehouse", str(_pick(row, ["warehouseId", "warehouseCode"]))
                )
            ]

        yield from collected

    def _pull_suppliers(self, since=None, until=None):
        method = METHOD_MAP["Supplier"]
        page_index = 0
        previous_digest = None
        while True:
            biz = {"includeDeleteAndBlockup": 1, "needAllPayAccount": 1}
            if since:
                biz["gmtModifiedStart"] = (
                    frappe.utils.get_datetime(since) - timedelta(minutes=1)
                ).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                biz["gmtModifiedEnd"] = frappe.utils.get_datetime(
                    until or frappe.utils.now_datetime()
                ).strftime("%Y-%m-%d %H:%M:%S")
            payload = self.request(method, biz, page_index, PAGE_SIZE)
            records = extract_records_by_identity(payload, ["vendId"])
            digest = hashlib.md5(
                json.dumps(records, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()
            if previous_digest is not None and digest == previous_digest:
                break
            previous_digest = digest
            for record in records:
                yield record
            if len(records) < PAGE_SIZE:
                break
            page_index += 1

    def _pull_customers_on_demand(
        self,
        customer_ids=None,
        customer_codes=None,
        customer_sources=None,
        since=None,
        until=None,
    ):
        customer_ids = [str(value) for value in (customer_ids or []) if str(value).strip()]
        customer_codes = [str(value) for value in (customer_codes or []) if str(value).strip()]
        customer_sources = [int(value) for value in (customer_sources or [])]
        if not customer_ids and not customer_codes and not customer_sources:
            frappe.throw(
                "客户同步必须提供 customer_ids、customer_codes 或 customer_sources，禁止百万级全量导入"
            )

        method = METHOD_MAP["Customer"]
        scroll_id = ""
        previous_scroll_id = None
        while True:
            biz = {"hasTotal": 1 if not scroll_id else 0, "scrollId": scroll_id}
            if customer_ids:
                biz["customerIdArr"] = customer_ids
            if customer_codes:
                biz["customerCodeArr"] = customer_codes
            if customer_sources:
                biz["customerCreateSourceBatch"] = customer_sources
            if since:
                biz["gmtModifiedBegin"] = (
                    frappe.utils.get_datetime(since) - timedelta(minutes=1)
                ).strftime("%Y-%m-%d %H:%M:%S")
                biz["gmtModifiedEnd"] = frappe.utils.get_datetime(
                    until or frappe.utils.now_datetime()
                ).strftime("%Y-%m-%d %H:%M:%S")
            payload = self.request(method, biz, page_index=0, page_size=PAGE_SIZE)
            records = extract_records_by_identity(payload, ["customerId", "customerCode"])
            for record in records:
                yield record

            data = (payload.get("result") or {}).get("data") or {}
            next_scroll_id = data.get("scrollId") if isinstance(data, dict) else None
            if not records or len(records) < PAGE_SIZE or not next_scroll_id:
                break
            if next_scroll_id == scroll_id or next_scroll_id == previous_scroll_id:
                break
            previous_scroll_id = scroll_id
            scroll_id = next_scroll_id

    def _pull_inventory(self, since=None, until=None):
        """按修改时间窗口和 quantityId 游标拉取库存快照。"""
        method = METHOD_MAP["Inventory"]
        windows = [(None, None)]
        if since:
            start = frappe.utils.get_datetime(since) - timedelta(minutes=1)
            end = frappe.utils.get_datetime(until or frappe.utils.now_datetime())
            windows = []
            while start < end:
                window_end = min(start + timedelta(days=1), end)
                windows.append((start, window_end))
                start = window_end

        for window_start, window_end in windows:
            max_quantity_id = 0
            while True:
                biz = {
                    "maxQuantityId": max_quantity_id,
                    "isBlockup": 2,
                    "isChannelReserve": 1,
                    "isbatchmanagement": 0,
                }
                if window_start:
                    biz["gmtModifiedStart"] = window_start.strftime("%Y-%m-%d %H:%M:%S")
                    biz["gmtModifiedEnd"] = window_end.strftime("%Y-%m-%d %H:%M:%S")

                payload = self.request(method, biz, page_index=0, page_size=STOCK_PAGE_SIZE)
                records = extract_records_by_identity(
                    payload, ["quantityId", "warehouseCode", "skuId", "goodsNo"]
                )
                if not records:
                    break
                for record in records:
                    yield record

                cursor_values = []
                for record in records:
                    try:
                        cursor_values.append(int(record.get("quantityId")))
                    except (TypeError, ValueError):
                        continue
                next_cursor = max(cursor_values, default=max_quantity_id)
                if next_cursor <= max_quantity_id or len(records) < STOCK_PAGE_SIZE:
                    break
                max_quantity_id = next_cursor

    def _pull_sales_orders(self, since=None, until=None, created_since=None, created_until=None):
        """首次按创建时间、后续按修改时间，以不超过七天的窗口拉取销售单。"""
        method = METHOD_MAP["Sales Order"]
        end = frappe.utils.get_datetime(created_until or until or frappe.utils.now_datetime())
        # 增量按修改时间拉取时，老订单若最近有变动（发货/完成/退款）也会被返回；
        # 只保留配置起始日期之后创建的订单，避免把历史单带进来。
        start_date = frappe.utils.getdate(self.connection.get("sales_order_start_date") or None)
        if created_since:
            start = frappe.utils.get_datetime(created_since)
            start_field, end_field = "startCreated", "endCreated"
        elif since:
            start = frappe.utils.get_datetime(since) - timedelta(minutes=1)
            start_field, end_field = "startModified", "endModified"
        else:
            start = frappe.utils.get_datetime(
                self.connection.get("sales_order_start_date") or "2026-01-01"
            )
            start_field, end_field = "startCreated", "endCreated"

        while start <= end:
            window_end = min(start + timedelta(days=7) - timedelta(seconds=1), end)
            scroll_id = ""
            previous_scroll_id = None
            while True:
                biz = {
                    start_field: start.strftime("%Y-%m-%d %H:%M:%S"),
                    end_field: window_end.strftime("%Y-%m-%d %H:%M:%S"),
                    "fields": SALES_ORDER_FIELDS,
                    "scrollId": scroll_id,
                    "isTableSwitch": 1,
                    "isDelete": "0",
                    "tradeTypeList": SALES_ORDER_TYPES,
                }
                payload = self.request(
                    method, biz, page_index=0, page_size=SALES_ORDER_PAGE_SIZE
                )
                records = extract_records_by_identity(payload, ["tradeId"])
                for record in records:
                    if start_date:
                        created = frappe.utils.getdate(record.get("gmtCreate"))
                        if created and created < start_date:
                            continue
                    # 淘系(天猫/淘宝)订单受奇门限制，goodsDetail 为空，但表头金额准确，
                    # 用占位商品承载金额，让店铺利润可算、链接利润待奇门资质补全。
                    has_detail = any(
                        frappe.utils.flt(_pick(row, ["sellCount", "baseUnitSellCount"])) > 0
                        for row in (record.get("goodsDetail") or [])
                    )
                    has_amount = frappe.utils.flt(record.get("totalFee")) > 0
                    if has_detail or has_amount:
                        yield record

                data = (payload.get("result") or {}).get("data") or {}
                next_scroll_id = data.get("scrollId") if isinstance(data, dict) else None
                if not records or len(records) < SALES_ORDER_PAGE_SIZE or not next_scroll_id:
                    break
                if next_scroll_id == scroll_id or next_scroll_id == previous_scroll_id:
                    break
                previous_scroll_id = scroll_id
                scroll_id = next_scroll_id
            start = window_end + timedelta(seconds=1)

    def _pull_purchase_orders(self, since=None, until=None):
        """按明细分页拉取采购单，并按 headId/orderNum 聚合为采购单头。"""
        method = METHOD_MAP["Purchase Order"]
        end = frappe.utils.get_datetime(until or frappe.utils.now_datetime())
        if since:
            start = frappe.utils.get_datetime(since) - timedelta(minutes=1)
            start_field, end_field = "startModifyDate", "endModifyDate"
        else:
            start = frappe.utils.get_datetime(
                self.connection.get("purchase_order_start_date")
                or self.connection.get("sales_order_start_date")
                or "2026-08-08"
            )
            start_field, end_field = "startDate", "endDate"

        while start <= end:
            window_end = min(start + timedelta(days=7) - timedelta(seconds=1), end)
            page_index = 0
            previous_digest = None
            grouped = {}
            while True:
                biz = {
                    start_field: start.strftime("%Y-%m-%d %H:%M:%S"),
                    end_field: window_end.strftime("%Y-%m-%d %H:%M:%S"),
                    "cols": PURCHASE_ORDER_FIELDS,
                }
                payload = self.request(
                    method,
                    biz,
                    page_index=page_index,
                    page_size=PURCHASE_ORDER_PAGE_SIZE,
                )
                records = extract_records_by_identity(payload, ["id", "headId", "orderNum"])
                digest = hashlib.md5(
                    json.dumps(records, sort_keys=True, ensure_ascii=False, default=str).encode(
                        "utf-8"
                    )
                ).hexdigest()
                if previous_digest is not None and digest == previous_digest:
                    break
                previous_digest = digest

                for record in records:
                    key = str(_pick(record, ["headId", "orderNum"]) or "")
                    if not key:
                        continue
                    if key not in grouped:
                        grouped[key] = dict(record)
                        grouped[key]["purchaseDetails"] = []
                    grouped[key]["purchaseDetails"].append(record)

                if len(records) < PURCHASE_ORDER_PAGE_SIZE:
                    break
                page_index += 1

            # 采购单全部同步：未审核(0/1)映射草稿、通过(2)提交、作废(10/20)取消，由 _maybe_submit_purchase_order 处理
            yield from grouped.values()
            start = window_end + timedelta(seconds=1)

    def _pull_purchase_receipts(
        self, since=None, until=None, resource="Purchase Receipt", inouttype="101"
    ):
        """新规格模式接口按明细返回；按 docId 聚合，并分别覆盖表头/明细增量。"""
        method = METHOD_MAP["Purchase Receipt"]
        configured_start = frappe.utils.get_datetime(
            self.connection.get(
                "sales_return_start_date"
                if resource == "Sales Return"
                else "purchase_receipt_start_date"
            )
            or self.connection.get("purchase_order_start_date")
            or "2026-08-08"
        )
        end = frappe.utils.get_datetime(until or frappe.utils.now_datetime())
        if since:
            start = max(configured_start, frappe.utils.get_datetime(since) - timedelta(minutes=1))
            window_fields = [
                ("gmtModifiedDateStart", "gmtModifiedDateEnd"),
                ("gmtModifiedDetailDateStart", "gmtModifiedDetailDateEnd"),
            ]
        else:
            start = configured_start
            window_fields = [("inOutDateStart", "inOutDateEnd")]

        while start <= end:
            window_end = min(start + timedelta(days=7) - timedelta(seconds=1), end)
            grouped = {}
            for start_field, end_field in window_fields:
                page_index = 0
                previous_digest = None
                biz = {
                    start_field: start.strftime("%Y-%m-%d %H:%M:%S"),
                    end_field: window_end.strftime("%Y-%m-%d %H:%M:%S"),
                    "inouttypes": inouttype,
                    "archived": 0,
                }
                count_payload = self.request(
                    "erp-busiorder.goodsdocin.search.count", biz, 0, 1
                )
                expected_count = find_numeric_by_keys(
                    count_payload, ["data", "count", "total"]
                )
                fetched = 0
                while True:
                    payload = self.request(
                        method,
                        {**biz, "cols": PURCHASE_RECEIPT_FIELDS},
                        page_index=page_index,
                        page_size=PAGE_SIZE,
                    )
                    records = extract_records_by_identity(
                        payload, ["recId", "docId", "goodsdocNo"]
                    )
                    fetched += len(records)
                    digest = hashlib.md5(
                        json.dumps(
                            records, sort_keys=True, ensure_ascii=False, default=str
                        ).encode("utf-8")
                    ).hexdigest()
                    if previous_digest is not None and digest == previous_digest:
                        break
                    previous_digest = digest

                    for record in records:
                        if frappe.utils.cint(record.get("redStatus")) != 1:
                            continue
                        key = str(_pick(record, ["docId", "goodsdocNo"]) or "")
                        if not key:
                            continue
                        group = grouped.setdefault(
                            key,
                            {**record, "goodsDocDetailList": [], "_detail_ids": set()},
                        )
                        detail_id = str(_pick(record, ["recId", "goodsNo"]) or "")
                        if detail_id and detail_id not in group["_detail_ids"]:
                            group["_detail_ids"].add(detail_id)
                            group["goodsDocDetailList"].append(record)

                    if len(records) < PAGE_SIZE:
                        break
                    page_index += 1

                    if expected_count is not None and fetched >= expected_count:
                        break

                if expected_count is not None and fetched < expected_count:
                    frappe.throw(
                        f"{resource} {start_field}={biz[start_field]} 数量校验失败："
                        f"总数接口 {expected_count}，实际拉取 {fetched}"
                    )

            for group in grouped.values():
                group.pop("_detail_ids", None)
                self._attach_serial_numbers(group.get("goodsDocDetailList") or [])
                yield group
            start = window_end + timedelta(seconds=1)

    def _attach_serial_numbers(self, details):
        """批量补齐入库规格行未内嵌返回的唯一码，并保留完整原始记录。"""
        source_ids = sorted(
            {
                str(row.get("serialSourceId"))
                for row in details
                if row.get("serialSourceId") and not row.get("serialNo")
            }
        )
        if not source_ids:
            return

        records_by_source = {}
        for offset in range(0, len(source_ids), 50):
            chunk = source_ids[offset : offset + 50]
            page_index = 0
            while True:
                payload = self.request(
                    "erp.storage.goodsdocserial",
                    {"serialSourceIdStrs": ",".join(chunk)},
                    page_index=page_index,
                    page_size=200,
                )
                records = extract_records_by_identity(
                    payload, ["serialSourceId", "serialNo"]
                )
                for record in records:
                    source_id = frappe.utils.cstr(record.get("serialSourceId")).strip()
                    if source_id:
                        records_by_source.setdefault(source_id, []).append(record)
                if len(records) < 200:
                    break
                page_index += 1

        for row in details:
            source_id = frappe.utils.cstr(row.get("serialSourceId")).strip()
            if source_id and source_id in records_by_source:
                row["serialRecords"] = records_by_source[source_id]

    def _pull_delivery_notes(
        self, since=None, until=None, resource="Delivery Note", inouttype="201"
    ):
        """逐日拉取销售出库规格行，count核对后按 goodsdocNo 聚合。"""
        method = METHOD_MAP["Delivery Note"]
        configured_start = frappe.utils.get_datetime(
            self.connection.get(
                "purchase_return_start_date"
                if resource == "Purchase Return"
                else "delivery_note_start_date"
            )
            or self.connection.get("sales_order_start_date")
            or "2026-08-08"
        )
        end = frappe.utils.get_datetime(until or frappe.utils.now_datetime())
        if since:
            start = max(configured_start, frappe.utils.get_datetime(since) - timedelta(minutes=1))
            window_fields = [
                ("gmtModifiedDateStart", "gmtModifiedDateEnd"),
                ("gmtModifiedDetailDateStart", "gmtModifiedDetailDateEnd"),
            ]
        else:
            start = configured_start
            window_fields = [("inOutDateStart", "inOutDateEnd")]

        while start <= end:
            window_end = min(start + timedelta(days=1) - timedelta(seconds=1), end)
            grouped = {}
            for start_field, end_field in window_fields:
                filters = {
                    start_field: start.strftime("%Y-%m-%d %H:%M:%S"),
                    end_field: window_end.strftime("%Y-%m-%d %H:%M:%S"),
                    "inouttypes": inouttype,
                    "archived": 0,
                }
                count_payload = self.request(
                    "erp-busiorder.goodsdocout.search.count", filters, 0, 1
                )
                expected_count = find_numeric_by_keys(count_payload, ["data", "count", "total"])
                page_index = 0
                fetched = 0
                previous_digest = None
                while True:
                    payload = self.request(
                        method,
                        {**filters, "cols": DELIVERY_NOTE_FIELDS},
                        page_index=page_index,
                        page_size=PAGE_SIZE,
                    )
                    records = extract_records_by_identity(payload, ["recId", "goodsdocNo"])
                    digest = hashlib.md5(
                        json.dumps(
                            records, sort_keys=True, ensure_ascii=False, default=str
                        ).encode("utf-8")
                    ).hexdigest()
                    if previous_digest is not None and digest == previous_digest:
                        break
                    previous_digest = digest
                    fetched += len(records)

                    for record in records:
                        if frappe.utils.cint(record.get("redStatus")) != 1:
                            continue
                        key = frappe.utils.cstr(record.get("goodsdocNo")).strip()
                        if not key:
                            continue
                        group = grouped.setdefault(
                            key,
                            {**record, "goodsDocDetailList": [], "_detail_ids": set()},
                        )
                        detail_id = str(_pick(record, ["recId", "goodsNo"]) or "")
                        if detail_id and detail_id not in group["_detail_ids"]:
                            group["_detail_ids"].add(detail_id)
                            group["goodsDocDetailList"].append(record)

                    if len(records) < PAGE_SIZE:
                        break
                    page_index += 1

                if expected_count is not None and fetched < expected_count:
                    frappe.throw(
                        f"{resource} {start_field}={filters[start_field]} 数量校验失败："
                        f"总数接口 {expected_count}，实际拉取 {fetched}"
                    )

            all_details = []
            for group in grouped.values():
                group.pop("_detail_ids", None)
                all_details.extend(group.get("goodsDocDetailList") or [])
            self._attach_serial_numbers(all_details)
            yield from grouped.values()
            start = window_end + timedelta(seconds=1)

    def _pull_stock_transfers(self, since=None, until=None):
        configured_start = frappe.utils.get_datetime(
            self.connection.get("stock_transfer_start_date") or "2026-08-08"
        )
        start = max(configured_start, frappe.utils.get_datetime(since)) if since else configured_start
        end = frappe.utils.get_datetime(until or frappe.utils.now_datetime())
        while start <= end:
            window_end = min(start + timedelta(days=7) - timedelta(seconds=1), end)
            grouped = {}
            page_index = 0
            while True:
                payload = self.request(
                    METHOD_MAP["Stock Transfer"],
                    {
                        "startCreateTime": start.strftime("%Y-%m-%d %H:%M:%S"),
                        "endCreateTime": window_end.strftime("%Y-%m-%d %H:%M:%S"),
                        "cols": STOCK_TRANSFER_FIELDS,
                    },
                    page_index,
                    PAGE_SIZE,
                )
                records = extract_records_by_identity(payload, ["allocateDetailId", "allocateId", "allocateNo"])
                for record in records:
                    key = str(_pick(record, ["allocateId", "allocateNo"]) or "")
                    if not key:
                        continue
                    group = grouped.setdefault(key, {**record, "allocateDetails": [], "_ids": set()})
                    detail_id = str(_pick(record, ["allocateDetailId", "skuId", "goodsNo"]) or "")
                    if detail_id and detail_id not in group["_ids"]:
                        group["_ids"].add(detail_id)
                        group["allocateDetails"].append(record)
                if len(records) < PAGE_SIZE:
                    break
                page_index += 1
            for group in grouped.values():
                group.pop("_ids", None)
                yield group
            start = window_end + timedelta(seconds=1)

    def _pull_stocktakes(self, since=None, until=None):
        configured_start = frappe.utils.get_datetime(
            self.connection.get("stocktake_start_date") or "2026-08-08"
        )
        start = max(configured_start, frappe.utils.get_datetime(since)) if since else configured_start
        end = frappe.utils.get_datetime(until or frappe.utils.now_datetime())
        page_index = 0
        while True:
            payload = self.request(
                METHOD_MAP["Stocktake"],
                {
                    "startPdDate": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "endPdDate": end.strftime("%Y-%m-%d %H:%M:%S"),
                },
                page_index,
                PAGE_SIZE,
            )
            records = extract_records_by_identity(payload, ["stocktakeId"])
            for record in records:
                yield record
            if len(records) < PAGE_SIZE:
                break
            page_index += 1

    def _pull_batches(self, since=None, until=None):
        configured_start = frappe.utils.get_datetime(
            self.connection.get("batch_start_date") or "2026-08-08"
        )
        start = max(configured_start, frappe.utils.get_datetime(since)) if since else configured_start
        end = frappe.utils.get_datetime(until or frappe.utils.now_datetime())
        page_index = 0
        while True:
            payload = self.request(
                METHOD_MAP["Batch"],
                {
                    "gmtCreateBegin": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "gmtCreateEnd": end.strftime("%Y-%m-%d %H:%M:%S"),
                    "notFilterNoStockBatch": 1,
                },
                page_index,
                PAGE_SIZE,
            )
            records = extract_records_by_identity(payload, ["batchNo", "goodsNo", "skuId"])
            yield from records
            if len(records) < PAGE_SIZE:
                break
            page_index += 1

    def _pull_stock_movements(self, since=None, until=None):
        """拉取非买卖、非调拨的出入库流水（生产领料/完工入库/盘盈亏/其他出入库/报废/翻新等）。"""
        for inouttype in STOCK_MOVEMENT_INBOUND_TYPES:
            yield from self._pull_purchase_receipts(
                since=since, until=until, resource="Stock Movement", inouttype=inouttype
            )
        for inouttype in STOCK_MOVEMENT_OUTBOUND_TYPES:
            yield from self._pull_delivery_notes(
                since=since, until=until, resource="Stock Movement", inouttype=inouttype
            )

    def _pull_product_bundles(self, since=None, until=None):
        """Pull packages with one-day windows and skuId cursor pagination.

        An initial sync has no time window and walks the complete dataset with maxSkuId.
        Incremental syncs split the requested range into one-day windows as recommended by
        JackYun, with a one-minute overlap handled safely by idempotent upserts.
        """
        method = METHOD_MAP["Product Bundle"]
        windows = [(None, None)]
        if since:
            start = frappe.utils.get_datetime(since) - timedelta(minutes=1)
            end = frappe.utils.get_datetime(until or frappe.utils.now_datetime())
            windows = []
            while start < end:
                window_end = min(start + timedelta(days=1), end)
                windows.append((start, window_end))
                start = window_end

        for window_start, window_end in windows:
            max_sku_id = 0
            previous_cursor = None

            while True:
                biz = {"maxSkuId": max_sku_id}
                if window_start:
                    biz["lastModifiedStart"] = window_start.strftime("%Y-%m-%d %H:%M:%S")
                    biz["lastModifiedEnd"] = window_end.strftime("%Y-%m-%d %H:%M:%S")

                payload = self.request(method, biz, page_index=0, page_size=PACKAGE_PAGE_SIZE)
                packages = extract_product_packages(payload)
                if not packages:
                    break

                for package in packages:
                    yield package

                cursor_values = []
                for package in packages:
                    try:
                        cursor_values.append(int(package.get("skuId")))
                    except (TypeError, ValueError):
                        continue
                next_cursor = max(cursor_values, default=max_sku_id)
                if next_cursor <= max_sku_id or next_cursor == previous_cursor:
                    break
                previous_cursor = max_sku_id
                max_sku_id = next_cursor

                if len(packages) < PACKAGE_PAGE_SIZE:
                    break

    def get_external_id(self, resource: str, raw: dict):
        if resource == "Company":
            return _pick(raw, ["companyId", "companyCode"])
        if resource == "Department":
            return _pick(raw, ["departId", "departCode"])
        if resource == "Category":
            return _pick(raw, ["cateId", "cateCode"])
        if resource == "Sales Channel":
            return _pick(raw, ["channelId", "channelCode"])
        if resource == "Warehouse":
            return _pick(raw, ["warehouseId", "warehouseCode"])
        if resource == "Supplier":
            return _pick(raw, ["vendId", "code"])
        if resource in {"Customer", "Offline Customer"}:
            return _pick(raw, ["customerId", "customerCode"])
        if resource == "SKU":
            # 用业务键去重：货品编号 goodsNo 决定 Item 的 item_code。
            # 吉客云同货品可能返回多行（同 goodsNo 不同 skuId），应合并为一个 Item 而非撞主键。
            return (
                _pick(raw, ["goodsNo", "goodsCode", "code"])
                or _pick(raw, ["skuBarcode", "mainBarcode"])
                or _pick(raw, ["skuId", "sku_id"])
            )
        if resource == "Product Bundle":
            return _pick(raw, ["skuId", "goodsNo"])
        if resource == "Sales Order":
            return _pick(raw, ["tradeId", "tradeNo"])
        if resource == "Purchase Order":
            return _pick(raw, ["headId", "orderNum"])
        if resource in {"Purchase Receipt", "Sales Return"}:
            return _pick(raw, ["docId", "recId", "goodsdocNo"])
        if resource in {"Delivery Note", "Purchase Return"}:
            return _pick(raw, ["goodsdocNo"])
        if resource == "Stock Movement":
            inouttype = str(_pick(raw, ["inouttype"]) or "")
            doc_no = _pick(raw, ["goodsdocNo", "docId", "recId"])
            return f"{inouttype}:{doc_no}" if inouttype and doc_no else None
        if resource == "Stock Transfer":
            return _pick(raw, ["allocateId", "allocateNo"])
        if resource == "Stocktake":
            return _pick(raw, ["stocktakeId", "id"])
        if resource == "Batch":
            batch_no = _pick(raw, ["batchNo", "batchNumber"])
            goods_key = _pick(raw, ["skuId", "goodsNo", "skuBarcode"])
            return f"{goods_key}:{batch_no}" if batch_no else None
        if resource == "Inventory":
            return _pick(raw, ["quantityId"]) or ":".join(
                str(value or "")
                for value in (
                    _pick(raw, ["warehouseCode"]),
                    _pick(raw, ["skuId", "goodsNo"]),
                )
            )
        return _pick(raw, ["id"])

    def transform(self, resource: str, raw: dict):
        if resource == "Company":
            return {
                "doctype": "Company",
                "external_id": self.get_external_id(resource, raw),
                "company_name": _pick(raw, ["companyName"]),
                "company_code": _pick(raw, ["companyCode"]),
                "currency": _pick(raw, ["currencyCode"]),
            }

        if resource == "Department":
            return {
                "doctype": "Department",
                "external_id": self.get_external_id(resource, raw),
                "department_code": _pick(raw, ["departCode"]),
                "department_name": _pick(raw, ["departName"]),
                "company_code": _pick(raw, ["companyCode"]),
            }

        if resource == "Category":
            return {
                "doctype": "Jackyun Goods Category",
                "external_id": self.get_external_id(resource, raw),
                "category_id": str(_pick(raw, ["cateId", "categoryId", "id"])),
                "category_code": _pick(raw, ["cateCode", "categoryNo", "cateNo", "code"]),
                "category_name": _pick(raw, ["cateName", "categoryName", "name"]),
                "parent_external_id": _pick(raw, ["parentCateId", "parentCategoryId"]),
                "full_name": _pick(raw, ["cateFullName", "fullName"]),
                "disabled": _as_check(_pick(raw, ["isBlockup", "disabled"])),
                "deleted": _as_check(_pick(raw, ["isDelete", "deleted"])),
            }

        if resource == "Sales Channel":
            custom_fields = {key: raw.get(key) for key in (f"field{i}" for i in range(1, 31)) if raw.get(key) not in (None, "")}
            return {
                "doctype": "Jackyun Sales Channel",
                "external_id": self.get_external_id(resource, raw),
                "channel_id": str(_pick(raw, ["channelId", "salesId", "shopId", "id"])),
                "channel_code": _pick(raw, ["channelCode", "salesNo", "channelNo", "shopCode", "code"]),
                "channel_name": _pick(raw, ["channelName", "salesName", "shopName", "name"]),
                "channel_type": _pick(raw, ["channelType"]),
                "online_platform_code": _pick(raw, ["onlinePlatTypeCode"]),
                "online_platform_name": _pick(raw, ["onlinePlatTypeName"]),
                "warehouse_code": _pick(raw, ["warehouseCode"]),
                "warehouse_name": _pick(raw, ["warehouseName"]),
                "company_code": _pick(raw, ["companyCode"]),
                "company_name": _pick(raw, ["companyName"]),
                "department_id": _pick(raw, ["channelDepartId"]),
                "department_name": _pick(raw, ["channelDepartName"]),
                "contact_person": _pick(raw, ["linkMan"]),
                "contact_phone": _pick(raw, ["linkTel"]),
                "email": _pick(raw, ["email"]),
                "office_address": _pick(raw, ["officeAddress"]),
                "settlement_type": _pick(raw, ["chargeType"]),
                "category_id": _pick(raw, ["cateId"]),
                "category_name": _pick(raw, ["cateName"]),
                "platform_shop_id": _pick(raw, ["platShopId"]),
                "platform_shop_name": _pick(raw, ["platShopName"]),
                "responsible_user_name": _pick(raw, ["responsibleUserName"]),
                "memo": _pick(raw, ["memo"]),
                "disabled": _as_check(_pick(raw, ["isBlockup", "disabled"])),
                "deleted": _as_check(_pick(raw, ["isDelete", "deleted"])),
                "custom_fields": custom_fields or None,
            }

        if resource == "Warehouse":
            return {
                "doctype": "Warehouse",
                "external_id": self.get_external_id(resource, raw),
                "warehouse_code": _pick(raw, ["warehouseCode"]),
                "warehouse_name": _pick(raw, ["warehouseName"]),
                "company_code": _pick(raw, ["warehouseCompanyCode", "companyCode"]),
                "company_id": _pick(raw, ["warehouseCompanyId", "companyId"]),
                "disabled": _as_check(raw.get("isBlockup")) or _as_check(raw.get("isDelete")),
                "deleted": _as_check(raw.get("isDelete")),
                "address_line_1": _pick(raw, ["address"]),
                "city": _pick(raw, ["cityName"]),
                "state": _pick(raw, ["provinceName"]),
                "pin": _pick(raw, ["postcode"]),
                "phone_no": _pick(raw, ["tel"]),
            }

        if resource == "Inventory":
            return {
                "doctype": "Jackyun Inventory Snapshot",
                "external_id": self.get_external_id(resource, raw),
                "quantity_id": str(_pick(raw, ["quantityId"]) or self.get_external_id(resource, raw)),
                "warehouse_code": _pick(raw, ["warehouseCode"]),
                "warehouse_name": _pick(raw, ["warehouseName"]),
                "goods_no": _pick(raw, ["goodsNo"]),
                "goods_name": _pick(raw, ["goodsName"]),
                "sku_id": str(_pick(raw, ["skuId"]) or ""),
                "sku_name": _pick(raw, ["skuName"]),
                "sku_barcode": _pick(raw, ["skuBarcode"]),
                "unit_name": _pick(raw, ["unitName"]),
                "current_quantity": frappe.utils.flt(_pick(raw, ["currentQuantity"])),
                "available_quantity": frappe.utils.flt(_pick(raw, ["useQuantity"])),
                "locked_quantity": frappe.utils.flt(_pick(raw, ["lockedQuantity"])),
                "defective_quantity": frappe.utils.flt(
                    _pick(raw, ["defectiveQuantity", "defectiveQuanity"])
                ),
                "defective_available_quantity": frappe.utils.flt(
                    _pick(raw, ["defectiveUseQuantity"])
                ),
                "reserve_quantity": frappe.utils.flt(_pick(raw, ["reserveQuantity"])),
                "purchasing_quantity": frappe.utils.flt(_pick(raw, ["purchasingQuantity"])),
                "allocate_quantity": frappe.utils.flt(_pick(raw, ["allocateQuantity"])),
                "ordering_quantity": frappe.utils.flt(_pick(raw, ["orderingQuantity"])),
                "outer_quantity": frappe.utils.flt(_pick(raw, ["outerQuantity"])),
                "sales_return_quantity": frappe.utils.flt(_pick(raw, ["salesReturnQuantity"])),
                "stock_in_quantity": frappe.utils.flt(_pick(raw, ["stockInQuantity"])),
                "production_quantity": frappe.utils.flt(_pick(raw, ["productingQuantity"])),
                "stock_out_quantity": frappe.utils.flt(
                    _pick(raw, ["stockOutQuantity", "stockOutuantity"])
                ),
                "cost_price": frappe.utils.flt(_pick(raw, ["costPrice"])),
                "stock_index": str(_pick(raw, ["stockIndex"]) or ""),
                "owner_name": _pick(raw, ["ownerName"]),
                "brand_name": _pick(raw, ["brandName"]),
                "batch_data": (
                    json.dumps(raw.get("batchList"), ensure_ascii=False, default=str)
                    if raw.get("batchList")
                    else None
                ),
                "last_synced_at": frappe.utils.now(),
            }

        if resource == "Supplier":
            return {
                "doctype": "Supplier",
                "external_id": self.get_external_id(resource, raw),
                "supplier_code": _pick(raw, ["code"]),
                "supplier_name": _pick(raw, ["name", "abbreviation", "code"]),
                "supplier_group": _pick(raw, ["className"]),
                "supplier_type": "Company",
                "country": _country_name(_pick(raw, ["countryName"])),
                "tax_id": _pick(raw, ["taxIdentifyNumber", "taxNumber"]),
                "website": _pick(raw, ["website"]),
                "supplier_details": _pick(raw, ["memo", "mainProductsAndServices"]),
                "disabled": _as_check(raw.get("isBlockup")) or _as_check(raw.get("isDelete")),
            }

        if resource in {"Customer", "Offline Customer"}:
            invoice = raw.get("invoice") or {}
            if isinstance(invoice, list):
                invoice = invoice[0] if invoice else {}
            return {
                "doctype": "Customer",
                "external_id": self.get_external_id(resource, raw),
                "customer_code": _pick(raw, ["customerCode"]),
                "customer_name": _pick(raw, ["nickname", "alias", "customerCode"]),
                "customer_type": "Individual",
                "customer_group": _pick(raw, ["customerTypeName"]),
                "territory": _customer_territory(_pick(raw, ["country"])),
                "tax_id": _pick(raw, ["taxNumber"]) or invoice.get("taxNumber"),
                "customer_details": _pick(raw, ["remark", "specialReminding"]),
                "disabled": (
                    _as_check(raw.get("isDelete"))
                    or _as_check(raw.get("blackList"))
                    or (1 if str(raw.get("enable")) == "0" else 0)
                ),
            }

        if resource == "SKU":
            mapped = {"doctype": "Item", "external_id": self.get_external_id(resource, raw)}
            for field, paths in GOODS_SOURCES.items():
                val = _pick(raw, paths)
                if val is not None:
                    mapped[field] = val
            # 主数据兜底：Item 的 item_group / stock_uom / brand 是 Link
            mapped["item_group"] = _ensure_item_group(mapped.get("item_group"))
            uom = _ensure_uom(mapped.get("stock_uom"))
            if uom:
                mapped["stock_uom"] = uom
            else:
                mapped.pop("stock_uom", None)
            brand = _ensure_brand(_pick(raw, ["brandName"]))
            if brand:
                mapped["brand"] = brand
            # 辅助单位（goodsUnit）：写入 Item.uoms 换算表，装箱页按"箱"换算率
            # 自动计算整箱数与余数（1箱 = countRate 个基础单位 → conversion_factor）。
            uoms_rows = []
            for unit in raw.get("goodsUnit") or []:
                unit_name = frappe.utils.cstr(unit.get("unitName")).strip()
                factor = frappe.utils.flt(unit.get("countRate"))
                if not unit_name or factor <= 0:
                    continue
                ensured = _ensure_uom(unit_name)
                if not ensured:
                    continue
                if any(row.get("uom") == ensured for row in uoms_rows):
                    continue
                uoms_rows.append({"uom": ensured, "conversion_factor": factor})
            if uoms_rows:
                mapped["uoms"] = uoms_rows
            return mapped

        if resource == "Product Bundle":
            return {
                "doctype": "Product Bundle",
                "external_id": self.get_external_id(resource, raw),
                "parent_item": {
                    "item_code": _pick(raw, ["goodsNo"]),
                    "item_name": _pick(raw, ["goodsName", "goodsAlias"]),
                    "item_group": _pick(raw, ["cateName"]),
                    "stock_uom": _pick(raw, ["unitName"]),
                    "description": _pick(raw, ["goodsDesc", "memo", "goodsAlias"]),
                    "brand": _pick(raw, ["brandName"]),
                },
                "items": raw.get("goodsPackageDetail") or [],
            }

        if resource == "Sales Order":
            return {
                "doctype": "Sales Order",
                "external_id": self.get_external_id(resource, raw),
                "trade_no": _pick(raw, ["tradeNo"]),
                "source_trade_no": _pick(raw, ["onlineTradeNo", "sourceTradeNo"]),
                "discount_fee": _pick(raw, ["discountFee"]),
                "received_post_fee": _pick(raw, ["receivedPostFee"]),
                "trade_status": _pick(raw, ["tradeStatus"]),
                "trade_status_explain": _pick(raw, ["tradeStatusExplain"]),
                "trade_type": _pick(raw, ["tradeType"]),
                "trade_time": _pick(raw, ["tradeTime", "gmtCreate"]),
                "total_fee": _pick(raw, ["totalFee", "payment", "receivedTotal"]),
                "company_name": _pick(raw, ["companyName"]),
                "shop_id": _pick(raw, ["shopId"]),
                "shop_name": _pick(raw, ["shopName"]),
                "channel_code": _pick(raw, ["channelCode"]),
                "warehouse_id": _pick(raw, ["warehouseId"]),
                "warehouse_code": _pick(raw, ["warehouseCode"]),
                "customer_code": _pick(raw, ["customerCode"]),
                "customer_name": _pick(raw, ["customerName", "customerAccount"]),
                "customer_snapshot_name": _pick(raw, ["customerName"]),
                "customer_account": _pick(raw, ["customerAccount"]),
                "customer_email": _pick(raw, ["email"]),
                "country": _pick(raw, ["country"]),
                "state": _pick(raw, ["state"]),
                "city": _pick(raw, ["city"]),
                "district": _pick(raw, ["district"]),
                "town": _pick(raw, ["town"]),
                "zip": _pick(raw, ["zip"]),
                "payer_name": _pick(raw, ["payerName"]),
                "payer_phone": _pick(raw, ["payerPhone"]),
                "payer_address": _pick(raw, ["payerAddress"]),
                "currency": _pick(raw, ["chargeCurrencyCode"]),
                "conversion_rate": frappe.utils.flt(_pick(raw, ["chargeExchangeRate"])) or 1,
                "buyer_memo": _pick(raw, ["buyerMemo"]),
                "seller_memo": _pick(raw, ["sellerMemo"]),
                "items": raw.get("goodsDetail") or [],
            }

        if resource == "Purchase Order":
            return {
                "doctype": "Purchase Order",
                "external_id": self.get_external_id(resource, raw),
                "order_num": _pick(raw, ["orderNum"]),
                "external_order_num": _pick(raw, ["vendOrderId"]),
                "review_status": _pick(raw, ["revwStatus"]),
                "order_time": _jackyun_datetime(_pick(raw, ["orderTime", "gmtCreate"])),
                "company_name": _pick(raw, ["companyName"]),
                "supplier_id": _pick(raw, ["vendId"]),
                "supplier_code": _pick(raw, ["vendCode"]),
                "supplier_name": _pick(raw, ["vendName"]),
                "currency": _pick(raw, ["currencyCode"]),
                "memo": _pick(raw, ["memo"]),
                "in_status": _pick(raw, ["inStatusName"]),
                "settlement_status": _pick(raw, ["settStatusName"]),
                "items": raw.get("purchaseDetails") or [raw],
            }

        if resource in {"Purchase Receipt", "Sales Return"}:
            inbound_type_name = _pick(raw, ["inouttypeName"])
            return {
                "doctype": "Delivery Note" if resource == "Sales Return" else "Purchase Receipt",
                "external_id": self.get_external_id(resource, raw),
                "receipt_no": _pick(raw, ["goodsdocNo"]),
                "source_order_no": _pick(raw, ["sourceBillNo", "billNo"]),
                "receipt_time": _jackyun_datetime(
                    _pick(raw, ["inOutDate", "gmtCreate"])
                ),
                "inbound_type": _pick(raw, ["inouttype"])
                or (
                    105
                    if resource == "Sales Return" or inbound_type_name == "销售退货"
                    else (101 if inbound_type_name == "采购入库" else None)
                ),
                "inbound_type_name": inbound_type_name,
                "red_status": _pick(raw, ["redStatus"]),
                "company_name": _pick(raw, ["companyName"]),
                "warehouse_code": _pick(raw, ["warehouseCode"]),
                "supplier_id": _pick(raw, ["vendCustomerId"]),
                "supplier_code": _pick(raw, ["vendCode", "vendCustomerCode"]),
                "supplier_name": _pick(raw, ["vendCustomerName"]),
                "customer_code": _pick(raw, ["vendCode", "vendCustomerCode"]),
                "customer_name": _pick(raw, ["vendCustomerName"]),
                "channel_code": _pick(raw, ["channelCode"]),
                "currency": _pick(raw, ["currencyCode"]),
                "conversion_rate": frappe.utils.flt(_pick(raw, ["currencyRate"])) or 1,
                "comment": _pick(raw, ["goodsdocRemark", "comment"]),
                "memo": _pick(raw, ["receiveGoodsRemark", "memo"]),
                "items": raw.get("goodsDocDetailList") or [],
            }

        if resource in {"Delivery Note", "Purchase Return"}:
            outbound_type_name = _pick(raw, ["inouttypeName"])
            return {
                "doctype": "Purchase Receipt" if resource == "Purchase Return" else "Delivery Note",
                "external_id": self.get_external_id(resource, raw),
                "delivery_no": _pick(raw, ["goodsdocNo"]),
                "source_order_no": _pick(raw, ["billNo", "sourceBillNo"]),
                "outbound_time": _jackyun_datetime(
                    _pick(raw, ["inOutDate", "gmtCreate"])
                ),
                "outbound_type": _pick(raw, ["inouttype"])
                or (
                    205
                    if resource == "Purchase Return" or outbound_type_name == "采购退货"
                    else (201 if outbound_type_name == "销售出库" else None)
                ),
                "outbound_type_name": outbound_type_name,
                "red_status": _pick(raw, ["redStatus"]),
                "company_name": _pick(raw, ["companyName"]),
                "warehouse_code": _pick(raw, ["warehouseCode"]),
                "customer_code": _pick(raw, ["vendCode"]),
                "customer_name": _pick(raw, ["vendCustomerName"]),
                "supplier_code": _pick(raw, ["vendCode"]),
                "supplier_name": _pick(raw, ["vendCustomerName"]),
                "channel_code": _pick(raw, ["channelCode"]),
                "currency": _pick(raw, ["currencyCode"]),
                "conversion_rate": frappe.utils.flt(_pick(raw, ["currencyRate"])) or 1,
                "logistic_no": _pick(raw, ["logisticNo"]),
                "logistic_name": _pick(raw, ["logisticName"]),
                "external_order_no": _pick(raw, ["outBillNo"]),
                "comment": _pick(raw, ["goodsdocRemark"]),
                "memo": _pick(raw, ["receiveGoodsRemark"]),
                "items": raw.get("goodsDocDetailList") or [],
            }

        if resource == "Stock Transfer":
            return {
                "doctype": "Stock Entry",
                "external_id": self.get_external_id(resource, raw),
                "allocate_no": _pick(raw, ["allocateNo"]),
                "company_name": _pick(raw, ["companyName"]),
                "out_warehouse_code": _pick(raw, ["outWarehouseCode"]),
                "in_warehouse_code": _pick(raw, ["inWarehouseCode"]),
                "is_cross_company": frappe.utils.cint(raw.get("isAllocateDifferentCompany")),
                "status": frappe.utils.cint(raw.get("status")),
                "out_status": frappe.utils.cint(raw.get("outStatus")),
                "in_status": frappe.utils.cint(raw.get("inStatus")),
                "posting_time": _jackyun_datetime(
                    _pick(raw, ["auditDate", "gmtModified", "gmtCreate", "applyDate"])
                ),
                "memo": _pick(raw, ["memo"]),
                "reason": _pick(raw, ["reason"]),
                "source_no": _pick(raw, ["sourceNo"]),
                "items": raw.get("allocateDetails") or [raw],
            }

        if resource == "Stocktake":
            return {
                "doctype": "Stock Reconciliation",
                "external_id": self.get_external_id(resource, raw),
                "stocktake_no": _pick(raw, ["stocktakeId"]),
                "warehouse_code": _pick(raw, ["warehouseCode"]),
                "warehouse_name": _pick(raw, ["warehouseName"]),
                "stocktake_time": _jackyun_datetime(_pick(raw, ["stocktakeDate"])),
                "status": frappe.utils.cint(raw.get("status")),
                "summary": _pick(raw, ["text"]),
                "created_by": _pick(raw, ["createName"]),
                "items": raw.get("stockTakeDetailViews") or [],
            }

        if resource == "Batch":
            return {
                "doctype": "Batch",
                "external_id": self.get_external_id(resource, raw),
                "batch_no": _pick(raw, ["batchNo", "batchNumber"]),
                "batch_number": _pick(raw, ["batchNumber"]),
                "goods_no": _pick(raw, ["goodsNo"]),
                "sku_id": _pick(raw, ["skuId"]),
                "sku_barcode": _pick(raw, ["skuBarcode"]),
                "manufacturing_date": _jackyun_datetime(_pick(raw, ["productionDate"])),
                "expiry_date": _jackyun_datetime(_pick(raw, ["expirationDate"])),
                "disabled": frappe.utils.cint(raw.get("isFreeze")),
                "description": _pick(raw, ["memo", "freezeReason"]),
            }

        if resource == "Stock Movement":
            inouttype = str(_pick(raw, ["inouttype"]) or "")
            stock_entry_type = (
                STOCK_MOVEMENT_INBOUND_TYPES.get(inouttype)
                or STOCK_MOVEMENT_OUTBOUND_TYPES.get(inouttype, "Material Issue")
            )
            return {
                "doctype": "Stock Entry",
                "external_id": self.get_external_id(resource, raw),
                "stock_entry_type": stock_entry_type,
                "direction": "in" if inouttype in STOCK_MOVEMENT_INBOUND_TYPES else "out",
                "goodsdoc_no": _pick(raw, ["goodsdocNo"]),
                "inouttype": inouttype,
                "inouttype_name": _pick(raw, ["inouttypeName"]),
                "company_name": _pick(raw, ["companyName"]),
                "warehouse_code": _pick(raw, ["warehouseCode"]),
                "posting_time": _jackyun_datetime(_pick(raw, ["inOutDate", "gmtCreate"])),
                "comment": _pick(raw, ["goodsdocRemark"]),
                "items": raw.get("goodsDocDetailList") or [],
            }

        raise NotImplementedError(f"资源 {resource} 的 transform 尚未实现")

    def remember_sku_aliases(self, raw, item_name):
        """Preserve specification IDs even while Item codes remain goodsNo-based."""
        for external_id in (_pick(raw, ["skuId", "sku_id"]), _pick(raw, ["goodsNo"])):
            if external_id not in (None, ""):
                self.remember_mapping("SKU", str(external_id), "Item", item_name)

    def _resolve_bundle_component(self, component):
        sku_id = _pick(component, ["skuId"])
        if sku_id not in (None, ""):
            mapping_name = self.get_mapping_name("SKU", str(sku_id))
            if mapping_name:
                return frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")

        goods_no = _pick(component, ["goodsNo"])
        if goods_no and frappe.db.exists("Item", goods_no):
            return goods_no
        return None

    def _ensure_bundle_parent_item(self, values):
        item_code = values.get("item_code")
        if not item_code:
            frappe.throw("组合装缺少 goodsNo，无法创建父 Item")

        if frappe.db.exists("Item", item_code):
            item = frappe.get_doc("Item", item_code)
            # Older SKU imports may have left duplicate rows in Item.uoms.  Any
            # later Item.save() then fails ERPNext's conversion-table validation,
            # so normalise the child table before converting the bundle parent.
            unique_uoms = []
            seen_uoms = set()
            for row in item.get("uoms") or []:
                uom = frappe.utils.cstr(row.uom)
                if not uom or uom in seen_uoms:
                    continue
                seen_uoms.add(uom)
                unique_uoms.append(row)
            if len(unique_uoms) != len(item.get("uoms") or []):
                item.set("uoms", unique_uoms)
            if item.is_stock_item:
                if frappe.db.exists("Stock Ledger Entry", {"item_code": item_code}):
                    frappe.throw(f"组合装父 Item {item_code} 已有库存流水，不能自动改为非库存商品")
                item.is_stock_item = 0
            item.is_sales_item = 1
            if values.get("item_name"):
                item.item_name = values["item_name"]
            if values.get("description"):
                item.description = values["description"]
            item.save(ignore_permissions=True)
            return item.name

        item_group = _ensure_item_group(values.get("item_group"))
        stock_uom = _ensure_uom(values.get("stock_uom")) or "Nos"
        _ensure_uom(stock_uom)
        item = frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": item_code,
                "item_name": values.get("item_name") or item_code,
                "item_group": item_group,
                "stock_uom": stock_uom,
                "description": values.get("description"),
                "is_stock_item": 0,
                "is_sales_item": 1,
            }
        )
        brand = _ensure_brand(values.get("brand"))
        if brand:
            item.brand = brand
        item.insert(ignore_permissions=True)
        return item.name

    def upsert(self, resource: str, mapped: dict):
        if resource == "Company":
            return self._upsert_company(mapped)
        if resource == "Department":
            return self._upsert_department(mapped)
        if resource == "Category":
            return self._upsert_category(mapped)
        if resource == "Sales Channel":
            return self._upsert_sales_channel(mapped)
        if resource == "Warehouse":
            return self._upsert_warehouse(mapped)
        if resource == "Inventory":
            return self._upsert_inventory(mapped)
        if resource == "Supplier":
            return self._upsert_supplier(mapped)
        if resource in {"Customer", "Offline Customer"}:
            return self._upsert_customer(mapped)
        if resource == "Sales Order":
            return self._upsert_sales_order(mapped)
        if resource == "Purchase Order":
            return self._upsert_purchase_order(mapped)
        if resource == "Purchase Receipt":
            return self._upsert_purchase_receipt(mapped)
        if resource == "Delivery Note":
            return self._upsert_delivery_note(mapped)
        if resource == "Sales Return":
            return self._upsert_sales_return(mapped)
        if resource == "Purchase Return":
            return self._upsert_purchase_return(mapped)
        if resource == "Stock Movement":
            return self._upsert_stock_movement(mapped)
        if resource == "Stock Transfer":
            return self._upsert_stock_transfer(mapped)
        if resource == "Stocktake":
            return self._upsert_stocktake(mapped)
        if resource == "Batch":
            return self._upsert_batch(mapped)
        if resource == "SKU":
            external_id = frappe.utils.cstr(mapped.get("external_id")).strip()
            item_code = frappe.utils.cstr(mapped.get("item_code")).strip()
            mapping_name = self.get_mapping_name("SKU", external_id)
            mapped_name = (
                frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
                if mapping_name
                else None
            )
            if mapping_name and (
                not mapped_name or not frappe.db.exists("Item", mapped_name)
            ):
                # 映射可能在业务数据清理后残留。不要把不存在的 Item 当成同步成功；
                # 使用当前吉客云 SKU 快照重建 Item，并原位修复映射。
                values = dict(mapped)
                values.pop("doctype", None)
                values.pop("external_id", None)
                doc = frappe.get_doc({"doctype": "Item", **values})
                doc.insert(ignore_permissions=True)
                self.remember_mapping("SKU", external_id, "Item", doc.name)
                return doc.name, "created"
            if (
                not mapping_name
                and item_code
                and frappe.db.exists("Item", item_code)
            ):
                # Older imports may have created the Item before the external-ID
                # map was introduced. Reattach that existing master instead of
                # attempting a duplicate primary-key insert on every sync.
                self.remember_mapping("SKU", external_id, "Item", item_code)
                return item_code, "updated"
        if resource != "Product Bundle":
            return super().upsert(resource, mapped)

        mapped = dict(mapped)
        external_id = str(mapped.pop("external_id"))
        mapped.pop("doctype", None)
        parent_item = self._ensure_bundle_parent_item(mapped["parent_item"])

        child_rows = []
        missing = []
        for component in mapped.get("items") or []:
            item_code = self._resolve_bundle_component(component)
            if not item_code:
                missing.append(str(_pick(component, ["skuId", "goodsNo"]) or "未知子件"))
                continue
            qty = frappe.utils.flt(component.get("goodsAmount"))
            if qty <= 0:
                frappe.throw(f"组合装 {parent_item} 的子件 {item_code} 数量必须大于 0")
            child_rows.append(
                {
                    "item_code": item_code,
                    "qty": qty,
                    "description": component.get("skuProperitesName") or component.get("goodsName"),
                    "rate": frappe.utils.flt(component.get("sharePrice")),
                }
            )

        if missing:
            frappe.throw(f"组合装 {parent_item} 存在未同步子件 skuId/goodsNo：{', '.join(missing)}")
        if not child_rows:
            frappe.throw(f"组合装 {parent_item} 没有有效子件")

        created = not frappe.db.exists("Product Bundle", parent_item)
        bundle = frappe.new_doc("Product Bundle") if created else frappe.get_doc("Product Bundle", parent_item)
        bundle.new_item_code = parent_item
        bundle.description = mapped["parent_item"].get("description") or mapped["parent_item"].get("item_name")
        bundle.disabled = 0
        bundle.set("items", child_rows)
        if created:
            bundle.insert(ignore_permissions=True)
        else:
            bundle.save(ignore_permissions=True)

        self.remember_mapping("Product Bundle", external_id, "Product Bundle", bundle.name)
        return bundle.name, "created" if created else "updated"

    def _upsert_company(self, mapped):
        external_id = str(mapped["external_id"])
        desired_name = frappe.utils.cstr(mapped.get("company_name")).strip()
        if not desired_name:
            frappe.throw("吉客云公司缺少 companyName")

        mapping_name = self.get_mapping_name("Company", external_id)
        company_name = None
        if mapping_name:
            company_name = frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
        elif frappe.db.exists("Company", desired_name):
            company_name = desired_name
        if not company_name:
            frappe.throw(f"吉客云公司 {desired_name} 未找到可映射的 ERPNext Company")

        if company_name != desired_name:
            if frappe.db.exists("Company", desired_name):
                frappe.throw(f"目标公司名称 {desired_name} 已存在，不能自动合并公司")
            company_name = frappe.rename_doc(
                "Company", company_name, desired_name, force=False, ignore_permissions=True
            )

        self.remember_mapping("Company", external_id, "Company", company_name)
        company_code = frappe.utils.cstr(mapped.get("company_code")).strip()
        if company_code:
            self.remember_mapping("Company", company_code, "Company", company_name)
        return company_name, "updated"

    def _upsert_department(self, mapped):
        external_id = str(mapped["external_id"])
        department_name = frappe.utils.cstr(mapped.get("department_name")).strip()
        company_code = frappe.utils.cstr(mapped.get("company_code")).strip()
        if not department_name:
            frappe.throw("吉客云部门缺少 departName")
        if not company_code:
            frappe.throw(f"吉客云部门 {department_name} 缺少 companyCode")

        company_mapping = self.get_mapping_name("Company", company_code)
        company = (
            frappe.db.get_value("External ID Mapping", company_mapping, "erpnext_name")
            if company_mapping
            else None
        )
        if not company or not frappe.db.exists("Company", company):
            frappe.throw(
                f"吉客云部门 {department_name} 的公司编码 {company_code} 尚未映射，请先同步公司"
            )

        mapping_name = self.get_mapping_name("Department", external_id)
        erpnext_name = (
            frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            if mapping_name
            else None
        )
        created = False
        if erpnext_name and frappe.db.exists("Department", erpnext_name):
            doc = frappe.get_doc("Department", erpnext_name)
            if doc.company != company:
                frappe.throw(
                    f"部门 {erpnext_name} 已属于 {doc.company}，不能自动迁移到 {company}"
                )
            if doc.department_name != department_name:
                erpnext_name = frappe.rename_doc(
                    "Department",
                    doc.name,
                    department_name,
                    force=False,
                    ignore_permissions=True,
                )
                doc = frappe.get_doc("Department", erpnext_name)
        else:
            erpnext_name = frappe.db.get_value(
                "Department",
                {"department_name": department_name, "company": company},
                "name",
            )
            if erpnext_name:
                doc = frappe.get_doc("Department", erpnext_name)
            else:
                doc = frappe.get_doc(
                    {
                        "doctype": "Department",
                        "department_name": department_name,
                        "company": company,
                        "is_group": 0,
                    }
                )
                doc.insert(ignore_permissions=True)
                erpnext_name = doc.name
                created = True

        self.remember_mapping("Department", external_id, "Department", erpnext_name)
        return erpnext_name, "created" if created else "updated"

    def _upsert_category(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)
        parent_external_id = values.pop("parent_external_id", None)
        values["parent_external_id"] = str(parent_external_id) if parent_external_id not in (None, "") else None
        values["parent_category"] = None
        if parent_external_id not in (None, ""):
            parent_mapping = self.get_mapping_name("Category", str(parent_external_id))
            if parent_mapping:
                values["parent_category"] = frappe.db.get_value(
                    "External ID Mapping", parent_mapping, "erpnext_name"
                )

        mapping_name = self.get_mapping_name("Category", external_id)
        created = not mapping_name
        if mapping_name:
            name = frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            doc = frappe.get_doc("Jackyun Goods Category", name)
            doc.update(values)
            doc.save(ignore_permissions=True)
        else:
            doc = frappe.get_doc({"doctype": "Jackyun Goods Category", **values})
            doc.insert(ignore_permissions=True)
        self.remember_mapping("Category", external_id, "Jackyun Goods Category", doc.name)
        _sync_category_item_group(doc)
        return doc.name, "created" if created else "updated"

    def _upsert_warehouse(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)
        warehouse_code = frappe.utils.cstr(values.pop("warehouse_code", None)).strip()
        warehouse_name = frappe.utils.cstr(values.get("warehouse_name")).strip()
        company_code = frappe.utils.cstr(values.pop("company_code", None)).strip()
        company_id = frappe.utils.cstr(values.pop("company_id", None)).strip()
        deleted = frappe.utils.cint(values.pop("deleted", 0))
        if not warehouse_name:
            frappe.throw("吉客云仓库缺少 warehouseName")

        company = None
        for company_key in (company_code, company_id):
            if not company_key:
                continue
            company_mapping = self.get_mapping_name("Company", company_key)
            if company_mapping:
                company = frappe.db.get_value(
                    "External ID Mapping", company_mapping, "erpnext_name"
                )
                break
        if not company or not frappe.db.exists("Company", company):
            frappe.throw(
                f"吉客云仓库 {warehouse_name} 的公司 {company_code or company_id} 尚未映射"
            )

        parent_warehouse = frappe.db.get_value(
            "Warehouse",
            {"company": company, "is_group": 1},
            "name",
            order_by="lft asc",
        )
        if not parent_warehouse:
            frappe.throw(f"ERPNext 公司 {company} 缺少仓库根节点")

        mapping_name = self.get_mapping_name("Warehouse", external_id)
        erpnext_name = (
            frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            if mapping_name
            else None
        )
        created = False
        if erpnext_name and frappe.db.exists("Warehouse", erpnext_name):
            doc = frappe.get_doc("Warehouse", erpnext_name)
            if doc.company != company:
                frappe.throw(f"仓库 {doc.name} 已属于 {doc.company}，不能自动迁移到 {company}")
            desired_name = f"{warehouse_name} - {frappe.get_cached_value('Company', company, 'abbr')}"
            if doc.name != desired_name:
                erpnext_name = frappe.rename_doc(
                    "Warehouse", doc.name, desired_name, force=False, ignore_permissions=True
                )
                doc = frappe.get_doc("Warehouse", erpnext_name)
        else:
            erpnext_name = frappe.db.get_value(
                "Warehouse", {"warehouse_name": warehouse_name, "company": company}, "name"
            )
            if erpnext_name:
                doc = frappe.get_doc("Warehouse", erpnext_name)
            else:
                doc = frappe.new_doc("Warehouse")
                doc.warehouse_name = warehouse_name
                doc.company = company
                doc.parent_warehouse = parent_warehouse
                doc.is_group = 0
                created = True

        doc.parent_warehouse = parent_warehouse
        for field in ("disabled", "address_line_1", "city", "state", "pin", "phone_no"):
            doc.set(field, values.get(field))
        if created:
            doc.insert(ignore_permissions=True)
        else:
            doc.save(ignore_permissions=True)
        erpnext_name = doc.name

        self.remember_mapping("Warehouse", external_id, "Warehouse", erpnext_name)
        if warehouse_code and not deleted:
            self.remember_mapping(
                "Warehouse", f"code:{warehouse_code}", "Warehouse", erpnext_name
            )
        return erpnext_name, "created" if created else "updated"

    def _upsert_inventory(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)
        warehouse_code = frappe.utils.cstr(values.get("warehouse_code")).strip()
        warehouse_mapping = self.get_mapping_name("Warehouse", f"code:{warehouse_code}")
        warehouse = (
            frappe.db.get_value("External ID Mapping", warehouse_mapping, "erpnext_name")
            if warehouse_mapping
            else None
        )
        if not warehouse or not frappe.db.exists("Warehouse", warehouse):
            frappe.throw(f"库存记录的仓库编码 {warehouse_code} 尚未映射")

        item = None
        sku_id = frappe.utils.cstr(values.get("sku_id")).strip()
        if sku_id:
            sku_mapping = self.get_mapping_name("SKU", sku_id)
            if sku_mapping:
                item = frappe.db.get_value("External ID Mapping", sku_mapping, "erpnext_name")
        goods_no = frappe.utils.cstr(values.get("goods_no")).strip()
        if not item and goods_no and frappe.db.exists("Item", goods_no):
            item = goods_no
        if not item or not frappe.db.exists("Item", item):
            item = self._restore_inventory_item_from_sku_snapshot(goods_no, sku_id)
        if not item or not frappe.db.exists("Item", item):
            frappe.throw(f"库存记录的商品 {goods_no or sku_id} 尚未同步")

        values["warehouse"] = warehouse
        values["item"] = item
        mapping_name = self.get_mapping_name("Inventory", external_id)
        created = not mapping_name
        if mapping_name:
            name = frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            doc = frappe.get_doc("Jackyun Inventory Snapshot", name)
            doc.update(values)
            doc.save(ignore_permissions=True)
        else:
            doc = frappe.get_doc({"doctype": "Jackyun Inventory Snapshot", **values})
            doc.insert(ignore_permissions=True)
        self.remember_mapping(
            "Inventory", external_id, "Jackyun Inventory Snapshot", doc.name
        )
        return doc.name, "created" if created else "updated"

    def _restore_inventory_item_from_sku_snapshot(self, goods_no, sku_id=None):
        """Repair a stale SKU mapping from an already preserved source snapshot.

        Inventory payloads do not contain enough master-data fields to invent an
        Item.  Recovery is therefore allowed only from a successful raw SKU
        response previously retained by the connector.
        """
        for external_id in (goods_no, sku_id):
            external_id = frappe.utils.cstr(external_id).strip()
            if not external_id:
                continue
            snapshots = frappe.get_all(
                "Jackyun Raw Record",
                filters={
                    "resource": "SKU",
                    "external_id": external_id,
                    "processing_status": "Succeeded",
                },
                fields=["raw_data"],
                order_by="modified desc",
                limit=1,
            )
            if not snapshots:
                continue
            try:
                raw = json.loads(snapshots[0].raw_data)
                item_name, _status = self.upsert("SKU", self.transform("SKU", raw))
                self.remember_sku_aliases(raw, item_name)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"恢复库存商品主数据失败：{external_id}",
                )
                continue
            if item_name and frappe.db.exists("Item", item_name):
                return item_name
        return None

    def _upsert_supplier(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)
        supplier_code = frappe.utils.cstr(values.pop("supplier_code", None)).strip()
        supplier_name = frappe.utils.cstr(values.get("supplier_name")).strip()
        if not supplier_name:
            frappe.throw("吉客云供应商缺少名称和编码")
        values["supplier_group"] = _ensure_supplier_group(values.get("supplier_group"))

        mapping_name = self.get_mapping_name("Supplier", external_id)
        erpnext_name = (
            frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            if mapping_name
            else None
        )
        created = False
        if erpnext_name and frappe.db.exists("Supplier", erpnext_name):
            doc = frappe.get_doc("Supplier", erpnext_name)
            doc.update(values)
            doc.save(ignore_permissions=True)
        else:
            erpnext_name = frappe.db.get_value(
                "Supplier", {"supplier_name": supplier_name}, "name"
            )
            if erpnext_name:
                doc = frappe.get_doc("Supplier", erpnext_name)
                doc.update(values)
                doc.save(ignore_permissions=True)
            else:
                doc = frappe.get_doc({"doctype": "Supplier", **values})
                doc.insert(ignore_permissions=True)
                created = True

        self.remember_mapping("Supplier", external_id, "Supplier", doc.name)
        if supplier_code:
            self.remember_mapping(
                "Supplier", f"code:{supplier_code}", "Supplier", doc.name
            )
        return doc.name, "created" if created else "updated"

    def _upsert_customer(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)
        customer_code = frappe.utils.cstr(values.pop("customer_code", None)).strip()
        customer_name = frappe.utils.cstr(values.get("customer_name")).strip()
        if not customer_name:
            frappe.throw("吉客云客户缺少名称和编码")
        values["customer_group"] = _ensure_customer_group(values.get("customer_group"))

        mapping_name = self.get_mapping_name("Customer", external_id)
        erpnext_name = (
            frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            if mapping_name
            else None
        )
        created = not (erpnext_name and frappe.db.exists("Customer", erpnext_name))
        if created:
            doc = frappe.get_doc({"doctype": "Customer", **values})
            doc.insert(ignore_permissions=True)
        else:
            doc = frappe.get_doc("Customer", erpnext_name)
            doc.update(values)
            doc.save(ignore_permissions=True)

        self.remember_mapping("Customer", external_id, "Customer", doc.name)
        if customer_code:
            self.remember_mapping(
                "Customer", f"code:{customer_code}", "Customer", doc.name
            )
        return doc.name, "created" if created else "updated"

    def _mapped_erpnext_name(self, resource, external_id):
        if external_id in (None, ""):
            return None
        mapping_name = self.get_mapping_name(resource, str(external_id))
        if not mapping_name:
            return None
        return frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")

    def _resolve_sales_order_warehouse(self, mapped):
        for warehouse_key in (
            mapped.get("warehouse_id"),
            f"code:{mapped.get('warehouse_code')}" if mapped.get("warehouse_code") else None,
        ):
            warehouse = self._mapped_erpnext_name("Warehouse", warehouse_key)
            if warehouse and frappe.db.exists("Warehouse", warehouse):
                return warehouse
        return None

    def _resolve_sales_order_company(self, mapped, warehouse=None):
        company_name = frappe.utils.cstr(mapped.get("company_name")).strip()
        if company_name and frappe.db.exists("Company", company_name):
            return company_name
        if warehouse:
            company = frappe.db.get_value("Warehouse", warehouse, "company")
            if company and frappe.db.exists("Company", company):
                return company
        frappe.throw(
            f"销售单 {mapped.get('trade_no') or mapped.get('external_id')} 的公司 "
            f"{company_name or '未知'} 尚未同步，且无法从仓库确定公司"
        )

    def _fallback_sales_order_warehouse(self, company):
        default_warehouse = frappe.get_cached_value("Company", company, "default_warehouse")
        if default_warehouse and not frappe.db.get_value(
            "Warehouse", default_warehouse, "disabled"
        ):
            return default_warehouse
        warehouses = frappe.get_all(
            "Warehouse",
            filters={"company": company, "is_group": 0, "disabled": 0},
            fields=["name", "warehouse_name"],
            order_by="lft asc",
        )
        preferred = next(
            (row.name for row in warehouses if row.warehouse_name == "拣货仓"), None
        )
        return preferred or (warehouses[0].name if warehouses else None)

    def _resolve_sales_channel(self, mapped):
        channel_code = frappe.utils.cstr(mapped.get("channel_code")).strip()
        if channel_code:
            channel = frappe.db.get_value(
                "Jackyun Sales Channel", {"channel_code": channel_code}, "name"
            )
            if channel:
                return channel

        shop_id = frappe.utils.cstr(mapped.get("shop_id")).strip()
        if shop_id:
            channel = frappe.db.get_value(
                "Jackyun Sales Channel", {"platform_shop_id": shop_id}, "name"
            )
            if channel:
                return channel

        shop_name = frappe.utils.cstr(mapped.get("shop_name")).strip()
        for fieldname in ("channel_name", "platform_shop_name"):
            if not shop_name:
                break
            matches = frappe.get_all(
                "Jackyun Sales Channel",
                filters={fieldname: shop_name},
                pluck="name",
                limit=2,
            )
            if len(matches) == 1:
                return matches[0]
        return None

    def _resolve_transaction_sales_channel(self, mapped, source_sales_order=None):
        """Prefer fields on the transaction, then inherit the channel from its source order."""
        channel = self._resolve_sales_channel(mapped)
        if channel:
            return channel
        if source_sales_order and frappe.db.exists("Sales Order", source_sales_order):
            return frappe.db.get_value(
                "Sales Order", source_sales_order, "custom_jackyun_sales_channel"
            )
        return None

    def _is_ecommerce_channel(self, mapped):
        channel = self._resolve_sales_channel(mapped)
        if not channel:
            return False
        values = frappe.db.get_value(
            "Jackyun Sales Channel",
            channel,
            ["online_platform_code", "online_platform_name", "platform_shop_id"],
            as_dict=True,
        )
        return bool(
            values
            and any(
                frappe.utils.cstr(values.get(field)).strip()
                for field in (
                    "online_platform_code",
                    "online_platform_name",
                    "platform_shop_id",
                )
            )
        )

    def _ensure_shared_ecommerce_customer(self):
        customer = frappe.db.get_value("Customer", {"customer_name": "电商客户"}, "name")
        if customer:
            return customer
        doc = frappe.get_doc(
            {
                "doctype": "Customer",
                "customer_name": "电商客户",
                "customer_type": "Individual",
                "customer_group": _ensure_customer_group("Individual"),
                "territory": "All Territories",
                "customer_details": "吉客云电商零售订单统一客户；实际来源以销售渠道字段为准。",
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name

    def _resolve_sales_order_customer(self, mapped):
        if self._is_ecommerce_channel(mapped):
            customer = self._ensure_shared_ecommerce_customer()
            channel = self._resolve_sales_channel(mapped)
            if channel:
                self.remember_mapping(
                    "Customer", f"channel:{channel}", "Customer", customer
                )
            return customer

        customer_code = frappe.utils.cstr(mapped.get("customer_code")).strip()
        if customer_code:
            customer = self._mapped_erpnext_name("Customer", f"code:{customer_code}")
            if customer and frappe.db.exists("Customer", customer):
                return customer

        shop_id = frappe.utils.cstr(mapped.get("shop_id")).strip()
        shop_name = frappe.utils.cstr(mapped.get("shop_name")).strip()
        shop_key = shop_id or shop_name or "未识别店铺"
        mapping_key = f"shop:{shop_key}"
        customer = self._mapped_erpnext_name("Customer", mapping_key)
        if customer and frappe.db.exists("Customer", customer):
            return customer

        customer_name = f"电商客户 - {shop_name or shop_id or '未识别店铺'}"
        customer = frappe.db.get_value("Customer", {"customer_name": customer_name}, "name")
        if not customer:
            doc = frappe.get_doc(
                {
                    "doctype": "Customer",
                    "customer_name": customer_name,
                    "customer_type": "Individual",
                    "customer_group": _ensure_customer_group("Individual"),
                    "territory": "All Territories",
                    "customer_details": "吉客云销售单同步使用的店铺共享客户，不代表单个电商消费者。",
                }
            )
            doc.insert(ignore_permissions=True)
            customer = doc.name
        self.remember_mapping("Customer", mapping_key, "Customer", customer)
        return customer

    def _resolve_order_item(self, item_row):
        for external_id in (
            _pick(item_row, ["specId", "skuId"]),
            _pick(item_row, ["goodsId"]),
            _pick(item_row, ["goodsNo"]),
        ):
            item_code = self._mapped_erpnext_name("SKU", external_id)
            if item_code and frappe.db.exists("Item", item_code):
                return item_code

        for item_code in (
            _pick(item_row, ["goodsNo"]),
            _pick(item_row, ["outerSkuId"]),
            _pick(item_row, ["outerId"]),
            _pick(item_row, ["barcode"]),
        ):
            if item_code and frappe.db.exists("Item", item_code):
                return item_code

        item_code = frappe.utils.cstr(
            _pick(item_row, ["goodsNo", "outerSkuId", "outerId", "barcode"])
        ).strip()
        if not item_code:
            frappe.throw(f"订单商品 {item_row.get('goodsName') or '未知'} 缺少可用商品编码")

        stock_uom = _ensure_uom(_pick(item_row, ["unit", "unitName"])) or "Nos"
        _ensure_uom(stock_uom)
        item = frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": item_code,
                "item_name": _pick(item_row, ["goodsName", "specName"]) or item_code,
                "item_group": "All Item Groups",
                "stock_uom": stock_uom,
                "is_stock_item": 1,
                "is_sales_item": 1,
                "is_purchase_item": 1,
                "description": _pick(
                    item_row, ["specName", "skuProperitesName", "goodsName"]
                ),
            }
        )
        item.insert(ignore_permissions=True)
        for external_id in (
            _pick(item_row, ["specId", "skuId"]),
            _pick(item_row, ["goodsId"]),
            _pick(item_row, ["goodsNo"]),
        ):
            if external_id not in (None, ""):
                self.remember_mapping("SKU", str(external_id), "Item", item.name)
        return item.name

    def _resolve_transaction_batch(self, item_code, raw_batch_no):
        """Resolve a JackYun batch number to an ERPNext Batch document."""
        raw_batch_no = frappe.utils.cstr(raw_batch_no).strip()
        if not raw_batch_no:
            return None
        if not frappe.db.get_value("Item", item_code, "has_batch_no"):
            return None
        batch = frappe.db.get_value(
            "Batch", {"item": item_code, "batch_id": raw_batch_no}, "name"
        )
        if batch:
            return batch
        candidate = f"{raw_batch_no}-{item_code}"[:140]
        if frappe.db.exists("Batch", candidate):
            return candidate
        if frappe.db.exists("Batch", raw_batch_no):
            existing_item = frappe.db.get_value("Batch", raw_batch_no, "item")
            if existing_item == item_code:
                return raw_batch_no
        doc = frappe.get_doc(
            {
                "doctype": "Batch",
                "batch_id": candidate if frappe.db.exists("Batch", raw_batch_no) else raw_batch_no,
                "item": item_code,
                "description": f"由吉客云库存业务单据补建；原始批次号：{raw_batch_no}",
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name

    def _resolve_purchase_order_supplier(self, values):
        for supplier_key in (
            values.get("supplier_id"),
            f"code:{values.get('supplier_code')}" if values.get("supplier_code") else None,
        ):
            supplier = self._mapped_erpnext_name("Supplier", supplier_key)
            if supplier and frappe.db.exists("Supplier", supplier):
                return supplier

        supplier_name = frappe.utils.cstr(values.get("supplier_name")).strip()
        if supplier_name:
            supplier = frappe.db.get_value(
                "Supplier", {"supplier_name": supplier_name}, "name"
            )
            if supplier:
                return supplier
        if not supplier_name:
            frappe.throw(
                f"采购单 {values.get('order_num') or values.get('external_id')} 缺少供应商"
            )

        supplier, _status = self._upsert_supplier(
            {
                "doctype": "Supplier",
                "external_id": values.get("supplier_id") or values.get("supplier_code"),
                "supplier_code": values.get("supplier_code"),
                "supplier_name": supplier_name,
                "supplier_type": "Company",
                "supplier_group": "All Supplier Groups",
                "disabled": 0,
            }
        )
        return supplier

    def _upsert_purchase_order(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)

        first_item = (values.get("items") or [{}])[0]
        warehouse = self._resolve_sales_order_warehouse(
            {
                "warehouse_id": _pick(first_item, ["warehouseId"]),
                "warehouse_code": _pick(first_item, ["warehouseCode"]),
            }
        )
        company_name = frappe.utils.cstr(values.get("company_name")).strip()
        company = company_name if frappe.db.exists("Company", company_name) else None
        if not company and warehouse:
            company = frappe.db.get_value("Warehouse", warehouse, "company")
        if not company:
            frappe.throw(
                f"采购单 {values.get('order_num') or external_id} 的公司 "
                f"{company_name or '未知'} 尚未同步"
            )
        if warehouse and (
            frappe.db.get_value("Warehouse", warehouse, "company") != company
            or frappe.db.get_value("Warehouse", warehouse, "disabled")
        ):
            warehouse = None
        if not warehouse:
            warehouse = self._fallback_sales_order_warehouse(company)

        supplier = self._resolve_purchase_order_supplier(values)
        transaction_date = frappe.utils.getdate(
            values.get("order_time") or frappe.utils.nowdate()
        )
        company_currency = frappe.get_cached_value("Company", company, "default_currency")
        currency = frappe.utils.cstr(values.get("currency")).strip()
        if currency in {"RMB", "人民币"}:
            currency = "CNY"
        if not currency or not frappe.db.exists("Currency", currency):
            currency = company_currency

        rows = []
        tax_rates = set()
        for item_row in values.get("items") or []:
            qty = frappe.utils.flt(item_row.get("quantity"))
            if qty <= 0:
                continue
            item_code = self._resolve_order_item(item_row)
            item_warehouse = self._resolve_sales_order_warehouse(
                {
                    "warehouse_id": _pick(item_row, ["warehouseId"]),
                    "warehouse_code": _pick(item_row, ["warehouseCode"]),
                }
            )
            if item_warehouse and (
                frappe.db.get_value("Warehouse", item_warehouse, "company") != company
                or frappe.db.get_value("Warehouse", item_warehouse, "disabled")
            ):
                item_warehouse = None
            item_warehouse = item_warehouse or warehouse
            received_date = _jackyun_datetime(item_row.get("recDate"))
            schedule_date = frappe.utils.getdate(received_date or transaction_date)
            if schedule_date < transaction_date:
                schedule_date = transaction_date
            amount = frappe.utils.flt(item_row.get("amount"))
            rate = amount / qty if amount else frappe.utils.flt(item_row.get("price"))
            row = {
                "item_code": item_code,
                "qty": qty,
                "rate": rate,
                "schedule_date": schedule_date,
                "description": item_row.get("rowRemark")
                or item_row.get("skuProperitesName")
                or item_row.get("goodsName"),
            }
            if item_warehouse:
                row["warehouse"] = item_warehouse
            rows.append(row)
            tax_rate = frappe.utils.flt(item_row.get("taxrate"))
            if tax_rate:
                tax_rates.add(tax_rate)
        if not rows:
            frappe.throw(f"采购单 {values.get('order_num') or external_id} 没有正数量商品")

        remarks = "\n".join(
            line
            for line in (
                f"吉客云采购单：{values.get('order_num') or external_id}",
                f"状态：{values.get('review_status')}",
                (
                    f"供应商外部单号：{values.get('external_order_num')}"
                    if values.get("external_order_num")
                    else None
                ),
                f"入库状态：{values.get('in_status')}" if values.get("in_status") else None,
                (
                    f"结算状态：{values.get('settlement_status')}"
                    if values.get("settlement_status")
                    else None
                ),
                f"明细税率：{', '.join(str(rate) for rate in sorted(tax_rates))}"
                if tax_rates
                else None,
                f"备注：{values.get('memo')}" if values.get("memo") else None,
            )
            if line
        )
        header = {
            "supplier": supplier,
            "company": company,
            "transaction_date": transaction_date,
            "schedule_date": max(row["schedule_date"] for row in rows),
            "currency": currency,
            "conversion_rate": 1,
            "supplier_order_info": values.get("external_order_num"),
            "remarks": remarks,
        }
        if warehouse:
            header["set_warehouse"] = warehouse

        mapping_name = self.get_mapping_name("Purchase Order", external_id)
        erpnext_name = (
            frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            if mapping_name
            else None
        )
        created = not (erpnext_name and frappe.db.exists("Purchase Order", erpnext_name))
        if created:
            doc = frappe.get_doc({"doctype": "Purchase Order", **header})
            doc.set("items", rows)
            doc.insert(
                ignore_permissions=True,
                set_name=_jackyun_name(values.get("order_num")),
            )
        else:
            doc = frappe.get_doc("Purchase Order", erpnext_name)
            if doc.docstatus == 0:
                self._dedup_payment_schedule(erpnext_name)
                doc.reload()
                doc.update(header)
                doc.set("items", rows)
                doc.save(ignore_permissions=True)
        # 去重付款计划：吉客云采购单无付款条件，ERPNext 会生成重复 due_date 导致提交失败
        self._dedup_payment_schedule(doc.name)

        self.remember_mapping("Purchase Order", external_id, "Purchase Order", doc.name)
        if values.get("order_num"):
            self.remember_mapping(
                "Purchase Order",
                f"order:{values['order_num']}",
                "Purchase Order",
                doc.name,
            )
        self._maybe_submit_purchase_order(doc, values)
        return doc.name, "created" if created else "updated"

    def _dedup_payment_schedule(self, po_name):
        """采购单无付款条件时 ERPNext 会生成重复 due_date 的付款计划，去重保留一条。"""
        frappe.db.sql(
            """
            delete ps1 from `tabPayment Schedule` ps1
            join `tabPayment Schedule` ps2
              on ps2.parent = ps1.parent and ps2.name < ps1.name
            where ps1.parent = %s
            """,
            (po_name,),
        )
        frappe.db.commit()

    def _resolve_purchase_receipt_source_order(self, values):
        source_order_no = frappe.utils.cstr(values.get("source_order_no")).strip()
        if not source_order_no:
            return None
        for external_id in (f"order:{source_order_no}", source_order_no):
            purchase_order = self._mapped_erpnext_name("Purchase Order", external_id)
            if purchase_order and self._purchase_order_is_linkable_to_receipt(
                purchase_order
            ):
                return purchase_order
        return None

    def _purchase_order_is_linkable_to_receipt(self, purchase_order):
        values = frappe.db.get_value(
            "Purchase Order",
            purchase_order,
            ["docstatus", "transaction_date"],
            as_dict=True,
        )
        if not values or frappe.utils.cint(values.docstatus) != 1:
            return False
        transaction_date = values.transaction_date
        return bool(
            transaction_date
            and frappe.utils.getdate(transaction_date)
            >= frappe.utils.getdate(PURCHASE_RECEIPT_LINK_CUTOFF)
        )

    def _upsert_purchase_receipt(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)
        if frappe.utils.cint(values.get("inbound_type")) != 101:
            frappe.throw(f"入库单 {values.get('receipt_no') or external_id} 不是采购入库")
        if frappe.utils.cint(values.get("red_status")) != 1:
            frappe.throw(f"入库单 {values.get('receipt_no') or external_id} 不是有效蓝单")

        company_name = frappe.utils.cstr(values.get("company_name")).strip()
        if not company_name or not frappe.db.exists("Company", company_name):
            frappe.throw(
                f"入库单 {values.get('receipt_no') or external_id} 的公司 "
                f"{company_name or '未知'} 尚未同步"
            )
        company = company_name
        warehouse = self._resolve_sales_order_warehouse(
            {"warehouse_code": values.get("warehouse_code")}
        )
        if warehouse and (
            frappe.db.get_value("Warehouse", warehouse, "company") != company
            or frappe.db.get_value("Warehouse", warehouse, "disabled")
        ):
            warehouse = None
        warehouse = warehouse or self._fallback_sales_order_warehouse(company)
        if not warehouse:
            frappe.throw(f"入库单 {values.get('receipt_no') or external_id} 缺少有效仓库")
        source_purchase_order = self._resolve_purchase_receipt_source_order(values)
        source_po_doc = (
            frappe.get_doc("Purchase Order", source_purchase_order)
            if source_purchase_order
            else None
        )
        supplier = (
            source_po_doc.supplier
            if source_po_doc
            else self._resolve_purchase_order_supplier(values)
        )
        source_rows = {}
        if source_po_doc:
            for source_row in source_po_doc.items:
                source_rows.setdefault(source_row.item_code, []).append(source_row)

        receipt_time = values.get("receipt_time") or frappe.utils.now_datetime()
        posting_date = frappe.utils.getdate(receipt_time)
        posting_time = frappe.utils.get_time(receipt_time)
        company_currency = frappe.get_cached_value("Company", company, "default_currency")
        currency = frappe.utils.cstr(values.get("currency")).strip()
        if currency in {"RMB", "人民币"}:
            currency = "CNY"
        if not currency or not frappe.db.exists("Currency", currency):
            currency = company_currency
        conversion_rate = frappe.utils.flt(values.get("conversion_rate")) or 1
        if currency == company_currency:
            conversion_rate = 1

        rows = []
        for item_row in values.get("items") or []:
            qty = frappe.utils.flt(item_row.get("quantity"))
            if qty <= 0:
                continue
            item_code = self._resolve_order_item(item_row)
            amount = frappe.utils.flt(
                _pick(
                    item_row,
                    ["baceCurrencyWithTaxAmount", "baceCurrencyCostAmount", "estCost", "cuValue"],
                )
            )
            rate = amount / qty if amount else frappe.utils.flt(
                _pick(
                    item_row,
                    ["baceCurrencyWithTaxPrice", "baceCurrencyCostPrice", "estPrice", "cuPrice"],
                )
            )
            serial_numbers = []
            if item_row.get("serialNo"):
                serial_numbers.append(frappe.utils.cstr(item_row.get("serialNo")).strip())
            for serial_record in item_row.get("serialRecords") or []:
                serial_no = frappe.utils.cstr(serial_record.get("serialNo")).strip()
                if serial_no and serial_no not in serial_numbers:
                    serial_numbers.append(serial_no)
            serial_summary = None
            if serial_numbers:
                preview = "、".join(serial_numbers[:20])
                suffix = (
                    f"……（共{len(serial_numbers)}个，完整数据见吉客云原始记录）"
                    if len(serial_numbers) > 20
                    else f"（共{len(serial_numbers)}个）"
                )
                serial_summary = f"唯一码：{preview}{suffix}"
            trace = "；".join(
                value
                for value in (
                    f"批次：{item_row.get('batchNo')}" if item_row.get("batchNo") else None,
                    (
                        f"生产日期：{item_row.get('productionDate')}"
                        if item_row.get("productionDate")
                        else None
                    ),
                    (
                        f"到期日期：{item_row.get('expirationDate')}"
                        if item_row.get("expirationDate")
                        else None
                    ),
                    serial_summary,
                )
                if value
            )
            description = (
                item_row.get("goodsDetailRemark")
                or item_row.get("rowRemark")
                or item_row.get("goodsName")
                or ""
            )
            if trace:
                description = f"{description}\n{trace}".strip()
            row = {
                    "item_code": item_code,
                    "qty": qty,
                    "rate": rate,
                    "warehouse": warehouse,
                    "batch_no": self._resolve_transaction_batch(
                        item_code, item_row.get("batchNo")
                    ),
                    "description": description,
                }
            if source_rows.get(item_code):
                source_row = source_rows[item_code][0]
                row["purchase_order"] = source_purchase_order
                row["purchase_order_item"] = source_row.name
                if not rate:
                    row["rate"] = source_row.rate
            rows.append(row)
        if not rows:
            frappe.throw(f"入库单 {values.get('receipt_no') or external_id} 没有正数量商品")

        remarks = "\n".join(
            line
            for line in (
                f"吉客云入库单：{values.get('receipt_no') or external_id}",
                f"来源采购单：{values.get('source_order_no')}"
                if values.get("source_order_no")
                else None,
                f"入库类型：{values.get('inbound_type_name') or values.get('inbound_type')}",
                f"备注：{values.get('comment')}" if values.get("comment") else None,
                f"收货备注：{values.get('memo')}" if values.get("memo") else None,
            )
            if line
        )
        header = {
            "supplier": supplier,
            "company": company,
            "posting_date": posting_date,
            "posting_time": posting_time,
            "set_posting_time": 1,
            "set_warehouse": warehouse,
            "currency": currency,
            "conversion_rate": conversion_rate,
            "supplier_delivery_note": values.get("receipt_no"),
            "remarks": remarks,
        }

        mapping_name = self.get_mapping_name("Purchase Receipt", external_id)
        erpnext_name = (
            frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            if mapping_name
            else None
        )
        created = not (erpnext_name and frappe.db.exists("Purchase Receipt", erpnext_name))
        if created:
            doc = frappe.get_doc({"doctype": "Purchase Receipt", **header})
            doc.set("items", rows)
            doc.insert(
                ignore_permissions=True,
                set_name=_jackyun_name(values.get("receipt_no")),
            )
        else:
            doc = frappe.get_doc("Purchase Receipt", erpnext_name)
            if doc.docstatus == 0:
                doc.update(header)
                doc.set("items", rows)
                doc.save(ignore_permissions=True)

        self.remember_mapping(
            "Purchase Receipt", external_id, "Purchase Receipt", doc.name
        )
        self._maybe_submit_purchase_receipt(doc, values)
        return doc.name, "created" if created else "updated"

    def _resolve_delivery_source_order(self, values):
        source_order_no = frappe.utils.cstr(values.get("source_order_no")).strip()
        sales_order = self._mapped_erpnext_name(
            "Sales Order", f"trade:{source_order_no}" if source_order_no else None
        )
        return sales_order if sales_order and frappe.db.exists("Sales Order", sales_order) else None

    def _resolve_delivery_customer(self, values, source_sales_order=None):
        if source_sales_order:
            customer = frappe.db.get_value("Sales Order", source_sales_order, "customer")
            if customer and frappe.db.exists("Customer", customer):
                return customer
        customer_code = frappe.utils.cstr(values.get("customer_code")).strip()
        if customer_code:
            customer = self._mapped_erpnext_name("Customer", f"code:{customer_code}")
            if customer and frappe.db.exists("Customer", customer):
                return customer
        channel_code = frappe.utils.cstr(values.get("channel_code")).strip()
        return self._resolve_sales_order_customer(
            {
                "channel_code": channel_code,
                "shop_id": f"channel:{channel_code or 'unknown'}",
                "shop_name": f"渠道 {channel_code}" if channel_code else "未识别渠道",
            }
        )

    def _upsert_sales_return(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)
        if frappe.utils.cint(values.get("inbound_type")) != 105:
            frappe.throw(f"入库单 {values.get('receipt_no') or external_id} 不是销售退货")
        if frappe.utils.cint(values.get("red_status")) != 1:
            frappe.throw(f"销售退货 {values.get('receipt_no') or external_id} 不是有效蓝单")

        company = frappe.utils.cstr(values.get("company_name")).strip()
        if not company or not frappe.db.exists("Company", company):
            frappe.throw(f"销售退货 {values.get('receipt_no') or external_id} 的公司尚未同步")
        warehouse = self._resolve_sales_order_warehouse(
            {"warehouse_code": values.get("warehouse_code")}
        )
        if not warehouse or frappe.db.get_value("Warehouse", warehouse, "company") != company:
            warehouse = self._fallback_sales_order_warehouse(company)
        source_order = self._resolve_delivery_source_order(
            {"source_order_no": values.get("source_order_no")}
        )
        customer = self._resolve_delivery_customer(values, source_order)
        source_rates = {}
        if source_order:
            source_rates = {
                row.item_code: row.rate for row in frappe.get_doc("Sales Order", source_order).items
            }
        posting = values.get("receipt_time") or frappe.utils.now_datetime()
        rows = []
        for item_row in values.get("items") or []:
            qty = abs(frappe.utils.flt(item_row.get("quantity")))
            if not qty:
                continue
            item_code = self._resolve_order_item(item_row)
            rows.append(
                {
                    "item_code": item_code,
                    "qty": -qty,
                    "rate": frappe.utils.flt(source_rates.get(item_code)),
                    "warehouse": warehouse,
                    "allow_zero_valuation_rate": 1,
                    "batch_no": self._resolve_transaction_batch(
                        item_code, item_row.get("batchNo")
                    ),
                    "description": item_row.get("goodsDetailRemark") or item_row.get("goodsName"),
                }
            )
        if not rows:
            frappe.throw(f"销售退货 {values.get('receipt_no') or external_id} 没有商品")
        # 已有退货单带着源单号就不重复反查；已提交单据靠 db_set 补写
        mapping_name = self.get_mapping_name("Sales Return", external_id)
        existing_name = (
            frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            if mapping_name
            else None
        )
        source_trade_no = ""
        if existing_name and frappe.db.exists("Delivery Note", existing_name):
            source_trade_no = frappe.utils.cstr(
                frappe.db.get_value("Delivery Note", existing_name, "custom_jackyun_source_trade_no")
            )
        if not source_trade_no:
            source_trade_no = self._resolve_return_source_trade_no(values.get("source_order_no"))
        header = {
            "customer": customer,
            "company": company,
            "is_return": 1,
            "posting_date": frappe.utils.getdate(posting),
            "posting_time": frappe.utils.get_time(posting),
            "set_posting_time": 1,
            "set_warehouse": warehouse,
            "selling_price_list": "Standard Selling",
            "price_list_currency": "CNY",
            "plc_conversion_rate": 1,
            "ignore_pricing_rule": 1,
            "custom_jackyun_sales_channel": self._resolve_transaction_sales_channel(
                values, source_order
            ),
            "custom_jackyun_source_trade_no": source_trade_no,
            "remarks": f"吉客云销售退货：{values.get('receipt_no') or external_id}\n来源销售单：{values.get('source_order_no') or ''}",
        }
        name, status = self._upsert_mapped_document(
            "Sales Return", external_id, "Delivery Note", header, rows,
            name=_jackyun_name(values.get("receipt_no")),
        )
        if source_trade_no and frappe.utils.cstr(
            frappe.db.get_value("Delivery Note", name, "custom_jackyun_source_trade_no")
        ) != source_trade_no:
            frappe.db.set_value(
                "Delivery Note", name,
                "custom_jackyun_source_trade_no", source_trade_no,
                update_modified=False,
            )
        return name, status

    def _resolve_return_source_trade_no(self, source_order_no):
        """退货按售后单号反查原销售单号；失败留空，仪表盘回退退货过账日。同号同轮同步内缓存。"""
        aftersale_no = frappe.utils.cstr(source_order_no).strip()
        if not aftersale_no or not aftersale_no.upper().startswith("SH"):
            return ""
        cache = getattr(self, "_return_trade_no_cache", None)
        if cache is None:
            cache = self._return_trade_no_cache = {}
        if aftersale_no in cache:
            return cache[aftersale_no]
        from channel_erp.integrations.aftersale_link import resolve_source_trade_no
        trade_no = resolve_source_trade_no(aftersale_no, self)
        cache[aftersale_no] = trade_no
        return trade_no

    def _upsert_purchase_return(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)
        if frappe.utils.cint(values.get("outbound_type")) != 205:
            frappe.throw(f"出库单 {values.get('delivery_no') or external_id} 不是采购退货")
        if frappe.utils.cint(values.get("red_status")) != 1:
            frappe.throw(f"采购退货 {values.get('delivery_no') or external_id} 不是有效蓝单")
        company = frappe.utils.cstr(values.get("company_name")).strip()
        if not company or not frappe.db.exists("Company", company):
            frappe.throw(f"采购退货 {values.get('delivery_no') or external_id} 的公司尚未同步")
        warehouse = self._resolve_sales_order_warehouse(
            {"warehouse_code": values.get("warehouse_code")}
        )
        if not warehouse or frappe.db.get_value("Warehouse", warehouse, "company") != company:
            warehouse = self._fallback_sales_order_warehouse(company)
        supplier = self._resolve_purchase_order_supplier(values)
        posting = values.get("outbound_time") or frappe.utils.now_datetime()
        rows = []
        for item_row in values.get("items") or []:
            qty = abs(frappe.utils.flt(item_row.get("quantity")))
            if not qty:
                continue
            item_code = self._resolve_order_item(item_row)
            amount = frappe.utils.flt(
                _pick(item_row, ["baceCurrencyWithTaxAmount", "baceCurrencyCostAmount"])
            )
            rows.append(
                {
                    "item_code": item_code,
                    "qty": -qty,
                    "rate": amount / qty if amount else frappe.utils.flt(
                        _pick(item_row, ["baceCurrencyWithTaxPrice", "baceCurrencyCostPrice"])
                    ),
                    "warehouse": warehouse,
                    "allow_zero_valuation_rate": 1,
                    "batch_no": self._resolve_transaction_batch(
                        item_code, item_row.get("batchNo")
                    ),
                    "description": item_row.get("goodsDetailRemark") or item_row.get("goodsName"),
                }
            )
        if not rows:
            frappe.throw(f"采购退货 {values.get('delivery_no') or external_id} 没有商品")
        header = {
            "supplier": supplier,
            "company": company,
            "is_return": 1,
            "posting_date": frappe.utils.getdate(posting),
            "posting_time": frappe.utils.get_time(posting),
            "set_posting_time": 1,
            "set_warehouse": warehouse,
            "supplier_delivery_note": values.get("delivery_no"),
            "remarks": f"吉客云采购退货：{values.get('delivery_no') or external_id}\n来源采购单：{values.get('source_order_no') or ''}",
        }
        return self._upsert_mapped_document(
            "Purchase Return", external_id, "Purchase Receipt", header, rows,
            name=_jackyun_name(values.get("delivery_no")),
        )

    def _upsert_mapped_document(self, resource, external_id, doctype, header, rows, name=None):
        mapping_name = self.get_mapping_name(resource, external_id)
        erpnext_name = (
            frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            if mapping_name
            else None
        )
        created = not (erpnext_name and frappe.db.exists(doctype, erpnext_name))
        if created:
            doc = frappe.get_doc({"doctype": doctype, **header})
            doc.set("items", rows)
            doc.insert(ignore_permissions=True, set_name=name)
        else:
            doc = frappe.get_doc(doctype, erpnext_name)
            if doc.docstatus == 0:
                doc.update(header)
                doc.set("items", rows)
                doc.save(ignore_permissions=True)
        self.remember_mapping(resource, external_id, doctype, doc.name)
        return doc.name, "created" if created else "updated"

    def _upsert_stock_movement(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)
        direction = values.pop("direction")
        stock_entry_type = values.get("stock_entry_type")

        company = frappe.utils.cstr(values.get("company_name")).strip()
        if not company or not frappe.db.exists("Company", company):
            frappe.throw(f"库存异动 {values.get('goodsdoc_no') or external_id} 的公司尚未同步")
        warehouse = self._resolve_sales_order_warehouse(
            {"warehouse_code": values.get("warehouse_code")}
        )
        if not warehouse or frappe.db.get_value("Warehouse", warehouse, "company") != company:
            warehouse = self._fallback_sales_order_warehouse(company)
        if not warehouse:
            frappe.throw(f"库存异动 {values.get('goodsdoc_no') or external_id} 缺少有效仓库")

        posting = values.get("posting_time") or frappe.utils.now_datetime()
        # JackYun can split one logical quantity into floating-point fragments
        # (for example 199.9992 + 0.0008).  ERPNext rounds the tiny fragment to
        # zero for non-fractional stock UOMs and rejects the whole Stock Entry.
        # Aggregate identical item/batch lines first so the source total is
        # preserved without creating zero-quantity rows.
        aggregated_rows = {}
        for item_row in values.get("items") or []:
            qty = frappe.utils.flt(item_row.get("quantity"))
            if qty <= 0:
                continue
            item_code = self._resolve_order_item(item_row)
            batch_no = self._resolve_transaction_batch(
                item_code, item_row.get("batchNo")
            )
            key = (item_code, batch_no or "")
            row = aggregated_rows.setdefault(key, {
                "item_code": item_code,
                "qty": 0,
                "allow_zero_valuation_rate": 1,
                "batch_no": batch_no,
                "description": item_row.get("goodsDetailRemark") or item_row.get("goodsName"),
            })
            row["qty"] += qty
            if direction == "in":
                row["t_warehouse"] = warehouse
            else:
                row["s_warehouse"] = warehouse
        rows = list(aggregated_rows.values())
        if not rows:
            frappe.throw(f"库存异动 {values.get('goodsdoc_no') or external_id} 没有正数量商品")

        header = {
            "company": company,
            "stock_entry_type": stock_entry_type,
            "posting_date": frappe.utils.getdate(posting),
            "posting_time": frappe.utils.get_time(posting),
            "set_posting_time": 1,
            "remarks": (
                f"吉客云库存异动：{values.get('goodsdoc_no') or external_id}\n"
                f"类型：{values.get('inouttype_name') or values.get('inouttype')}"
            ),
        }
        name, status = self._upsert_mapped_document(
            "Stock Movement", external_id, "Stock Entry", header, rows
        )
        self._maybe_submit_stock_movement(frappe.get_doc("Stock Entry", name), values)
        return name, status

    def _upsert_stock_transfer(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)
        out_warehouse = self._resolve_sales_order_warehouse(
            {"warehouse_code": values.get("out_warehouse_code")}
        )
        in_warehouse = self._resolve_sales_order_warehouse(
            {"warehouse_code": values.get("in_warehouse_code")}
        )
        if not out_warehouse or not in_warehouse:
            frappe.throw(f"调拨单 {values.get('allocate_no') or external_id} 缺少已同步仓库")
        out_company = frappe.db.get_value("Warehouse", out_warehouse, "company")
        in_company = frappe.db.get_value("Warehouse", in_warehouse, "company")
        posting = values.get("posting_time") or frappe.utils.now_datetime()
        common_rows = []
        for item_row in values.get("items") or []:
            qty = frappe.utils.flt(item_row.get("goodsSkuCount"))
            if qty <= 0:
                continue
            item_code = self._resolve_order_item(item_row)
            common_rows.append(
                {
                    "item_code": item_code,
                    "qty": qty,
                    "basic_rate": frappe.utils.flt(item_row.get("skuPrice")),
                    "allow_zero_valuation_rate": 1,
                    "batch_no": self._resolve_transaction_batch(
                        item_code, item_row.get("batchNo")
                    ),
                    "description": item_row.get("rowRemark") or item_row.get("goodsName"),
                }
            )
        if not common_rows:
            frappe.throw(f"调拨单 {values.get('allocate_no') or external_id} 没有正数量商品")
        allocate_no = _jackyun_name(values.get("allocate_no"))
        remarks = f"吉客云调拨单：{values.get('allocate_no') or external_id}\n原因：{values.get('reason') or ''}\n备注：{values.get('memo') or ''}"
        base_header = {
            "posting_date": frappe.utils.getdate(posting),
            "posting_time": frappe.utils.get_time(posting),
            "set_posting_time": 1,
            "remarks": remarks,
        }
        if out_company == in_company:
            rows = [{**row, "s_warehouse": out_warehouse, "t_warehouse": in_warehouse} for row in common_rows]
            name, status = self._upsert_mapped_document(
                "Stock Transfer",
                external_id,
                "Stock Entry",
                {**base_header, "company": out_company, "stock_entry_type": "Material Transfer"},
                rows,
                name=allocate_no,
            )
            self._maybe_submit_stock_transfer(frappe.get_doc("Stock Entry", name), values)
            return name, status

        out_rows = [{**row, "s_warehouse": out_warehouse} for row in common_rows]
        in_rows = [{**row, "t_warehouse": in_warehouse} for row in common_rows]
        out_name, status = self._upsert_mapped_document(
            "Stock Transfer",
            f"{external_id}:out",
            "Stock Entry",
            {**base_header, "company": out_company, "stock_entry_type": "Material Issue"},
            out_rows,
            name=f"{allocate_no}-out" if allocate_no else None,
        )
        in_name, _ = self._upsert_mapped_document(
            "Stock Transfer",
            f"{external_id}:in",
            "Stock Entry",
            {**base_header, "company": in_company, "stock_entry_type": "Material Receipt"},
            in_rows,
            name=f"{allocate_no}-in" if allocate_no else None,
        )
        self._maybe_submit_stock_transfer(frappe.get_doc("Stock Entry", out_name), values)
        self._maybe_submit_stock_transfer(frappe.get_doc("Stock Entry", in_name), values)
        self.remember_mapping("Stock Transfer", external_id, "Stock Entry", out_name)
        return out_name, status

    def _upsert_stocktake(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)
        if frappe.utils.cint(values.get("status")) != 5:
            return f"未完成盘点 {values.get('stocktake_no') or external_id}", "skipped"
        warehouse = self._resolve_sales_order_warehouse(
            {"warehouse_code": values.get("warehouse_code")}
        )
        if not warehouse:
            frappe.throw(f"盘点单 {values.get('stocktake_no') or external_id} 的仓库尚未同步")
        company = frappe.db.get_value("Warehouse", warehouse, "company")
        posting = values.get("stocktake_time") or frappe.utils.now_datetime()
        rows_by_item = {}
        for item_row in values.get("items") or []:
            item_code = self._resolve_order_item(item_row)
            batch_no = self._resolve_transaction_batch(
                item_code, item_row.get("batchNo")
            )
            key = (item_code, warehouse, batch_no or "")
            row = rows_by_item.setdefault(
                key,
                {
                    "item_code": item_code,
                    "warehouse": warehouse,
                    "qty": 0,
                    "valuation_rate": 0,
                    "batch_no": batch_no,
                    **({"use_serial_batch_fields": 1} if batch_no else {}),
                },
            )
            row["qty"] += frappe.utils.flt(item_row.get("takeQuan"))
            if not row["valuation_rate"]:
                row["valuation_rate"] = frappe.utils.flt(item_row.get("price"))
        rows = list(rows_by_item.values())
        if not rows:
            frappe.throw(f"盘点单 {values.get('stocktake_no') or external_id} 没有商品")
        return self._upsert_mapped_document(
            "Stocktake",
            external_id,
            "Stock Reconciliation",
            {
                "company": company,
                "purpose": "Stock Reconciliation",
                "posting_date": frappe.utils.getdate(posting),
                "posting_time": frappe.utils.get_time(posting),
                "set_posting_time": 1,
                "set_warehouse": warehouse,
            },
            rows,
            name=_jackyun_name(values.get("stocktake_no")),
        )

    def _upsert_batch(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)
        item_code = self._resolve_order_item(
            {
                "goodsNo": values.get("goods_no"),
                "skuId": values.get("sku_id"),
                "skuBarcode": values.get("sku_barcode"),
            }
        )
        batch_no = frappe.utils.cstr(values.get("batch_no")).strip()
        if not batch_no:
            frappe.throw(f"批次 {external_id} 缺少批次号")
        if not frappe.db.get_value("Item", item_code, "has_batch_no"):
            if frappe.db.exists("Stock Ledger Entry", {"item_code": item_code}):
                frappe.throw(
                    f"商品 {item_code} 已有库存流水，不能自动启用批次管理；批次 {batch_no} 仅保留原始记录"
                )
            frappe.db.set_value("Item", item_code, "has_batch_no", 1, update_modified=False)
        existing_batch = frappe.db.get_value(
            "Batch", {"batch_id": batch_no}, ["name", "item"], as_dict=True
        )
        erp_batch_id = batch_no
        if existing_batch and existing_batch.item != item_code:
            erp_batch_id = f"{batch_no}-{item_code}"[:140]

        mapping_name = self.get_mapping_name("Batch", external_id)
        erpnext_name = (
            frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            if mapping_name
            else None
        )
        if not erpnext_name:
            erpnext_name = frappe.db.get_value(
                "Batch", {"batch_id": erp_batch_id, "item": item_code}
            )
        created = not (erpnext_name and frappe.db.exists("Batch", erpnext_name))
        fields = {
            "batch_id": erp_batch_id,
            "item": item_code,
            "manufacturing_date": frappe.utils.getdate(values.get("manufacturing_date")) if values.get("manufacturing_date") else None,
            "expiry_date": frappe.utils.getdate(values.get("expiry_date")) if values.get("expiry_date") else None,
            "disabled": values.get("disabled"),
            "description": "\n".join(
                value
                for value in (
                    f"吉客云原始批次号：{batch_no}" if erp_batch_id != batch_no else None,
                    values.get("description"),
                )
                if value
            ),
        }
        if created:
            doc = frappe.get_doc({"doctype": "Batch", **fields})
            doc.insert(ignore_permissions=True)
        else:
            doc = frappe.get_doc("Batch", erpnext_name)
            doc.update(fields)
            doc.save(ignore_permissions=True)
        self.remember_mapping("Batch", external_id, "Batch", doc.name)
        return doc.name, "created" if created else "updated"

    def _upsert_delivery_note(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)
        if frappe.utils.cint(values.get("outbound_type")) != 201:
            frappe.throw(f"出库单 {values.get('delivery_no') or external_id} 不是销售出库")
        if frappe.utils.cint(values.get("red_status")) != 1:
            frappe.throw(f"出库单 {values.get('delivery_no') or external_id} 不是有效蓝单")

        company_name = frappe.utils.cstr(values.get("company_name")).strip()
        if not company_name or not frappe.db.exists("Company", company_name):
            frappe.throw(
                f"出库单 {values.get('delivery_no') or external_id} 的公司 "
                f"{company_name or '未知'} 尚未同步"
            )
        company = company_name
        warehouse = self._resolve_sales_order_warehouse(
            {"warehouse_code": values.get("warehouse_code")}
        )
        if warehouse and (
            frappe.db.get_value("Warehouse", warehouse, "company") != company
            or frappe.db.get_value("Warehouse", warehouse, "disabled")
        ):
            warehouse = None
        warehouse = warehouse or self._fallback_sales_order_warehouse(company)
        if not warehouse:
            frappe.throw(f"出库单 {values.get('delivery_no') or external_id} 缺少有效仓库")

        source_sales_order = self._resolve_delivery_source_order(values)
        # 回写闭环：ERPNext 生成并已推送吉客云的销售订单，其吉客云出库单回拉时
        # 不再重复建单扣库存（ERP 出库单在回写时已提交），记映射直接返回已有出库单
        if source_sales_order and (
            frappe.db.get_value("Sales Order", source_sales_order, "custom_connector_source") == "ERPNext"
        ):
            existing_dn = frappe.get_all(
                "Delivery Note Item",
                filters={"against_sales_order": source_sales_order, "docstatus": 1},
                pluck="parent",
                limit=1,
            )
            if existing_dn:
                self.remember_mapping("Delivery Note", external_id, "Delivery Note", existing_dn[0])
                return existing_dn[0], "skipped"
        customer = self._resolve_delivery_customer(values, source_sales_order)
        source_rates = {}
        source_rows = {}
        if source_sales_order:
            source_doc = frappe.get_doc("Sales Order", source_sales_order)
            source_rates = {row.item_code: row.rate for row in source_doc.items}
            for source_row in source_doc.items:
                source_rows.setdefault(source_row.item_code, []).append(source_row)

        outbound_time = values.get("outbound_time") or frappe.utils.now_datetime()
        posting_date = frappe.utils.getdate(outbound_time)
        posting_time = frappe.utils.get_time(outbound_time)
        company_currency = frappe.get_cached_value("Company", company, "default_currency")
        currency = frappe.utils.cstr(values.get("currency")).strip()
        if currency in {"RMB", "人民币"}:
            currency = "CNY"
        if not currency or not frappe.db.exists("Currency", currency):
            currency = company_currency
        conversion_rate = frappe.utils.flt(values.get("conversion_rate")) or 1
        if currency == company_currency:
            conversion_rate = 1

        rows = []
        for item_row in values.get("items") or []:
            qty = frappe.utils.flt(item_row.get("quantity"))
            if qty <= 0:
                continue
            item_code = self._resolve_order_item(item_row)
            serial_numbers = []
            if item_row.get("serialNo"):
                serial_numbers.append(frappe.utils.cstr(item_row.get("serialNo")).strip())
            for serial_record in item_row.get("serialRecords") or []:
                serial_no = frappe.utils.cstr(serial_record.get("serialNo")).strip()
                if serial_no and serial_no not in serial_numbers:
                    serial_numbers.append(serial_no)
            trace_parts = []
            if item_row.get("batchNo"):
                trace_parts.append(f"批次：{item_row.get('batchNo')}")
            if serial_numbers:
                preview = "、".join(serial_numbers[:20])
                suffix = (
                    f"……（共{len(serial_numbers)}个，完整数据见吉客云原始记录）"
                    if len(serial_numbers) > 20
                    else f"（共{len(serial_numbers)}个）"
                )
                trace_parts.append(f"唯一码：{preview}{suffix}")
            description = (
                item_row.get("goodsDetailRemark") or item_row.get("goodsName") or ""
            )
            if trace_parts:
                description = f"{description}\n{'；'.join(trace_parts)}".strip()
            row = {
                    "item_code": item_code,
                    "qty": qty,
                    "rate": frappe.utils.flt(source_rates.get(item_code)),
                    "warehouse": warehouse,
                    "batch_no": self._resolve_transaction_batch(
                        item_code, item_row.get("batchNo")
                    ),
                    "description": description,
                }
            if source_rows.get(item_code):
                source_row = source_rows[item_code][0]
                row["against_sales_order"] = source_sales_order
                row["so_detail"] = source_row.name
            rows.append(row)
        if not rows:
            frappe.throw(f"出库单 {values.get('delivery_no') or external_id} 没有正数量商品")

        remarks = "\n".join(
            line
            for line in (
                f"吉客云销售出库单：{values.get('delivery_no') or external_id}",
                f"来源销售单：{values.get('source_order_no')}"
                if values.get("source_order_no")
                else None,
                f"平台订单号：{values.get('external_order_no')}"
                if values.get("external_order_no")
                else None,
                f"物流：{values.get('logistic_name') or ''} {values.get('logistic_no') or ''}".strip()
                if values.get("logistic_name") or values.get("logistic_no")
                else None,
                f"备注：{values.get('comment')}" if values.get("comment") else None,
                f"发货备注：{values.get('memo')}" if values.get("memo") else None,
            )
            if line
        )
        header = {
            "customer": customer,
            "company": company,
            "posting_date": posting_date,
            "posting_time": posting_time,
            "set_posting_time": 1,
            "set_warehouse": warehouse,
            "currency": currency,
            "conversion_rate": conversion_rate,
            "selling_price_list": "Standard Selling",
            "price_list_currency": "CNY",
            "plc_conversion_rate": 1,
            "ignore_pricing_rule": 1,
            "lr_no": values.get("logistic_no"),
            "remarks": remarks,
            "custom_jackyun_sales_channel": self._resolve_transaction_sales_channel(
                values, source_sales_order
            ),
            "custom_jackyun_source_order": frappe.utils.cstr(values.get("source_order_no")).strip() or None,
        }

        mapping_name = self.get_mapping_name("Delivery Note", external_id)
        erpnext_name = (
            frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            if mapping_name
            else None
        )
        created = not (erpnext_name and frappe.db.exists("Delivery Note", erpnext_name))
        if created:
            doc = frappe.get_doc({"doctype": "Delivery Note", **header})
            doc.set("items", rows)
            doc.insert(
                ignore_permissions=True,
                set_name=_jackyun_name(values.get("delivery_no")),
            )
        else:
            doc = frappe.get_doc("Delivery Note", erpnext_name)
            if doc.docstatus == 0:
                doc.update(header)
                doc.set("items", rows)
                doc.save(ignore_permissions=True)

        self.remember_mapping("Delivery Note", external_id, "Delivery Note", doc.name)
        self._maybe_submit_delivery_note(doc, values)
        return doc.name, "created" if created else "updated"

    def _upsert_sales_order(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)

        warehouse = self._resolve_sales_order_warehouse(values)
        company = self._resolve_sales_order_company(values, warehouse)
        if warehouse and frappe.db.get_value("Warehouse", warehouse, "company") != company:
            warehouse = None
        if warehouse and frappe.db.get_value("Warehouse", warehouse, "disabled"):
            warehouse = None
        if not warehouse:
            warehouse = self._fallback_sales_order_warehouse(company)
        customer = self._resolve_sales_order_customer(values)

        transaction_date = frappe.utils.getdate(
            values.get("trade_time") or frappe.utils.nowdate()
        )
        currency = frappe.utils.cstr(values.get("currency")).strip()
        if currency in {"RMB", "人民币"}:
            currency = "CNY"
        company_currency = frappe.get_cached_value("Company", company, "default_currency")
        if not currency or not frappe.db.exists("Currency", currency):
            currency = company_currency
        conversion_rate = frappe.utils.flt(values.get("conversion_rate")) or 1
        if currency == company_currency:
            conversion_rate = 1

        rows = []
        for item_row in values.get("items") or []:
            qty = frappe.utils.flt(_pick(item_row, ["sellCount", "baseUnitSellCount"]))
            if qty <= 0:
                continue
            item_code = self._resolve_order_item(item_row)
            divided_total = frappe.utils.flt(_pick(item_row, ["divideSellTotal", "sellTotal"]))
            rate = divided_total / qty if divided_total else frappe.utils.flt(item_row.get("sellPrice"))
            row = {
                "item_code": item_code,
                "qty": qty,
                "rate": rate,
                "delivery_date": transaction_date,
                "custom_ecommerce_listing_id": frappe.utils.cstr(
                    item_row.get("platGoodsId")
                ).strip(),
                "custom_ecommerce_platform_sku_id": frappe.utils.cstr(
                    item_row.get("platSkuId")
                ).strip(),
                "custom_ecommerce_platform_code": frappe.utils.cstr(
                    item_row.get("platCode")
                ).strip(),
                "custom_ecommerce_platform_item_name": frappe.utils.cstr(
                    item_row.get("goodsName")
                ).strip(),
                "custom_ecommerce_platform_sku": frappe.utils.cstr(
                    item_row.get("sourceSubtradeNo") or item_row.get("outerSkuId")
                ).strip(),
                "custom_jackyun_outer_id": frappe.utils.cstr(
                    item_row.get("outerId")
                ).strip(),
                "custom_jackyun_outer_sku_id": frappe.utils.cstr(
                    item_row.get("outerSkuId")
                ).strip(),
                "custom_jackyun_source_trade_no": frappe.utils.cstr(
                    item_row.get("sourceTradeNo") or values.get("source_trade_no")
                ).strip(),
                "custom_jackyun_source_subtrade_no": frappe.utils.cstr(
                    item_row.get("sourceSubtradeNo")
                ).strip(),
                "custom_jackyun_refund_status": frappe.utils.cint(
                    item_row.get("refundStatus")
                ),
                "custom_jackyun_line_sell_total": frappe.utils.flt(
                    item_row.get("sellTotal")
                ),
            }
            if warehouse:
                row["warehouse"] = warehouse
            rows.append(row)
        if not rows:
            # 淘系(天猫)订单受奇门限制无 goodsDetail，用占位商品承载表头金额，
            # 让店铺利润可算。链接维度利润待奇门资质补全。
            total_fee = frappe.utils.flt(values.get("total_fee"))
            if total_fee > 0:
                rows = [{
                    "item_code": "TM-PLACEHOLDER",
                    "qty": 1,
                    "rate": total_fee,
                    "delivery_date": transaction_date,
                    "custom_jackyun_source_trade_no": frappe.utils.cstr(values.get("source_trade_no")).strip(),
                }]
                if warehouse:
                    rows[0]["warehouse"] = warehouse
            else:
                frappe.throw(f"销售单 {values.get('trade_no') or external_id} 没有正数量商品")

        remarks = "\n".join(
            line
            for line in (
                f"吉客云销售单：{values.get('trade_no') or external_id}",
                f"状态：{values.get('trade_status')} {values.get('trade_status_explain') or ''}".strip(),
                (
                    f"来源订单号：{values.get('source_trade_no')}"
                    if values.get("source_trade_no")
                    else None
                ),
                f"买家备注：{values.get('buyer_memo')}" if values.get("buyer_memo") else None,
                f"卖家备注：{values.get('seller_memo')}" if values.get("seller_memo") else None,
            )
            if line
        )
        header = {
            "customer": customer,
            "company": company,
            "order_type": "Sales",
            "transaction_date": transaction_date,
            "delivery_date": transaction_date,
            # 吉客云会把一个平台订单拆成多张销售单；ERPNext 默认禁止同客户重复
            # PO 号，因此来源订单号保留在备注中，不写入 po_no。
            "po_no": None,
            "currency": currency,
            "conversion_rate": conversion_rate,
            "selling_price_list": "Standard Selling",
            "price_list_currency": "CNY",
            "plc_conversion_rate": 1,
            "ignore_pricing_rule": 1,
            "remarks": remarks,
            "custom_jackyun_sales_channel": self._resolve_sales_channel(values),
            "custom_jackyun_trade_no": frappe.utils.cstr(values.get("trade_no")).strip(),
            "custom_jackyun_source_trade_no": frappe.utils.cstr(
                values.get("source_trade_no")
            ).strip(),
            "custom_jackyun_order_time": values.get("trade_time"),
            "custom_jackyun_discount_fee": frappe.utils.flt(values.get("discount_fee")),
            "custom_jackyun_received_post_fee": frappe.utils.flt(values.get("received_post_fee")),
        }
        # This explicit provenance marker is the loop-prevention boundary for
        # outbound integrations. setup.py owns the Custom Field migration, so
        # older sites remain compatible until that migration has run.
        if frappe.get_meta("Sales Order").has_field("custom_connector_source"):
            header["custom_connector_source"] = "JackYun"
        if frappe.get_meta("Sales Order").has_field("custom_jackyun_trade_type"):
            header["custom_jackyun_trade_type"] = {
                "1": "零售业务",
                "9": "批发业务",
            }.get(frappe.utils.cstr(values.get("trade_type")).strip())
        if warehouse:
            header["set_warehouse"] = warehouse

        mapping_name = self.get_mapping_name("Sales Order", external_id)
        erpnext_name = (
            frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            if mapping_name
            else None
        )
        trade_no = frappe.utils.cstr(values.get("trade_no")).strip()
        if not erpnext_name and trade_no:
            # ERPNext-origin orders keep their own local name and receive the
            # JackYun trade number after Create succeeds.  When that order is
            # later returned by the pull API, attach it by the unique receipt
            # field instead of attempting to insert a second Sales Order named
            # after the JackYun number.
            erpnext_name = frappe.db.get_value(
                "Sales Order", {"custom_jackyun_trade_no": trade_no}, "name"
            )
        if not erpnext_name and trade_no and frappe.db.exists("Sales Order", trade_no):
            existing_trade_no = frappe.utils.cstr(
                frappe.db.get_value(
                    "Sales Order", trade_no, "custom_jackyun_trade_no"
                )
            ).strip()
            if existing_trade_no == trade_no:
                # A previous per-record failure may have committed the order but
                # not its external-ID map. Reattach only when the persisted
                # JackYun trade number proves this is the same source order.
                erpnext_name = trade_no
        created = not (erpnext_name and frappe.db.exists("Sales Order", erpnext_name))
        if created:
            doc = frappe.get_doc({"doctype": "Sales Order", **header})
            doc.set("items", rows)
            if trade_no and frappe.db.exists("Sales Order", trade_no):
                frappe.throw(f"吉客云销售单号 {trade_no} 已被其他 ERPNext 销售订单占用")
            doc.insert(ignore_permissions=True, set_name=trade_no or None)
            self._dedup_payment_schedule(doc.name)
            doc.reload()
        else:
            doc = frappe.get_doc("Sales Order", erpnext_name)
            if doc.docstatus == 0:
                self._dedup_payment_schedule(erpnext_name)
                doc.reload()
                doc.update(header)
                doc.set("items", rows)
                doc.save(ignore_permissions=True)
            else:
                # 已提交订单在吉客云后来取消的（如发货前全额退款），跟随取消
                if doc.docstatus == 1 and sales_order_status_action(
                    values.get("trade_status")
                ) == "cancel":
                    doc.flags.ignore_permissions = True
                    doc.cancel()
                    doc.reload()
                self._update_submitted_sales_order_listing_metadata(doc, rows)
                frappe.db.set_value(
                    "Sales Order",
                    doc.name,
                    {
                        "custom_jackyun_trade_no": header["custom_jackyun_trade_no"],
                        "custom_jackyun_source_trade_no": header[
                            "custom_jackyun_source_trade_no"
                        ],
                        "custom_jackyun_order_time": header[
                            "custom_jackyun_order_time"
                        ],
                        "custom_jackyun_discount_fee": header[
                            "custom_jackyun_discount_fee"
                        ],
                        "custom_jackyun_received_post_fee": header[
                            "custom_jackyun_received_post_fee"
                        ],
                    },
                    update_modified=False,
                )

        self.remember_mapping("Sales Order", external_id, "Sales Order", doc.name)
        if values.get("trade_no"):
            self.remember_mapping(
                "Sales Order", f"trade:{values['trade_no']}", "Sales Order", doc.name
            )
        self._maybe_submit_sales_order(doc, values.get("trade_status"))
        self._sync_ecommerce_listings(doc)
        return doc.name, "created" if created else "updated"

    def _connection_flag(self, fieldname):
        if not self.connection:
            return False
        if hasattr(self.connection, "get"):
            value = self.connection.get(fieldname)
        else:
            value = getattr(self.connection, fieldname, None)
        return bool(frappe.utils.cint(value))

    def _maybe_submit_sales_order(self, sales_order, trade_status):
        """Submit (or submit+cancel) once, only when the connection opt-in allows it.

        占位商品(TM-PLACEHOLDER)销售订单（淘系奇门限制无明细）保持草稿，
        等出库时补明细后再提交，避免下单即提交后无法改明细。
        """
        if not self._connection_flag("auto_submit_sales_orders"):
            return False
        if sales_order.docstatus != 0:
            return False
        # 占位商品单：取消状态直接提交+取消（留记录）；其他状态保持草稿等出库补明细
        is_placeholder = any(
            row.item_code == "TM-PLACEHOLDER" for row in (sales_order.items or [])
        )
        action = sales_order_status_action(trade_status)
        if is_placeholder:
            if action == "cancel":
                sales_order.submit()
                sales_order.cancel()
                return True
            return False  # 占位+未取消 → 保持草稿，等出库补明细
        if action == "cancel":
            sales_order.submit()
            sales_order.cancel()
            return True
        if action == "submit":
            sales_order.submit()
            return True
        return False

    def _maybe_submit_delivery_note(self, delivery_note, values):
        """Submit an effective sales outbound only after an explicit stock opt-in.

        A linked draft Sales Order is deliberately not submitted from here. It
        must pass its own JackYun status policy first; otherwise the outbound is
        retained as a draft for a later idempotent retry.
        """
        if not self._connection_flag("auto_submit_delivery_notes"):
            return False
        if delivery_note.docstatus != 0 or not delivery_note_can_auto_submit(
            values.get("outbound_type"), values.get("red_status")
        ):
            return False
        source_orders = {
            item.against_sales_order
            for item in delivery_note.items
            if item.against_sales_order
        }
        if any(
            frappe.db.get_value("Sales Order", name, "docstatus") != 1
            for name in source_orders
        ):
            frappe.logger("channel_erp.integrations").warning(
                "Skipping JackYun Delivery Note %s auto-submit because its Sales Order is draft",
                delivery_note.name,
            )
            return False
        unlinked_over_delivery = False
        for item in delivery_note.items:
            if not item.so_detail:
                continue
            ordered, delivered = frappe.db.get_value(
                "Sales Order Item", item.so_detail, ["qty", "delivered_qty"]
            ) or (0, 0)
            if frappe.utils.flt(item.qty) > max(
                0, frappe.utils.flt(ordered) - frappe.utils.flt(delivered)
            ):
                item.against_sales_order = None
                item.so_detail = None
                unlinked_over_delivery = True
        if unlinked_over_delivery:
            delivery_note.save(ignore_permissions=True)
        try:
            delivery_note.submit()
        except Exception as exc:
            # A JackYun outbound can be a later split/reissue against an order
            # whose ERPNext delivered quantity is already full. The outbound is
            # still authoritative stock movement, so retry it as a standalone
            # Delivery Note instead of discarding the source document.
            from erpnext.controllers.status_updater import OverAllowanceError
            if not isinstance(exc, OverAllowanceError):
                raise
            delivery_note.reload()
            if delivery_note.docstatus != 0:
                raise
            for item in delivery_note.items:
                item.against_sales_order = None
                item.so_detail = None
            delivery_note.save(ignore_permissions=True)
            delivery_note.submit()
        return True

    def _maybe_submit_purchase_order(self, purchase_order, values):
        """Submit (or submit+cancel) a purchase order after opt-in.

        审核通过(revwStatus=2) -> submit；作废(10/20) -> submit+cancel。
        """
        if not self._connection_flag("auto_submit_purchase_orders"):
            return False
        if purchase_order.docstatus != 0:
            return False
        revw = frappe.utils.cint(values.get("review_status") or 0)
        if revw in (10, 20):
            purchase_order.submit()
            purchase_order.cancel()
            return True
        if revw == 2:
            purchase_order.submit()
            return True
        return False

    def _maybe_submit_purchase_receipt(self, purchase_receipt, values):
        """Submit an effective purchase inbound only after an explicit stock opt-in.

        Only JackYun purchase receipts (inbound_type 101) with red_status 1 are
        submitted. The submit runs ERPNext's normal stock validation and posts a
        stock ledger entry, so it is deliberately gated behind the connection flag.
        """
        if not self._connection_flag("auto_submit_purchase_receipts"):
            return False
        if purchase_receipt.docstatus != 0 or not (
            frappe.utils.cint(values.get("inbound_type")) == 101
            and frappe.utils.cint(values.get("red_status")) == 1
        ):
            return False
        purchase_receipt.submit()
        return True

    def _maybe_submit_stock_transfer(self, stock_entry, values):
        """Submit a JackYun stock-transfer Stock Entry (Material Transfer / Issue / Receipt) after opt-in."""
        if not self._connection_flag("auto_submit_stock_transfers"):
            return False
        if stock_entry.docstatus != 0:
            return False
        stock_entry.submit()
        return True

    def _maybe_submit_stock_movement(self, stock_entry, values):
        """Submit a JackYun stock-movement Stock Entry (生产领料/完工入库/其他出入库等) after opt-in."""
        if not self._connection_flag("auto_submit_stock_movements"):
            return False
        if stock_entry.docstatus != 0:
            return False
        stock_entry.submit()
        return True

    def _update_submitted_sales_order_listing_metadata(self, sales_order, source_rows):
        """Enrich immutable submitted rows without changing accounting fields."""
        remaining = list(source_rows)
        for item in sales_order.items:
            match_index = next(
                (
                    index
                    for index, source in enumerate(remaining)
                    if source.get("item_code") == item.item_code
                    and abs(frappe.utils.flt(source.get("qty")) - frappe.utils.flt(item.qty)) < 0.000001
                ),
                None,
            )
            if match_index is None:
                continue
            source = remaining.pop(match_index)
            values = {field: source.get(field) or "" for field in SALES_ORDER_LISTING_FIELDS}
            refund_status = frappe.utils.cint(source.get("custom_jackyun_refund_status"))
            if frappe.utils.cint(item.custom_jackyun_refund_status) != refund_status:
                values["custom_jackyun_refund_status"] = refund_status
            sell_total = frappe.utils.flt(source.get("custom_jackyun_line_sell_total"))
            if abs(frappe.utils.flt(item.custom_jackyun_line_sell_total) - sell_total) > 0.005:
                values["custom_jackyun_line_sell_total"] = sell_total
            frappe.db.set_value(
                "Sales Order Item", item.name, values, update_modified=False
            )
            item.update(values)

    def _sync_ecommerce_listings(self, sales_order):
        """Optionally maintain the generic dashboard listing archive.

        Channel ERP remains installable without yimed_ecommerce, so the bridge is
        deliberately conditional.  When the dashboard app is present it owns the
        generic cross-channel listing model and this adapter only supplies source
        identifiers already persisted on Sales Order Item.
        """
        if not frappe.db.exists("DocType", "Ecommerce Product Listing"):
            return
        bridge_path = "yimed_ecommerce.ecommerce_dashboard.product_listing"
        try:
            module = importlib.import_module(bridge_path)
            sync_listings = getattr(module, "sync_sales_order_listings")
            sync_listings(sales_order.name)
        except Exception:
            # This is an optional dashboard projection. A stale DocType left by
            # an uninstalled app, an old worker import path, or a bridge runtime
            # error must not mark the source Sales Order sync as failed.
            frappe.logger("channel_erp.integrations").exception(
                "Skipped optional yimed_ecommerce listing bridge for Sales Order %s",
                sales_order.name,
            )

    def _upsert_sales_channel(self, mapped):
        values = dict(mapped)
        external_id = str(values.pop("external_id"))
        values.pop("doctype", None)
        mapping_name = self.get_mapping_name("Sales Channel", external_id)
        created = not mapping_name
        if mapping_name:
            name = frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            doc = frappe.get_doc("Jackyun Sales Channel", name)
            doc.update(values)
            doc.save(ignore_permissions=True)
        else:
            doc = frappe.get_doc({"doctype": "Jackyun Sales Channel", **values})
            doc.insert(ignore_permissions=True)
        self.remember_mapping("Sales Channel", external_id, "Jackyun Sales Channel", doc.name)
        return doc.name, "created" if created else "updated"

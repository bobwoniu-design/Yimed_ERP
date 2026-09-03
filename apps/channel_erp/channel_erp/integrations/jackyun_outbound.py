"""吉客云销售订单出站服务。

本模块只负责报文、业务回执和动作计划。队列、DocType 事件以及是否正式启用
写入由通用连接器层控制，因此这里的纯函数可被其他 ERP 适配器测试和复用。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Iterable, Mapping


CREATE_METHOD = "oms.trade.ordercreate"
CANCEL_METHOD = "oms.trade.ordercancel"
AUDIT_METHOD = "oms.trade.audit.pass"
QUERY_METHOD = "oms.trade.fullinfoget"
LOG_METHOD = "oms.trade.orderloglist"

DEFAULT_CANCEL_REASON = "420001"
DEFAULT_AUDIT_OPERATOR = "admin"
DEFAULT_AUTO_AUDIT = False
JACKYUN_SOURCE_NAMES = {"jackyun", "jikeyun", "吉客云"}


class OutboundValidationError(ValueError):
    """The ERPNext order is not complete enough to send."""


def _get(value: Any, key: str, default=None):
    if isinstance(value, Mapping):
        return value.get(key, default)
    getter = getattr(value, "get", None)
    if callable(getter):
        result = getter(key)
        return default if result is None else result
    return getattr(value, key, default)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _number(value: Any) -> float:
    try:
        return float(Decimal(str(value or 0)))
    except Exception:
        return 0.0


def _datetime_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return f"{value.isoformat()} 00:00:00"
    text = _text(value)
    if len(text) == 10:
        return f"{text} 00:00:00"
    return text


def _order_datetime_text(order: Any) -> str:
    """Return the complete JackYun tradeTime for an ERPNext Sales Order.

    ERPNext stores ``transaction_date`` and ``transaction_time`` separately.
    Sending the Time field by itself makes JackYun reject the payload because
    ``oms.trade.ordercreate.tradeTime`` requires ``YYYY-MM-DD HH:MM:SS``.
    """
    explicit = _get(order, "custom_order_datetime")
    if explicit:
        return _datetime_text(explicit)

    date_value = _get(order, "transaction_date")
    time_value = _get(order, "transaction_time")
    if date_value:
        if isinstance(date_value, (date, datetime)):
            date_text = date_value.strftime("%Y-%m-%d")
        else:
            date_text = _text(date_value)[:10]

        if isinstance(time_value, time):
            time_text = time_value.strftime("%H:%M:%S")
        else:
            raw_time = _text(time_value)
            try:
                parts = raw_time.split(":")
                time_text = f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(float(parts[2])):02d}"
            except (IndexError, TypeError, ValueError):
                time_text = "00:00:00"
        return f"{date_text} {time_text}"

    return _datetime_text(_get(order, "creation"))


def _require(errors: list[str], value: Any, label: str):
    if not _text(value):
        errors.append(label)


def build_online_trade_no(
    order_name: str,
    revision: int = 0,
    *,
    test_mode: bool = False,
    test_batch: str | None = None,
) -> str:
    """Build the stable JackYun onlineTradeNo (maximum length: 50)."""
    base = _text(order_name)
    if not base:
        raise OutboundValidationError("销售订单编号不能为空")
    if revision < 0:
        raise OutboundValidationError("修订版本不能小于 0")
    if revision:
        base = f"{base}-R{revision}"
    if test_mode:
        batch = _text(test_batch) or datetime.now().strftime("%Y%m%d%H%M")
        base = f"ERPTEST-{batch}-{base}"
    if len(base) > 50:
        raise OutboundValidationError("网店订单号超过吉客云 50 字符限制")
    return base


def _currency_name(code: str) -> str:
    return {
        "CNY": "人民币",
        "RMB": "人民币",
        "USD": "美元",
        "EUR": "欧元",
        "HKD": "港币",
    }.get(_text(code).upper(), _text(code) or "人民币")


def _trade_type_code(value: Any):
    text = _text(value)
    mapping = {
        "零售业务": 1,
        "批发业务": 9,
        "1": 1,
        "9": 9,
    }
    return mapping.get(text)


def _item_payload(row: Any, online_trade_no: str) -> dict:
    goods_no = _text(_get(row, "goods_no") or _get(row, "item_code"))
    barcode = _text(_get(row, "barcode"))
    spec_name = _text(_get(row, "spec_name") or _get(row, "variant_of"))
    unit = _text(_get(row, "stock_uom") or _get(row, "uom"))
    qty = _number(_get(row, "qty"))
    rate = _number(_get(row, "rate"))
    amount = _number(_get(row, "amount")) or qty * rate
    errors = []
    if not barcode and not (goods_no and spec_name):
        # 吉客云要求条码，或“货品编号 + 规格名称”二者之一。
        errors.append(f"商品 {goods_no or _text(_get(row, 'item_name')) or '?'} 缺少条码或规格名称")
    _require(errors, unit, f"商品 {goods_no or '?'} 的单位")
    if qty <= 0:
        errors.append(f"商品 {goods_no or '?'} 的数量必须大于 0")
    if errors:
        raise OutboundValidationError("；".join(errors))
    result = {
        "goodsNo": goods_no,
        "goodsName": _text(_get(row, "item_name")),
        "specName": spec_name,
        "barcode": barcode,
        "unit": unit,
        "sellPrice": rate,
        "sellCount": qty,
        "sellTotal": amount,
        "discountFee": _number(_get(row, "discount_amount")),
        "taxFee": _number(_get(row, "tax_amount")),
        "goodsMemo": _text(_get(row, "description"))[:100],
        "isGift": int(bool(_get(row, "is_free_item"))),
        "sourceTradeNo": online_trade_no,
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def build_sales_order_create_payload(
    order: Any,
    *,
    channel: Any,
    shipping: Any,
    warehouse: Any | None = None,
    revision: int = 0,
    test_mode: bool = False,
    test_batch: str | None = None,
) -> dict:
    """Convert an ERPNext Sales Order-shaped object into ordercreate bizcontent.

    ``shipping`` is explicit by design: the integration layer must resolve the
    selected Address/Contact first instead of accidentally using customer master
    data that is unrelated to this order.
    """
    source = _text(_get(order, "custom_connector_source") or _get(order, "source_system"))
    errors: list[str] = []
    if source.lower() in JACKYUN_SOURCE_NAMES or source in JACKYUN_SOURCE_NAMES:
        errors.append("吉客云来源订单禁止回推")

    order_name = _text(_get(order, "name"))
    online_trade_no = build_online_trade_no(
        order_name, revision, test_mode=test_mode, test_batch=test_batch
    )
    channel_name = _text(
        _get(channel, "platform_shop_name")
        or _get(channel, "channel_name")
        or _get(channel, "name")
    )
    channel_code = _text(_get(channel, "channel_code"))
    trade_type = _trade_type_code(_get(order, "custom_jackyun_trade_type"))
    warehouse_code = _text(
        _get(warehouse, "warehouse_code") or _get(channel, "warehouse_code")
    )
    warehouse_name = _text(
        _get(warehouse, "warehouse_name")
        or _get(channel, "warehouse_name")
        or _get(order, "set_warehouse")
    )
    receiver = _text(_get(shipping, "receiver_name") or _get(shipping, "contact_person"))
    phone = _text(_get(shipping, "phone"))
    mobile = _text(_get(shipping, "mobile") or _get(shipping, "contact_mobile"))
    if phone and not mobile:
        mobile = phone
    if mobile and not phone:
        phone = mobile
    state = _text(_get(shipping, "state"))
    city = _text(_get(shipping, "city"))
    address = _text(
        _get(shipping, "address")
        or " ".join(
            filter(None, (_text(_get(shipping, "address_line1")), _text(_get(shipping, "address_line2"))))
        )
    )
    _require(errors, channel_name, "销售渠道/店铺名称")
    _require(errors, trade_type, "销售业务类型（零售/批发）")
    _require(errors, warehouse_code or warehouse_name, "仓库映射")
    # Contact and shipping data are optional for ERPNext-originated orders.
    # When present they are forwarded; absent values are omitted from the
    # JackYun payload instead of blocking the business document.

    items = []
    for row in _get(order, "items", []) or []:
        try:
            items.append(_item_payload(row, online_trade_no))
        except OutboundValidationError as exc:
            errors.append(str(exc))
    if not items:
        errors.append("销售订单至少需要一个有效商品")
    if errors:
        raise OutboundValidationError("；".join(errors))

    trade_time = _order_datetime_text(order)
    if not trade_time:
        raise OutboundValidationError("订单时间不能为空")
    currency = _text(_get(order, "currency")) or "CNY"
    total_fee = sum(_number(item.get("sellTotal")) for item in items)
    discount_fee = _number(_get(order, "discount_amount"))
    payment = _number(_get(order, "grand_total")) or max(0, total_fee - discount_fee)
    memo = _text(_get(order, "remarks"))
    if test_mode:
        memo = "ERPNext接口测试，请勿处理" + (f"；{memo}" if memo else "")

    trade_order = {
        "tradeTime": trade_time,
        "shopName": channel_name,
        "shopCode": channel_code,
        "warehouseCode": warehouse_code,
        "warehouseName": warehouse_name,
        "tradeType": int(trade_type),
        "totalFee": total_fee,
        "discountFee": discount_fee,
        "payment": payment,
        "receivedTotal": _number(_get(order, "advance_paid")),
        "receivedPostFee": _number(_get(order, "shipping_amount")),
        "chargeCurrency": _currency_name(currency),
        "chargeCurrencyCode": currency,
        "customerName": _text(_get(order, "customer_name") or _get(order, "customer")),
        "receiverName": receiver,
        "phone": phone,
        "mobile": mobile,
        "country": _text(_get(shipping, "country")),
        "state": state,
        "city": city,
        "district": _text(_get(shipping, "county") or _get(shipping, "district")),
        "town": _text(_get(shipping, "town")),
        "zip": _text(_get(shipping, "pincode") or _get(shipping, "zip")),
        "address": address,
        "onlineTradeNo": online_trade_no,
        "sellerMemo": memo[:500],
        "chargeType": int(_get(channel, "settlement_type") or 5),
        "payStatus": int(_get(order, "custom_pay_status") or 0),
        "tradeOrderDetails": items,
    }
    return {"tradeOrder": {k: v for k, v in trade_order.items() if v not in (None, "")}}


def _response_data(response: Any) -> dict:
    if not isinstance(response, Mapping):
        return {}
    result = response.get("result") or {}
    data = result.get("data") if isinstance(result, Mapping) else {}
    return data if isinstance(data, Mapping) else {}


def _base_outcome(response: Any) -> dict:
    response = response if isinstance(response, Mapping) else {}
    result = response.get("result") or {}
    return {
        "code": str(response.get("code") or ""),
        "message": _text(response.get("msg")),
        "sub_code": _text(response.get("subCode")),
        "context_id": _text(
            response.get("contextId")
            or (result.get("contextId") if isinstance(result, Mapping) else None)
        ),
        "response": response,
    }


def parse_create_response(response: Any) -> dict:
    outcome = _base_outcome(response)
    data = _response_data(response)
    trade_order = data.get("tradeOrder") if isinstance(data, Mapping) else {}
    trade_no = _text((trade_order or {}).get("tradeNo"))
    outcome.update(status="Succeeded" if outcome["code"] == "200" and trade_no else "Failed", trade_no=trade_no)
    if outcome["code"] == "200" and not trade_no:
        outcome["message"] = outcome["message"] or "吉客云未返回销售单号 tradeNo"
    return outcome


def parse_cancel_response(response: Any) -> dict:
    outcome = _base_outcome(response)
    data = _response_data(response)
    failed = data.get("failedResults") or []
    success = data.get("succees")
    if success is None:
        success = data.get("success")
    ok = outcome["code"] == "200" and success is True and not failed
    outcome.update(status="Succeeded" if ok else "Failed", failed_results=failed)
    return outcome


def parse_audit_response(response: Any) -> dict:
    outcome = _base_outcome(response)
    data = _response_data(response)
    failed = data.get("failedResults") or []
    ok = outcome["code"] == "200" and data.get("success") is True and not failed
    outcome.update(status="Succeeded" if ok else "Failed", failed_results=failed)
    return outcome


def parse_query_response(response: Any) -> dict:
    outcome = _base_outcome(response)
    result = response.get("result") if isinstance(response, Mapping) else {}
    raw_data = result.get("data") if isinstance(result, Mapping) else None
    if isinstance(raw_data, list):
        records = raw_data
        data = {}
    else:
        data = raw_data if isinstance(raw_data, Mapping) else {}
        records = data.get("tradeOrderList") or data.get("data") or data.get("list") or []
    if isinstance(records, Mapping):
        records = [records]
    # Some tenant responses return one order directly in result.data.
    if not records and any(data.get(k) for k in ("tradeNo", "tradeId", "onlineTradeNo")):
        records = [data]
    outcome.update(status="Succeeded" if outcome["code"] == "200" else "Failed", records=records)
    return outcome


def parse_log_response(response: Any) -> dict:
    outcome = _base_outcome(response)
    data = _response_data(response)
    records = data.get("tradeOrderDbLogList") or []
    if isinstance(records, Mapping):
        records = [records]
    outcome.update(
        status="Succeeded" if outcome["code"] == "200" else "Failed",
        records=records,
        cursor_id=_text(data.get("cursorId")),
        total=int(data.get("total") or 0),
    )
    return outcome


def cancel_payload(trade_no: str, reason: str = DEFAULT_CANCEL_REASON) -> dict:
    if not _text(trade_no):
        raise OutboundValidationError("取消销售单必须提供吉客云 tradeNo")
    return {"cancelReason": _text(reason) or DEFAULT_CANCEL_REASON, "tradeNos": [_text(trade_no)]}


def audit_payload(trade_no: str, operator: str = DEFAULT_AUDIT_OPERATOR) -> dict:
    if not _text(trade_no):
        raise OutboundValidationError("审核销售单必须提供吉客云 tradeNo")
    return {"tradeNos": [_text(trade_no)], "operator": _text(operator) or DEFAULT_AUDIT_OPERATOR}


def revision_rebuild_plan(
    original_trade_no: str,
    original_online_trade_no: str,
    new_create_payload: dict,
    revision: int,
    *,
    cancel_reason: str = DEFAULT_CANCEL_REASON,
) -> dict:
    """Return a gated cancel/recreate plan; this function performs no writes."""
    if revision < 1:
        raise OutboundValidationError("取消重建的修订版本必须大于 0")
    create_payload = deepcopy(new_create_payload)
    order = create_payload.get("tradeOrder") or {}
    order["onlineTradeNo"] = build_online_trade_no(original_online_trade_no, revision)
    create_payload["tradeOrder"] = order
    return {
        "status": "Awaiting Cancellation",
        "cancel": cancel_payload(original_trade_no, cancel_reason),
        "create_after_cancel_succeeds": create_payload,
        "blind_create_allowed": False,
    }


def plan_test_order_cleanup(records: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Plan exact test-order cancellations; never performs network requests."""
    plans = []
    for row in records:
        online_no = _text(row.get("onlineTradeNo") or row.get("online_trade_no"))
        trade_no = _text(row.get("tradeNo") or row.get("trade_no"))
        status = _text(row.get("status") or row.get("tradeStatus")).lower()
        if not online_no.startswith("ERPTEST-") or not trade_no:
            continue
        if status in {"cancelled", "canceled", "已取消", "5010", "5020", "5030"}:
            continue
        plans.append({
            "resource": "Sales Order",
            "operation": "Cancel",
            "external_id": trade_no,
            "online_trade_no": online_no,
            "payload": cancel_payload(trade_no),
        })
    return plans


class JackyunSalesOrderOutboundService:
    """Small orchestration facade around :class:`JackYunAdapter`."""

    def __init__(self, adapter):
        self.adapter = adapter

    def create(self, payload: dict) -> dict:
        online_no = _text((payload.get("tradeOrder") or {}).get("onlineTradeNo"))
        try:
            return parse_create_response(self.adapter.push("Sales Order", "Create", payload))
        except Exception as exc:
            if _text(getattr(exc, "category", "")).upper() == "NETWORK":
                return {
                    "status": "Uncertain",
                    "message": str(exc),
                    "online_trade_no": online_no,
                    "blind_retry_allowed": False,
                    "probe": {"operation": "Query", "payload": {"onlineTradeNo": online_no}},
                }
            raise

    def cancel(self, trade_no: str, reason: str = DEFAULT_CANCEL_REASON) -> dict:
        return parse_cancel_response(
            self.adapter.push("Sales Order", "Cancel", cancel_payload(trade_no, reason))
        )

    def audit(self, trade_no: str, operator: str = DEFAULT_AUDIT_OPERATOR) -> dict:
        return parse_audit_response(
            self.adapter.push("Sales Order", "Audit", audit_payload(trade_no, operator))
        )

    def query_by_online_trade_no(self, online_trade_no: str) -> dict:
        return parse_query_response(
            self.adapter.push(
                "Sales Order",
                "Query",
                {
                    "sourceTradeNos": _text(online_trade_no),
                    "fields": "tradeNo,sourceTradeNo,onlineTradeNo",
                    "isTableSwitch": 1,
                },
            )
        )

    def probe_create(self, online_trade_no: str) -> dict:
        """Resolve an uncertain create without ever issuing another create."""
        online_trade_no = _text(online_trade_no)
        queried = self.query_by_online_trade_no(online_trade_no)
        if queried.get("status") != "Succeeded":
            return {
                "status": "Uncertain",
                "online_trade_no": online_trade_no,
                "blind_retry_allowed": False,
                "query": queried,
            }
        matches = [
            row for row in queried.get("records") or []
            if _text(_get(row, "onlineTradeNo") or _get(row, "sourceTradeNo")) == online_trade_no
        ]
        if matches:
            trade_no = _text(_get(matches[0], "tradeNo"))
            return {
                "status": "Reconciled",
                "online_trade_no": online_trade_no,
                "trade_no": trade_no,
                "record": matches[0],
                "blind_retry_allowed": False,
            }
        # Only a conclusive successful query may authorize the queue to retry.
        return {
            "status": "Not Found",
            "online_trade_no": online_trade_no,
            "retry_create_allowed": True,
            "query": queried,
        }

    def logs(self, *, start_time: Any, end_time: Any, trade_id: str | None = None, cursor_id: str | None = None) -> dict:
        payload = {
            "startTime": _datetime_text(start_time),
            "endTime": _datetime_text(end_time),
            "pageIndex": 0,
            "pageSize": 50,
        }
        if trade_id:
            payload["tradeId"] = trade_id
        if cursor_id:
            payload["cursorId"] = cursor_id
        return parse_log_response(self.adapter.push("Sales Order", "Log", payload))

from unittest import TestCase

from channel_erp.integrations.jackyun import JackYunAdapter, JikeyunError
from channel_erp.integrations.jackyun_outbound import (
    JackyunSalesOrderOutboundService,
    OutboundValidationError,
    audit_payload,
    build_online_trade_no,
    build_sales_order_create_payload,
    parse_audit_response,
    parse_cancel_response,
    parse_create_response,
    parse_log_response,
    plan_test_order_cleanup,
    revision_rebuild_plan,
)


def _order(**overrides):
    value = {
        "name": "SO-0001",
        "custom_connector_source": "ERPNext",
        "transaction_date": "2026-08-30",
        "currency": "CNY",
        "customer": "线下客户",
        "customer_name": "线下客户",
        "custom_jackyun_trade_type": "批发业务",
        "grand_total": 20,
        "items": [
            {
                "item_code": "GOODS-1",
                "item_name": "商品一",
                "spec_name": "默认规格",
                "stock_uom": "个",
                "qty": 2,
                "rate": 10,
                "amount": 20,
            }
        ],
    }
    value.update(overrides)
    return value


CHANNEL = {
    "channel_name": "OTC销售组",
    "channel_code": "OTC",
    "warehouse_code": "WH-01",
    "warehouse_name": "武汉仓",
}
SHIPPING = {
    "receiver_name": "张三",
    "mobile": "13800000000",
    "state": "湖北省",
    "city": "武汉市",
    "district": "洪山区",
    "address_line1": "光谷大道1号",
    "country": "中国",
}


class TestJackyunOutboundPayload(TestCase):
    def test_build_create_payload_and_duplicate_phone(self):
        payload = build_sales_order_create_payload(
            _order(), channel=CHANNEL, shipping=SHIPPING
        )["tradeOrder"]
        self.assertEqual(payload["onlineTradeNo"], "SO-0001")
        self.assertEqual(payload["tradeType"], 9)
        self.assertEqual(payload["phone"], "13800000000")
        self.assertEqual(payload["mobile"], "13800000000")
        self.assertEqual(payload["warehouseCode"], "WH-01")
        self.assertEqual(payload["tradeOrderDetails"][0]["sellCount"], 2)
        self.assertEqual(payload["tradeTime"], "2026-08-30 00:00:00")

    def test_transaction_date_and_time_are_combined_for_jackyun(self):
        payload = build_sales_order_create_payload(
            _order(transaction_time="9:25:02"), channel=CHANNEL, shipping=SHIPPING
        )["tradeOrder"]
        self.assertEqual(payload["tradeTime"], "2026-08-30 09:25:02")

    def test_jackyun_source_never_pushes(self):
        with self.assertRaisesRegex(OutboundValidationError, "禁止回推"):
            build_sales_order_create_payload(
                _order(custom_connector_source="JackYun"),
                channel=CHANNEL,
                shipping=SHIPPING,
            )

    def test_required_shipping_and_item_identity(self):
        bad_order = _order(items=[{"item_code": "A", "qty": 1, "rate": 1, "uom": "个"}])
        with self.assertRaises(OutboundValidationError) as caught:
            build_sales_order_create_payload(bad_order, channel=CHANNEL, shipping={})
        self.assertIn("条码或规格名称", str(caught.exception))

    def test_shipping_and_contact_are_optional(self):
        payload = build_sales_order_create_payload(
            _order(), channel=CHANNEL, shipping={}
        )["tradeOrder"]
        self.assertNotIn("phone", payload)
        self.assertNotIn("address", payload)

    def test_test_number_and_memo(self):
        payload = build_sales_order_create_payload(
            _order(), channel=CHANNEL, shipping=SHIPPING,
            test_mode=True, test_batch="B01",
        )["tradeOrder"]
        self.assertEqual(payload["onlineTradeNo"], "ERPTEST-B01-SO-0001")
        self.assertIn("请勿处理", payload["sellerMemo"])

    def test_revision_number(self):
        self.assertEqual(build_online_trade_no("SO-0001", 2), "SO-0001-R2")


class TestJackyunOutboundAcknowledgements(TestCase):
    def test_create_requires_trade_no(self):
        response = {"code": 200, "result": {"data": {"tradeOrder": {"tradeNo": ""}}}}
        result = parse_create_response(response)
        self.assertEqual(result["status"], "Failed")
        self.assertIn("tradeNo", result["message"])
        response["result"]["data"]["tradeOrder"]["tradeNo"] = "JY-1"
        self.assertEqual(parse_create_response(response)["status"], "Succeeded")

    def test_cancel_accepts_documented_typo_only_without_failures(self):
        ok = {"code": 200, "result": {"data": {"succees": True, "failedResults": []}}}
        self.assertEqual(parse_cancel_response(ok)["status"], "Succeeded")
        bad = {"code": 200, "result": {"data": {"success": True, "failedResults": [{"msg": "失败"}]}}}
        self.assertEqual(parse_cancel_response(bad)["status"], "Failed")

    def test_audit_needs_success_and_no_failed_results(self):
        ok = {"code": 200, "result": {"data": {"success": True, "failedResults": []}}}
        self.assertEqual(parse_audit_response(ok)["status"], "Succeeded")
        self.assertEqual(audit_payload("JY-1")["operator"], "admin")

    def test_log_response_normalizes_single_record(self):
        response = {
            "code": 200,
            "result": {"data": {"total": 1, "cursorId": "C-1", "tradeOrderDbLogList": {"id": 1}}},
        }
        parsed = parse_log_response(response)
        self.assertEqual(parsed["status"], "Succeeded")
        self.assertEqual(parsed["cursor_id"], "C-1")
        self.assertEqual(parsed["records"], [{"id": 1}])


class _FakeAdapter:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def push(self, resource, operation, payload):
        self.calls.append((resource, operation, payload))
        if self.error:
            raise self.error
        return self.response


class TestJackyunOutboundWorkflow(TestCase):
    def test_query_uses_documented_source_trade_number_parameter(self):
        adapter = _FakeAdapter(
            response={"code": 200, "result": {"data": {"tradeOrderList": []}}}
        )
        JackyunSalesOrderOutboundService(adapter).query_by_online_trade_no("ERPTEST-SO-1")
        _, operation, payload = adapter.calls[0]
        self.assertEqual(operation, "Query")
        self.assertEqual(payload["sourceTradeNos"], "ERPTEST-SO-1")
        self.assertIn("sourceTradeNo", payload["fields"])

    def test_adapter_create_uses_approved_method_without_paging_or_retries(self):
        class _Connection:
            def get(self, key):
                return "{}" if key == "push_method_map" else None

        adapter = JackYunAdapter.__new__(JackYunAdapter)
        adapter.connection = _Connection()
        captured = {}

        def request(method, payload, **kwargs):
            captured.update(method=method, payload=payload, kwargs=kwargs)
            return {"code": 200}

        adapter.request = request
        adapter.push("Sales Order", "Create", {"tradeOrder": {}})
        self.assertEqual(captured["method"], "oms.trade.ordercreate")
        self.assertFalse(captured["kwargs"]["include_pagination"])
        self.assertEqual(captured["kwargs"]["max_retries"], 0)

    def test_network_create_is_uncertain_and_probe_only(self):
        adapter = _FakeAdapter(error=JikeyunError("timeout", "NETWORK"))
        result = JackyunSalesOrderOutboundService(adapter).create(
            {"tradeOrder": {"onlineTradeNo": "SO-1"}}
        )
        self.assertEqual(result["status"], "Uncertain")
        self.assertFalse(result["blind_retry_allowed"])
        self.assertEqual(result["probe"]["payload"]["onlineTradeNo"], "SO-1")
        self.assertEqual(len(adapter.calls), 1)

    def test_revision_is_gated_by_cancel_success(self):
        plan = revision_rebuild_plan(
            "JY-OLD", "SO-1", {"tradeOrder": {"onlineTradeNo": "SO-1"}}, 1
        )
        self.assertEqual(plan["cancel"]["cancelReason"], "420001")
        self.assertEqual(
            plan["create_after_cancel_succeeds"]["tradeOrder"]["onlineTradeNo"],
            "SO-1-R1",
        )
        self.assertFalse(plan["blind_create_allowed"])

    def test_probe_reconciles_uncertain_create_by_online_number(self):
        response = {
            "code": 200,
            "result": {"data": [{"onlineTradeNo": "SO-1", "tradeNo": "JY-1"}]},
        }
        result = JackyunSalesOrderOutboundService(_FakeAdapter(response=response)).probe_create("SO-1")
        self.assertEqual(result["status"], "Reconciled")
        self.assertEqual(result["trade_no"], "JY-1")
        self.assertFalse(result["blind_retry_allowed"])

    def test_cleanup_plans_only_exact_test_orders(self):
        plans = plan_test_order_cleanup([
            {"onlineTradeNo": "ERPTEST-B1-SO-1", "tradeNo": "JY-1", "status": "有效"},
            {"onlineTradeNo": "SO-2", "tradeNo": "JY-2", "status": "有效"},
            {"onlineTradeNo": "ERPTEST-B1-SO-3", "tradeNo": "JY-3", "status": "已取消"},
        ])
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["external_id"], "JY-1")

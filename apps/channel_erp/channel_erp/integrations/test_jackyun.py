import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from channel_erp.integrations import jackyun as jackyun_module
from channel_erp.integrations.jackyun import (
    JackYunAdapter,
    PACKAGE_PAGE_SIZE,
    PAGE_SIZE,
    PURCHASE_ORDER_PAGE_SIZE,
    SALES_ORDER_PAGE_SIZE,
    SALES_ORDER_FIELDS,
    STOCK_PAGE_SIZE,
    STOCK_TRANSFER_FIELDS,
    build_public_params,
    delivery_note_can_auto_submit,
    extract_product_packages,
    extract_records_by_identity,
    sales_order_status_action,
)


def package(sku_id, goods_no=None):
    return {
        "skuId": str(sku_id),
        "goodsNo": goods_no or f"BUNDLE-{sku_id}",
        "goodsName": f"组合装 {sku_id}",
        "goodsPackageDetail": [
            {
                "skuId": "9001",
                "goodsNo": "CHILD-1",
                "goodsName": "子件",
                "goodsAmount": 2,
            }
        ],
    }


class FakePackageAdapter(JackYunAdapter):
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, bizcontent, page_index=0, page_size=100):
        self.calls.append((method, bizcontent, page_index, page_size))
        return next(self.responses)


class FakeConnection(dict):
    pass


class TestJackYunProductPackages(unittest.TestCase):
    def test_sales_order_status_action_submits_active_and_cancels_terminal(self):
        """现行设计：取消(取消中/已取消) -> cancel；其余(未发货/已发货/已完成) -> submit。"""
        for status in (6000, "9090", 1010, 4110, 4112):
            self.assertEqual(sales_order_status_action(status), "submit")
        for status in (4121, 4122, 5010, 5020, 5030):
            with self.subTest(status=status):
                self.assertEqual(sales_order_status_action(status), "cancel")

    def test_delivery_note_auto_submit_policy_requires_sales_blue_document(self):
        self.assertTrue(delivery_note_can_auto_submit(201, 1))
        self.assertFalse(delivery_note_can_auto_submit(201, 0))
        self.assertFalse(delivery_note_can_auto_submit(205, 1))

    def test_auto_submit_switches_are_off_by_default(self):
        adapter = JackYunAdapter(FakeConnection())
        order = SimpleNamespace(docstatus=0, submit=MagicMock())
        delivery = SimpleNamespace(docstatus=0, items=[], submit=MagicMock())

        self.assertFalse(adapter._maybe_submit_sales_order(order, 9090))
        self.assertFalse(
            adapter._maybe_submit_delivery_note(
                delivery, {"outbound_type": 201, "red_status": 1}
            )
        )
        order.submit.assert_not_called()
        delivery.submit.assert_not_called()

    def test_sales_order_auto_submit_is_idempotent_and_status_guarded(self):
        adapter = JackYunAdapter(FakeConnection(auto_submit_sales_orders=1))
        cancelled = SimpleNamespace(docstatus=0, items=[], submit=MagicMock(), cancel=MagicMock())
        submitted = SimpleNamespace(docstatus=1, items=[], submit=MagicMock(), cancel=MagicMock())
        active = SimpleNamespace(docstatus=0, items=[], submit=MagicMock(), cancel=MagicMock())

        # 取消状态：提交后取消（留记录）；已提交的单不重复处理；活动状态直接提交。
        self.assertTrue(adapter._maybe_submit_sales_order(cancelled, 5010))
        self.assertFalse(adapter._maybe_submit_sales_order(submitted, 9090))
        self.assertTrue(adapter._maybe_submit_sales_order(active, 6000))
        cancelled.submit.assert_called_once()
        cancelled.cancel.assert_called_once()
        submitted.submit.assert_not_called()
        active.submit.assert_called_once_with()
        active.cancel.assert_not_called()

    def test_delivery_note_auto_submit_requires_explicit_opt_in(self):
        adapter = JackYunAdapter(FakeConnection(auto_submit_delivery_notes=1))
        delivery = SimpleNamespace(
            docstatus=0, name="DN-001", items=[], submit=MagicMock()
        )

        self.assertTrue(
            adapter._maybe_submit_delivery_note(
                delivery, {"outbound_type": 201, "red_status": 1}
            )
        )
        delivery.submit.assert_called_once_with()

    @patch("channel_erp.integrations.jackyun.importlib.import_module")
    def test_missing_optional_ecommerce_module_does_not_fail_sales_order_sync(
        self, import_module
    ):
        logger = MagicMock()
        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(exists=MagicMock(return_value=True)),
            logger=MagicMock(return_value=logger),
        )
        import_module.side_effect = ModuleNotFoundError("No module named 'yimed_ecommerce'")
        adapter = JackYunAdapter(None)

        with patch.object(jackyun_module, "frappe", fake_frappe):
            adapter._sync_ecommerce_listings(SimpleNamespace(name="SO-001"))

        logger.exception.assert_called_once()

    def test_sku_upsert_reattaches_existing_item_when_mapping_is_missing(self):
        adapter = JackYunAdapter(None)
        mapped = {
            "doctype": "Item",
            "external_id": "sku:123",
            "item_code": "ITEM-123",
            "item_name": "Existing item",
        }
        with (
            patch.object(adapter, "get_mapping_name", return_value=None),
            patch.object(adapter, "remember_mapping") as remember_mapping,
            patch.object(jackyun_module.frappe.db, "exists", return_value=True),
        ):
            result = adapter.upsert("SKU", mapped)

        self.assertEqual(result, ("ITEM-123", "updated"))
        remember_mapping.assert_called_once_with(
            "SKU", "sku:123", "Item", "ITEM-123"
        )

    def test_sku_upsert_rebuilds_item_behind_stale_mapping(self):
        adapter = JackYunAdapter(None)
        mapped = {
            "doctype": "Item",
            "external_id": "0200022",
            "item_code": "0200022",
            "item_name": "医麦德产品-宣传册",
            "stock_uom": "个",
        }
        doc = SimpleNamespace(name="0200022", insert=MagicMock())
        with (
            patch.object(adapter, "get_mapping_name", return_value="MAP-0200022"),
            patch.object(adapter, "remember_mapping") as remember_mapping,
            patch.object(jackyun_module.frappe.db, "get_value", return_value="0200022"),
            patch.object(jackyun_module.frappe.db, "exists", return_value=False),
            patch.object(jackyun_module.frappe, "get_doc", return_value=doc),
        ):
            result = adapter.upsert("SKU", mapped)

        self.assertEqual(("0200022", "created"), result)
        doc.insert.assert_called_once_with(ignore_permissions=True)
        remember_mapping.assert_called_once_with("SKU", "0200022", "Item", "0200022")

    def test_inventory_item_recovery_uses_preserved_sku_snapshot(self):
        adapter = JackYunAdapter(None)
        raw = {
            "goodsNo": "0200022",
            "goodsName": "医麦德产品-宣传册",
            "unitName": "个",
        }
        with (
            patch.object(
                jackyun_module.frappe,
                "get_all",
                return_value=[SimpleNamespace(raw_data=jackyun_module.json.dumps(raw))],
            ),
            patch.object(adapter, "upsert", return_value=("0200022", "created")) as upsert,
            patch.object(adapter, "remember_sku_aliases") as remember_aliases,
            patch.object(jackyun_module.frappe.db, "exists", return_value=True),
        ):
            result = adapter._restore_inventory_item_from_sku_snapshot("0200022")

        self.assertEqual("0200022", result)
        upsert.assert_called_once()
        remember_aliases.assert_called_once_with(raw, "0200022")

    def test_purchase_receipt_links_only_submitted_post_cutover_order(self):
        adapter = JackYunAdapter(None)
        cases = (
            (SimpleNamespace(docstatus=2, transaction_date="2026-08-24"), False),
            (SimpleNamespace(docstatus=1, transaction_date="2026-08-22"), False),
            (SimpleNamespace(docstatus=0, transaction_date="2026-08-24"), False),
            (SimpleNamespace(docstatus=1, transaction_date="2026-08-23"), True),
        )
        for source, expected in cases:
            with self.subTest(source=source), patch.object(
                jackyun_module.frappe.db, "get_value", return_value=source
            ):
                self.assertEqual(
                    expected,
                    adapter._purchase_order_is_linkable_to_receipt("PO-001"),
                )

    def test_stock_movement_filters_zero_quantity_details(self):
        adapter = JackYunAdapter(FakeConnection())
        captured = {}

        def upsert_document(resource, external_id, doctype, header, rows, name=None):
            captured["rows"] = rows
            return "STE-001", "created"

        with (
            patch.object(adapter, "_resolve_sales_order_warehouse", return_value="仓库 - T"),
            patch.object(adapter, "_resolve_order_item", return_value="ITEM-1"),
            patch.object(adapter, "_resolve_transaction_batch", return_value=None),
            patch.object(adapter, "_upsert_mapped_document", side_effect=upsert_document),
            patch.object(adapter, "_maybe_submit_stock_movement"),
            patch.object(jackyun_module.frappe.db, "exists", return_value=True),
            patch.object(jackyun_module.frappe.db, "get_value", return_value="测试公司"),
            patch.object(jackyun_module.frappe, "get_doc", return_value=SimpleNamespace()),
        ):
            adapter._upsert_stock_movement(
                {
                    "external_id": "104:CRK001",
                    "doctype": "Stock Entry",
                    "direction": "in",
                    "stock_entry_type": "Material Receipt",
                    "company_name": "测试公司",
                    "warehouse_code": "W1",
                    "goodsdoc_no": "CRK001",
                    "items": [
                        {"goodsNo": "ITEM-1", "quantity": 0},
                        {"goodsNo": "ITEM-1", "quantity": 199.9992},
                        {"goodsNo": "ITEM-1", "quantity": 0.0008},
                    ],
                }
            )

        self.assertEqual(1, len(captured["rows"]))
        self.assertAlmostEqual(200, captured["rows"][0]["qty"])

    def test_stocktake_aggregates_duplicate_item_warehouse_rows(self):
        adapter = JackYunAdapter(None)
        captured = {}

        def upsert_document(resource, external_id, doctype, header, rows, name=None):
            captured["rows"] = rows
            return "REC-001", "created"

        with (
            patch.object(adapter, "_resolve_sales_order_warehouse", return_value="仓库 - T"),
            patch.object(adapter, "_resolve_order_item", return_value="ITEM-1"),
            patch.object(adapter, "_upsert_mapped_document", side_effect=upsert_document),
            patch.object(jackyun_module.frappe.db, "get_value", return_value="测试公司"),
        ):
            adapter._upsert_stocktake(
                {
                    "external_id": "PD001",
                    "doctype": "Stock Reconciliation",
                    "status": 5,
                    "stocktake_no": "PD001",
                    "warehouse_code": "W1",
                    "items": [
                        {"goodsNo": "ITEM-1", "takeQuan": 2, "price": 3},
                        {"goodsNo": "ITEM-1", "takeQuan": 4, "price": 3},
                    ],
                }
            )

        self.assertEqual(1, len(captured["rows"]))
        self.assertEqual(6, captured["rows"][0]["qty"])
        self.assertEqual(3, captured["rows"][0]["valuation_rate"])

    def test_request_timestamp_uses_beijing_time(self):
        params = build_public_params("key", "method", {}, "secret")
        stamp = datetime.strptime(params["timestamp"], "%Y-%m-%d %H:%M:%S")
        beijing_now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        self.assertLess(abs((beijing_now - stamp).total_seconds()), 5)

    def test_sales_order_requests_platform_listing_identifiers(self):
        self.assertIn("goodsDetail.platGoodsId", SALES_ORDER_FIELDS)
        self.assertIn("goodsDetail.platSkuId", SALES_ORDER_FIELDS)
        self.assertIn("goodsDetail.platCode", SALES_ORDER_FIELDS)
        self.assertIn("goodsDetail.sourceSubtradeNo", SALES_ORDER_FIELDS)
        self.assertIn("onlineTradeNo", SALES_ORDER_FIELDS)

    def test_extracts_single_company_object(self):
        company = {"companyId": "u9090", "companyCode": "DF", "companyName": "笛佛"}
        payload = {"code": 200, "result": {"data": company}}

        self.assertEqual(extract_records_by_identity(payload, ["companyId"]), [company])

    def test_extracts_and_transforms_single_department_object(self):
        department = {
            "departId": "123",
            "departCode": "FIN",
            "departName": "财务部",
            "companyCode": "0001",
        }
        payload = {"code": 200, "result": {"data": department}}

        self.assertEqual(extract_records_by_identity(payload, ["departId"]), [department])
        mapped = JackYunAdapter(None).transform("Department", department)
        self.assertEqual(mapped["external_id"], "123")
        self.assertEqual(mapped["department_name"], "财务部")
        self.assertEqual(mapped["company_code"], "0001")

    def test_sales_channel_string_zero_is_not_disabled(self):
        mapped = JackYunAdapter(None).transform(
            "Sales Channel",
            {
                "channelId": "123",
                "channelCode": "001",
                "channelName": "直营网店",
                "isBlockup": "0",
                "isDelete": "1",
            },
        )

        self.assertEqual(mapped["disabled"], 0)
        self.assertEqual(mapped["deleted"], 1)

    def test_deleted_warehouse_is_disabled_even_when_not_blocked(self):
        mapped = JackYunAdapter(None).transform(
            "Warehouse",
            {
                "warehouseId": "1",
                "warehouseName": "测试仓",
                "warehouseCode": "W1",
                "warehouseCompanyCode": "0001",
                "isBlockup": 0,
                "isDelete": 1,
            },
        )

        self.assertEqual(mapped["disabled"], 1)

    def test_inventory_cursor_uses_max_quantity_id(self):
        first_page = [{"quantityId": str(i), "goodsNo": f"G{i}"} for i in range(1, STOCK_PAGE_SIZE + 1)]
        last_page = [{"quantityId": str(STOCK_PAGE_SIZE + 1), "goodsNo": "LAST"}]
        adapter = FakePackageAdapter(
            [
                {"code": 200, "result": {"data": {"goodsStockQuantity": first_page}}},
                {"code": 200, "result": {"data": {"goodsStockQuantity": last_page}}},
            ]
        )

        result = list(adapter.pull("Inventory"))

        self.assertEqual(len(result), STOCK_PAGE_SIZE + 1)
        self.assertEqual(adapter.calls[0][1]["maxQuantityId"], 0)
        self.assertEqual(adapter.calls[1][1]["maxQuantityId"], STOCK_PAGE_SIZE)

    def test_customer_on_demand_uses_scroll_cursor(self):
        first_page = [
            {"customerId": str(i), "customerCode": f"C{i}"} for i in range(1, PAGE_SIZE + 1)
        ]
        last_page = [{"customerId": str(PAGE_SIZE + 1), "customerCode": "LAST"}]
        adapter = FakePackageAdapter(
            [
                {
                    "code": 200,
                    "result": {"data": {"customers": first_page, "scrollId": "next-page"}},
                },
                {
                    "code": 200,
                    "result": {"data": {"customers": last_page, "scrollId": "done"}},
                },
            ]
        )

        result = list(adapter.pull("Customer", customer_ids=["1"]))

        self.assertEqual(len(result), PAGE_SIZE + 1)
        self.assertEqual(adapter.calls[0][1]["customerIdArr"], ["1"])
        self.assertEqual(adapter.calls[1][1]["scrollId"], "next-page")

    def test_supplier_transform_maps_standard_fields(self):
        mapped = JackYunAdapter(None).transform(
            "Supplier",
            {
                "vendId": "11",
                "code": "V001",
                "name": "测试供应商",
                "className": "医疗耗材",
                "isBlockup": 0,
                "isDelete": 1,
            },
        )

        self.assertEqual(mapped["external_id"], "11")
        self.assertEqual(mapped["supplier_name"], "测试供应商")
        self.assertEqual(mapped["disabled"], 1)

    def test_supplier_response_code_is_not_a_supplier_record(self):
        supplier = {"vendId": "11", "code": "V001", "name": "测试供应商"}
        payload = {"code": 200, "result": {"data": {"vendInfo": [supplier]}}}

        self.assertEqual(extract_records_by_identity(payload, ["vendId"]), [supplier])

    def test_extracts_parent_without_treating_components_as_packages(self):
        parent = package(123, "BUNDLE-A")
        payload = {"code": 200, "result": {"data": parent}}

        self.assertEqual(extract_product_packages(payload), [parent])

    def test_extracts_paginated_parent_list(self):
        parents = [package(1), package(2)]
        payload = {"code": 200, "result": {"data": parents}}

        self.assertEqual(extract_product_packages(payload), parents)

    def test_cursor_pagination_uses_last_sku_id(self):
        first_page = [package(i) for i in range(1, PACKAGE_PAGE_SIZE + 1)]
        last_page = [package(PACKAGE_PAGE_SIZE + 1)]
        adapter = FakePackageAdapter(
            [
                {"code": 200, "result": {"data": first_page}},
                {"code": 200, "result": {"data": last_page}},
            ]
        )

        result = list(adapter.pull("Product Bundle"))

        self.assertEqual(len(result), PACKAGE_PAGE_SIZE + 1)
        self.assertEqual(adapter.calls[0][1]["maxSkuId"], 0)
        self.assertEqual(adapter.calls[1][1]["maxSkuId"], PACKAGE_PAGE_SIZE)
        self.assertTrue(all(call[3] == PACKAGE_PAGE_SIZE for call in adapter.calls))

    def test_transform_keeps_parent_and_component_payload(self):
        raw = package(123, "BUNDLE-A")
        mapped = JackYunAdapter(None).transform("Product Bundle", raw)

        self.assertEqual(mapped["doctype"], "Product Bundle")
        self.assertEqual(mapped["external_id"], "123")
        self.assertEqual(mapped["parent_item"]["item_code"], "BUNDLE-A")
        self.assertEqual(mapped["items"][0]["goodsAmount"], 2)

    def test_sales_order_uses_scroll_cursor(self):
        first_page = [
            {"tradeId": str(i), "goodsDetail": [{"sellCount": 1}]}
            for i in range(1, SALES_ORDER_PAGE_SIZE + 1)
        ]
        last_page = [
            {"tradeId": str(SALES_ORDER_PAGE_SIZE + 1), "goodsDetail": [{"sellCount": 1}]}
        ]
        adapter = FakePackageAdapter(
            [
                {"code": 200, "result": {"data": {"trades": first_page, "scrollId": "next"}}},
                {"code": 200, "result": {"data": {"trades": last_page, "scrollId": "done"}}},
            ]
        )
        adapter.connection = FakeConnection(sales_order_start_date="2026-08-08")

        result = list(adapter.pull("Sales Order", until=datetime(2026, 8, 14, 23, 59, 59)))

        self.assertEqual(len(result), SALES_ORDER_PAGE_SIZE + 1)
        self.assertEqual(adapter.calls[0][1]["startCreated"], "2026-08-08 00:00:00")
        self.assertEqual(adapter.calls[1][1]["scrollId"], "next")
        self.assertEqual(adapter.calls[0][3], SALES_ORDER_PAGE_SIZE)

    def test_sales_order_transform_keeps_draft_source_fields(self):
        mapped = JackYunAdapter(None).transform(
            "Sales Order",
            {
                "tradeId": "2382341399615340928",
                "tradeNo": "JY202601010001",
                "onlineTradeNo": "P782749202050409681",
                "companyName": "武汉医麦德医疗用品有限公司",
                "tradeTime": "2026-01-01 00:06:42",
                "warehouseCode": "0006",
                "channelCode": "103",
                "customerName": "平台客户",
                "customerAccount": "buyer-001",
                "state": "湖北省",
                "city": "武汉市",
                "payerPhone": "027-12345678",
                "goodsDetail": [{"goodsNo": "CI-19801", "sellCount": 2}],
            },
        )

        self.assertEqual(mapped["external_id"], "2382341399615340928")
        self.assertEqual(mapped["trade_no"], "JY202601010001")
        self.assertEqual(mapped["source_trade_no"], "P782749202050409681")
        self.assertEqual(mapped["trade_time"], "2026-01-01 00:06:42")
        self.assertEqual(mapped["warehouse_code"], "0006")
        self.assertEqual(mapped["channel_code"], "103")
        self.assertEqual(mapped["customer_snapshot_name"], "平台客户")
        self.assertEqual(mapped["customer_account"], "buyer-001")
        self.assertEqual(mapped["state"], "湖北省")
        self.assertEqual(mapped["payer_phone"], "027-12345678")
        self.assertEqual(mapped["items"][0]["sellCount"], 2)

    def test_purchase_order_groups_detail_rows_by_head(self):
        rows = [
            {
                "id": "1",
                "headId": "100",
                "orderNum": "CG001",
                "goodsNo": "A",
                "quantity": 2,
            },
            {
                "id": "2",
                "headId": "100",
                "orderNum": "CG001",
                "goodsNo": "B",
                "quantity": 3,
            },
        ]
        adapter = FakePackageAdapter(
            [{"code": 200, "result": {"data": {"rows": rows}}}]
        )
        adapter.connection = FakeConnection(purchase_order_start_date="2026-08-08")

        result = list(
            adapter.pull("Purchase Order", until=datetime(2026, 8, 14, 23, 59, 59))
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]["purchaseDetails"]), 2)
        self.assertEqual(adapter.calls[0][3], PURCHASE_ORDER_PAGE_SIZE)
        self.assertEqual(adapter.calls[0][1]["startDate"], "2026-08-08 00:00:00")

    def test_purchase_order_transform_uses_head_id(self):
        mapped = JackYunAdapter(None).transform(
            "Purchase Order",
            {
                "headId": "100",
                "orderNum": "CG001",
                "orderTime": "2026-08-10 13:08:12",
                "companyName": "武汉医麦德医疗用品有限公司",
                "vendId": "200",
                "vendName": "测试供应商",
                "purchaseDetails": [{"id": "1", "goodsNo": "A", "quantity": 2}],
            },
        )

        self.assertEqual(mapped["external_id"], "100")
        self.assertEqual(mapped["order_num"], "CG001")
        self.assertEqual(len(mapped["items"]), 1)

    def test_purchase_receipt_only_queries_purchase_blue_receipts(self):
        detail = {
            "docId": "300",
            "recId": "301",
            "goodsdocNo": "CRK001",
            "inouttypeName": "采购入库",
            "redStatus": 1,
            "goodsNo": "A",
            "quantity": 2,
        }
        adapter = FakePackageAdapter(
            [
                {"code": 200, "result": {"data": 1}},
                {"code": 200, "result": {"data": [detail]}},
            ]
        )
        adapter.connection = FakeConnection(purchase_receipt_start_date="2026-08-08")

        result = list(
            adapter.pull("Purchase Receipt", until=datetime(2026, 8, 14, 23, 59, 59))
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["docId"], "300")
        self.assertEqual(result[0]["goodsDocDetailList"], [detail])
        self.assertEqual(adapter.calls[0][1]["inouttypes"], "101")
        self.assertEqual(adapter.calls[0][1]["archived"], 0)
        self.assertIn("inOutDateStart", adapter.calls[0][1])
        self.assertEqual(adapter.calls[0][0], "erp-busiorder.goodsdocin.search.count")
        self.assertEqual(adapter.calls[1][0], "erp-busiorder.goodsdocin.search")

    def test_purchase_receipt_transform_maps_details(self):
        mapped = JackYunAdapter(None).transform(
            "Purchase Receipt",
            {
                "docId": "300",
                "recId": "301",
                "goodsdocNo": "CRK001",
                "inOutDate": "2026-08-10 10:16:23",
                "inouttypeName": "采购入库",
                "redStatus": 1,
                "companyName": "咸宁医麦德实业有限公司",
                "warehouseCode": "20001",
                "vendCode": "600031",
                "vendCustomerName": "测试供应商",
                "goodsDocDetailList": [{"recId": "301", "goodsNo": "A", "quantity": 2}],
            },
        )

        self.assertEqual(mapped["external_id"], "300")
        self.assertEqual(mapped["receipt_no"], "CRK001")
        self.assertEqual(mapped["warehouse_code"], "20001")
        self.assertEqual(mapped["inbound_type"], 101)
        self.assertEqual(len(mapped["items"]), 1)

    def test_purchase_receipt_incremental_covers_header_and_detail_changes(self):
        detail = {
            "docId": "300",
            "recId": "301",
            "goodsdocNo": "CRK001",
            "redStatus": 1,
            "goodsNo": "A",
            "quantity": 2,
        }
        adapter = FakePackageAdapter(
            [
                {"code": 200, "result": {"data": 1}},
                {"code": 200, "result": {"data": [detail]}},
                {"code": 200, "result": {"data": 1}},
                {"code": 200, "result": {"data": [detail]}},
            ]
        )
        adapter.connection = FakeConnection(purchase_receipt_start_date="2026-08-08")

        result = list(
            adapter.pull(
                "Purchase Receipt",
                since=datetime(2026, 8, 14, 12, 0, 0),
                until=datetime(2026, 8, 14, 13, 0, 0),
            )
        )

        self.assertEqual(len(result), 1)
        self.assertIn("gmtModifiedDateStart", adapter.calls[0][1])
        self.assertIn("gmtModifiedDetailDateStart", adapter.calls[2][1])

    def test_purchase_receipt_resolves_serial_source_ids_in_batch(self):
        detail = {
            "docId": "300",
            "recId": "301",
            "goodsdocNo": "CRK001",
            "redStatus": 1,
            "goodsNo": "A",
            "quantity": 2,
            "serialSourceId": "serial-source-1",
        }
        serials = [
            {"serialSourceId": "serial-source-1", "serialNo": "SN001"},
            {"serialSourceId": "serial-source-1", "serialNo": "SN002"},
        ]
        adapter = FakePackageAdapter(
            [
                {"code": 200, "result": {"data": 1}},
                {"code": 200, "result": {"data": [detail]}},
                {"code": 200, "result": {"data": serials}},
            ]
        )
        adapter.connection = FakeConnection(purchase_receipt_start_date="2026-08-08")

        result = list(
            adapter.pull("Purchase Receipt", until=datetime(2026, 8, 14, 23, 59, 59))
        )

        resolved = result[0]["goodsDocDetailList"][0]["serialRecords"]
        self.assertEqual([row["serialNo"] for row in resolved], ["SN001", "SN002"])
        self.assertEqual(adapter.calls[2][0], "erp.storage.goodsdocserial")
        self.assertEqual(
            adapter.calls[2][1]["serialSourceIdStrs"], "serial-source-1"
        )
        self.assertEqual(adapter.calls[2][3], 200)

    def test_delivery_note_uses_count_and_groups_spec_rows(self):
        rows = [
            {
                "recId": "1",
                "goodsdocNo": "CRK001",
                "redStatus": 1,
                "goodsNo": "A",
                "quantity": 2,
            },
            {
                "recId": "2",
                "goodsdocNo": "CRK001",
                "redStatus": 1,
                "goodsNo": "B",
                "quantity": 3,
            },
        ]
        adapter = FakePackageAdapter(
            [
                {"code": 200, "result": {"data": 2}},
                {"code": 200, "result": {"data": rows}},
            ]
        )
        adapter.connection = FakeConnection(delivery_note_start_date="2026-08-14")

        result = list(
            adapter.pull("Delivery Note", until=datetime(2026, 8, 14, 23, 59, 59))
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]["goodsDocDetailList"]), 2)
        self.assertEqual(adapter.calls[0][0], "erp-busiorder.goodsdocout.search.count")
        self.assertEqual(adapter.calls[1][0], "erp-busiorder.goodsdocout.search")
        self.assertEqual(adapter.calls[1][1]["inouttypes"], "201")

    def test_delivery_note_transform_maps_sales_outbound(self):
        mapped = JackYunAdapter(None).transform(
            "Delivery Note",
            {
                "goodsdocNo": "CRK001",
                "billNo": "JY202608140014",
                "inOutDate": "2026-08-14 10:00:00",
                "inouttypeName": "销售出库",
                "redStatus": 1,
                "companyName": "武汉医麦德医疗用品有限公司",
                "warehouseCode": "0006",
                "channelCode": "103",
                "goodsDocDetailList": [{"recId": "1", "goodsNo": "A", "quantity": 2}],
            },
        )

        self.assertEqual(mapped["external_id"], "CRK001")
        self.assertEqual(mapped["source_order_no"], "JY202608140014")
        self.assertEqual(mapped["outbound_type"], 201)
        self.assertEqual(len(mapped["items"]), 1)

    def test_sales_return_uses_inbound_count_and_type_105(self):
        row = {
            "docId": "500",
            "recId": "501",
            "goodsdocNo": "TH001",
            "redStatus": 1,
            "goodsNo": "A",
            "quantity": 1,
        }
        adapter = FakePackageAdapter(
            [
                {"code": 200, "result": {"data": 1}},
                {"code": 200, "result": {"data": [row]}},
            ]
        )
        adapter.connection = FakeConnection(sales_return_start_date="2026-08-14")

        result = list(adapter.pull("Sales Return", until=datetime(2026, 8, 14, 23, 59, 59)))

        self.assertEqual(len(result), 1)
        self.assertEqual(adapter.calls[0][0], "erp-busiorder.goodsdocin.search.count")
        self.assertEqual(adapter.calls[0][1]["inouttypes"], "105")

    def test_purchase_return_uses_outbound_type_205(self):
        row = {
            "recId": "601",
            "goodsdocNo": "CT001",
            "redStatus": 1,
            "goodsNo": "A",
            "quantity": 1,
        }
        adapter = FakePackageAdapter(
            [
                {"code": 200, "result": {"data": 1}},
                {"code": 200, "result": {"data": [row]}},
            ]
        )
        adapter.connection = FakeConnection(purchase_return_start_date="2026-08-14")

        result = list(adapter.pull("Purchase Return", until=datetime(2026, 8, 14, 23, 59, 59)))

        self.assertEqual(len(result), 1)
        self.assertEqual(adapter.calls[0][1]["inouttypes"], "205")

    def test_stock_transfer_groups_detail_rows(self):
        rows = [
            {"allocateId": "700", "allocateNo": "DB001", "allocateDetailId": "1", "goodsNo": "A"},
            {"allocateId": "700", "allocateNo": "DB001", "allocateDetailId": "2", "goodsNo": "B"},
        ]
        adapter = FakePackageAdapter([{"code": 200, "result": {"data": rows}}])
        adapter.connection = FakeConnection(stock_transfer_start_date="2026-08-14")

        result = list(adapter.pull("Stock Transfer", until=datetime(2026, 8, 14, 23, 59, 59)))

        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]["allocateDetails"]), 2)
        self.assertEqual(adapter.calls[0][1]["cols"], STOCK_TRANSFER_FIELDS)

    def test_stocktake_transform_keeps_counted_quantity(self):
        mapped = JackYunAdapter(None).transform(
            "Stocktake",
            {
                "stocktakeId": "PD001",
                "status": 5,
                "warehouseCode": "CK01",
                "stockTakeDetailViews": [{"goodsNo": "A", "takeQuan": 20}],
            },
        )

        self.assertEqual(mapped["external_id"], "PD001")
        self.assertEqual(mapped["status"], 5)
        self.assertEqual(mapped["items"][0]["takeQuan"], 20)

    def test_batch_external_id_is_item_and_batch(self):
        mapped = JackYunAdapter(None).transform(
            "Batch",
            {"skuId": "800", "goodsNo": "A", "batchNo": "PC001"},
        )

        self.assertEqual(mapped["external_id"], "800:PC001")
        self.assertEqual(mapped["batch_no"], "PC001")


if __name__ == "__main__":
    unittest.main()

from copy import deepcopy
from unittest import TestCase
from unittest.mock import MagicMock, call, patch

import frappe

from channel_erp.integrations.connector_operations import (
    _dependency_decision,
    _update_reference_status,
)
from channel_erp.integrations.outbound_events import (
    _enqueue_cancel_rebuild,
    has_material_sales_order_change,
    material_sales_order_snapshot,
    on_sales_order_update_after_submit,
)


class _Order(frappe._dict):
    def db_set(self, values, **kwargs):
        del kwargs
        self.update(values)


def _order(**overrides):
    value = _Order(
        name="SO-REV-1",
        docstatus=1,
        custom_connector_source="ERPNext",
        custom_jackyun_trade_no="JY-OLD",
        custom_external_revision=0,
        custom_jackyun_sales_channel="OTC销售组",
        set_warehouse="武汉仓 - WMD",
        customer="客户甲",
        remarks="初始备注",
        delivery_date="2026-08-30",
        custom_external_sync_status="Succeeded",
        items=[
            frappe._dict(
                item_code="ITEM-1",
                qty=2,
                rate=10,
                warehouse="武汉仓 - WMD",
                uom="个",
                description="商品一",
            ),
            frappe._dict(
                item_code="ITEM-2",
                qty=1,
                rate=20,
                warehouse="武汉仓 - WMD",
                stock_uom="盒",
                description="商品二",
            ),
        ],
    )
    value.update(overrides)
    return value


class TestSalesOrderMaterialFingerprint(TestCase):
    def test_snapshot_contains_only_material_remote_fields(self):
        snapshot = material_sales_order_snapshot(_order())
        self.assertEqual(
            {"sales_channel", "set_warehouse", "items"}, set(snapshot)
        )
        self.assertEqual(
            {"item_code", "qty", "rate", "warehouse", "uom"},
            set(snapshot["items"][0]),
        )
        serialized = frappe.as_json(snapshot)
        for excluded in (
            "客户甲",
            "初始备注",
            "2026-08-30",
            "Succeeded",
            "description",
        ):
            self.assertNotIn(excluded, serialized)

    def test_non_material_changes_do_not_change_fingerprint(self):
        before = _order()
        after = _order(
            customer="客户乙",
            remarks="仅修改内部备注",
            delivery_date="2026-09-01",
            custom_external_sync_status="Processing",
            custom_external_sync_message="队列回执",
        )
        self.assertFalse(has_material_sales_order_change(after, before))

    def test_each_material_field_changes_fingerprint(self):
        before = _order()
        variants = []
        for header, value in (
            ("custom_jackyun_sales_channel", "跨境销售组"),
            ("set_warehouse", "咸宁仓 - XN"),
        ):
            changed = _order()
            changed[header] = value
            variants.append(changed)
        for fieldname, value in (
            ("item_code", "ITEM-X"),
            ("qty", 3),
            ("rate", 11),
            ("warehouse", "咸宁仓 - XN"),
            ("uom", "箱"),
        ):
            changed = _order()
            changed["items"] = deepcopy(changed.get("items"))
            changed.get("items")[0][fieldname] = value
            variants.append(changed)
        for changed in variants:
            self.assertTrue(
                has_material_sales_order_change(changed, before),
                msg=f"未识别实质变更：{changed}",
            )

    def test_reordering_unchanged_items_is_not_a_material_change(self):
        before = _order()
        after = _order(items=list(reversed(deepcopy(before.get("items")))))
        self.assertFalse(has_material_sales_order_change(after, before))


class TestSalesOrderRevisionEvent(TestCase):
    @patch("channel_erp.integrations.outbound_events._enqueue_cancel_rebuild")
    @patch("channel_erp.integrations.outbound_events._is_jackyun_source", return_value=False)
    @patch("channel_erp.integrations.outbound_events._active_connection")
    def test_no_material_change_does_not_enqueue(
        self, active_connection, _source, enqueue_rebuild
    ):
        active_connection.return_value = frappe._dict(name="CONN-1")
        before = _order()
        doc = _order(remarks="仅修改内部备注")
        doc.get_doc_before_save = lambda: before
        self.assertIsNone(on_sales_order_update_after_submit(doc))
        enqueue_rebuild.assert_not_called()

    @patch("channel_erp.integrations.outbound_events._enqueue_cancel_rebuild")
    @patch("channel_erp.integrations.outbound_events._is_jackyun_source", return_value=True)
    @patch("channel_erp.integrations.outbound_events._active_connection")
    def test_jackyun_source_never_enqueues_rebuild(
        self, active_connection, _source, enqueue_rebuild
    ):
        active_connection.return_value = frappe._dict(name="CONN-1")
        doc = _order(custom_connector_source="JackYun")
        before = _order(custom_connector_source="JackYun")
        doc.get("items")[0].qty = 99
        doc.get_doc_before_save = lambda: before
        self.assertIsNone(on_sales_order_update_after_submit(doc))
        enqueue_rebuild.assert_not_called()

    def test_repeated_same_snapshot_is_idempotent_and_create_depends_on_cancel(self):
        doc = _order()
        connection = frappe._dict(
            name="CONN-1", default_cancel_reason="420001"
        )
        cancel = frappe._dict(name="OUT-CANCEL-1", status="Preflight")
        create = frappe._dict(
            name="OUT-CREATE-1",
            status="Preflight",
            origin_message=cancel.name,
        )
        create_payload = {
            "tradeOrder": {"onlineTradeNo": "SO-REV-1-R1"}
        }
        with (
            patch(
                "channel_erp.integrations.outbound_events._is_jackyun_source",
                return_value=False,
            ),
            patch(
                "channel_erp.integrations.outbound_events._existing_rebuild_create",
                side_effect=[None, create],
            ),
            patch(
                "channel_erp.integrations.outbound_events._rebuild_cancel_message",
                return_value=cancel,
            ),
            patch(
                "channel_erp.integrations.outbound_events._retire_stale_rebuild_creates"
            ),
            patch(
                "channel_erp.integrations.outbound_events._build_create",
                return_value=(create_payload, 1),
            ),
            patch(
                "channel_erp.integrations.outbound_events._generic_connection",
                return_value="GENERIC-1",
            ),
            patch(
                "channel_erp.integrations.outbound_events.enqueue_outbound_internal",
                return_value=create,
            ) as enqueue,
            patch.object(frappe, "get_doc", return_value=cancel),
        ):
            first = _enqueue_cancel_rebuild(doc, connection)
            second = _enqueue_cancel_rebuild(doc, connection)

        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        enqueue.assert_called_once()
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(cancel.name, kwargs["origin_message"])
        self.assertTrue(kwargs["operation_identity"].startswith("REBUILD-CREATE:"))
        self.assertEqual(first["create"].name, second["create"].name)

    @patch.object(frappe.db, "get_value")
    def test_cancel_failure_is_terminal_for_dependent_create(self, get_value):
        create = frappe._dict(name="OUT-CREATE-1", origin_message="OUT-CANCEL-1")
        get_value.return_value = frappe._dict(
            status="Failed", origin_message=None
        )
        ready, reason, terminal = _dependency_decision(create)
        self.assertFalse(ready)
        self.assertTrue(terminal)
        self.assertIn("Failed", reason)

    @patch.object(frappe.db, "set_value")
    @patch.object(frappe, "get_meta")
    @patch.object(frappe.db, "exists", return_value=True)
    def test_successful_revision_create_writes_new_trade_no(
        self, _exists, get_meta, set_value
    ):
        get_meta.return_value.has_field.side_effect = lambda fieldname: fieldname in {
            "custom_external_sync_status",
            "custom_external_sync_last_at",
            "custom_external_sync_message",
            "custom_jackyun_trade_no",
        }
        message = frappe._dict(
            reference_doctype="Sales Order",
            reference_name="SO-REV-1",
            operation="Create",
            status="Succeeded",
            external_id="JY-NEW",
            error_message=None,
        )
        _update_reference_status(message)
        values = set_value.call_args.args[2]
        self.assertEqual("JY-NEW", values["custom_jackyun_trade_no"])
        self.assertEqual("Succeeded", values["custom_external_sync_status"])

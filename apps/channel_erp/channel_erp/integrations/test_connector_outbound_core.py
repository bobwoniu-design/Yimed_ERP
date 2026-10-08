from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from channel_erp.integrations.base import BaseAdapter
from channel_erp.integrations.connector_contracts import (
    OperationBlockedError,
    OperationResult,
    ProbeResult,
    ensure_transition,
    normalize_operation_result,
)
from channel_erp.integrations.connector_operations import (
    _dependency_decision,
    _maybe_enqueue_auto_audit,
    build_idempotency_key,
    is_echo_source,
    probe_uncertain_outbound,
    process_outbound_queue,
)


class FakeAdapter(BaseAdapter):
    platform = "fake"

    def sign(self, params, secret):
        return "signature"

    def request(self, method, bizcontent, page_index):
        raise AssertionError("network request is forbidden in this test")

    def pull(self, resource):
        return iter(())

    def transform(self, resource, raw):
        return raw

    def outbound_capabilities(self, resource=None):
        return {"Create", "Update", "Audit", "Query"}

    def push(self, resource, operation, payload, **kwargs):
        return OperationResult.succeeded(
            external_id="REMOTE-1", context_id="CTX-1", response={"ok": True}
        )

    def query_operation(self, resource, operation, payload, **kwargs):
        return {"status": "Succeeded", "rows": []}


class FakeMessage(frappe._dict):
    def save(self, **kwargs):
        self.saved = self.get("saved", 0) + 1
        return self

    def check_permission(self, permission_type):
        self.permission_checked = permission_type

    def as_dict(self):
        return dict(self)


def _message(operation="Create", status="Pending"):
    return FakeMessage(
        name="OUT-1",
        connector_connection="GENERIC-1",
        connection=None,
        platform="fake",
        adapter_key="fake",
        resource="Sales Order",
        operation=operation,
        status=status,
        payload='{"value": 1}',
        idempotency_key="stable-key",
        attempts=0,
        max_attempts=5,
        source_system="erpnext",
        source_event_id="EVENT-1",
        reference_doctype="Sales Order",
        reference_name="SO-1",
        external_id=None,
        uncertain_since=None,
        probe_attempts=0,
    )


def _descriptor():
    return {
        "platform": "fake",
        "adapter_key": "fake",
        "runtime": frappe._dict(conflict_policy="人工复核"),
        "allowed_operations": {"Create", "Update", "Query"},
        "source_marker": "erpnext",
    }


class TestConnectorOutboundCore(TestCase):
    def test_dependency_only_runs_after_upstream_succeeds(self):
        message = _message("Create", "Preflight")
        message.origin_message = "OUT-UPSTREAM"
        adapter = FakeAdapter()
        with (
            patch.object(
                frappe.db,
                "get_value",
                return_value=frappe._dict(status="Succeeded", origin_message=None),
            ),
            patch("channel_erp.integrations.connector_operations._connection_descriptor", return_value=_descriptor()),
            patch("channel_erp.integrations.connector_operations.get_adapter", return_value=adapter),
            patch.object(frappe.db, "sql", return_value=["OUT-1"]),
            patch.object(frappe, "get_doc", return_value=message),
            patch.object(frappe.db, "commit"),
        ):
            stats = process_outbound_queue()
        self.assertEqual("Succeeded", message.status)
        self.assertEqual(1, stats["succeeded"])

    def test_unresolved_or_uncertain_dependency_waits_without_transport(self):
        for upstream_status in ("Pending", "Processing", "Retrying", "Uncertain"):
            message = _message("Create", "Preflight")
            message.origin_message = "OUT-UPSTREAM"
            adapter = FakeAdapter()
            adapter.execute_operation = MagicMock()
            with (
                patch.object(
                    frappe.db,
                    "get_value",
                    return_value=frappe._dict(
                        status=upstream_status, origin_message=None
                    ),
                ),
                patch("channel_erp.integrations.connector_operations._connection_descriptor") as descriptor,
                patch("channel_erp.integrations.connector_operations.get_adapter", return_value=adapter),
                patch.object(frappe.db, "sql", return_value=["OUT-1"]),
                patch.object(frappe, "get_doc", return_value=message),
                patch.object(frappe.db, "commit"),
            ):
                stats = process_outbound_queue()
            self.assertEqual("Preflight", message.status)
            self.assertEqual(1, stats["waiting"])
            self.assertEqual(0, message.attempts)
            self.assertIsNotNone(message.next_retry_at)
            descriptor.assert_not_called()
            adapter.execute_operation.assert_not_called()

    def test_terminal_dependency_blocks_without_transport(self):
        for upstream_status in ("Failed", "Blocked", "Conflict"):
            message = _message("Update", "Pending")
            message.origin_message = "OUT-UPSTREAM"
            adapter = FakeAdapter()
            adapter.execute_operation = MagicMock()
            with (
                patch.object(
                    frappe.db,
                    "get_value",
                    return_value=frappe._dict(
                        status=upstream_status, origin_message=None
                    ),
                ),
                patch("channel_erp.integrations.connector_operations._connection_descriptor") as descriptor,
                patch("channel_erp.integrations.connector_operations.get_adapter", return_value=adapter),
                patch.object(frappe.db, "sql", return_value=["OUT-1"]),
                patch.object(frappe, "get_doc", return_value=message),
                patch.object(frappe.db, "commit"),
            ):
                stats = process_outbound_queue()
            self.assertEqual("Blocked", message.status)
            self.assertEqual(1, stats["blocked"])
            self.assertEqual(0, message.attempts)
            descriptor.assert_not_called()
            adapter.execute_operation.assert_not_called()

    def test_missing_or_self_dependency_is_terminal(self):
        message = _message()
        message.origin_message = message.name
        ready, reason, terminal = _dependency_decision(message)
        self.assertFalse(ready)
        self.assertTrue(terminal)
        self.assertIn("自身", reason)

    def test_create_key_is_stable_across_payload_serialization_and_changes(self):
        first, identity = build_idempotency_key(
            "CONN-1", "Sales Order", "Create", "Sales Order", "SO-1",
            payload={"b": 2, "a": 1},
        )
        reordered, _ = build_idempotency_key(
            "CONN-1", "Sales Order", "Create", "Sales Order", "SO-1",
            payload={"a": 1, "b": 2},
        )
        changed, _ = build_idempotency_key(
            "CONN-1", "Sales Order", "Create", "Sales Order", "SO-1",
            payload={"a": 999},
        )
        self.assertEqual("Sales Order:SO-1", identity)
        self.assertEqual(first, reordered)
        self.assertEqual(first, changed)

    def test_update_key_changes_only_when_semantic_payload_changes(self):
        first, _ = build_idempotency_key(
            "CONN-1", "Sales Order", "Update", external_id="REMOTE-1",
            payload={"b": 2, "a": 1},
        )
        reordered, _ = build_idempotency_key(
            "CONN-1", "Sales Order", "Update", external_id="REMOTE-1",
            payload={"a": 1, "b": 2},
        )
        changed, _ = build_idempotency_key(
            "CONN-1", "Sales Order", "Update", external_id="REMOTE-1",
            payload={"a": 2, "b": 2},
        )
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)

    def test_missing_stable_identity_fails_closed(self):
        with self.assertRaises(OperationBlockedError):
            build_idempotency_key("CONN-1", "Sales Order", "Create", payload={})

    def test_adapter_capabilities_and_preflight_are_explicit(self):
        adapter = FakeAdapter()
        self.assertTrue(adapter.supports_operation("Sales Order", "Create"))
        self.assertFalse(adapter.supports_operation("Sales Order", "Cancel"))
        self.assertTrue(
            adapter.preflight_operation("Sales Order", "Create", {}).allowed
        )
        self.assertFalse(
            adapter.preflight_operation("Sales Order", "Cancel", {}).allowed
        )

    def test_audit_is_a_write_operation_while_query_is_read_only(self):
        adapter = FakeAdapter()
        adapter.push = MagicMock(return_value=OperationResult.succeeded())
        adapter.query_operation = MagicMock(return_value={"status": "Succeeded"})
        adapter.execute_operation(
            "Sales Order", "Audit", {}, idempotency_key="audit-key"
        )
        adapter.execute_operation(
            "Sales Order", "Query", {}, idempotency_key="query-key"
        )
        adapter.push.assert_called_once()
        adapter.query_operation.assert_called_once()

    def test_uncertain_state_cannot_blindly_execute_again(self):
        with self.assertRaises(OperationBlockedError):
            ensure_transition("Uncertain", "Processing")
        self.assertEqual("Pending", ensure_transition("Uncertain", "Pending"))

    def test_source_marker_blocks_same_platform_echo(self):
        self.assertTrue(is_echo_source("FAKE", "fake"))
        self.assertFalse(is_echo_source("erpnext", "fake"))

    def test_create_transport_failure_becomes_uncertain_not_retrying(self):
        message = _message("Create", "Pending")
        adapter = FakeAdapter()
        adapter.execute_operation = MagicMock(side_effect=Exception("HTTP 503 timeout"))
        with (
            patch("channel_erp.integrations.connector_operations._connection_descriptor", return_value=_descriptor()),
            patch("channel_erp.integrations.connector_operations.get_adapter", return_value=adapter),
            patch.object(frappe.db, "sql", return_value=["OUT-1"]),
            patch.object(frappe, "get_doc", return_value=message),
            patch.object(frappe.db, "commit"),
        ):
            stats = process_outbound_queue()
        self.assertEqual("Uncertain", message.status)
        self.assertEqual(1, stats["uncertain"])
        self.assertIsNone(message.next_retry_at)

    def test_update_transport_failure_can_retry(self):
        message = _message("Update", "Pending")
        adapter = FakeAdapter()
        adapter.execute_operation = MagicMock(side_effect=Exception("HTTP 503 timeout"))
        with (
            patch("channel_erp.integrations.connector_operations._connection_descriptor", return_value=_descriptor()),
            patch("channel_erp.integrations.connector_operations.get_adapter", return_value=adapter),
            patch.object(frappe.db, "sql", return_value=["OUT-1"]),
            patch.object(frappe, "get_doc", return_value=message),
            patch.object(frappe.db, "commit"),
        ):
            stats = process_outbound_queue()
        self.assertEqual("Retrying", message.status)
        self.assertEqual(1, stats["retrying"])

    def test_adapter_failed_receipt_is_not_misreported_as_success(self):
        message = _message("Update", "Pending")
        adapter = FakeAdapter()
        adapter.execute_operation = MagicMock(
            return_value={"status": "Failed", "contextId": "CTX-FAILED"}
        )
        with (
            patch("channel_erp.integrations.connector_operations._connection_descriptor", return_value=_descriptor()),
            patch("channel_erp.integrations.connector_operations.get_adapter", return_value=adapter),
            patch.object(frappe.db, "sql", return_value=["OUT-1"]),
            patch.object(frappe, "get_doc", return_value=message),
            patch.object(frappe.db, "commit"),
        ):
            stats = process_outbound_queue()
        self.assertEqual("Failed", message.status)
        self.assertEqual("CTX-FAILED", message.response_context_id)
        self.assertEqual(1, stats["failed"])

    def test_probe_found_resolves_uncertain_without_repeating_write(self):
        message = _message("Create", "Uncertain")
        adapter = FakeAdapter()
        adapter.probe_operation = MagicMock(
            return_value=ProbeResult.found(
                external_id="REMOTE-1", context_id="PROBE-CTX", response={"found": True}
            )
        )
        adapter.execute_operation = MagicMock()
        with (
            patch("channel_erp.integrations.connector_operations._connection_descriptor", return_value=_descriptor()),
            patch("channel_erp.integrations.connector_operations.get_adapter", return_value=adapter),
            patch.object(frappe, "get_doc", return_value=message),
        ):
            result = probe_uncertain_outbound("OUT-1")
        self.assertEqual("Succeeded", result["status"])
        self.assertEqual("REMOTE-1", result["external_id"])
        adapter.execute_operation.assert_not_called()

    def test_probe_not_found_is_only_path_back_to_pending(self):
        message = _message("Create", "Uncertain")
        adapter = FakeAdapter()
        adapter.probe_operation = MagicMock(return_value=ProbeResult.not_found())
        with (
            patch("channel_erp.integrations.connector_operations._connection_descriptor", return_value=_descriptor()),
            patch("channel_erp.integrations.connector_operations.get_adapter", return_value=adapter),
            patch.object(frappe, "get_doc", return_value=message),
        ):
            result = probe_uncertain_outbound("OUT-1")
        self.assertEqual("Pending", result["status"])

    def test_normalize_uncertain_adapter_receipt(self):
        result = normalize_operation_result(
            {"status": "unknown", "contextId": "CTX-UNKNOWN"}
        )
        self.assertEqual("Uncertain", result.outcome)
        self.assertEqual("CTX-UNKNOWN", result.context_id)


class TestAutoAuditChain(TestCase):
    def _succeeded_create(self):
        message = _message("Create", "Succeeded")
        message.external_id = "JY-1"
        message.payload = '{"tradeOrder": {"onlineTradeNo": "SO-1"}}'
        return message

    def _settings(self, **overrides):
        value = frappe._dict(
            credential_doctype="Jackyun Connection",
            credential_name="JY-CONN",
            auto_audit_outbound=1,
            audit_operator="operator-a",
            outbound_test_mode=0,
        )
        value.update(overrides)
        return value

    def test_audit_enqueued_after_create_success(self):
        message = self._succeeded_create()
        with (
            patch.object(frappe.db, "get_value", return_value=self._settings()),
            patch.object(frappe.db, "exists", return_value=False),
            patch(
                "channel_erp.integrations.connector_operations.enqueue_outbound_internal"
            ) as enqueue,
        ):
            _maybe_enqueue_auto_audit(message)
        enqueue.assert_called_once()
        kwargs = enqueue.call_args.kwargs
        self.assertEqual("Sales Order", kwargs["resource"])
        self.assertEqual("Audit", kwargs["operation"])
        self.assertEqual("JY-1", kwargs["external_id"])
        self.assertEqual({"tradeNos": ["JY-1"], "operator": "operator-a"}, kwargs["payload"])
        self.assertEqual("AUTO-AUDIT:JY-1", kwargs["operation_identity"])
        self.assertEqual("OUT-1", kwargs["origin_message"])

    def test_audit_skipped_when_disabled_or_test_mode(self):
        for settings in (
            self._settings(auto_audit_outbound=0),
            self._settings(outbound_test_mode=1),
        ):
            message = self._succeeded_create()
            with (
                patch.object(frappe.db, "get_value", return_value=settings),
                patch(
                    "channel_erp.integrations.connector_operations.enqueue_outbound_internal"
                ) as enqueue,
            ):
                _maybe_enqueue_auto_audit(message)
            enqueue.assert_not_called()

    def test_audit_skipped_for_erptest_or_non_success_messages(self):
        erptest = self._succeeded_create()
        erptest.payload = '{"tradeOrder": {"onlineTradeNo": "ERPTEST-20260928-SO-1"}}'
        pending = _message("Create", "Pending")
        cancel = _message("Cancel", "Succeeded")
        for message in (erptest, pending, cancel):
            message.external_id = "JY-1"
            with (
                patch.object(frappe.db, "get_value", return_value=self._settings()),
                patch(
                    "channel_erp.integrations.connector_operations.enqueue_outbound_internal"
                ) as enqueue,
            ):
                _maybe_enqueue_auto_audit(message)
            enqueue.assert_not_called()

    def test_audit_skipped_when_trade_already_cancelled(self):
        message = self._succeeded_create()
        with (
            patch.object(frappe.db, "get_value", return_value=self._settings()),
            patch.object(frappe.db, "exists", return_value=True),
            patch(
                "channel_erp.integrations.connector_operations.enqueue_outbound_internal"
            ) as enqueue,
        ):
            _maybe_enqueue_auto_audit(message)
        enqueue.assert_not_called()

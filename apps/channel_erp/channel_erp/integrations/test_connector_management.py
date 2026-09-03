from types import SimpleNamespace
from unittest import TestCase

import frappe

from channel_erp.integrations.exception_classifier import classify_exception
from channel_erp.integrations.connector_operations import evaluate_conflict
from channel_erp.integrations.field_mapping_catalog import (
    ERP_FIELD,
    FIELD_MAPPING_CATALOG,
    NOT_USED,
    RAW_ONLY,
    SYNC_CONTROL,
    catalog_source_fields,
)
from channel_erp.integrations.jackyun import (
    DELIVERY_NOTE_FIELDS,
    PURCHASE_ORDER_FIELDS,
    PURCHASE_RECEIPT_FIELDS,
    SALES_ORDER_FIELDS,
    STOCK_TRANSFER_FIELDS,
    extract_context_id,
)
from channel_erp.integrations.tasks import (
    SYNC_RESOURCE_ORDER,
    _apply_sync_start_floor,
    _effective_request_window,
    _successful_raw_snapshot_exists,
    apply_sync_log_metrics,
    apply_schedule_result,
    get_resource_config,
    is_pull_enabled,
)


class _Connection:
    def __init__(self, schedules=None):
        self.sync_schedules = schedules or []

    def get(self, key):
        return getattr(self, key, None)


class _SyncLog(frappe._dict):
    def set(self, key, value):
        self[key] = value


def _schedule(resource="Sales Order", direction="Pull Only", enabled=1):
    row = frappe._dict(
        resource=resource,
        direction=direction,
        enabled=enabled,
    )
    return row


class TestConnectorManagement(TestCase):

    def test_bidirectional_conflict_policy_is_deterministic(self):
        baseline = "2026-08-29 09:00:00"
        self.assertEqual("No Conflict", evaluate_conflict("人工复核", None, "2026-08-29 10:00:00", baseline))
        self.assertEqual("Source Wins", evaluate_conflict("吉客云优先", "2026-08-29 10:00:00", "2026-08-29 10:01:00", baseline))
        self.assertEqual("ERPNext Wins", evaluate_conflict("ERPNext优先", "2026-08-29 10:00:00", "2026-08-29 10:01:00", baseline))
        self.assertEqual("ERPNext Wins", evaluate_conflict("最新修改优先", "2026-08-29 10:00:00", "2026-08-29 10:01:00", baseline))
        self.assertEqual("Manual Review", evaluate_conflict("人工复核", "2026-08-29 10:00:00", "2026-08-29 10:01:00", baseline))
    def test_existing_connection_without_resource_rows_remains_pull_enabled(self):
        self.assertTrue(is_pull_enabled(_Connection(), "Sales Order"))

    def test_pull_direction_rules(self):
        self.assertTrue(is_pull_enabled(_Connection([_schedule(direction="Pull Only")]), "Sales Order"))
        self.assertTrue(is_pull_enabled(_Connection([_schedule(direction="Bidirectional")]), "Sales Order"))
        self.assertFalse(is_pull_enabled(_Connection([_schedule(direction="Push Only")]), "Sales Order"))
        self.assertFalse(is_pull_enabled(_Connection([_schedule(direction="Disabled")]), "Sales Order"))
        self.assertFalse(is_pull_enabled(_Connection([_schedule(enabled=0)]), "Sales Order"))

    def test_resource_config_is_scoped_by_resource(self):
        warehouse = _schedule(resource="Warehouse")
        sales_order = _schedule(resource="Sales Order")
        self.assertIs(get_resource_config(_Connection([warehouse, sales_order]), "Sales Order"), sales_order)

    def test_core_field_mapping_documentation_is_present(self):
        resources = {row["resource"] for row in FIELD_MAPPING_CATALOG}
        self.assertTrue({"Sales Order", "SKU", "Warehouse", "Inventory", "Purchase Order"} <= resources)

    def test_every_enabled_sync_resource_has_mapping_catalog(self):
        resources = {row["resource"] for row in FIELD_MAPPING_CATALOG}
        self.assertEqual(set(), set(SYNC_RESOURCE_ORDER) - resources)

    def test_every_explicit_core_request_field_is_classified(self):
        explicit_fields = {
            "Sales Order": SALES_ORDER_FIELDS,
            "Purchase Order": PURCHASE_ORDER_FIELDS,
            "Purchase Receipt": PURCHASE_RECEIPT_FIELDS,
            "Sales Return": PURCHASE_RECEIPT_FIELDS,
            "Delivery Note": DELIVERY_NOTE_FIELDS,
            "Purchase Return": DELIVERY_NOTE_FIELDS,
            "Stock Transfer": STOCK_TRANSFER_FIELDS,
        }
        missing = {}
        for resource, fields_csv in explicit_fields.items():
            requested = {field.strip() for field in fields_csv.split(",") if field.strip()}
            uncovered = requested - catalog_source_fields(resource)
            if uncovered:
                missing[resource] = sorted(uncovered)
        self.assertEqual({}, missing)

    def test_mapping_catalog_classifications_are_explicit(self):
        valid = {ERP_FIELD, SYNC_CONTROL, RAW_ONLY, NOT_USED}
        invalid = [row for row in FIELD_MAPPING_CATALOG if row["classification"] not in valid]
        missing_target = [
            row
            for row in FIELD_MAPPING_CATALOG
            if row["classification"] == ERP_FIELD
            and (not row["target_doctype"] or not row["target_field"])
        ]
        self.assertEqual([], invalid)
        self.assertEqual([], missing_target)

    def test_mapping_catalog_seed_keys_are_unique(self):
        keys = [
            (row["platform"], row["resource"], row["source_fields"][0])
            for row in FIELD_MAPPING_CATALOG
        ]
        self.assertEqual(len(keys), len(set(keys)))

    def test_mapping_governance_metadata_is_complete(self):
        required_keys = {
            "sync_direction",
            "required",
            "key_role",
            "data_type",
            "null_policy",
            "update_policy",
            "source_of_truth",
            "sensitive_level",
            "api_method_or_version",
        }
        incomplete = [
            row
            for row in FIELD_MAPPING_CATALOG
            if any(row.get(key) in (None, "") for key in required_keys)
        ]
        self.assertEqual([], incomplete)
        self.assertTrue(
            all(row["sync_direction"] in {"拉取", "推送", "双向", "不适用"} for row in FIELD_MAPPING_CATALOG)
        )
        self.assertTrue(
            all(row["sensitive_level"] in {"普通", "内部", "敏感"} for row in FIELD_MAPPING_CATALOG)
        )

    def test_exception_classification_and_retry_guidance(self):
        cases = {
            "签名错误 token 无效": ("接口/鉴权", 0),
            "HTTP 503 connection timeout": ("接口/鉴权", 1),
            "商品 A 尚未同步": ("缺少主数据", 1),
            "外部ID映射冲突": ("映射", 1),
            "MandatoryError: company is required": ("ERP校验", 0),
            "负库存，不允许提交批次": ("库存/批次", 0),
            "来源销售单未找到": ("关联单据", 1),
            "行号2：库存单位的数量不可为零": ("映射", 1),
            "不能链接到已取消单据": ("关联单据", 1),
            "unexpected boom": ("未知", 0),
        }
        for message, expected in cases.items():
            result = classify_exception(message)
            self.assertEqual(expected, (result["failure_category"], result["retryable"]))
            self.assertTrue(result["handling_suggestion"])

    def test_schedule_health_summary_resets_and_counts_failures(self):
        schedule = frappe._dict(
            interval_minutes=60,
            last_enqueued_at="2026-08-29 09:00:00",
            consecutive_failures=2,
        )
        apply_schedule_result(schedule, "失败", "2026-08-29 09:05:00")
        self.assertEqual(3, schedule.consecutive_failures)
        self.assertEqual("2026-08-29 10:00:00", str(schedule.next_run_at))
        apply_schedule_result(schedule, "成功", "2026-08-29 09:10:00")
        self.assertEqual(0, schedule.consecutive_failures)
        self.assertEqual("2026-08-29 09:10:00", str(schedule.last_success_at))

    def test_request_window_prefers_explicit_manual_range(self):
        start, end = _effective_request_window(
            "2026-08-20 00:00:00",
            "2026-08-29 00:00:00",
            {
                "created_since": "2026-08-25 08:00:00",
                "created_until": "2026-08-26 09:00:00",
            },
        )
        self.assertEqual("2026-08-25 08:00:00", str(start))
        self.assertEqual("2026-08-26 09:00:00", str(end))

    def test_context_id_is_only_read_from_known_response_envelopes(self):
        self.assertEqual(
            "CTX-1", extract_context_id({"result": {"contextId": "CTX-1"}})
        )
        self.assertEqual(
            "CTX-2",
            extract_context_id({"result": {"data": {"contextId": "CTX-2"}}}),
        )
        self.assertIsNone(extract_context_id({"result": {"data": []}}))

    def test_sync_log_metrics_use_observed_counters_and_adapter_telemetry(self):
        log = _SyncLog(
            started_at="2026-08-29 09:00:00",
            last_modified_at="2026-08-29 09:05:00",
        )
        counters = {
            "total_pulled": 12,
            "total_created": 2,
            "total_updated": 7,
            "total_skipped": 1,
            "duplicate_raw_count": 4,
            "total_failed": 2,
        }
        adapter = SimpleNamespace(request_count=3, last_context_id="CTX-3")
        apply_sync_log_metrics(
            log, "2026-08-29 09:00:02", counters, adapter, None
        )
        self.assertEqual(2.0, log.duration_seconds)
        self.assertEqual(12, log.total_pulled)
        self.assertEqual(1, log.total_skipped)
        self.assertEqual(4, log.duplicate_raw_count)
        self.assertEqual(3, log.api_request_count)
        self.assertEqual("CTX-3", log.context_id)
        self.assertEqual("2026-08-29 09:05:00", log.cursor_end_at)

    def test_successful_snapshot_lookup_is_scoped_to_exact_payload(self):
        original_exists = frappe.db.exists
        captured = {}

        def fake_exists(doctype, filters):
            captured.update({"doctype": doctype, "filters": filters})
            return "RAW-1"

        frappe.db.exists = fake_exists
        try:
            self.assertTrue(_successful_raw_snapshot_exists("SKU", 123, "digest"))
        finally:
            frappe.db.exists = original_exists

        self.assertEqual(captured["doctype"], "Jackyun Raw Record")
        self.assertEqual(
            captured["filters"],
            {
                "resource": "SKU",
                "external_id": "123",
                "content_hash": "digest",
                "processing_status": "Succeeded",
            },
        )

    def test_configured_start_is_a_hard_incremental_floor(self):
        floor = "2026-08-23 00:00:00"
        self.assertEqual(str(_apply_sync_start_floor(None, floor)), floor)
        self.assertEqual(
            str(_apply_sync_start_floor("2026-08-20 12:00:00", floor)), floor
        )
        self.assertEqual(
            str(_apply_sync_start_floor("2026-08-24 12:00:00", floor)),
            "2026-08-24 12:00:00",
        )

from unittest import TestCase
from unittest.mock import patch

import frappe

from channel_erp.integrations import tasks
from channel_erp.jackyun_integration.doctype.jackyun_connection.jackyun_connection import (
    JackyunConnection,
)


class TestMinuteScheduler(TestCase):
    def test_production_frequency_profile(self):
        self.assertEqual(5, tasks.DEFAULT_SYNC_INTERVALS["Sales Order"])
        self.assertEqual(5, tasks.DEFAULT_SYNC_INTERVALS["Delivery Note"])
        self.assertEqual(5, tasks.DEFAULT_SYNC_INTERVALS["Sales Return"])
        self.assertEqual(15, tasks.DEFAULT_SYNC_INTERVALS["Purchase Order"])
        self.assertEqual(15, tasks.DEFAULT_SYNC_INTERVALS["Purchase Receipt"])
        self.assertEqual(15, tasks.DEFAULT_SYNC_INTERVALS["Inventory"])
        self.assertEqual({1, 5, 15, 30, 60, 360}, tasks.SUPPORTED_SYNC_INTERVALS)

    def test_connection_accepts_each_supported_interval(self):
        connection = type("Connection", (), {})()
        connection.sync_schedules = [
            frappe._dict(
                resource=f"Resource {interval}",
                interval_minutes=interval,
                direction="Pull Only",
            )
            for interval in sorted(tasks.SUPPORTED_SYNC_INTERVALS)
        ]
        JackyunConnection.validate(connection)

    def test_connection_rejects_unsupported_interval(self):
        connection = type("Connection", (), {})()
        connection.sync_schedules = [
            frappe._dict(
                resource="Sales Order",
                interval_minutes=10,
                direction="Pull Only",
            )
        ]
        with self.assertRaises(frappe.ValidationError):
            JackyunConnection.validate(connection)

    def test_incremental_overlap_is_three_minutes(self):
        actual = tasks._apply_incremental_overlap("2026-08-30 12:00:00")
        self.assertEqual("2026-08-30 11:57:00", str(actual))

    def test_incremental_overlap_never_crosses_configured_floor(self):
        actual = tasks._apply_incremental_overlap(
            "2026-08-30 12:01:00", "2026-08-30 12:00:00"
        )
        self.assertEqual("2026-08-30 12:00:00", str(actual))

    def test_five_minute_schedule_health_uses_actual_interval(self):
        schedule = frappe._dict(
            interval_minutes=5,
            last_enqueued_at="2026-08-30 12:00:00",
            consecutive_failures=0,
        )
        tasks.apply_schedule_result(
            schedule, "成功", "2026-08-30 12:01:00", pulled=17
        )
        self.assertEqual("2026-08-30 12:05:00", str(schedule.next_run_at))
        self.assertEqual(17, schedule.last_pulled_count)

    @patch("channel_erp.integrations.tasks.enqueue")
    @patch("channel_erp.integrations.tasks._resource_sync_in_progress", return_value=True)
    def test_same_connection_and_resource_is_not_enqueued_twice(
        self, _in_progress, enqueue
    ):
        self.assertFalse(
            tasks.enqueue_sync_resource("吉客云主账号", "Sales Order")
        )
        enqueue.assert_not_called()

    @patch(
        "frappe.utils.background_jobs.get_queues_timeout",
        return_value={"short": 300, "default": 300, "long": 1500},
    )
    def test_queue_falls_back_to_existing_long_worker(self, _queues):
        with patch.object(tasks.frappe, "conf", {}):
            self.assertEqual("long", tasks._integration_queue_name())

    @patch(
        "frappe.utils.background_jobs.get_queues_timeout",
        return_value={
            "short": 300,
            "default": 300,
            "long": 1500,
            "integration": 7200,
        },
    )
    def test_dedicated_integration_queue_is_used_when_available(self, _queues):
        with patch.object(
            tasks.frappe, "conf", {"channel_erp_integration_queue": "integration"}
        ):
            self.assertEqual("integration", tasks._integration_queue_name())

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import generate_hash

from yimed_ecommerce.ecommerce_dashboard.product_listing import (
	_get_channel,
	_upsert_listing_sku,
	backfill_listing_store_names,
	cleanup_legacy_listing_sku_ids,
)


class TestEcommerceProductListing(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.company = frappe.db.get_value("Company", {}, "name")
		self.item = frappe.db.get_value("Item", {"disabled": 0}, "name")

	def test_channel_uses_platform_name_and_store_uses_jackyun_channel_name(self):
		channel = self._channel("DISPLAY")
		values = _get_channel(channel.name)
		self.assertEqual(values.channel_name, "天猫")
		self.assertEqual(values.store_name, "医麦德天猫旗舰店")
		self.assertEqual(values.platform_code, channel.online_platform_code)
		self.assertEqual(values.store_id, channel.platform_shop_id)
		self.assertEqual(values.platform_shop_name, "平台技术店铺名")

	def test_backfill_is_dry_run_safe_idempotent_and_preserves_identity(self):
		channel = self._channel("BACKFILL")
		listing = self._listing(channel, store_name=channel.platform_shop_name)
		identity_before = self._identity(listing.name)

		preview = backfill_listing_store_names(dry_run=1, limit=5000)
		self.assertIn(listing.name, {row["listing"] for row in preview["changes"]})
		self.assertEqual(frappe.db.get_value("Ecommerce Product Listing", listing.name, "store_name"), channel.platform_shop_name)

		applied = backfill_listing_store_names(dry_run=0, limit=5000)
		self.assertGreaterEqual(applied["updated"], 1)
		self.assertEqual(frappe.db.get_value("Ecommerce Product Listing", listing.name, "store_name"), channel.channel_name)
		self.assertEqual(self._identity(listing.name), identity_before)
		again = backfill_listing_store_names(dry_run=0, limit=5000)
		self.assertNotIn(listing.name, {row["listing"] for row in again["changes"]})
		self.assertEqual(self._identity(listing.name), identity_before)

	def test_backfill_uses_online_platform_code_not_internal_channel_code(self):
		channel = self._channel("PDD-CODE", online_platform_code="PDD", channel_code="103")
		listing = self._listing(channel, store_name=channel.platform_shop_name)

		preview = backfill_listing_store_names(dry_run=1, limit=5000)
		self.assertIn(listing.name, {row["listing"] for row in preview["changes"]})
		self.assertNotIn(listing.name, preview["identity_conflicts"])

	def test_backfill_skips_ambiguous_technical_identity(self):
		channel = self._channel("AMBIGUOUS")
		listing = self._listing(channel, store_name=channel.platform_shop_name)
		duplicate = frappe.copy_doc(channel)
		duplicate.channel_id = f"_TEST-EC-CHANNEL-{generate_hash(length=10)}"
		duplicate.channel_name = "另一个业务店铺"
		duplicate.insert()

		result = backfill_listing_store_names(dry_run=0, limit=5000)
		self.assertIn(listing.name, result["ambiguous"])
		self.assertEqual(frappe.db.get_value("Ecommerce Product Listing", listing.name, "store_name"), channel.platform_shop_name)

	def test_backfill_requires_system_manager(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				backfill_listing_store_names()
		finally:
			frappe.set_user("Administrator")

	def test_platform_child_link_id_replaces_legacy_merchant_sku_value(self):
		channel = self._channel("CHILD-LINK")
		listing = self._listing(channel, store_name=channel.channel_name)
		listing.skus[0].platform_sku_id = "MERCHANT-SKU-1"
		listing.skus[0].outer_sku_id = "MERCHANT-SKU-1"

		row = _upsert_listing_sku(
			listing, self.item, "2819809416", "MERCHANT-SKU-1"
		)

		self.assertEqual(row.platform_sku_id, "2819809416")
		self.assertEqual(row.outer_sku_id, "MERCHANT-SKU-1")
		self.assertEqual(len(listing.skus), 1)

	def test_legacy_merchant_sku_cleanup_is_dry_run_safe(self):
		channel = self._channel("LEGACY-CHILD-CLEANUP")
		listing = self._listing(channel, store_name=channel.channel_name)
		row_name = listing.skus[0].name
		frappe.db.set_value(
			"Ecommerce Product Listing SKU",
			row_name,
			{"platform_sku_id": "MERCHANT-ONLY", "outer_sku_id": "MERCHANT-ONLY"},
			update_modified=False,
		)

		preview = cleanup_legacy_listing_sku_ids(dry_run=1)
		self.assertIn(row_name, {row["row"] for row in preview["actions"]})
		self.assertEqual(
			frappe.db.get_value("Ecommerce Product Listing SKU", row_name, "platform_sku_id"),
			"MERCHANT-ONLY",
		)

		cleanup_legacy_listing_sku_ids(dry_run=0)
		self.assertEqual(
			frappe.db.get_value("Ecommerce Product Listing SKU", row_name, "platform_sku_id"),
			"",
		)

	def _channel(self, label, online_platform_code=None, channel_code=None):
		suffix = generate_hash(length=10)
		return frappe.get_doc(
			{
				"doctype": "Jackyun Sales Channel",
				"channel_id": f"_TEST-EC-CHANNEL-{label}-{suffix}",
				"channel_code": channel_code or f"_TEST-EC-INTERNAL-{label}-{suffix}",
				"channel_name": "医麦德天猫旗舰店",
				"online_platform_code": online_platform_code or f"_TEST-EC-PLATFORM-{label}-{suffix}",
				"online_platform_name": "天猫",
				"platform_shop_id": f"_TEST-EC-SHOP-{label}-{suffix}",
				"platform_shop_name": "平台技术店铺名",
			}
		).insert()

	def _listing(self, channel, store_name):
		return frappe.get_doc(
			{
				"doctype": "Ecommerce Product Listing",
				"company": self.company,
				"channel_name": channel.online_platform_name,
				"platform_code": channel.online_platform_code,
				"store_id": channel.platform_shop_id,
				"store_name": store_name,
				"platform_item_id": f"_TEST-LISTING-{generate_hash(length=10)}",
				"skus": [{"item_code": self.item}],
			}
		).insert()

	def _identity(self, listing):
		return frappe.db.get_value(
			"Ecommerce Product Listing",
			listing,
			["company", "platform_code", "store_id", "platform_item_id"],
		)

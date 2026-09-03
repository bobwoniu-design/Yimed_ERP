from __future__ import annotations

import frappe


def sync_sales_order_listings(sales_order: str):
	"""Create or enrich listing records from identifiers on a Sales Order.

	The listing identity is deliberately scoped by company, platform and store;
	platform item IDs are not assumed to be globally unique across shops.
	"""
	order = frappe.get_doc("Sales Order", sales_order)
	channel = _get_channel(order.custom_jackyun_sales_channel)
	for row in order.items:
		listing_id = frappe.utils.cstr(row.get("custom_ecommerce_listing_id")).strip()
		if not listing_id:
			continue
		platform_code = frappe.utils.cstr(
			row.get("custom_ecommerce_platform_code") or channel.get("platform_code")
		).strip()
		store_id = frappe.utils.cstr(channel.get("store_id")).strip()
		store_name = frappe.utils.cstr(channel.get("store_name")).strip()
		channel_name = frappe.utils.cstr(channel.get("channel_name")).strip()
		if not platform_code or not store_id or not channel_name or not store_name:
			# Keep the order-line identifiers for reporting, but do not create an
			# ambiguous master record that could combine two different stores.
			continue

		name = frappe.db.get_value(
			"Ecommerce Product Listing",
			{
				"company": order.company,
				"platform_code": platform_code,
				"store_id": store_id,
				"platform_item_id": listing_id,
			},
		)
		if name:
			doc = frappe.get_doc("Ecommerce Product Listing", name)
		else:
			doc = frappe.new_doc("Ecommerce Product Listing")
			doc.update(
				{
					"company": order.company,
					"platform_code": platform_code,
					"store_id": store_id,
					"platform_item_id": listing_id,
					"source": "吉客云订单",
				}
			)

		doc.channel_name = channel_name
		doc.store_name = store_name or store_id
		doc.platform_item_name = (
			frappe.utils.cstr(row.get("custom_ecommerce_platform_item_name")).strip()
			or doc.platform_item_name
		)
		doc.last_seen_order = order.name
		doc.last_seen_date = order.transaction_date
		platform_sku = frappe.utils.cstr(
			row.get("custom_ecommerce_platform_sku_id")
		).strip()
		outer_sku = frappe.utils.cstr(row.get("custom_jackyun_outer_sku_id")).strip()
		_upsert_listing_sku(doc, row.item_code, platform_sku, outer_sku)
		doc.flags.ignore_permissions = True
		doc.save()


def _upsert_listing_sku(doc, item_code: str, platform_sku_id: str, outer_sku_id: str):
	"""Keep the platform child-link ID separate from the merchant SKU code.

	Older records stored ``outerSkuId`` in ``platform_sku_id``.  When a verified
	``platSkuId`` arrives, reuse that legacy row instead of creating a duplicate.
	"""
	for item in doc.skus:
		if (
			platform_sku_id
			and item.item_code == item_code
			and item.platform_sku_id == platform_sku_id
		):
			item.outer_sku_id = outer_sku_id
			return item
	legacy = next(
		(
			item for item in doc.skus
			if item.item_code == item_code
			and item.outer_sku_id == outer_sku_id
			and item.platform_sku_id in ("", outer_sku_id)
		),
		None,
	)
	if legacy:
		if platform_sku_id:
			legacy.platform_sku_id = platform_sku_id
		return legacy
	return doc.append(
		"skus",
		{
			"platform_sku_id": platform_sku_id,
			"outer_sku_id": outer_sku_id,
			"item_code": item_code,
		},
	)


@frappe.whitelist()
def cleanup_legacy_listing_sku_ids(dry_run=1, limit=5000):
	"""Remove the old merchant-SKU-as-child-ID pollution safely.

	Only rows where ``platform_sku_id == outer_sku_id`` are touched.  If a real
	child-link row already exists for the same listing/item/merchant SKU, the
	legacy duplicate is removed; otherwise only its false child ID is cleared.
	"""
	frappe.only_for("System Manager")
	dry_run = frappe.utils.cint(dry_run)
	limit = min(max(frappe.utils.cint(limit) or 5000, 1), 5000)
	legacy_rows = frappe.db.sql(
		"""select name, parent, item_code, platform_sku_id, outer_sku_id
		     from `tabEcommerce Product Listing SKU`
		    where ifnull(platform_sku_id, '') <> ''
		      and platform_sku_id = outer_sku_id
		    order by parent, idx, name
		    limit %s""",
		(limit,),
		as_dict=True,
	)
	actions = []
	for row in legacy_rows:
		real_row = frappe.db.get_value(
			"Ecommerce Product Listing SKU",
			{
				"parent": row.parent,
				"item_code": row.item_code,
				"outer_sku_id": row.outer_sku_id,
				"platform_sku_id": ["not in", ["", row.outer_sku_id]],
				"name": ["!=", row.name],
			},
		)
		action = "delete_duplicate" if real_row else "clear_false_child_id"
		actions.append({"row": row.name, "listing": row.parent, "action": action})
		if dry_run:
			continue
		if real_row:
			frappe.db.delete("Ecommerce Product Listing SKU", row.name)
		else:
			frappe.db.set_value(
				"Ecommerce Product Listing SKU",
				row.name,
				"platform_sku_id",
				"",
				update_modified=False,
			)
	return {
		"dry_run": bool(dry_run),
		"matched": len(legacy_rows),
		"actions": actions,
		"has_more": len(legacy_rows) == limit,
	}


def _get_channel(channel_name: str | None) -> frappe._dict:
	if not channel_name or not frappe.db.exists("Jackyun Sales Channel", channel_name):
		return frappe._dict()
	row = frappe.db.get_value(
		"Jackyun Sales Channel",
		channel_name,
		[
			"online_platform_code", "online_platform_name", "platform_shop_id",
			"platform_shop_name", "channel_name",
		],
		as_dict=True,
	)
	return frappe._dict(
		channel_name=row.online_platform_name,
		platform_code=row.online_platform_code,
		store_id=row.platform_shop_id,
		store_name=row.channel_name,
		platform_shop_name=row.platform_shop_name,
	)


@frappe.whitelist()
def backfill_listing_store_names(
	dry_run: int = 1, limit: int = 500, start_after: str | None = None
) -> dict:
	"""Safely repair listing display labels without changing master identity fields.

	The immutable matching identity remains company + platform_code + store_id +
	platform_item_id.  A Sales Order link is preferred because it identifies the
	exact Jackyun channel; otherwise the technical online_platform_code + platform_shop_id
	pair must resolve to exactly one channel. Ambiguous/conflicting rows are skipped.
	"""
	frappe.only_for("System Manager")
	dry_run = bool(int(dry_run))
	limit = max(1, min(int(limit or 500), 5000))
	filters = {"name": [">", start_after]} if start_after else None
	rows = frappe.get_all(
		"Ecommerce Product Listing",
		filters=filters,
		fields=[
			"name", "company", "channel_name", "platform_code", "store_id",
			"store_name", "platform_item_id", "last_seen_order",
		],
		order_by="name",
		limit=limit + 1,
	)
	has_more = len(rows) > limit
	rows = rows[:limit]
	result = {
		"dry_run": dry_run,
		"scanned": len(rows),
		"start_after": start_after,
		"next_start_after": rows[-1].name if has_more and rows else None,
		"has_more": has_more,
		"would_update": 0,
		"updated": 0,
		"unchanged": 0,
		"unresolved": [],
		"ambiguous": [],
		"identity_conflicts": [],
		"changes": [],
	}
	for listing in rows:
		channel, resolution = _resolve_listing_channel(listing)
		if not channel:
			result[resolution].append(listing.name)
			continue
		new_channel = frappe.utils.cstr(channel.online_platform_name).strip()
		new_store = frappe.utils.cstr(channel.channel_name).strip()
		if not new_channel or not new_store:
			result["unresolved"].append(listing.name)
			continue
		if listing.channel_name == new_channel and listing.store_name == new_store:
			result["unchanged"] += 1
			continue
		result["would_update"] += 1
		result["changes"].append(
			{
				"listing": listing.name,
				"channel_name": {"from": listing.channel_name, "to": new_channel},
				"store_name": {"from": listing.store_name, "to": new_store},
			}
		)
		if not dry_run:
			# Deliberately update display fields only. Never rewrite the four fields
			# that form the listing master identity.
			frappe.db.set_value(
				"Ecommerce Product Listing",
				listing.name,
				{"channel_name": new_channel, "store_name": new_store},
			)
			result["updated"] += 1
	return result


def _resolve_listing_channel(listing) -> tuple[frappe._dict | None, str]:
	if listing.last_seen_order and frappe.db.exists("Sales Order", listing.last_seen_order):
		channel_name = frappe.db.get_value(
			"Sales Order", listing.last_seen_order, "custom_jackyun_sales_channel"
		)
		if channel_name and frappe.db.exists("Jackyun Sales Channel", channel_name):
			channel = frappe.db.get_value(
				"Jackyun Sales Channel",
				channel_name,
				[
					"name", "channel_name", "online_platform_code",
					"online_platform_name", "platform_shop_id",
				],
				as_dict=True,
			)
			if _listing_identity_matches_channel(listing, channel):
				return channel, ""
			return None, "identity_conflicts"

	matches = frappe.get_all(
		"Jackyun Sales Channel",
		filters={"online_platform_code": listing.platform_code, "platform_shop_id": listing.store_id},
		fields=[
			"name", "channel_name", "online_platform_code", "online_platform_name",
			"platform_shop_id",
		],
		limit=2,
	)
	if len(matches) == 1:
		return matches[0], ""
	return (None, "ambiguous") if len(matches) > 1 else (None, "unresolved")


def _listing_identity_matches_channel(listing, channel) -> bool:
	return (
		frappe.utils.cstr(listing.platform_code).strip()
		== frappe.utils.cstr(channel.online_platform_code).strip()
		and frappe.utils.cstr(listing.store_id).strip()
		== frappe.utils.cstr(channel.platform_shop_id).strip()
	)

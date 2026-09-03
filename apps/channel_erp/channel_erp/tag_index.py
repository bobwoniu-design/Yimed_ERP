import frappe
from frappe.utils import cint


def sync_asset_tags(asset):
	from channel_erp.content_center import _normalize_tags

	if not frappe.db.exists("Channel Content Asset", asset):
		delete_asset_tags(asset)
		return 0
	doc = frappe.db.get_value("Channel Content Asset", asset, ["folder", "tags"], as_dict=True)
	canonical = _normalize_tags(doc.tags)
	if canonical != (doc.tags or ""):
		frappe.db.set_value("Channel Content Asset", asset, "tags", canonical, update_modified=False)
	displays = [tag.strip() for tag in canonical.split(",") if tag.strip()]
	existing = {
		row.normalized_tag: row
		for row in frappe.get_all(
			"Channel Content Asset Tag", filters={"asset": asset},
			fields=["name", "normalized_tag", "display_tag", "folder"], limit=0,
		)
	}
	wanted = {tag.casefold(): tag for tag in displays}
	for normalized, row in existing.items():
		if normalized not in wanted:
			frappe.delete_doc("Channel Content Asset Tag", row.name, ignore_permissions=True)
	for normalized, display in wanted.items():
		row = existing.get(normalized)
		values = {"folder": doc.folder, "display_tag": display}
		if row:
			if row.display_tag != display or row.folder != doc.folder:
				frappe.db.set_value("Channel Content Asset Tag", row.name, values, update_modified=False)
		else:
			frappe.get_doc({
				"doctype": "Channel Content Asset Tag", "asset": asset,
				"normalized_tag": normalized, **values,
			}).insert(ignore_permissions=True)
	return len(wanted)


def delete_asset_tags(asset):
	for name in frappe.get_all("Channel Content Asset Tag", filters={"asset": asset}, pluck="name", limit=0):
		frappe.delete_doc("Channel Content Asset Tag", name, ignore_permissions=True)


def rebuild_tag_index(asset=None, company=None):
	if asset:
		return {"assets": 1, "tags": sync_asset_tags(asset)}
	names = frappe.get_all("Channel Content Asset", pluck="name", limit=0)
	tags = sum(sync_asset_tags(name) for name in names)
	return {"assets": len(names), "tags": tags}


def backfill_missing_tag_index(company=None, limit=500):
	count = 0
	for row in frappe.get_all(
		"Channel Content Asset", filters={"tags": ["is", "set"]},
		fields=["name", "tags"], limit=cint(limit) or 500,
	):
		if row.tags and not frappe.db.exists("Channel Content Asset Tag", {"asset": row.name}):
			sync_asset_tags(row.name)
			count += 1
	return count

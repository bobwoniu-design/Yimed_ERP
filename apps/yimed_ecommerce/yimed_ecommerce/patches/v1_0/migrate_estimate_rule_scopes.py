import frappe


def execute():
	"""把费用预估规则上旧的单行渠道/店铺字段迁移为 scopes 子表行。"""
	if not frappe.db.table_exists("Ecommerce Fee Estimate Rule"):
		return
	if not frappe.db.has_column("Ecommerce Fee Estimate Rule", "channel_name"):
		return

	for row in frappe.get_all(
		"Ecommerce Fee Estimate Rule",
		fields=["name", "channel_name", "store_name"],
	):
		channel = (row.channel_name or "").strip()
		store = (row.store_name or "").strip()
		if not channel and not store:
			continue
		if frappe.db.exists("Ecommerce Fee Estimate Scope", {"parent": row.name}):
			continue
		frappe.get_doc({
			"doctype": "Ecommerce Fee Estimate Scope",
			"parent": row.name,
			"parenttype": "Ecommerce Fee Estimate Rule",
			"parentfield": "scopes",
			"channel_name": channel,
			"store_name": store,
		}).db_insert()

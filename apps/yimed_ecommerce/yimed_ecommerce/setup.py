import json

import frappe

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


STOCK_ENTRY_CUSTOM_FIELDS = {
	"Stock Entry": [
		{
			"fieldname": "custom_jd_section",
			"fieldtype": "Section Break",
			"insert_after": "inspection_required",
			"label": "京东自营",
			"collapsible": 1,
		},
		{
			"fieldname": "custom_jd_purchase_order",
			"fieldtype": "Link",
			"insert_after": "custom_jd_section",
			"label": "京东采购单",
			"options": "JD Purchase Order",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "custom_jd_transfer_mode",
			"fieldtype": "Select",
			"insert_after": "custom_jd_purchase_order",
			"label": "京东调拨方式",
			"options": "一步调拨\n两步调拨",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "custom_jd_final_warehouse",
			"fieldtype": "Link",
			"insert_after": "custom_jd_transfer_mode",
			"label": "京东最终收货仓",
			"options": "Warehouse",
			"read_only": 1,
			"no_copy": 1,
		},
	],
}


def after_install():
	create_custom_fields(STOCK_ENTRY_CUSTOM_FIELDS, update=True)
	ensure_system_fee_items()
	ensure_jd_workspace_route()
	ensure_ecommerce_workspace_route()


def after_migrate():
	create_custom_fields(STOCK_ENTRY_CUSTOM_FIELDS, update=True)
	ensure_system_fee_items()
	backfill_jd_import_batch_codes()
	ensure_jd_workspace_route()
	ensure_ecommerce_workspace_route()


def ensure_system_fee_items():
	"""幂等确保系统推广费项目存在，但不覆盖用户的启用/显示选择。"""
	if not frappe.db.table_exists("Ecommerce Fee Item"):
		return None
	name = frappe.db.get_value("Ecommerce Fee Item", {"dashboard_field": "promotion_expense"}, "name")
	if name:
		return name

	name = frappe.db.get_value("Ecommerce Fee Item", {"item_name": "推广费"}, "name")
	if name:
		frappe.db.set_value(
			"Ecommerce Fee Item", name,
			{"dashboard_field": "promotion_expense"},
			update_modified=False,
		)
		return name

	doc = frappe.get_doc({
		"doctype": "Ecommerce Fee Item", "item_name": "推广费", "enabled": 1,
		"dashboard_field": "promotion_expense", "show_on_dashboard": 1,
		"display_order": 10, "default_allocation_method": "不分摊",
		"description": "由推广分析明细确认后生成的实际推广费用；不创建自动计提规则。",
	})
	doc.flags.ignore_permissions = True
	return doc.insert().name


def backfill_jd_import_batch_codes():
	"""Keep existing import batches compatible with the editable pre-insert batch code."""
	if not frappe.db.table_exists("JD Purchase Import Batch"):
		return
	if not frappe.db.has_column("JD Purchase Import Batch", "batch_code"):
		return
	frappe.db.sql(
		"""
		update `tabJD Purchase Import Batch`
		set batch_code = name
		where ifnull(batch_code, '') = ''
		"""
	)


def ensure_jd_workspace_route():
	"""Keep the standard JD workspace aligned with its code fixture and route-safe title."""
	if frappe.db.exists("Workspace", "JD Self Operated"):
		_sync_jd_workspace()

	if frappe.db.exists("Workspace Sidebar", "JD Self Operated"):
		sidebar = frappe.get_doc("Workspace Sidebar", "JD Self Operated")
		sidebar.set(
			"items",
			[
				{
					"label": "Home",
					"type": "Link",
					"link_type": "Workspace",
					"link_to": "JD Self Operated",
					"icon": "home",
				},
				{
					"label": "JD Purchase Workbench",
					"type": "Link",
					"link_type": "DocType",
					"link_to": "JD Purchase Import Batch",
					"icon": "shopping-cart",
				},
				{
					"label": "采购订单",
					"type": "Link",
					"link_type": "DocType",
					"link_to": "JD Purchase Order",
					"icon": "list",
				},
				{
					"label": "Master Data",
					"type": "Section Break",
					"link_type": "DocType",
					"icon": "link",
					"indent": 1,
				},
				{
					"label": "SKU Mapping",
					"type": "Link",
					"link_type": "DocType",
					"link_to": "JD SKU Mapping",
					"child": 1,
				},
				{
					"label": "JD Self Operated Settings",
					"type": "Link",
					"link_type": "DocType",
					"link_to": "JD Self Operated Settings",
					"child": 1,
				},
			],
		)
		sidebar.flags.ignore_permissions = True
		sidebar.save()


def _sync_jd_workspace():
	"""Sync fields that Frappe may skip when fixture and database modified timestamps match."""
	source = _load_jd_workspace_source()
	workspace = frappe.get_doc("Workspace", "JD Self Operated")
	desired_shortcuts = source.get("shortcuts") or []
	changed = any(
		workspace.get(fieldname) != source.get(fieldname)
		for fieldname in ("content", "label", "title")
	)
	changed = changed or _shortcut_signatures(workspace.shortcuts) != _shortcut_signatures(desired_shortcuts)
	if not changed:
		return False

	workspace.content = source.get("content") or "[]"
	workspace.label = source.get("label") or "JD Self Operated"
	workspace.title = source.get("title") or "JD Self Operated"
	workspace.set("shortcuts", desired_shortcuts)
	workspace.flags.ignore_permissions = True
	workspace.save()
	return True


def _load_jd_workspace_source() -> dict:
	path = frappe.get_app_path(
		"yimed_ecommerce",
		"yimed_ecommerce",
		"workspace",
		"jd_self_operated",
		"jd_self_operated.json",
	)
	with open(path, encoding="utf-8") as source_file:
		return json.load(source_file)


def _shortcut_signatures(shortcuts) -> list[tuple]:
	fields = ("label", "type", "link_to", "doc_view", "color", "report_ref_doctype")
	return [tuple((row.get(fieldname) or "") for fieldname in fields) for row in shortcuts]


def ensure_ecommerce_workspace_route():
	"""Keep import as a list action and expose only operational profit reports."""
	if frappe.db.exists("Workspace", "Ecommerce Dashboard"):
		source = _load_ecommerce_workspace_source()
		workspace = frappe.get_doc("Workspace", "Ecommerce Dashboard")
		desired_shortcuts = source.get("shortcuts") or []
		changed = any(
			workspace.get(fieldname) != source.get(fieldname)
			for fieldname in ("content", "label", "title")
		)
		changed = changed or _shortcut_signatures(workspace.shortcuts) != _shortcut_signatures(desired_shortcuts)
		if changed:
			workspace.content = source.get("content") or "[]"
			workspace.label = source.get("label") or "Ecommerce Dashboard"
			workspace.title = source.get("title") or "Ecommerce Dashboard"
			workspace.type = source.get("type") or "Workspace"
			workspace.set("shortcuts", desired_shortcuts)
			workspace.flags.ignore_permissions = True
			workspace.save()

	if frappe.db.exists("Workspace Sidebar", "Ecommerce Dashboard"):
		source = _load_ecommerce_sidebar_source()
		sidebar = frappe.get_doc("Workspace Sidebar", "Ecommerce Dashboard")
		desired_items = source.get("items") or []
		if _sidebar_signatures(sidebar.items) != _sidebar_signatures(desired_items):
			sidebar.set("items", desired_items)
			sidebar.flags.ignore_permissions = True
			sidebar.save()


def _load_ecommerce_workspace_source() -> dict:
	path = frappe.get_app_path(
		"yimed_ecommerce",
		"ecommerce_dashboard",
		"workspace",
		"ecommerce_dashboard",
		"ecommerce_dashboard.json",
	)
	with open(path, encoding="utf-8") as source_file:
		return json.load(source_file)


def _load_ecommerce_sidebar_source() -> dict:
	path = frappe.get_app_path(
		"yimed_ecommerce",
		"workspace_sidebar",
		"ecommerce_dashboard.json",
	)
	with open(path, encoding="utf-8") as source_file:
		return json.load(source_file)


def _sidebar_signatures(items) -> list[tuple]:
	fields = ("label", "type", "link_type", "link_to", "child", "icon", "indent")
	return [tuple((row.get(fieldname) or "") for fieldname in fields) for row in items]

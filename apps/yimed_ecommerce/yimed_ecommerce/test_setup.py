import frappe
from frappe.tests import IntegrationTestCase

from yimed_ecommerce.setup import (
	_load_jd_workspace_source,
	_load_ecommerce_sidebar_source,
	_load_ecommerce_workspace_source,
	_shortcut_signatures,
	_sidebar_signatures,
	ensure_ecommerce_workspace_route,
	ensure_system_fee_items,
	ensure_jd_workspace_route,
)


class TestJDWorkspaceSetup(IntegrationTestCase):
	def test_workspace_content_is_repaired_and_second_sync_is_idempotent(self):
		workspace = frappe.get_doc("Workspace", "JD Self Operated")
		workspace.content = "[]"
		workspace.set(
			"shortcuts",
			[
				{
					"label": "旧装箱明细",
					"type": "DocType",
					"link_to": "JD Carton",
				}
			],
		)
		workspace.flags.ignore_permissions = True
		workspace.save()
		# Reproduce the fixture edge case: timestamps are equal but stored content is stale.
		frappe.db.set_value(
			"Workspace",
			workspace.name,
			"modified",
			"2026-08-19 03:00:00.000000",
			update_modified=False,
		)

		ensure_jd_workspace_route()
		workspace.reload()
		source = _load_jd_workspace_source()
		self.assertEqual(workspace.content, source["content"])
		self.assertEqual(_shortcut_signatures(workspace.shortcuts), _shortcut_signatures(source["shortcuts"]))
		self.assertEqual([row.label for row in workspace.shortcuts], ["京东采购备货", "SKU映射", "京东自营设置"])

		modified_after_repair = workspace.modified
		ensure_jd_workspace_route()
		workspace.reload()
		self.assertEqual(workspace.modified, modified_after_repair)


class TestEcommerceWorkspaceSetup(IntegrationTestCase):
	def test_system_promotion_fee_item_is_idempotent_and_not_an_accrual_rule(self):
		first = ensure_system_fee_items()
		second = ensure_system_fee_items()
		self.assertEqual(first, second)
		item = frappe.db.get_value(
			"Ecommerce Fee Item", first,
			["dashboard_field", "item_name"], as_dict=True,
		)
		self.assertEqual(item.dashboard_field, "promotion_expense")
		self.assertEqual(item.item_name, "推广费")
		self.assertFalse(frappe.db.exists("Ecommerce Fee Rule", {"fee_item": first, "auto_accrual": 1}))

	def test_import_is_not_a_menu_and_profit_reports_are_exposed(self):
		ensure_ecommerce_workspace_route()
		workspace = frappe.get_doc("Workspace", "Ecommerce Dashboard")
		sidebar = frappe.get_doc("Workspace Sidebar", "Ecommerce Dashboard")
		workspace_source = _load_ecommerce_workspace_source()
		sidebar_source = _load_ecommerce_sidebar_source()
		self.assertEqual(
			_shortcut_signatures(workspace.shortcuts),
			_shortcut_signatures(workspace_source["shortcuts"]),
		)
		self.assertEqual(
			_sidebar_signatures(sidebar.items),
			_sidebar_signatures(sidebar_source["items"]),
		)
		links = {row.link_to for row in sidebar.items}
		self.assertNotIn("Ecommerce Promotion Import Batch", links)
		self.assertIn("Ecommerce Store Profit", links)
		self.assertIn("Ecommerce Listing Profit", links)
		items = list(sidebar.items)
		dashboard_group = next(index for index, row in enumerate(items) if row.label == "Dashboards")
		self.assertEqual(items[dashboard_group].type, "Section Break")
		self.assertEqual(items[dashboard_group].indent, 1)
		self.assertEqual(
			[(row.label, row.link_to, row.child) for row in items[dashboard_group + 1:dashboard_group + 3]],
			[
				("Business Dashboard", "ecommerce-business-dashboard", 1),
				("Promotion Analytics Dashboard", "ecommerce-promotion-dashboard", 1),
			],
		)
		maintenance_group = next(index for index, row in enumerate(items) if row.label == "Data Maintenance")
		self.assertEqual(
			[(row.label, row.link_to, row.child) for row in items[maintenance_group + 1:maintenance_group + 6]],
			[
				("Fee Items", "Ecommerce Fee Item", 1),
				("Fee Estimate Rules", "Ecommerce Fee Estimate Rule", 1),
				("Fee Details", "Ecommerce Fee Detail", 1),
				("Promotion Expense Detail", "Ecommerce Promotion Expense", 1),
				("Fee Rules", "Ecommerce Fee Rule", 1),
			],
		)

	def test_sidebar_route_ownership_covers_fee_detail_and_both_dashboards(self):
		route_source = frappe.get_app_path("yimed_ecommerce", "public", "js", "jd_sidebar_routes.js")
		with open(route_source, encoding="utf-8") as source_file:
			source = source_file.read()
		self.assertIn('"Ecommerce Fee Detail"', source)
		self.assertIn('"Ecommerce Fee Item"', source)
		self.assertIn('"ecommerce-business-dashboard"', source)
		self.assertIn('"ecommerce-promotion-dashboard"', source)
